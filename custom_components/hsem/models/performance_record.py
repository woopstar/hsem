"""Records behind the HSEM performance sensors (issue #1182).

Three plain carriers, shared by the tracker, the scoring and the sensors:

- :class:`PerformanceSample` — what one coordinator cycle read;
- :class:`PerformanceSlot` — what actually happened in one slot, built up
  from consecutive samples;
- :class:`PerformanceScore` — the hindsight score of a run of slots: the
  trailing 24 hours, or one local day.

No Home Assistant imports.  Missing is never read as zero: a slot only counts
the seconds in which every input it needs was observed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from custom_components.hsem.models.performance_charger import (
    ChargerPiece,
    ChargerRecord,
    ChargerSample,
    add_optional,
    as_list,
    merge_charger_pieces,
    optional_bool,
)
from custom_components.hsem.planner.hindsight_scoring import (
    WindowScore,
    utilisation_share,
)
from custom_components.hsem.utils.persistence import (
    aware_datetime_from_iso,
    finite_float,
)

#: Why a run of slots has no score.  Stable strings: they are published in the
#: sensors' ``reason`` attribute and written to the history file.
REASON_NO_SITE_LIMITS = "no_site_limits"
REASON_INSUFFICIENT_COVERAGE = "insufficient_coverage"
REASON_SCIPY_UNAVAILABLE = "scipy_unavailable"
REASON_STORED_ENERGY_OUTSIDE_LIMITS = "stored_energy_outside_limits"
REASON_ENERGY_BALANCE_RESIDUAL = "energy_balance_residual"
REASON_ORACLE_UNSOLVED = "oracle_unsolved"
#: Why a scored run has no capture share: its other figures are still valid.
REASON_POTENTIAL_BELOW_THRESHOLD = "potential_below_threshold"
#: Why a scored run has no battery / solar split: the extra solve failed.
REASON_BREAKDOWN_UNSOLVED = "breakdown_unsolved"
#: Why a scored run has no EV figures: no managed session delivered energy
#: that could have been moved, or the EV solves failed.
REASON_NO_EV_SESSION = "no_ev_session"
REASON_EV_UNSOLVED = "ev_unsolved"


@dataclass(frozen=True)
class PerformanceSample:
    """What one coordinator cycle read, as far as the performance record needs.

    Attributes:
        at: When the cycle read it (timezone-aware).
        grid_import_energy_kwh: Cumulative grid import meter.
        grid_export_energy_kwh: Cumulative grid export meter.
        soc_pct: Battery state of charge.
        import_price: Import price in force, or ``None`` when not authoritative.
        export_price: Raw export price in force, before the export fee.
        in_control: Whether HSEM was running the battery itself: force mode
            ``auto``, not read-only and not degraded.
        pv_energy_kwh: Cumulative PV production meter, when configured.
        pv_power_w: Instantaneous PV production, the fallback for the meter.
        ev_power_w: Charging power of the configured EV chargers together, or
            ``None`` when no EV is configured.
        ev_connected: Whether a configured EV was plugged in.
        chargers: Each configured charger on its own, for the EV timing
            score (issue #1271): primary first.
    """

    at: datetime
    grid_import_energy_kwh: float | None
    grid_export_energy_kwh: float | None
    soc_pct: float | None
    import_price: float | None
    export_price: float | None
    in_control: bool = True
    pv_energy_kwh: float | None = None
    pv_power_w: float | None = None
    ev_power_w: float | None = None
    ev_connected: bool | None = None
    chargers: tuple[ChargerSample, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """Return the sample as a plain dict for JSON serialisation."""
        return {
            "at": self.at.astimezone(UTC).isoformat(),
            "grid_import_energy_kwh": self.grid_import_energy_kwh,
            "grid_export_energy_kwh": self.grid_export_energy_kwh,
            "soc_pct": self.soc_pct,
            "import_price": self.import_price,
            "export_price": self.export_price,
            "in_control": self.in_control,
            "pv_energy_kwh": self.pv_energy_kwh,
            "pv_power_w": self.pv_power_w,
            "ev_power_w": self.ev_power_w,
            "ev_connected": self.ev_connected,
            "chargers": [charger.as_dict() for charger in self.chargers],
        }

    @classmethod
    def from_dict(cls, data: Any) -> PerformanceSample | None:
        """Rebuild a sample from JSON, or ``None`` when it is not usable."""
        if not isinstance(data, dict):
            return None
        at = aware_datetime_from_iso(data.get("at"))
        if at is None:
            return None
        return cls(
            at=at.astimezone(UTC),
            grid_import_energy_kwh=finite_float(data.get("grid_import_energy_kwh")),
            grid_export_energy_kwh=finite_float(data.get("grid_export_energy_kwh")),
            soc_pct=finite_float(data.get("soc_pct")),
            import_price=finite_float(data.get("import_price")),
            export_price=finite_float(data.get("export_price")),
            in_control=bool(data.get("in_control", False)),
            pv_energy_kwh=finite_float(data.get("pv_energy_kwh")),
            pv_power_w=finite_float(data.get("pv_power_w")),
            ev_power_w=finite_float(data.get("ev_power_w")),
            ev_connected=optional_bool(data.get("ev_connected")),
            chargers=tuple(
                ChargerSample.from_dict(raw) for raw in as_list(data.get("chargers"))
            ),
        )


@dataclass
class PerformanceSlot:
    """What actually happened in one slot.

    The battery's flows are not stored: the stored energy at the slot's two
    ends is, and the flows follow from it at scoring time under the same
    efficiencies the hindsight runs use.

    Attributes:
        start: Slot start in UTC.
        minutes: Slot width.
        grid_import_kwh: Grid import, from the cumulative meter.
        grid_export_kwh: Grid export, from the cumulative meter.
        soc_start_pct: Battery SoC at the first observed instant of the slot.
        soc_end_pct: Battery SoC at the last observed instant of the slot.
        import_price: Import price, time-weighted over the observed seconds.
        export_price: Raw export price, time-weighted likewise.
        coverage_seconds: Seconds of the slot in which everything a score
            needs was observed.
        control_seconds: Those of them in which HSEM was in control.
        pv_kwh: PV production, or ``None`` when it was not observed for every
            covered second.
        ev_kwh: Energy delivered to the configured EVs, or ``None`` likewise
            (and whenever no EV is configured).
        ev_connected: Whether a configured EV was plugged in at any time.
        chargers: Each configured charger on its own, primary first
            (issue #1271).  Empty in slots recorded before that existed.
    """

    start: datetime
    minutes: int
    grid_import_kwh: float = 0.0
    grid_export_kwh: float = 0.0
    soc_start_pct: float | None = None
    soc_end_pct: float | None = None
    import_price: float = 0.0
    export_price: float = 0.0
    coverage_seconds: float = 0.0
    control_seconds: float = 0.0
    pv_kwh: float | None = 0.0
    ev_kwh: float | None = 0.0
    ev_connected: bool | None = None
    chargers: list[ChargerRecord] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        """Return the observed share of the slot, 0 to 1."""
        return min(self.coverage_seconds / (self.minutes * 60.0), 1.0)

    def add(
        self,
        *,
        seconds: float,
        grid_import_kwh: float,
        grid_export_kwh: float,
        soc_start_pct: float,
        soc_end_pct: float,
        import_price: float,
        export_price: float,
        in_control: bool,
        pv_kwh: float | None,
        ev_kwh: float | None,
        ev_connected: bool | None,
        chargers: Sequence[ChargerPiece] = (),
    ) -> None:
        """Add one observed stretch of the slot.

        Args:
            seconds: Length of the stretch.
            grid_import_kwh: Grid import during it.
            grid_export_kwh: Grid export during it.
            soc_start_pct: Battery SoC at its start.
            soc_end_pct: Battery SoC at its end.
            import_price: Import price in force.
            export_price: Raw export price in force.
            in_control: Whether HSEM was in control throughout.
            pv_kwh: PV production during it, or ``None`` if unobserved.
            ev_kwh: EV energy delivered during it, or ``None`` if unobserved.
            ev_connected: Whether an EV was plugged in, or ``None`` if unknown.
            chargers: What each configured charger did during it.
        """
        merge_charger_pieces(
            self.chargers, chargers, first=self.coverage_seconds <= 0.0
        )
        covered = self.coverage_seconds + seconds
        if covered > 0.0:
            self.import_price = (
                self.import_price * self.coverage_seconds + import_price * seconds
            ) / covered
            self.export_price = (
                self.export_price * self.coverage_seconds + export_price * seconds
            ) / covered
        self.coverage_seconds = covered
        if in_control:
            self.control_seconds += seconds
        self.grid_import_kwh += grid_import_kwh
        self.grid_export_kwh += grid_export_kwh
        if self.soc_start_pct is None:
            self.soc_start_pct = soc_start_pct
        self.soc_end_pct = soc_end_pct
        self.pv_kwh = add_optional(self.pv_kwh, pv_kwh)
        self.ev_kwh = add_optional(self.ev_kwh, ev_kwh)
        if ev_connected is not None:
            self.ev_connected = bool(self.ev_connected) or ev_connected

    def as_dict(self) -> dict[str, Any]:
        """Return the slot as a plain dict for JSON serialisation."""
        return {
            "start": self.start.astimezone(UTC).isoformat(),
            "minutes": self.minutes,
            "grid_import_kwh": self.grid_import_kwh,
            "grid_export_kwh": self.grid_export_kwh,
            "soc_start_pct": self.soc_start_pct,
            "soc_end_pct": self.soc_end_pct,
            "import_price": self.import_price,
            "export_price": self.export_price,
            "coverage_seconds": self.coverage_seconds,
            "control_seconds": self.control_seconds,
            "pv_kwh": self.pv_kwh,
            "ev_kwh": self.ev_kwh,
            "ev_connected": self.ev_connected,
            "chargers": [charger.as_dict() for charger in self.chargers],
        }

    @classmethod
    def from_dict(cls, data: Any) -> PerformanceSlot | None:
        """Rebuild a slot from JSON, or ``None`` when it is not usable."""
        if not isinstance(data, dict):
            return None
        start = aware_datetime_from_iso(data.get("start"))
        minutes = finite_float(data.get("minutes"), minimum=1.0, maximum=1440.0)
        required = {
            name: finite_float(data.get(name))
            for name in (
                "grid_import_kwh",
                "grid_export_kwh",
                "import_price",
                "export_price",
                "coverage_seconds",
                "control_seconds",
            )
        }
        if start is None or minutes is None or None in required.values():
            return None
        slot = cls(start=start.astimezone(UTC), minutes=int(minutes))
        for name, value in required.items():
            setattr(slot, name, value)
        slot.soc_start_pct = finite_float(data.get("soc_start_pct"))
        slot.soc_end_pct = finite_float(data.get("soc_end_pct"))
        slot.pv_kwh = finite_float(data.get("pv_kwh"))
        slot.ev_kwh = finite_float(data.get("ev_kwh"))
        slot.ev_connected = optional_bool(data.get("ev_connected"))
        slot.chargers = [
            ChargerRecord.from_dict(raw) for raw in as_list(data.get("chargers"))
        ]
        return slot


@dataclass(frozen=True)
class PerformanceScore:
    """The hindsight score of a run of consecutive slots.

    Every cost is ``None`` on a run that could not be scored; :attr:`reason`
    then says why.

    Attributes:
        start: Start of the first slot, in UTC.
        end: End of the last slot, in UTC.
        slot_count: Slots the run is made of.
        reason: Why the run has no score, or ``None``.
        coverage: Observed share of the run, 0 to 1.
        control_coverage: Share of the observed time in which HSEM was in
            control, or ``None`` when nothing was observed.
        realized_cost: Grid cost that was paid.
        oracle_cost: Grid cost of the perfect-foresight oracle that ends with
            at least the stored energy the battery really ended with.
        baseline_cost: Grid cost of plain inverter self-consumption.
        potential: ``baseline_cost`` minus the cost of the oracle that ends
            where self-consumption ends.
        pv_as_realized_cost: Grid cost of the oracle that keeps the realized
            handling of PV surplus; splits the regret into a battery and a
            solar part (issue #1270).  ``None`` on scores from before that
            split existed and when its solve failed.
        ev_shiftable_kwh: EV energy delivered in managed sessions, which
            could have been delivered at other times (issue #1271).  ``None``
            on scores from before the EV score existed.
        ev_joint_oracle_cost: Grid cost with the battery and the EV timing
            both chosen by hindsight, ending like ``oracle_cost``'s run.
        ev_plug_and_charge_cost: Grid cost with the battery chosen by
            hindsight and every session charged at full power from its start.
        start_kwh: Stored energy the run started with.
        realized_end_kwh: Stored energy the run really ended with.
        baseline_end_kwh: Stored energy self-consumption would have ended with.
        residual_kwh: Energy the meters cannot explain, or ``None`` when PV
            was not observed throughout.
        notes: Anything about the data that qualifies the score.
        day: The local calendar day the run is, or ``None`` for a window.
    """

    start: datetime
    end: datetime
    slot_count: int
    reason: str | None = None
    coverage: float = 0.0
    control_coverage: float | None = None
    realized_cost: float | None = None
    oracle_cost: float | None = None
    baseline_cost: float | None = None
    potential: float | None = None
    pv_as_realized_cost: float | None = None
    ev_shiftable_kwh: float | None = None
    ev_joint_oracle_cost: float | None = None
    ev_plug_and_charge_cost: float | None = None
    start_kwh: float | None = None
    realized_end_kwh: float | None = None
    baseline_end_kwh: float | None = None
    residual_kwh: float | None = None
    notes: tuple[str, ...] = ()
    day: str | None = None

    @property
    def window_score(self) -> WindowScore | None:
        """Return the score in the shared form, or ``None`` if there is none."""
        if (
            self.reason is not None
            or self.realized_cost is None
            or self.oracle_cost is None
            or self.baseline_cost is None
            or self.potential is None
            or self.realized_end_kwh is None
            or self.baseline_end_kwh is None
        ):
            return None
        return WindowScore(
            realized_cost=self.realized_cost,
            oracle_cost=self.oracle_cost,
            baseline_cost=self.baseline_cost,
            potential=self.potential,
            realized_end_kwh=self.realized_end_kwh,
            baseline_end_kwh=self.baseline_end_kwh,
            pv_as_realized_cost=self.pv_as_realized_cost,
        )

    @property
    def is_scored(self) -> bool:
        """Return ``True`` when the run carries a score."""
        return self.window_score is not None

    @property
    def regret(self) -> float | None:
        """Return the missed opportunity: ``realized − oracle``."""
        score = self.window_score
        return None if score is None else score.regret

    @property
    def savings(self) -> float | None:
        """Return the actual benefit: ``potential − regret``."""
        score = self.window_score
        return None if score is None else score.savings

    @property
    def capture(self) -> float | None:
        """Return the savings capture as a fraction, if the run has one."""
        score = self.window_score
        return None if score is None else score.capture

    @property
    def capture_reason(self) -> str | None:
        """Return why the run has no capture share, or ``None`` when it has."""
        if self.reason is not None:
            return self.reason
        if self.capture is None:
            return REASON_POTENTIAL_BELOW_THRESHOLD
        return None

    @property
    def battery_loss(self) -> float | None:
        """Return the missed opportunity that lies in battery dispatch."""
        score = self.window_score
        return None if score is None else score.battery_loss

    @property
    def solar_loss(self) -> float | None:
        """Return the missed opportunity that lies in PV surplus handling."""
        score = self.window_score
        return None if score is None else score.solar_loss

    @property
    def battery_utilisation(self) -> float | None:
        """Return the battery utilisation as a fraction, if the run has one."""
        score = self.window_score
        return None if score is None else score.battery_utilisation

    @property
    def solar_utilisation(self) -> float | None:
        """Return the solar utilisation as a fraction, if the run has one."""
        score = self.window_score
        return None if score is None else score.solar_utilisation

    @property
    def breakdown_reason(self) -> str | None:
        """Return why the run has no battery / solar shares, or ``None``."""
        if self.reason is not None:
            return self.reason
        if self.pv_as_realized_cost is None:
            return REASON_BREAKDOWN_UNSOLVED
        if self.battery_utilisation is None:
            return REASON_POTENTIAL_BELOW_THRESHOLD
        return None

    @property
    def has_ev_score(self) -> bool:
        """Return whether both EV hindsight runs were solved for this run."""
        return (
            self.is_scored
            and self.ev_joint_oracle_cost is not None
            and self.ev_plug_and_charge_cost is not None
        )

    @property
    def ev_missed(self) -> float | None:
        """Return what better EV timing would have saved: ``oracle − joint``."""
        if not self.has_ev_score or self.oracle_cost is None:
            return None
        return self.oracle_cost - (self.ev_joint_oracle_cost or 0.0)

    @property
    def ev_potential(self) -> float | None:
        """Return what the best EV timing saves over plugging in and charging."""
        if not self.has_ev_score:
            return None
        return (self.ev_plug_and_charge_cost or 0.0) - (
            self.ev_joint_oracle_cost or 0.0
        )

    @property
    def ev_efficiency(self) -> float | None:
        """Return the share of the EV potential that was realized, if any."""
        potential = self.ev_potential
        if potential is None:
            return None
        return utilisation_share(self.ev_missed, potential)

    @property
    def ev_reason(self) -> str | None:
        """Return why the run has no EV charging efficiency, or ``None``."""
        if self.reason is not None:
            return self.reason
        if not self.ev_shiftable_kwh:
            return REASON_NO_EV_SESSION
        if not self.has_ev_score:
            return REASON_EV_UNSOLVED
        if self.ev_efficiency is None:
            return REASON_POTENTIAL_BELOW_THRESHOLD
        return None

    def for_day(self, day: str) -> PerformanceScore:
        """Return this score labelled as the local calendar day *day*."""
        return replace(self, day=day)

    def as_dict(self) -> dict[str, Any]:
        """Return the score as a plain dict for JSON serialisation."""
        return {
            "start": self.start.astimezone(UTC).isoformat(),
            "end": self.end.astimezone(UTC).isoformat(),
            "slot_count": self.slot_count,
            "reason": self.reason,
            "coverage": self.coverage,
            "control_coverage": self.control_coverage,
            "realized_cost": self.realized_cost,
            "oracle_cost": self.oracle_cost,
            "baseline_cost": self.baseline_cost,
            "potential": self.potential,
            "pv_as_realized_cost": self.pv_as_realized_cost,
            "ev_shiftable_kwh": self.ev_shiftable_kwh,
            "ev_joint_oracle_cost": self.ev_joint_oracle_cost,
            "ev_plug_and_charge_cost": self.ev_plug_and_charge_cost,
            "start_kwh": self.start_kwh,
            "realized_end_kwh": self.realized_end_kwh,
            "baseline_end_kwh": self.baseline_end_kwh,
            "residual_kwh": self.residual_kwh,
            "notes": list(self.notes),
            "day": self.day,
        }

    @classmethod
    def from_dict(cls, data: Any) -> PerformanceScore | None:
        """Rebuild a score from JSON, or ``None`` when it is not usable."""
        if not isinstance(data, dict):
            return None
        start = aware_datetime_from_iso(data.get("start"))
        end = aware_datetime_from_iso(data.get("end"))
        slot_count = finite_float(data.get("slot_count"), minimum=0.0)
        if start is None or end is None or slot_count is None:
            return None
        reason = data.get("reason")
        notes = data.get("notes")
        day = data.get("day")
        return cls(
            start=start.astimezone(UTC),
            end=end.astimezone(UTC),
            slot_count=int(slot_count),
            reason=reason if isinstance(reason, str) else None,
            coverage=finite_float(data.get("coverage"), minimum=0.0, maximum=1.0)
            or 0.0,
            control_coverage=finite_float(
                data.get("control_coverage"), minimum=0.0, maximum=1.0
            ),
            realized_cost=finite_float(data.get("realized_cost")),
            oracle_cost=finite_float(data.get("oracle_cost")),
            baseline_cost=finite_float(data.get("baseline_cost")),
            potential=finite_float(data.get("potential")),
            pv_as_realized_cost=finite_float(data.get("pv_as_realized_cost")),
            ev_shiftable_kwh=finite_float(data.get("ev_shiftable_kwh"), minimum=0.0),
            ev_joint_oracle_cost=finite_float(data.get("ev_joint_oracle_cost")),
            ev_plug_and_charge_cost=finite_float(data.get("ev_plug_and_charge_cost")),
            start_kwh=finite_float(data.get("start_kwh")),
            realized_end_kwh=finite_float(data.get("realized_end_kwh")),
            baseline_end_kwh=finite_float(data.get("baseline_end_kwh")),
            residual_kwh=finite_float(data.get("residual_kwh")),
            notes=tuple(str(note) for note in notes) if isinstance(notes, list) else (),
            day=day if isinstance(day, str) else None,
        )
