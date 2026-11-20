"""Recorded days kept for the ``hsem.what_if`` service (issue #1432).

The performance tracker keeps per-slot actuals for two days and only a score
per day after that.  A what-if replay needs the slots themselves, so each
completed local day whose slots are complete is copied here once, in a compact
form, and kept for :data:`MAX_RECORDED_DAYS` days in its own history file.

Only what the meters read is stored: grid import and export, the SoC at each
slot boundary, both prices and PV.  The house load is derived from them at
replay time, the same way the performance scoring derives it.

No Home Assistant imports.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from custom_components.hsem.models.performance_record import PerformanceSlot
from custom_components.hsem.utils.persistence import (
    aware_datetime_from_iso,
    finite_float,
    read_json_history_file,
    write_json_history_file,
)

#: Version of the history file.  A file of another version is not read.
SCHEMA_VERSION = 1

#: Recorded days kept, and the longest history a what-if can replay.
MAX_RECORDED_DAYS = 90

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


def _floats(raw: Any, count: int) -> tuple[float, ...] | None:
    """Return *raw* as *count* finite floats, or ``None`` when it is not that."""
    if not isinstance(raw, list) or len(raw) != count:
        return None
    values = [finite_float(value) for value in raw]
    if any(value is None for value in values):
        return None
    return tuple(value for value in values if value is not None)


@dataclass(frozen=True)
class RecordedDay:
    """One completed local day, as the meters recorded it.

    Attributes:
        day: The local day as an ISO date.
        start: UTC start of the day's first slot.
        slot_minutes: Slot width.
        soc_pct: Battery SoC at each slot boundary: one value more than there
            are slots.
        grid_import_kwh: Grid import per slot.
        grid_export_kwh: Grid export per slot.
        import_price: Import price per slot.
        export_price: Raw export price per slot, before the export fee.
        pv_kwh: PV production per slot, ``None`` where it was not observed.
    """

    day: str
    start: datetime
    slot_minutes: int
    soc_pct: tuple[float, ...]
    grid_import_kwh: tuple[float, ...]
    grid_export_kwh: tuple[float, ...]
    import_price: tuple[float, ...]
    export_price: tuple[float, ...]
    pv_kwh: tuple[float | None, ...]

    @classmethod
    def from_slots(cls, day: str, slots: Sequence[PerformanceSlot]) -> RecordedDay:
        """Return the day made of consecutive, complete recorded slots.

        A slot's SoC runs to the next slot's first reading, as in the
        performance scoring, so the boundaries form one series.

        Args:
            day: The local day as an ISO date.
            slots: The day's slots in order, each with both SoC readings.

        Raises:
            ValueError: If *slots* is empty.
        """
        if not slots:
            raise ValueError("a recorded day needs at least one slot")
        return cls(
            day=day,
            start=slots[0].start.astimezone(UTC),
            slot_minutes=slots[0].minutes,
            soc_pct=(
                *(slot.soc_start_pct or 0.0 for slot in slots),
                slots[-1].soc_end_pct or 0.0,
            ),
            grid_import_kwh=tuple(slot.grid_import_kwh for slot in slots),
            grid_export_kwh=tuple(slot.grid_export_kwh for slot in slots),
            import_price=tuple(slot.import_price for slot in slots),
            export_price=tuple(slot.export_price for slot in slots),
            pv_kwh=tuple(slot.pv_kwh for slot in slots),
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
        if (
            not isinstance(day, str)
            or start is None
            or minutes is None
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
            soc_pct=soc,
            pv_kwh=tuple(finite_float(value) for value in pv),
            **{name: values or () for name, values in series.items()},
        )


@dataclass
class WhatIfHistory:
    """The recorded days a what-if can replay, with their own history file.

    Attributes:
        days: Recorded days by ISO date.
        history_file: Path of the JSON history file.
    """

    days: dict[str, RecordedDay] = field(default_factory=dict)
    history_file: str = ""

    def add(self, day: RecordedDay) -> None:
        """Keep *day*, dropping the oldest beyond :data:`MAX_RECORDED_DAYS`."""
        self.days[day.day] = day
        for old in sorted(self.days)[:-MAX_RECORDED_DAYS]:
            del self.days[old]

    def as_dict(self) -> dict[str, Any]:
        """Return the history as a plain dict for JSON serialisation."""
        return {
            "schema": SCHEMA_VERSION,
            "days": [self.days[day].as_dict() for day in sorted(self.days)],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WhatIfHistory:
        """Create a history from a deserialised file, leaving out bad entries."""
        history = cls()
        if data.get("schema") != SCHEMA_VERSION:
            return history
        for raw in data.get("days") or []:
            day = RecordedDay.from_dict(raw)
            if day is not None:
                history.add(day)
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
        data = await asyncio.to_thread(read_json_history_file, Path(self.history_file))
        if data is not None:
            # A day added while the file was being read is newer than the file.
            self.days = {**WhatIfHistory.from_dict(data).days, **self.days}
