"""Recorded days kept for the ``hsem.what_if`` service (issue #1432).

The performance tracker keeps per-slot actuals for two days and only a score
per day after that.  A what-if replay needs the slots themselves, so each
completed local day whose slots are complete is copied here once, in a compact
form, and kept for :data:`MAX_RECORDED_DAYS` days in its own history file.

Only what the meters read is stored: grid import and export, the SoC at each
slot boundary, both prices and PV.  The house load is derived from them at
replay time, the same way the performance scoring derives it.  That takes the
battery's capacity and efficiencies, which turn a change in SoC into energy,
so the ones in force on the day are stored with it: a battery that is changed
later must not rewrite the days before the change.  The SoC limits and the
power limits of the day are stored for the same reason (issue #1455): they
are what the day's baseline is solved with.

The file also holds the real hourly prices of up to
:data:`MAX_HISTORY_DAYS` days.  Long-term statistics know the load and the PV
of a day that was never recorded here, but not its prices, so the prices are
what has to be kept to replay such a day (``utils/what_if_rebuild.py``).

No Home Assistant imports.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from custom_components.hsem.models.performance_record import PerformanceSlot
from custom_components.hsem.models.price_history_tracker import (
    merged_price_source,
    price_source_differs,
)
from custom_components.hsem.models.price_profile import PricePair
from custom_components.hsem.planner.hindsight_scoring import SiteLimits
from custom_components.hsem.utils.persistence import (
    async_load_history,
    aware_datetime_from_iso,
    finite_float,
    write_json_history_file,
)

#: Version of the history file.  Version 2 added the limits of each day and
#: the hourly prices (issue #1455).
SCHEMA_VERSION = 2

#: Versions that are read.  A version 1 file holds days without limits and no
#: prices; a file of any other version is not read.
READABLE_SCHEMAS = (1, SCHEMA_VERSION)

#: Decimals kept of an efficiency fraction.
_EFFICIENCY_DIGITS = 6

#: Recorded days kept.
MAX_RECORDED_DAYS = 90

#: Days of hourly prices kept, and the longest history a what-if can replay.
MAX_HISTORY_DAYS = 365

#: Latest hour start read from the file, as a Unix timestamp: the year 2200.
_MAX_TIMESTAMP = 7258118400.0

#: Decimals kept in the history file: 0.1 Wh, 0.0001 % SoC and 0.00001 of the
#: currency are below what any of the meters resolve.
_ENERGY_DIGITS = 4
_PRICE_DIGITS = 5

#: The per-slot series of a recorded day that hold one number per slot.
_SERIES: tuple[str, ...] = (
    "grid_import_kwh",
    "grid_export_kwh",
    "import_price",
    "export_price",
)


def _close(
    first: Sequence[float | None], second: Sequence[float | None], digits: int
) -> bool:
    """Return whether two series agree at the resolution of the history file.

    Args:
        first: One series.
        second: The other series.
        digits: Decimals the history file keeps of the series.  Rounding moves
            a value by at most half a unit of the last decimal, so two values
            closer than one unit are the same reading.
    """
    tolerance = 10.0**-digits
    return len(first) == len(second) and all(
        (a is None and b is None)
        or (a is not None and b is not None and abs(a - b) < tolerance)
        for a, b in zip(first, second, strict=True)
    )


def _floats(raw: Any, count: int) -> tuple[float, ...] | None:
    """Return *raw* as *count* finite floats, or ``None`` when it is not that."""
    if not isinstance(raw, list) or len(raw) != count:
        return None
    values = [finite_float(value) for value in raw]
    if any(value is None for value in values):
        return None
    return tuple(value for value in values if value is not None)


@dataclass(frozen=True)
class DayLimits:
    """The battery limits in force on a recorded day (issue #1455).

    Attributes:
        min_soc_pct: End-of-discharge SoC.
        max_soc_pct: Charging cut-off SoC, above :attr:`min_soc_pct`.
        max_charge_kw: Charge power limit.
        max_discharge_kw: Discharge power limit.
    """

    min_soc_pct: float
    max_soc_pct: float
    max_charge_kw: float
    max_discharge_kw: float

    @classmethod
    def from_site(cls, site: SiteLimits) -> DayLimits | None:
        """Return the limits of *site*, or ``None`` when it has no capacity.

        Args:
            site: The installation's limits on the day.
        """
        if site.max_soc_pct <= site.min_soc_pct:
            return None
        return cls(
            min_soc_pct=site.min_soc_pct,
            max_soc_pct=site.max_soc_pct,
            max_charge_kw=site.max_charge_kw,
            max_discharge_kw=site.max_discharge_kw,
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DayLimits | None:
        """Rebuild the limits from a day's JSON, or ``None`` when it has none.

        A day of a version 1 file has none.  Limits that are incomplete or
        leave no capacity are dropped as a whole.
        """
        low = finite_float(data.get("min_soc_pct"), minimum=0.0, maximum=100.0)
        high = finite_float(data.get("max_soc_pct"), minimum=0.0, maximum=100.0)
        charge = finite_float(data.get("max_charge_kw"), minimum=0.0)
        discharge = finite_float(data.get("max_discharge_kw"), minimum=0.0)
        if (
            low is None
            or high is None
            or charge is None
            or discharge is None
            or high <= low
        ):
            return None
        return cls(low, high, charge, discharge)

    def as_tuple(self) -> tuple[float, float, float, float]:
        """Return the four limits in the order of the attributes."""
        return (
            self.min_soc_pct,
            self.max_soc_pct,
            self.max_charge_kw,
            self.max_discharge_kw,
        )

    def as_dict(self) -> dict[str, float]:
        """Return the limits as the keys they have in a day's JSON."""
        return {
            "min_soc_pct": round(self.min_soc_pct, _ENERGY_DIGITS),
            "max_soc_pct": round(self.max_soc_pct, _ENERGY_DIGITS),
            "max_charge_kw": round(self.max_charge_kw, _ENERGY_DIGITS),
            "max_discharge_kw": round(self.max_discharge_kw, _ENERGY_DIGITS),
        }


@dataclass(frozen=True)
class RecordedDay:
    """One completed local day, as the meters recorded it.

    Attributes:
        day: The local day as an ISO date.
        start: UTC start of the day's first slot.
        slot_minutes: Slot width.
        rated_kwh: Rated battery capacity the day was recorded with.
        charge_efficiency: Charge efficiency of that day, as a fraction.
        discharge_efficiency: Discharge efficiency of that day, as a fraction.
        soc_pct: Battery SoC at each slot boundary: one value more than there
            are slots.
        grid_import_kwh: Grid import per slot.
        grid_export_kwh: Grid export per slot.
        import_price: Import price per slot.
        export_price: Raw export price per slot, before the export fee.
        pv_kwh: PV production per slot, ``None`` where it was not observed.
        limits: The SoC and power limits of that day, ``None`` for a day
            recorded before they were stored: such a day is replayed with
            today's limits.
        from_statistics: Whether the day was rebuilt from hourly long-term
            statistics for one replay.  Such a day is never stored.
    """

    day: str
    start: datetime
    slot_minutes: int
    rated_kwh: float
    charge_efficiency: float
    discharge_efficiency: float
    soc_pct: tuple[float, ...]
    grid_import_kwh: tuple[float, ...]
    grid_export_kwh: tuple[float, ...]
    import_price: tuple[float, ...]
    export_price: tuple[float, ...]
    pv_kwh: tuple[float | None, ...]
    limits: DayLimits | None = None
    from_statistics: bool = False

    @classmethod
    def from_slots(
        cls, day: str, slots: Sequence[PerformanceSlot], site: SiteLimits
    ) -> RecordedDay:
        """Return the day made of consecutive, complete recorded slots.

        A slot's SoC runs to the next slot's first reading, as in the
        performance scoring, so the boundaries form one series.

        Args:
            day: The local day as an ISO date.
            slots: The day's slots in order, each with both SoC readings.
            site: The installation's limits on that day.

        Raises:
            ValueError: If *slots* is empty.
        """
        if not slots:
            raise ValueError("a recorded day needs at least one slot")
        return cls(
            day=day,
            start=slots[0].start.astimezone(UTC),
            slot_minutes=slots[0].minutes,
            rated_kwh=site.rated_kwh,
            charge_efficiency=site.charge_efficiency,
            discharge_efficiency=site.discharge_efficiency,
            soc_pct=(
                *(slot.soc_start_pct or 0.0 for slot in slots),
                slots[-1].soc_end_pct or 0.0,
            ),
            grid_import_kwh=tuple(slot.grid_import_kwh for slot in slots),
            grid_export_kwh=tuple(slot.grid_export_kwh for slot in slots),
            import_price=tuple(slot.import_price for slot in slots),
            export_price=tuple(slot.export_price for slot in slots),
            pv_kwh=tuple(slot.pv_kwh for slot in slots),
            limits=DayLimits.from_site(site),
        )

    def site_limits(self, current: SiteLimits) -> SiteLimits:
        """Return *current* with the SoC and power limits of this day.

        Args:
            current: The installation's limits as configured today.

        Returns:
            *current* itself for a day without stored limits.
        """
        if self.limits is None:
            return current
        return replace(
            current,
            min_soc_pct=self.limits.min_soc_pct,
            max_soc_pct=self.limits.max_soc_pct,
            max_charge_kw=self.limits.max_charge_kw,
            max_discharge_kw=self.limits.max_discharge_kw,
        )

    def matches(self, other: RecordedDay | None) -> bool:
        """Return whether *other* holds the same readings as this day.

        Compared at the resolution of the history file, so a day read back
        from disk, which is rounded, matches the unrounded day it was written
        from (issue #1454).

        Args:
            other: The day to compare with, ``None`` for no day.
        """
        return (
            other is not None
            and self.day == other.day
            and self.start == other.start
            and self.slot_minutes == other.slot_minutes
            and _close(
                (self.rated_kwh, *self.soc_pct, *self.pv_kwh),
                (other.rated_kwh, *other.soc_pct, *other.pv_kwh),
                _ENERGY_DIGITS,
            )
            and _close(
                () if self.limits is None else self.limits.as_tuple(),
                () if other.limits is None else other.limits.as_tuple(),
                _ENERGY_DIGITS,
            )
            and _close(
                (self.charge_efficiency, self.discharge_efficiency),
                (other.charge_efficiency, other.discharge_efficiency),
                _EFFICIENCY_DIGITS,
            )
            and _close(
                (*self.grid_import_kwh, *self.grid_export_kwh),
                (*other.grid_import_kwh, *other.grid_export_kwh),
                _ENERGY_DIGITS,
            )
            and _close(
                (*self.import_price, *self.export_price),
                (*other.import_price, *other.export_price),
                _PRICE_DIGITS,
            )
        )

    def performance_slots(self) -> list[PerformanceSlot]:
        """Return the day as the slots the performance scoring works on."""
        step = timedelta(minutes=self.slot_minutes)
        return [
            PerformanceSlot(
                start=self.start + index * step,
                minutes=self.slot_minutes,
                grid_import_kwh=self.grid_import_kwh[index],
                grid_export_kwh=self.grid_export_kwh[index],
                soc_start_pct=self.soc_pct[index],
                soc_end_pct=self.soc_pct[index + 1],
                import_price=self.import_price[index],
                export_price=self.export_price[index],
                coverage_seconds=step.total_seconds(),
                pv_kwh=self.pv_kwh[index],
                ev_kwh=None,
            )
            for index in range(len(self.grid_import_kwh))
        ]

    def as_dict(self) -> dict[str, Any]:
        """Return the day as a plain dict for JSON serialisation."""
        return {
            "day": self.day,
            "start": self.start.isoformat(),
            "slot_minutes": self.slot_minutes,
            "rated_kwh": round(self.rated_kwh, _ENERGY_DIGITS),
            "charge_efficiency": round(self.charge_efficiency, _EFFICIENCY_DIGITS),
            "discharge_efficiency": round(
                self.discharge_efficiency, _EFFICIENCY_DIGITS
            ),
            "soc_pct": [round(value, _ENERGY_DIGITS) for value in self.soc_pct],
            "grid_import_kwh": [
                round(value, _ENERGY_DIGITS) for value in self.grid_import_kwh
            ],
            "grid_export_kwh": [
                round(value, _ENERGY_DIGITS) for value in self.grid_export_kwh
            ],
            "import_price": [
                round(value, _PRICE_DIGITS) for value in self.import_price
            ],
            "export_price": [
                round(value, _PRICE_DIGITS) for value in self.export_price
            ],
            "pv_kwh": [
                None if value is None else round(value, _ENERGY_DIGITS)
                for value in self.pv_kwh
            ],
            **({} if self.limits is None else self.limits.as_dict()),
        }

    @classmethod
    def from_dict(cls, data: Any) -> RecordedDay | None:
        """Rebuild a day from JSON, or ``None`` when it is not usable."""
        if not isinstance(data, dict):
            return None
        day = data.get("day")
        start = aware_datetime_from_iso(data.get("start"))
        minutes = finite_float(data.get("slot_minutes"), minimum=1.0, maximum=1440.0)
        pv = data.get("pv_kwh")
        rated = finite_float(data.get("rated_kwh"), minimum=1e-6)
        charge = finite_float(data.get("charge_efficiency"), minimum=1e-6, maximum=1.0)
        discharge = finite_float(
            data.get("discharge_efficiency"), minimum=1e-6, maximum=1.0
        )
        if (
            not isinstance(day, str)
            or start is None
            or minutes is None
            or rated is None
            or charge is None
            or discharge is None
            or not isinstance(pv, list)
            or not pv
        ):
            return None
        count = len(pv)
        soc = _floats(data.get("soc_pct"), count + 1)
        series = {name: _floats(data.get(name), count) for name in _SERIES}
        if soc is None or any(values is None for values in series.values()):
            return None
        return cls(
            day=day,
            start=start.astimezone(UTC),
            slot_minutes=int(minutes),
            rated_kwh=rated,
            charge_efficiency=charge,
            discharge_efficiency=discharge,
            soc_pct=soc,
            pv_kwh=tuple(finite_float(value) for value in pv),
            limits=DayLimits.from_dict(data),
            grid_import_kwh=series["grid_import_kwh"] or (),
            grid_export_kwh=series["grid_export_kwh"] or (),
            import_price=series["import_price"] or (),
            export_price=series["export_price"] or (),
        )


@dataclass
class WhatIfHistory:
    """The recorded days a what-if can replay, with their own history file.

    Attributes:
        days: Recorded days by ISO date.
        history_file: Path of the JSON history file.
        prices: Real import and export price by UTC hour start, the mean of
            the hour's slots.
        price_source: The price source the kept prices belong to: the price
            entities and their units, as the price history names them.
    """

    days: dict[str, RecordedDay] = field(default_factory=dict)
    history_file: str = ""
    prices: dict[datetime, PricePair] = field(default_factory=dict)
    price_source: dict[str, str] = field(default_factory=dict)

    def add(self, day: RecordedDay) -> None:
        """Keep *day*, dropping the oldest beyond :data:`MAX_RECORDED_DAYS`."""
        self.days[day.day] = day
        for old in sorted(self.days)[:-MAX_RECORDED_DAYS]:
            del self.days[old]

    def prices_for(self, source: Mapping[str, str]) -> dict[datetime, PricePair]:
        """Return the kept prices when they belong to *source*, else none.

        Args:
            source: The price source in use now.
        """
        if price_source_differs(self.price_source, source):
            return {}
        return dict(self.prices)

    def add_prices(
        self,
        hourly: Mapping[datetime, PricePair],
        before: datetime,
        source: Mapping[str, str],
    ) -> bool:
        """Keep the hourly prices before *before* that are not held yet.

        A held hour is never replaced, so the file changes only when a new
        hour arrives.  Hours more than :data:`MAX_HISTORY_DAYS` days before
        *before* are dropped.

        The kept prices belong to one price source.  When the price entity or
        its unit changes, they are on another scale and are dropped first, by
        the rule of the price history
        (:func:`~custom_components.hsem.models.price_history_tracker.price_source_differs`).

        Args:
            hourly: Real import and export price by UTC hour start.
            before: Hours starting at or after this instant are left out.
                The caller passes the start of the local day, so the file is
                written once a day and not once an hour.
            source: The price source *hourly* comes from.

        Returns:
            Whether the held prices or their source changed.
        """
        merged = merged_price_source(self.price_source, source)
        changed = merged != self.price_source
        if price_source_differs(self.price_source, source):
            self.prices.clear()
        self.price_source = merged
        new = {
            hour: pair
            for hour, pair in hourly.items()
            if hour < before and hour not in self.prices
        }
        cutoff = before - timedelta(days=MAX_HISTORY_DAYS + 1)
        old = [hour for hour in self.prices if hour < cutoff]
        for hour in old:
            del self.prices[hour]
        self.prices.update(new)
        return bool(new or old or changed)

    def as_dict(self) -> dict[str, Any]:
        """Return the history as a plain dict for JSON serialisation."""
        return {
            "schema": SCHEMA_VERSION,
            "days": [self.days[day].as_dict() for day in sorted(self.days)],
            "price_source": dict(self.price_source),
            "prices": [
                [
                    int(hour.timestamp()),
                    round(self.prices[hour][0], _PRICE_DIGITS),
                    round(self.prices[hour][1], _PRICE_DIGITS),
                ]
                for hour in sorted(self.prices)
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WhatIfHistory:
        """Create a history from a deserialised file, leaving out bad entries."""
        history = cls()
        if data.get("schema") not in READABLE_SCHEMAS:
            return history
        for raw in data.get("days") or []:
            day = RecordedDay.from_dict(raw)
            if day is not None:
                history.add(day)
        source = data.get("price_source")
        if isinstance(source, dict):
            history.price_source = merged_price_source({}, source)
        for row in data.get("prices") or []:
            values = _floats(row, 3)
            if values is not None and 0.0 <= values[0] <= _MAX_TIMESTAMP:
                history.prices[datetime.fromtimestamp(values[0], UTC)] = (
                    values[1],
                    values[2],
                )
        return history

    async def save_history(self) -> bool:
        """Persist the history to disk atomically.

        Returns:
            ``True`` if the state was written.
        """
        if not self.history_file:
            return False
        return await asyncio.to_thread(
            write_json_history_file, Path(self.history_file), self.as_dict()
        )

    async def load_history(self) -> None:
        """Restore the history from its file, if there is one."""
        if not self.history_file:
            return
        await async_load_history(Path(self.history_file), self._merge_loaded)

    def _merge_loaded(self, data: dict[str, Any]) -> None:
        """Merge a loaded history file into the days already held."""
        # A day added while the file was being read is newer than the file.
        loaded = WhatIfHistory.from_dict(data)
        self.days = {**loaded.days, **self.days}
        if not self.price_source:
            self.price_source = loaded.price_source
        self.prices = {**loaded.prices_for(self.price_source), **self.prices}
