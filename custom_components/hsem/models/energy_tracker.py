"""Energy tracker: daily energy totals and the monthly KPIs built from them.

One record per local day holds what the house used, what crossed the grid
meter, what the PV produced and what went into and out of the battery.  The
monthly figures (self-sufficiency, full equivalent cycles, wear cost and the
measured round-trip loss) are always ratios of sums over those daily records,
never averages of daily ratios (issue #1435).  The round-trip loss is measured
on the battery side and, with an AC-side battery power sensor, on the AC side
(:mod:`~custom_components.hsem.models.energy_round_trip`, issue #1450).

The tracker is a sibling of
:class:`~custom_components.hsem.models.financial_tracker.FinancialTracker`
with a history file of its own, so the financial history keeps its format.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from custom_components.hsem.models.energy_round_trip import (
    REASON_AC_BATTERY_POWER_NOT_MEASURED,
    REASON_BATTERY_POWER_NOT_MEASURED,
    REASON_IMPLAUSIBLE_RATIO,
    REASON_NO_DATA,
    REASON_SOC_CHANGE_TOO_LARGE,
    REASON_THROUGHPUT_BELOW_MINIMUM,
    round_trip_summary,
)
from custom_components.hsem.utils.metering import (
    mean_power_kwh,
    meter_delta,
    signed_power_kwh,
)
from custom_components.hsem.utils.misc import round_or_none
from custom_components.hsem.utils.persistence import (
    async_load_history,
    aware_datetime_from_iso,
    finite_float,
    write_json_history_file,
)

#: Version of the history file.  A file of another version is not read.
SCHEMA_VERSION = 1

#: Days of daily records kept.  One full year (plus a leap day) covers the
#: current month and the eleven before it.
MAX_DAILY_LOG_DAYS = 366

#: Months published in the sensors' ``monthly`` attribute, the current one
#: included.
MONTHLY_ATTRIBUTE_MONTHS = 12

#: House consumption below which a self-sufficiency share is not given.
MIN_CONSUMPTION_KWH = 1.0

#: Share of the battery throughput that must have a known cycle cost before
#: a wear cost is given.
MIN_PRICED_THROUGHPUT_SHARE = 0.95

#: Allowance on the grid import a month's consumption and battery charge can
#: explain, as a share and in kWh, before the import is taken for something
#: the consumption does not cover.
IMPORT_EXCESS_SHARE = 0.05
IMPORT_EXCESS_KWH = 1.0

#: Why a monthly figure is unknown.  The round-trip reasons are in
#: :mod:`energy_round_trip` and are re-exported here.
REASON_CONSUMPTION_NOT_MEASURED = "consumption_not_measured"
REASON_CONSUMPTION_BELOW_MINIMUM = "consumption_below_minimum"
REASON_IMPORT_EXCEEDS_CONSUMPTION = "import_exceeds_consumption"
REASON_BATTERY_NOT_MEASURED = "battery_not_measured"
REASON_CYCLE_COST_UNKNOWN = "cycle_cost_unknown"
REASON_GRID_IMPORT_NOT_MEASURED = "grid_import_not_measured"
REASON_CAPACITY_UNKNOWN = "capacity_unknown"

__all__ = [
    "REASON_AC_BATTERY_POWER_NOT_MEASURED",
    "REASON_BATTERY_POWER_NOT_MEASURED",
    "REASON_IMPLAUSIBLE_RATIO",
    "REASON_NO_DATA",
    "REASON_SOC_CHANGE_TOO_LARGE",
    "REASON_THROUGHPUT_BELOW_MINIMUM",
]

#: The additive quantities of a day.  A period sums every one of them before
#: any ratio is taken.
SUMMED_FIELDS: tuple[str, ...] = (
    "observed_hours",
    "house_consumption_kwh",
    "grid_import_kwh",
    "grid_export_kwh",
    "pv_kwh",
    "battery_charged_kwh",
    "battery_discharged_kwh",
    "wear_cost",
    "paired_consumption_kwh",
    "paired_import_kwh",
    "measured_charged_kwh",
    "measured_discharged_kwh",
    "measured_stored_delta_kwh",
    "ac_charged_kwh",
    "ac_discharged_kwh",
    "ac_stored_delta_kwh",
    "house_hours",
    "grid_import_hours",
    "grid_export_hours",
    "pv_hours",
    "battery_hours",
    "priced_throughput_kwh",
)

#: The summed quantities a month publishes, each with the hours in which it
#: was measured.  A quantity that was never measured is published as unknown.
_PUBLISHED_TOTALS: tuple[tuple[str, str], ...] = (
    ("house_consumption_kwh", "house_hours"),
    ("grid_import_kwh", "grid_import_hours"),
    ("grid_export_kwh", "grid_export_hours"),
    ("pv_kwh", "pv_hours"),
    ("battery_charged_kwh", "battery_hours"),
    ("battery_discharged_kwh", "battery_hours"),
)

#: The readings of a sample, in the order they are persisted.
_SAMPLE_READINGS: tuple[str, ...] = (
    "grid_import_energy_kwh",
    "grid_export_energy_kwh",
    "pv_energy_kwh",
    "pv_power_w",
    "house_power_w",
    "battery_power_w",
    "battery_ac_power_w",
    "soc_pct",
)


@dataclass(frozen=True)
class EnergySample:
    """One coordinator cycle's readings.  A missing reading is ``None``.

    Attributes:
        at: When the readings were taken (timezone-aware).
        grid_import_energy_kwh: Cumulative grid import meter.
        grid_export_energy_kwh: Cumulative grid export meter.
        pv_energy_kwh: Cumulative PV production meter.
        pv_power_w: PV power, used when the PV meter is not available.
        house_power_w: House consumption power, EV charging included.
        battery_power_w: Signed battery power, positive while charging.
        battery_ac_power_w: The same on the AC side of the inverter.
        soc_pct: Battery state of charge.
    """

    at: datetime
    grid_import_energy_kwh: float | None = None
    grid_export_energy_kwh: float | None = None
    pv_energy_kwh: float | None = None
    pv_power_w: float | None = None
    house_power_w: float | None = None
    battery_power_w: float | None = None
    battery_ac_power_w: float | None = None
    soc_pct: float | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the sample as a plain dict for JSON serialisation."""
        data: dict[str, Any] = {"at": self.at.isoformat()}
        for name in _SAMPLE_READINGS:
            data[name] = getattr(self, name)
        return data

    @classmethod
    def from_dict(cls, data: Any) -> EnergySample | None:
        """Create a sample from a deserialised dict, or ``None`` if invalid."""
        if not isinstance(data, dict):
            return None
        at = aware_datetime_from_iso(data.get("at"))
        if at is None:
            return None
        return cls(
            at=at, **{name: finite_float(data.get(name)) for name in _SAMPLE_READINGS}
        )


@dataclass(frozen=True)
class BatterySite:
    """What turns a cycle's battery readings into energy, wear and cycles.

    Attributes:
        rated_capacity_kwh: Nameplate capacity, the base of the SoC.
        usable_capacity_kwh: Capacity between the SoC limits, the base of a
            full equivalent cycle.
        charge_efficiency: Charge efficiency as a fraction.
        discharge_efficiency: Discharge efficiency as a fraction.
        cycle_cost_per_kwh: The planner's cycle cost per kWh of throughput,
            or ``None`` when it cannot be worked out.
    """

    rated_capacity_kwh: float = 0.0
    usable_capacity_kwh: float = 0.0
    charge_efficiency: float = 1.0
    discharge_efficiency: float = 1.0
    cycle_cost_per_kwh: float | None = None


@dataclass
class EnergyDayEntry:
    """One local day's energy totals.

    Attributes:
        date: ISO date of the day.
        observed_hours: Hours covered by consecutive samples.
        house_consumption_kwh: House consumption.
        grid_import_kwh: Grid import.
        grid_export_kwh: Grid export.
        pv_kwh: PV production.
        battery_charged_kwh: Energy into the battery.
        battery_discharged_kwh: Energy out of the battery.
        wear_cost: Cycle cost of that battery throughput.
        paired_consumption_kwh: House consumption of the intervals in which
            the grid import was measured too.
        paired_import_kwh: Grid import of those same intervals.
        measured_charged_kwh: Battery charge taken from the battery power
            reading, in intervals whose SoC change is known.
        measured_discharged_kwh: Battery discharge of those intervals.
        measured_stored_delta_kwh: Change in stored energy over them.
        ac_charged_kwh: Battery charge taken from the AC-side battery power
            reading, in intervals whose SoC change is known.
        ac_discharged_kwh: AC-side battery discharge of those intervals.
        ac_stored_delta_kwh: Change in stored energy over them.
        house_hours: Hours in which the house consumption was measured.
        grid_import_hours: Hours in which the grid import was measured.
        grid_export_hours: Hours in which the grid export was measured.
        pv_hours: Hours in which the PV production was measured.
        battery_hours: Hours in which the battery flows were known.
        priced_throughput_kwh: Battery throughput with a known cycle cost.
        usable_capacity_kwh: Usable capacity as last seen that day.
        rated_capacity_kwh: Rated capacity as last seen that day.
        backfilled: The day is from long-term statistics, not measured.
    """

    date: str
    observed_hours: float = 0.0
    house_consumption_kwh: float = 0.0
    grid_import_kwh: float = 0.0
    grid_export_kwh: float = 0.0
    pv_kwh: float = 0.0
    battery_charged_kwh: float = 0.0
    battery_discharged_kwh: float = 0.0
    wear_cost: float = 0.0
    paired_consumption_kwh: float = 0.0
    paired_import_kwh: float = 0.0
    measured_charged_kwh: float = 0.0
    measured_discharged_kwh: float = 0.0
    measured_stored_delta_kwh: float = 0.0
    ac_charged_kwh: float = 0.0
    ac_discharged_kwh: float = 0.0
    ac_stored_delta_kwh: float = 0.0
    house_hours: float = 0.0
    grid_import_hours: float = 0.0
    grid_export_hours: float = 0.0
    pv_hours: float = 0.0
    battery_hours: float = 0.0
    priced_throughput_kwh: float = 0.0
    usable_capacity_kwh: float = 0.0
    rated_capacity_kwh: float = 0.0
    backfilled: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Return the entry as a plain dict for JSON serialisation."""
        data: dict[str, Any] = {"date": self.date}
        for name in (*SUMMED_FIELDS, "usable_capacity_kwh", "rated_capacity_kwh"):
            data[name] = round(getattr(self, name), 4)
        data["backfilled"] = self.backfilled
        return data

    @classmethod
    def from_dict(cls, data: Any) -> EnergyDayEntry | None:
        """Create an entry from a deserialised dict, or ``None`` if invalid.

        A quantity the file lacks (written by an older version) or that does
        not parse is zero: the day then contributes nothing to it.
        """
        if not isinstance(data, dict):
            return None
        try:
            day = date.fromisoformat(str(data.get("date"))).isoformat()
        except ValueError:
            return None
        entry = cls(date=day)
        for name in (*SUMMED_FIELDS, "usable_capacity_kwh", "rated_capacity_kwh"):
            setattr(entry, name, finite_float(data.get(name)) or 0.0)
        entry.backfilled = data.get("backfilled") is True
        return entry


def previous_month(month: str) -> str:
    """Return the ``YYYY-MM`` key of the month before *month*."""
    first = date.fromisoformat(f"{month}-01")
    return (first - timedelta(days=1)).strftime("%Y-%m")


@dataclass
class EnergyTracker:
    """Accumulates daily energy totals and rolls them up per calendar month.

    Attributes:
        daily_log: Daily records by ISO date, today's included, bounded to
            :data:`MAX_DAILY_LOG_DAYS` days.
        history_file: Path of the JSON history file.
        backfill_complete: The statistics backfill has run (issue #1450).
    """

    daily_log: dict[str, EnergyDayEntry] = field(default_factory=dict)
    history_file: str = ""
    backfill_complete: bool = False
    _last_sample: EnergySample | None = field(default=None, repr=False)
    _saved_at: datetime | None = field(default=None, repr=False)
    _revision: int = field(default=0, repr=False)
    _published: tuple[int, date, dict[str, Any]] | None = field(
        default=None, repr=False
    )

    # ------------------------------------------------------------------
    # Accumulation
    # ------------------------------------------------------------------

    def accumulate(
        self, sample: EnergySample, site: BatterySite, *, max_gap_seconds: float
    ) -> bool:
        """Add the energy between the previous sample and *sample*.

        The interval is booked on the local day of *sample*.  Nothing missing
        is read as zero: a quantity is only added, and its hours only counted,
        when both of its readings are there, and an interval longer than
        *max_gap_seconds* only sets a new baseline.  The length of the
        interval is elapsed time, so a daylight-saving shift between two
        samples changes nothing.

        Args:
            sample: This cycle's readings.
            site: The battery's capacities, efficiencies and cycle cost.
            max_gap_seconds: Longest gap between two samples still booked.

        Returns:
            ``True`` when the interval was booked.
        """
        previous = self._last_sample
        self._last_sample = sample
        if previous is None:
            return False
        # Two datetimes of one time zone subtract as wall-clock times.
        seconds = sample.at.timestamp() - previous.at.timestamp()
        if seconds <= 0.0 or seconds > max_gap_seconds:
            return False
        hours = seconds / 3600.0

        day = sample.at.date()
        entry = self.daily_log.get(day.isoformat())
        if entry is None:
            entry = self.daily_log[day.isoformat()] = EnergyDayEntry(day.isoformat())
            self._prune(day)
        entry.observed_hours += hours
        self._revision += 1

        grid_import = meter_delta(
            previous.grid_import_energy_kwh, sample.grid_import_energy_kwh, hours
        )
        grid_export = meter_delta(
            previous.grid_export_energy_kwh, sample.grid_export_energy_kwh, hours
        )
        pv = meter_delta(previous.pv_energy_kwh, sample.pv_energy_kwh, hours)
        if pv is None:
            pv = mean_power_kwh(previous.pv_power_w, sample.pv_power_w, hours)
        house = mean_power_kwh(previous.house_power_w, sample.house_power_w, hours)

        if grid_import is not None:
            entry.grid_import_kwh += grid_import
            entry.grid_import_hours += hours
        if grid_export is not None:
            entry.grid_export_kwh += grid_export
            entry.grid_export_hours += hours
        if pv is not None:
            entry.pv_kwh += max(pv, 0.0)
            entry.pv_hours += hours
        if house is not None:
            house = max(house, 0.0)
            entry.house_consumption_kwh += house
            entry.house_hours += hours
            if grid_import is not None:
                entry.paired_consumption_kwh += house
                entry.paired_import_kwh += grid_import

        self._accumulate_battery(entry, previous, sample, site, hours)
        return True

    @staticmethod
    def _accumulate_battery(
        entry: EnergyDayEntry,
        previous: EnergySample,
        sample: EnergySample,
        site: BatterySite,
        hours: float,
    ) -> None:
        """Add an interval's battery flows, wear and stored-energy change.

        The flows come from the battery power reading.  Without it they are
        derived from the SoC change and the configured efficiencies, which
        serves the cycle count and the wear cost but is no measurement of the
        round-trip loss.  The AC-side reading only feeds the AC-side round
        trip.
        """
        if site.usable_capacity_kwh > 1e-9:
            entry.usable_capacity_kwh = site.usable_capacity_kwh
        if site.rated_capacity_kwh > 1e-9:
            entry.rated_capacity_kwh = site.rated_capacity_kwh

        stored_delta: float | None = None
        if (
            previous.soc_pct is not None
            and sample.soc_pct is not None
            and site.rated_capacity_kwh > 1e-9
        ):
            stored_delta = (
                site.rated_capacity_kwh * (sample.soc_pct - previous.soc_pct) / 100.0
            )

        ac_flows = signed_power_kwh(
            previous.battery_ac_power_w, sample.battery_ac_power_w, hours
        )
        if ac_flows is not None and stored_delta is not None:
            entry.ac_charged_kwh += ac_flows[0]
            entry.ac_discharged_kwh += ac_flows[1]
            entry.ac_stored_delta_kwh += stored_delta

        flows = signed_power_kwh(
            previous.battery_power_w, sample.battery_power_w, hours
        )
        if flows is not None:
            charged, discharged = flows
            if stored_delta is not None:
                entry.measured_charged_kwh += charged
                entry.measured_discharged_kwh += discharged
                entry.measured_stored_delta_kwh += stored_delta
        elif stored_delta is None:
            return
        elif stored_delta > 0.0:
            charged, discharged = stored_delta / site.charge_efficiency, 0.0
        else:
            charged, discharged = 0.0, -stored_delta * site.discharge_efficiency

        entry.battery_charged_kwh += charged
        entry.battery_discharged_kwh += discharged
        entry.battery_hours += hours
        if site.cycle_cost_per_kwh is not None:
            entry.wear_cost += (charged + discharged) * site.cycle_cost_per_kwh
            entry.priced_throughput_kwh += charged + discharged

    def apply_backfill(self, days: Iterable[EnergyDayEntry], today: date) -> int:
        """Add days from statistics, close the backfill, return the count.

        Only a day before the first measured day and before *today* is taken,
        and never one with a record: a measured day is not changed.
        """
        measured = [
            key for key, entry in self.daily_log.items() if not entry.backfilled
        ]
        before = min([*measured, today.isoformat()])
        added = 0
        for entry in days:
            if entry.date < before and entry.date not in self.daily_log:
                entry.backfilled = True
                self.daily_log[entry.date] = entry
                added += 1
        self._prune(today)
        self.backfill_complete = True
        self._revision += 1
        return added

    def _prune(self, today: date) -> None:
        """Drop records older than :data:`MAX_DAILY_LOG_DAYS` days."""
        oldest_kept = (today - timedelta(days=MAX_DAILY_LOG_DAYS)).isoformat()
        for key in [key for key in self.daily_log if key < oldest_kept]:
            del self.daily_log[key]

    # ------------------------------------------------------------------
    # Monthly rollups
    # ------------------------------------------------------------------

    def month_summary(self, month: str) -> dict[str, Any]:
        """Return a calendar month's totals and KPIs.

        Every KPI is computed from the month's summed daily quantities.  A
        KPI that cannot be given is ``None`` and its ``*_reason`` says why.

        Args:
            month: The month as ``YYYY-MM``.

        Returns:
            The month's figures as a flat dict.
        """
        days = [
            entry for key, entry in sorted(self.daily_log.items()) if key[:7] == month
        ]
        total: dict[str, float] = {
            name: float(sum(getattr(entry, name) for entry in days))
            for name in SUMMED_FIELDS
        }
        usable = next(
            (
                entry.usable_capacity_kwh
                for entry in reversed(days)
                if entry.usable_capacity_kwh > 1e-9
            ),
            0.0,
        )
        rated = next(
            (
                entry.rated_capacity_kwh
                for entry in reversed(days)
                if entry.rated_capacity_kwh > 1e-9
            ),
            0.0,
        )

        summary: dict[str, Any] = {
            "month": month,
            "days": len(days),
            "backfilled_days": sum(1 for entry in days if entry.backfilled),
            "observed_hours": round(total["observed_hours"], 1),
        }
        for name, hours_name in _PUBLISHED_TOTALS:
            measured = total[hours_name] > 1e-9
            summary[name] = round(total[name], 3) if measured else None

        share, share_reason = _self_sufficiency(total, bool(days))
        summary["self_sufficiency"] = round_or_none(
            None if share is None else share * 100.0, 1
        )
        summary["self_sufficiency_reason"] = share_reason

        cycles: float | None = None
        cycles_reason: str | None = None
        if not days:
            cycles_reason = REASON_NO_DATA
        elif total["battery_hours"] <= 1e-9:
            cycles_reason = REASON_BATTERY_NOT_MEASURED
        elif usable <= 1e-9:
            cycles_reason = REASON_CAPACITY_UNKNOWN
        else:
            cycles = total["battery_discharged_kwh"] / usable
        summary["battery_cycles"] = round_or_none(cycles, 2)
        summary["battery_cycles_reason"] = cycles_reason
        summary["usable_capacity_kwh"] = round(usable, 2) if usable > 1e-9 else None

        wear, wear_reason = _wear_cost(total, bool(days))
        summary["battery_wear_cost"] = round_or_none(wear, 3)
        summary["battery_wear_cost_reason"] = wear_reason

        summary.update(
            round_trip_summary(
                "",
                total["measured_charged_kwh"],
                total["measured_discharged_kwh"],
                total["measured_stored_delta_kwh"],
                rated,
                has_days=bool(days),
                not_measured_reason=REASON_BATTERY_POWER_NOT_MEASURED,
            )
        )
        summary.update(
            round_trip_summary(
                "ac_",
                total["ac_charged_kwh"],
                total["ac_discharged_kwh"],
                total["ac_stored_delta_kwh"],
                rated,
                has_days=bool(days),
                not_measured_reason=REASON_AC_BATTERY_POWER_NOT_MEASURED,
            )
        )
        return summary

    def as_sensor_attributes(self, today: date) -> dict[str, Any]:
        """Return what the monthly energy sensors publish.

        Args:
            today: The current local day.

        Returns:
            ``month`` (the current month's key), ``this_month``,
            ``previous_month`` and ``monthly``: the current month and every
            month with a record among the eleven before it, oldest first.
            ``backfilled_*``: the days taken from statistics.
        """
        published = self._published
        if published is not None and published[:2] == (self._revision, today):
            return dict(published[2])
        current = today.strftime("%Y-%m")
        months = [current]
        while len(months) < MONTHLY_ATTRIBUTE_MONTHS:
            months.append(previous_month(months[-1]))
        recorded = {key[:7] for key in self.daily_log}
        monthly = [
            self.month_summary(month)
            for month in reversed(months)
            if month == current or month in recorded
        ]
        backfilled = sorted(k for k, e in self.daily_log.items() if e.backfilled)
        attributes: dict[str, Any] = {
            "month": current,
            "this_month": monthly[-1],
            "previous_month": self.month_summary(previous_month(current)),
            "monthly": monthly,
            "backfilled_days": len(backfilled),
            "backfilled_from": backfilled[0] if backfilled else None,
            "backfilled_until": backfilled[-1] if backfilled else None,
        }
        # Every monthly sensor asks for the same tables on every cycle.
        self._published = (self._revision, today, attributes)
        return dict(attributes)

    # ------------------------------------------------------------------
    # JSON persistence
    # ------------------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        """Return the tracker's state as a plain dict for JSON serialisation."""
        return {
            "schema": SCHEMA_VERSION,
            "backfill_complete": self.backfill_complete,
            "last_sample": (
                None if self._last_sample is None else self._last_sample.as_dict()
            ),
            "daily_log": [
                self.daily_log[day].as_dict() for day in sorted(self.daily_log)
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EnergyTracker:
        """Create a tracker from a deserialised history file.

        A file of another schema version, and any entry that does not parse,
        is left out: the tracker then starts recording afresh.
        """
        tracker = cls()
        if data.get("schema") != SCHEMA_VERSION:
            return tracker
        daily_log = data.get("daily_log")
        for raw in daily_log if isinstance(daily_log, list) else []:
            entry = EnergyDayEntry.from_dict(raw)
            if entry is not None:
                tracker.daily_log[entry.date] = entry
        tracker._last_sample = EnergySample.from_dict(data.get("last_sample"))
        tracker.backfill_complete = data.get("backfill_complete") is True
        return tracker

    def restore_from(self, loaded: EnergyTracker) -> None:
        """Replace this tracker's state with a reconstructed tracker's."""
        history_file = self.history_file
        revision = self._revision
        self.__dict__.update(loaded.__dict__)
        self.history_file = history_file
        self._revision = revision + 1

    def save_due(self, interval_seconds: float) -> bool:
        """Return whether the samples since the last save are worth a write.

        Args:
            interval_seconds: Longest stretch of samples that may be unsaved.
        """
        if self._last_sample is None:
            return False
        if self._saved_at is None:
            return True
        unsaved = self._last_sample.at.timestamp() - self._saved_at.timestamp()
        return unsaved >= interval_seconds

    async def save_history(self) -> bool:
        """Persist the tracker to disk atomically.

        Returns:
            ``True`` if the state was written.
        """
        if not self.history_file:
            return False
        saved_at = None if self._last_sample is None else self._last_sample.at
        saved = await asyncio.to_thread(
            write_json_history_file, Path(self.history_file), self.as_dict()
        )
        if saved:
            self._saved_at = saved_at
        return saved

    async def load_history(self) -> None:
        """Restore the tracker from its history file, if there is one."""
        if not self.history_file:
            return
        await async_load_history(
            Path(self.history_file),
            lambda data: self.restore_from(EnergyTracker.from_dict(data)),
        )


def _self_sufficiency(
    total: dict[str, float], has_days: bool
) -> tuple[float | None, str | None]:
    """Return the share of the consumption not imported, or why it is unknown.

    ``1 − grid import ÷ house consumption`` over the intervals in which both
    were measured.  Grid energy goes to the house or into the battery, so a
    month can import more than the house used by what it charged: the share is
    then zero.  An import beyond that is energy the consumption figure does
    not cover, and the share is unknown instead of zero.
    """
    if not has_days:
        return None, REASON_NO_DATA
    if total["house_hours"] <= 1e-9:
        return None, REASON_CONSUMPTION_NOT_MEASURED
    if total["house_consumption_kwh"] < MIN_CONSUMPTION_KWH:
        return None, REASON_CONSUMPTION_BELOW_MINIMUM
    consumption = total["paired_consumption_kwh"]
    if consumption < MIN_CONSUMPTION_KWH:
        return None, REASON_GRID_IMPORT_NOT_MEASURED
    grid_import = total["paired_import_kwh"]
    explained = consumption + total["battery_charged_kwh"]
    if grid_import > explained * (1.0 + IMPORT_EXCESS_SHARE) + IMPORT_EXCESS_KWH:
        return None, REASON_IMPORT_EXCEEDS_CONSUMPTION
    return max(1.0 - grid_import / consumption, 0.0), None


def _wear_cost(
    total: dict[str, float], has_days: bool
) -> tuple[float | None, str | None]:
    """Return the cycle cost of the battery throughput, or why it is unknown.

    A throughput without a cycle cost (no purchase price, expected cycles or
    usable capacity) is not worth nothing: the cost is then unknown.
    """
    if not has_days:
        return None, REASON_NO_DATA
    if total["battery_hours"] <= 1e-9:
        return None, REASON_BATTERY_NOT_MEASURED
    throughput = total["battery_charged_kwh"] + total["battery_discharged_kwh"]
    if total["priced_throughput_kwh"] < MIN_PRICED_THROUGHPUT_SHARE * throughput:
        return None, REASON_CYCLE_COST_UNKNOWN
    return total["wear_cost"], None
