"""Learns battery charge and discharge efficiency per power step (issue #1431).

Between two SoC changes the learner integrates the battery power sensor and
compares that energy with the energy the SoC change implies
(``delta_soc x capacity``).  Each measurement is shared between the 500 W
power steps it ran at, in proportion to the energy measured in each, and
added to the total of its direction.  Every figure is a ratio of summed energies, never a mean of
single ratios: the SoC is reported in whole percent, so one measurement is
off by up to a cycle's worth of SoC in either direction, and only the sums
let those errors cancel.  For the same reason no measurement is rejected for
its own result, except a gross sensor fault.

The capacity is the rated one, so the charge and discharge figures are
*apparent*: a battery that holds less than its rated capacity reads high
when charging and low when discharging by the same factor.  The factor
cancels in the round trip, which is the one figure that stands on its own.

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
    async_load_history,
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
#: SoC change that closes a measurement (%).  Long enough to keep the SoC
#: resolution from leaning single power steps when the power varies.
MIN_DELTA_SOC = 5.0
#: A single measurement is a sensor fault when stored and measured energy
#: differ by more than this factor, either way.  The SoC resolution alone
#: keeps an honest measurement within a factor of 1.25.
MAX_SAMPLE_FACTOR = 2.0
#: A step is used from this many samples.
MIN_SAMPLES = 10
#: A step is used from this much stored energy (kWh).
MIN_ENERGY_KWH = 2.0
#: A direction as a whole is used from this many samples.
MIN_TOTAL_SAMPLES = 20
#: A direction as a whole is used from this much stored energy (kWh).
MIN_TOTAL_ENERGY_KWH = 10.0
#: An apparent charge or discharge efficiency outside this band is
#: implausible.  The band is wide and reciprocal because the figure carries
#: the capacity error.
MIN_APPARENT_EFFICIENCY = 0.5
MAX_APPARENT_EFFICIENCY = 2.0
#: A round trip outside this band is discarded as implausible.
MIN_ROUND_TRIP = 0.5
MAX_ROUND_TRIP = 1.0
#: Samples a step weighs; older ones fade out beyond this.
MAX_SAMPLES = 200
#: Schema version of the persisted file.  Version 1 filtered single
#: measurements in a way that biased the sums, so it is not read back.
STORAGE_VERSION = 2

STATUS_LEARNING = "learning"
STATUS_OK = "ok"
STATUS_IMPLAUSIBLE = "implausible"

_EPSILON = 1e-9


@dataclass
class EfficiencyStep:
    """Accumulated energy of one power step, or of a whole direction."""

    samples: int = 0
    """Number of measurements that contributed, capped at ``MAX_SAMPLES``."""

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
        """Return the step's apparent efficiency as a fraction, or ``None``."""
        return _efficiency(direction, self.measured_kwh, self.stored_kwh)

    def status(self, direction: Direction) -> str:
        """Return ``learning``, ``ok`` or ``implausible`` for this step."""
        return _status(
            self.efficiency(direction),
            self.samples,
            self.stored_kwh,
            MIN_SAMPLES,
            MIN_ENERGY_KWH,
        )


@dataclass
class _Window:
    """One measurement in progress."""

    direction: Direction
    last_at: datetime
    last_power_w: float
    last_soc: float
    anchor_soc: float | None = None
    energy_kwh: dict[int, float] = field(default_factory=dict)
    """Measured energy per power step (kWh)."""

    def anchor(self, soc_pct: float) -> None:
        """Start integrating from a SoC change."""
        self.anchor_soc = soc_pct
        self.energy_kwh = {}


def _efficiency(
    direction: Direction, measured_kwh: float, stored_kwh: float
) -> float | None:
    """Return energy out over energy in, or ``None`` without energy."""
    if measured_kwh <= _EPSILON or stored_kwh <= _EPSILON:
        return None
    if direction == CHARGE:
        return stored_kwh / measured_kwh
    return measured_kwh / stored_kwh


def _status(
    efficiency: float | None,
    samples: int,
    stored_kwh: float,
    min_samples: int,
    min_energy_kwh: float,
) -> str:
    """Return ``learning``, ``ok`` or ``implausible`` for summed energies."""
    if efficiency is None or samples < min_samples or stored_kwh < min_energy_kwh:
        return STATUS_LEARNING
    if not MIN_APPARENT_EFFICIENCY <= efficiency <= MAX_APPARENT_EFFICIENCY:
        return STATUS_IMPLAUSIBLE
    return STATUS_OK


def _direction(power_w: float) -> Direction | None:
    """Return the direction of a signed battery power, ``None`` when idle."""
    if power_w >= IDLE_POWER_W:
        return CHARGE
    if power_w <= -IDLE_POWER_W:
        return DISCHARGE
    return None


def _parse_energy(raw: object) -> EfficiencyStep | None:
    """Return restored energy sums, or ``None`` when the entry is invalid."""
    if not isinstance(raw, dict):
        return None
    try:
        samples = int(raw["samples"])
        measured_kwh = float(raw["measured_kwh"])
        stored_kwh = float(raw["stored_kwh"])
    except KeyError, TypeError, ValueError, OverflowError:
        return None
    if (
        not 0 < samples <= MAX_SAMPLES
        or not math.isfinite(measured_kwh)
        or not math.isfinite(stored_kwh)
        or measured_kwh <= 0.0
        or stored_kwh <= 0.0
    ):
        return None
    return EfficiencyStep(samples, measured_kwh, stored_kwh)


def _parse_step_w(key: object) -> int | None:
    """Return a stored step key as W, or ``None`` when it is not a step."""
    try:
        step_w = int(str(key))
    except ValueError:
        return None
    if step_w < 0 or step_w > MAX_STEP_W or step_w % STEP_W:
        return None
    return step_w


def _energy_dict(step: EfficiencyStep) -> dict[str, Any]:
    """Return energy sums as a JSON-serialisable dict."""
    return {
        "samples": step.samples,
        "measured_kwh": step.measured_kwh,
        "stored_kwh": step.stored_kwh,
    }


@dataclass
class EfficiencyLearner:
    """Learns charge and discharge efficiency per 500 W power step.

    Feed it once per coordinator cycle through :meth:`update`.  A measurement
    starts at a SoC change, so both ends sit on the same edge of the SoC
    resolution.  It ends when the SoC has moved ``MIN_DELTA_SOC`` further,
    and the next one starts at that same reading.

    A measurement is dropped when the battery idled, reversed direction, a
    reading was missing, the cycle gap was too long, or the SoC moved against
    the power.  None of these depends on the measurement's own result.
    """

    history_file: str = ""
    """Full path of the JSON file; empty until the coordinator sets it."""

    charge: dict[int, EfficiencyStep] = field(default_factory=dict)
    """Charge steps keyed by the lower bound of the step in W."""

    discharge: dict[int, EfficiencyStep] = field(default_factory=dict)
    """Discharge steps keyed by the lower bound of the step in W."""

    charge_total: EfficiencyStep = field(default_factory=EfficiencyStep)
    """All charge measurements, whatever their power."""

    discharge_total: EfficiencyStep = field(default_factory=EfficiencyStep)
    """All discharge measurements, whatever their power."""

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
        """Return the apparent charge efficiency at any power (%)."""
        return self._efficiency_pct(CHARGE)

    @property
    def discharge_efficiency_pct(self) -> float | None:
        """Return the apparent discharge efficiency at any power (%)."""
        return self._efficiency_pct(DISCHARGE)

    @property
    def round_trip_status(self) -> str:
        """Return ``learning``, ``ok`` or ``implausible`` for the round trip."""
        round_trip = self._round_trip()
        if round_trip is None:
            return STATUS_LEARNING
        if not MIN_ROUND_TRIP <= round_trip <= MAX_ROUND_TRIP:
            return STATUS_IMPLAUSIBLE
        return STATUS_OK

    @property
    def round_trip_efficiency_pct(self) -> float | None:
        """Return the measured round trip (%), or ``None`` unless plausible.

        The capacity cancels out of charge x discharge, so this figure does
        not depend on how much the battery really holds.
        """
        round_trip = self._round_trip()
        if round_trip is None or self.round_trip_status != STATUS_OK:
            return None
        return round_trip * 100.0

    def status(self, direction: Direction) -> str:
        """Return ``learning``, ``ok`` or ``implausible`` for *direction*."""
        total = self._total(direction)
        return _status(
            total.efficiency(direction),
            total.samples,
            total.stored_kwh,
            MIN_TOTAL_SAMPLES,
            MIN_TOTAL_ENERGY_KWH,
        )

    def sample_count(self, direction: Direction) -> int:
        """Return the number of measurements held for *direction*."""
        return self._total(direction).samples

    def curve(self, direction: Direction) -> list[dict[str, Any]]:
        """Return the efficiency curve for *direction*, lowest power first.

        Each entry holds the step's power range, its apparent efficiency in
        percent (``None`` until the step is usable), the sample count, the
        stored energy and the status.
        """
        rows: list[dict[str, Any]] = []
        for step_w, step in sorted(self._steps(direction).items()):
            status = step.status(direction)
            efficiency = step.efficiency(direction)
            rows.append(
                {
                    "power_from_w": step_w,
                    "power_to_w": step_w + STEP_W,
                    "apparent_efficiency_pct": (
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

    def _total(self, direction: Direction) -> EfficiencyStep:
        """Return the sums of all measurements for *direction*."""
        return self.charge_total if direction == CHARGE else self.discharge_total

    def _efficiency(self, direction: Direction) -> float | None:
        """Return the apparent efficiency of *direction* as a fraction.

        Every measurement counts, whatever the status of the steps it ran
        at: leaving out the steps that read too high would pull the figure
        down.
        """
        if self.status(direction) != STATUS_OK:
            return None
        return self._total(direction).efficiency(direction)

    def _efficiency_pct(self, direction: Direction) -> float | None:
        """Return the apparent efficiency of *direction* in percent."""
        efficiency = self._efficiency(direction)
        return None if efficiency is None else efficiency * 100.0

    def _round_trip(self) -> float | None:
        """Return charge x discharge as a fraction, before the plausibility cut."""
        charge = self._efficiency(CHARGE)
        discharge = self._efficiency(DISCHARGE)
        if charge is None or discharge is None:
            return None
        return charge * discharge

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
                window.anchor(soc_pct)
            return

        mean_w = (previous_power_w + abs_w) / 2.0
        step_w = min(int(mean_w // STEP_W) * STEP_W, MAX_STEP_W)
        window.energy_kwh[step_w] = (
            window.energy_kwh.get(step_w, 0.0) + mean_w * elapsed / 3.6e6
        )

        delta_soc = soc_pct - window.anchor_soc
        if direction == DISCHARGE:
            delta_soc = -delta_soc
        if delta_soc < -_EPSILON:
            # The SoC moved against the power: not a clean measurement.
            self._window = _Window(direction, now, abs_w, soc_pct)
            return
        if delta_soc >= MIN_DELTA_SOC:
            self._record(window, delta_soc / 100.0 * capacity_kwh)
            window.anchor(soc_pct)

    def _record(self, window: _Window, stored_kwh: float) -> None:
        """Add a finished measurement to its direction and its power steps.

        Each step gets the share of the stored energy that matches its share
        of the measured energy.  Only a gross sensor fault is rejected, by
        the same factor in both directions, so the cut cannot lean the sums
        one way.
        """
        measured_kwh = sum(window.energy_kwh.values())
        if (
            measured_kwh <= _EPSILON
            or not 1.0 / MAX_SAMPLE_FACTOR
            <= stored_kwh / measured_kwh
            <= MAX_SAMPLE_FACTOR
        ):
            return
        self._total(window.direction).add(measured_kwh, stored_kwh)
        steps = self._steps(window.direction)
        for step_w, step_kwh in window.energy_kwh.items():
            steps.setdefault(step_w, EfficiencyStep()).add(
                step_kwh, stored_kwh * step_kwh / measured_kwh
            )
        self._dirty = True

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        """Return what was learned as a JSON-serialisable dict."""
        return {
            "version": STORAGE_VERSION,
            **{
                direction: {
                    "total": _energy_dict(self._total(direction)),
                    "steps": {
                        str(step_w): _energy_dict(step)
                        for step_w, step in sorted(self._steps(direction).items())
                    },
                }
                for direction in (CHARGE, DISCHARGE)
            },
        }

    def restore(self, data: dict[str, Any]) -> None:
        """Replace what was learned with *data*, skipping invalid entries.

        A file of another schema version is not read, and neither is a
        direction without valid totals: the learner starts empty instead.
        """
        current = data.get("version") == STORAGE_VERSION
        for direction in (CHARGE, DISCHARGE):
            raw = data.get(direction) if current else None
            total = _parse_energy(raw.get("total")) if isinstance(raw, dict) else None
            steps = self._steps(direction)
            steps.clear()
            if direction == CHARGE:
                self.charge_total = total or EfficiencyStep()
            else:
                self.discharge_total = total or EfficiencyStep()
            raw_steps = raw.get("steps") if total and isinstance(raw, dict) else None
            if not isinstance(raw_steps, dict):
                continue
            for key, raw_step in raw_steps.items():
                step_w = _parse_step_w(key)
                step = _parse_energy(raw_step)
                if step_w is not None and step is not None:
                    steps[step_w] = step
        self._window = None
        self._dirty = False

    async def load_history(self) -> None:
        """Restore the learned steps from disk; a missing file is not an error."""
        if not self.history_file:
            return
        await async_load_history(Path(self.history_file), self.restore)

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
