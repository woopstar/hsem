"""Learns battery charge and discharge efficiency per power step (issue #1431).

Between two SoC changes the learner integrates the battery power sensor and
compares that energy with the energy the SoC change implies
(``delta_soc x capacity``).  Each accepted measurement lands in a 500 W power
step for its direction.  A step is reported once it holds enough samples and
energy and its efficiency is plausible.

The result is published for diagnostics only.  The planner keeps the
configured efficiencies.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from custom_components.hsem.utils.persistence import (
    read_json_history_file,
    write_json_history_file,
)

type Direction = Literal["charge", "discharge"]

CHARGE: Direction = "charge"
DISCHARGE: Direction = "discharge"

#: Width of one power step (W).
STEP_W = 500
#: Highest step kept (W).  Stronger measurements land in this step.
MAX_STEP_W = 20_000
#: Below this absolute power the battery counts as idle (W).
IDLE_POWER_W = 100.0
#: SoC change that closes a measurement (%).
MIN_DELTA_SOC = 2.0
#: A measurement is rejected when its power varied by more than this (W).
MAX_POWER_SPREAD_W = 500.0
#: Single measurements outside this band are sensor faults, not efficiency.
MIN_SAMPLE_EFFICIENCY = 0.3
MAX_SAMPLE_EFFICIENCY = 1.3
#: A step is used from this many samples.
MIN_SAMPLES = 10
#: A step is used from this much stored energy (kWh).
MIN_ENERGY_KWH = 2.0
#: A step outside this band is discarded as implausible.
MIN_EFFICIENCY = 0.5
MAX_EFFICIENCY = 1.0
#: Samples a step weighs; older ones fade out beyond this.
MAX_SAMPLES = 200
#: Schema version of the persisted file.
STORAGE_VERSION = 1

STATUS_LEARNING = "learning"
STATUS_OK = "ok"
STATUS_IMPLAUSIBLE = "implausible"

_EPSILON = 1e-9


@dataclass
class EfficiencyStep:
    """Accumulated energy of one power step in one direction."""

    samples: int = 0
    """Number of accepted measurements, capped at ``MAX_SAMPLES``."""

    measured_kwh: float = 0.0
    """Energy seen by the battery power sensor (kWh)."""

    stored_kwh: float = 0.0
    """Energy implied by the SoC changes (kWh)."""

    def add(self, measured_kwh: float, stored_kwh: float) -> None:
        """Add one measurement, fading the oldest out once the step is full."""
        if self.samples >= MAX_SAMPLES:
            keep = (MAX_SAMPLES - 1) / MAX_SAMPLES
            self.measured_kwh *= keep
            self.stored_kwh *= keep
        else:
            self.samples += 1
        self.measured_kwh += measured_kwh
        self.stored_kwh += stored_kwh

    def efficiency(self, direction: Direction) -> float | None:
        """Return the step's efficiency as a fraction, or ``None`` if empty."""
        return _efficiency(direction, self.measured_kwh, self.stored_kwh)

    def status(self, direction: Direction) -> str:
        """Return ``learning``, ``ok`` or ``implausible`` for this step."""
        efficiency = self.efficiency(direction)
        if (
            efficiency is None
            or self.samples < MIN_SAMPLES
            or self.stored_kwh < MIN_ENERGY_KWH
        ):
            return STATUS_LEARNING
        if not MIN_EFFICIENCY <= efficiency <= MAX_EFFICIENCY:
            return STATUS_IMPLAUSIBLE
        return STATUS_OK


@dataclass
class _Window:
    """One measurement in progress."""

    direction: Direction
    last_at: datetime
    last_power_w: float
    last_soc: float
    anchor_soc: float | None = None
    energy_kwh: float = 0.0
    seconds: float = 0.0
    min_power_w: float = 0.0
    max_power_w: float = 0.0

    def anchor(self, soc_pct: float, power_w: float) -> None:
        """Start integrating from a SoC change."""
        self.anchor_soc = soc_pct
        self.energy_kwh = 0.0
        self.seconds = 0.0
        self.min_power_w = power_w
        self.max_power_w = power_w


def _efficiency(
    direction: Direction, measured_kwh: float, stored_kwh: float
) -> float | None:
    """Return energy out over energy in, or ``None`` without energy."""
    if measured_kwh <= _EPSILON or stored_kwh <= _EPSILON:
        return None
    if direction == CHARGE:
        return stored_kwh / measured_kwh
    return measured_kwh / stored_kwh


def _direction(power_w: float) -> Direction | None:
    """Return the direction of a signed battery power, ``None`` when idle."""
    if power_w >= IDLE_POWER_W:
        return CHARGE
    if power_w <= -IDLE_POWER_W:
        return DISCHARGE
    return None


def _parse_step(key: object, raw: object) -> tuple[int, EfficiencyStep] | None:
    """Return a restored step, or ``None`` when the stored entry is invalid."""
    if not isinstance(raw, dict):
        return None
    try:
        step_w = int(str(key))
        samples = int(raw["samples"])
        measured_kwh = float(raw["measured_kwh"])
        stored_kwh = float(raw["stored_kwh"])
    except KeyError, TypeError, ValueError:
        return None
    if (
        step_w < 0
        or step_w > MAX_STEP_W
        or step_w % STEP_W
        or not 0 < samples <= MAX_SAMPLES
        or not math.isfinite(measured_kwh)
        or not math.isfinite(stored_kwh)
        or measured_kwh <= 0.0
        or stored_kwh <= 0.0
    ):
        return None
    return step_w, EfficiencyStep(samples, measured_kwh, stored_kwh)


@dataclass
class EfficiencyLearner:
    """Learns charge and discharge efficiency per 500 W power step.

    Feed it once per coordinator cycle through :meth:`update`.  A measurement
    starts at a SoC change, so both ends sit on the same edge of the SoC
    resolution.  It ends when the SoC has moved ``MIN_DELTA_SOC`` further.

    A measurement is dropped when the battery idled, reversed direction, a
    reading was missing, the cycle gap was too long, the SoC moved against
    the power, or the power varied by more than one step.
    """

    history_file: str = ""
    """Full path of the JSON file; empty until the coordinator sets it."""

    charge: dict[int, EfficiencyStep] = field(default_factory=dict)
    """Charge steps keyed by the lower bound of the step in W."""

    discharge: dict[int, EfficiencyStep] = field(default_factory=dict)
    """Discharge steps keyed by the lower bound of the step in W."""

    _window: _Window | None = field(default=None, init=False, repr=False)
    _dirty: bool = field(default=False, init=False, repr=False)

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------

    @property
    def dirty(self) -> bool:
        """Return whether a measurement was accepted since the last save."""
        return self._dirty

    @property
    def charge_efficiency_pct(self) -> float | None:
        """Return the measured charge efficiency over all usable steps (%)."""
        return self._efficiency_pct(CHARGE)

    @property
    def discharge_efficiency_pct(self) -> float | None:
        """Return the measured discharge efficiency over all usable steps (%)."""
        return self._efficiency_pct(DISCHARGE)

    @property
    def round_trip_efficiency_pct(self) -> float | None:
        """Return measured charge x discharge efficiency (%), or ``None``."""
        charge = self.charge_efficiency_pct
        discharge = self.discharge_efficiency_pct
        if charge is None or discharge is None:
            return None
        return charge * discharge / 100.0

    def sample_count(self, direction: Direction) -> int:
        """Return the number of samples held for *direction*."""
        return sum(step.samples for step in self._steps(direction).values())

    def curve(self, direction: Direction) -> list[dict[str, Any]]:
        """Return the efficiency curve for *direction*, lowest power first.

        Each entry holds the step's power range, its efficiency in percent
        (``None`` until the step is usable), the sample count, the stored
        energy and the status.
        """
        rows: list[dict[str, Any]] = []
        for step_w, step in sorted(self._steps(direction).items()):
            status = step.status(direction)
            efficiency = step.efficiency(direction)
            rows.append(
                {
                    "power_from_w": step_w,
                    "power_to_w": step_w + STEP_W,
                    "efficiency_pct": (
                        round(efficiency * 100.0, 1)
                        if efficiency is not None and status == STATUS_OK
                        else None
                    ),
                    "samples": step.samples,
                    "energy_kwh": round(step.stored_kwh, 2),
                    "status": status,
                }
            )
        return rows

    def _steps(self, direction: Direction) -> dict[int, EfficiencyStep]:
        """Return the step table for *direction*."""
        return self.charge if direction == CHARGE else self.discharge

    def _efficiency_pct(self, direction: Direction) -> float | None:
        """Return the energy-weighted efficiency of the usable steps (%)."""
        usable = [
            step
            for step in self._steps(direction).values()
            if step.status(direction) == STATUS_OK
        ]
        efficiency = _efficiency(
            direction,
            sum(step.measured_kwh for step in usable),
            sum(step.stored_kwh for step in usable),
        )
        return None if efficiency is None else efficiency * 100.0

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    def update(
        self,
        now: datetime,
        power_w: float | None,
        soc_pct: float | None,
        capacity_kwh: float | None,
        *,
        max_gap_seconds: float = 600.0,
    ) -> None:
        """Feed one reading and record a measurement when one completes.

        Args:
            now: Time of the reading (timezone-aware).
            power_w: Signed battery power in W, positive when charging, or
                ``None`` if unavailable.
            soc_pct: Battery state of charge (0-100), or ``None`` if
                unavailable.
            capacity_kwh: Battery capacity at 100 % SoC in kWh, or ``None``
                if unknown.
            max_gap_seconds: Longest time between two readings of one
                measurement.
        """
        if (
            power_w is None
            or soc_pct is None
            or capacity_kwh is None
            or capacity_kwh <= 0.0
        ):
            self._window = None
            return
        direction = _direction(power_w)
        if direction is None:
            self._window = None
            return

        abs_w = abs(power_w)
        window = self._window
        elapsed = (now - window.last_at).total_seconds() if window is not None else 0.0
        if (
            window is None
            or window.direction != direction
            or not 0.0 < elapsed <= max_gap_seconds
        ):
            self._window = _Window(direction, now, abs_w, soc_pct)
            return

        soc_changed = abs(soc_pct - window.last_soc) > _EPSILON
        previous_power_w = window.last_power_w
        window.last_at = now
        window.last_power_w = abs_w
        window.last_soc = soc_pct

        if window.anchor_soc is None:
            if soc_changed:
                window.anchor(soc_pct, abs_w)
            return

        window.energy_kwh += (previous_power_w + abs_w) / 2.0 * elapsed / 3.6e6
        window.seconds += elapsed
        window.min_power_w = min(window.min_power_w, abs_w)
        window.max_power_w = max(window.max_power_w, abs_w)

        delta_soc = soc_pct - window.anchor_soc
        if direction == DISCHARGE:
            delta_soc = -delta_soc
        if delta_soc < -_EPSILON:
            # The SoC moved against the power: not a clean measurement.
            self._window = _Window(direction, now, abs_w, soc_pct)
            return
        if delta_soc >= MIN_DELTA_SOC:
            self._record(window, delta_soc / 100.0 * capacity_kwh)
            window.anchor(soc_pct, abs_w)

    def _record(self, window: _Window, stored_kwh: float) -> None:
        """Add a finished measurement to its step unless it is rejected."""
        if window.max_power_w - window.min_power_w > MAX_POWER_SPREAD_W:
            return
        efficiency = _efficiency(window.direction, window.energy_kwh, stored_kwh)
        if (
            efficiency is None
            or not MIN_SAMPLE_EFFICIENCY <= efficiency <= MAX_SAMPLE_EFFICIENCY
        ):
            return
        mean_power_w = window.energy_kwh * 3.6e6 / window.seconds
        step_w = min(int(mean_power_w // STEP_W) * STEP_W, MAX_STEP_W)
        steps = self._steps(window.direction)
        steps.setdefault(step_w, EfficiencyStep()).add(window.energy_kwh, stored_kwh)
        self._dirty = True

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        """Return the learned steps as a JSON-serialisable dict."""
        return {
            "version": STORAGE_VERSION,
            **{
                direction: {
                    str(step_w): {
                        "samples": step.samples,
                        "measured_kwh": step.measured_kwh,
                        "stored_kwh": step.stored_kwh,
                    }
                    for step_w, step in sorted(self._steps(direction).items())
                }
                for direction in (CHARGE, DISCHARGE)
            },
        }

    def restore(self, data: dict[str, Any]) -> None:
        """Replace the learned steps with *data*, skipping invalid entries."""
        for direction in (CHARGE, DISCHARGE):
            steps = self._steps(direction)
            steps.clear()
            raw_steps = data.get(direction)
            if not isinstance(raw_steps, dict):
                continue
            for key, raw in raw_steps.items():
                parsed = _parse_step(key, raw)
                if parsed is not None:
                    steps[parsed[0]] = parsed[1]
        self._window = None
        self._dirty = False

    async def load_history(self) -> None:
        """Restore the learned steps from disk; a missing file is not an error."""
        if not self.history_file:
            return
        data = await asyncio.to_thread(read_json_history_file, Path(self.history_file))
        if data is not None:
            self.restore(data)

    async def save_history(self) -> bool:
        """Persist the learned steps.  Return ``True`` when written."""
        if not self.history_file:
            return False
        written = await asyncio.to_thread(
            write_json_history_file, Path(self.history_file), self.as_dict()
        )
        if written:
            self._dirty = False
        return written
