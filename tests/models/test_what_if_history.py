"""Tests for the recorded days behind ``hsem.what_if`` (issue #1432).

A what-if replays whole days long after the performance tracker has dropped
their slots.  These tests pin that a day survives the trip into the compact
record and back, that a damaged file costs only the damaged entries, and that
the history stays bounded.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from custom_components.hsem.models.performance_record import PerformanceSlot
from custom_components.hsem.models.what_if_history import (
    MAX_RECORDED_DAYS,
    SCHEMA_VERSION,
    RecordedDay,
    WhatIfHistory,
)
from tests.utils.what_if_days import FIRST_DAY, SITE, make_day

_START = datetime(2026, 6, 9, 22, 0, tzinfo=UTC)


def _slots(count: int = 4) -> list[PerformanceSlot]:
    """Return consecutive hourly slots whose SoC readings do not quite meet."""
    return [
        PerformanceSlot(
            start=_START + timedelta(hours=index),
            minutes=60,
            grid_import_kwh=1.0 + index,
            grid_export_kwh=0.1 * index,
            soc_start_pct=50.0 + index,
            soc_end_pct=50.9 + index,
            import_price=1.5,
            export_price=0.6,
            coverage_seconds=3600.0,
            pv_kwh=None if index == 2 else 0.25 * index,
        )
        for index in range(count)
    ]


class TestRecordedDay:
    def test_the_soc_boundaries_form_one_series(self) -> None:
        """A slot runs to the next slot's first reading, as in the scoring."""
        day = RecordedDay.from_slots("2026-06-10", _slots(), SITE)

        assert day.start == _START
        assert day.slot_minutes == 60
        # The battery that turns the SoC into energy is kept with the day.
        assert day.rated_kwh == pytest.approx(SITE.rated_kwh)
        assert day.charge_efficiency == pytest.approx(SITE.charge_efficiency)
        assert day.discharge_efficiency == pytest.approx(SITE.discharge_efficiency)
        assert day.soc_pct == pytest.approx((50.0, 51.0, 52.0, 53.0, 53.9))
        assert day.grid_import_kwh == pytest.approx((1.0, 2.0, 3.0, 4.0))
        assert day.pv_kwh[2] is None

    def test_a_day_without_slots_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least one slot"):
            RecordedDay.from_slots("2026-06-10", [], SITE)

    def test_it_comes_back_as_slots_the_scoring_takes(self) -> None:
        slots = RecordedDay.from_slots("2026-06-10", _slots(), SITE).performance_slots()

        assert [slot.start for slot in slots] == [
            _START + timedelta(hours=index) for index in range(4)
        ]
        assert slots[1].soc_start_pct == pytest.approx(51.0)
        assert slots[1].soc_end_pct == pytest.approx(52.0)
        assert slots[3].soc_end_pct == pytest.approx(53.9)
        assert slots[1].grid_import_kwh == pytest.approx(2.0)
        assert slots[1].grid_export_kwh == pytest.approx(0.1)
        assert slots[1].import_price == pytest.approx(1.5)
        assert slots[1].export_price == pytest.approx(0.6)
        assert slots[2].pv_kwh is None
        assert all(slot.coverage == pytest.approx(1.0) for slot in slots)

    def test_it_survives_json(self) -> None:
        day = make_day(FIRST_DAY)

        restored = RecordedDay.from_dict(json.loads(json.dumps(day.as_dict())))

        assert restored is not None
        assert restored.day == day.day
        assert restored.start == day.start
        assert restored.slot_minutes == day.slot_minutes
        assert restored.rated_kwh == pytest.approx(day.rated_kwh)
        assert restored.charge_efficiency == pytest.approx(day.charge_efficiency)
        assert restored.discharge_efficiency == pytest.approx(day.discharge_efficiency)
        assert restored.soc_pct == pytest.approx(day.soc_pct, abs=1e-4)
        assert restored.grid_import_kwh == pytest.approx(day.grid_import_kwh, abs=1e-4)
        assert restored.grid_export_kwh == pytest.approx(day.grid_export_kwh, abs=1e-4)
        assert restored.import_price == pytest.approx(day.import_price, abs=1e-5)
        assert restored.export_price == pytest.approx(day.export_price, abs=1e-5)
        assert restored.pv_kwh == pytest.approx(day.pv_kwh, abs=1e-4)

    def test_unobserved_pv_stays_unobserved(self) -> None:
        day = RecordedDay.from_slots("2026-06-10", _slots(), SITE)

        restored = RecordedDay.from_dict(day.as_dict())

        assert restored is not None
        assert restored.pv_kwh[2] is None
        assert restored.pv_kwh[3] == pytest.approx(0.75)

    @pytest.mark.parametrize(
        "damage",
        [
            {"day": 20260610},
            {"start": "yesterday"},
            {"slot_minutes": 0},
            {"pv_kwh": None},
            {"pv_kwh": []},
            {"soc_pct": [50.0, 51.0]},
            {"grid_import_kwh": [1.0, 2.0, 3.0, "much"]},
            {"export_price": "none"},
            {"rated_kwh": None},
            {"rated_kwh": 0.0},
            {"charge_efficiency": 1.2},
            {"discharge_efficiency": "good"},
        ],
    )
    def test_a_damaged_entry_is_not_read(self, damage: dict[str, Any]) -> None:
        data = RecordedDay.from_slots("2026-06-10", _slots(), SITE).as_dict()

        assert RecordedDay.from_dict({**data, **damage}) is None

    def test_something_that_is_not_an_entry_is_not_read(self) -> None:
        assert RecordedDay.from_dict(["2026-06-10"]) is None


class TestHistory:
    def test_only_the_newest_days_are_kept(self) -> None:
        history = WhatIfHistory()

        for offset in range(MAX_RECORDED_DAYS + 3):
            history.add(make_day(FIRST_DAY + timedelta(days=offset), minutes=360))

        assert len(history.days) == MAX_RECORDED_DAYS
        assert min(history.days) == (FIRST_DAY + timedelta(days=3)).isoformat()

    def test_a_day_recorded_twice_is_kept_once(self) -> None:
        history = WhatIfHistory()
        history.add(make_day(FIRST_DAY, seed=1))
        again = make_day(FIRST_DAY, seed=2)

        history.add(again)

        assert history.days == {again.day: again}

    def test_a_file_of_another_version_is_not_read(self) -> None:
        data = {"schema": SCHEMA_VERSION + 1, "days": [make_day(FIRST_DAY).as_dict()]}

        assert WhatIfHistory.from_dict(data).days == {}

    def test_a_damaged_entry_costs_only_itself(self) -> None:
        good = make_day(FIRST_DAY)

        history = WhatIfHistory.from_dict(
            {"schema": SCHEMA_VERSION, "days": [{"day": "broken"}, good.as_dict()]}
        )

        assert list(history.days) == [good.day]

    def test_a_file_without_days_gives_an_empty_history(self) -> None:
        assert WhatIfHistory.from_dict({"schema": SCHEMA_VERSION}).days == {}

    @pytest.mark.asyncio
    async def test_it_is_saved_and_restored(self, tmp_path: Path) -> None:
        path = tmp_path / ".storage" / "what_if.json"
        history = WhatIfHistory(history_file=str(path))
        history.add(make_day(FIRST_DAY))
        history.add(make_day(date(2026, 6, 2)))

        assert await history.save_history() is True

        restored = WhatIfHistory(history_file=str(path))
        await restored.load_history()
        assert sorted(restored.days) == ["2026-06-01", "2026-06-02"]
        assert json.loads(path.read_text(encoding="utf-8"))["schema"] == SCHEMA_VERSION

    @pytest.mark.asyncio
    async def test_nothing_is_saved_or_loaded_without_a_file(self) -> None:
        history = WhatIfHistory()
        history.add(make_day(FIRST_DAY))

        assert await history.save_history() is False
        await history.load_history()

        assert list(history.days) == ["2026-06-01"]

    @pytest.mark.asyncio
    async def test_a_missing_file_leaves_the_history_alone(
        self, tmp_path: Path
    ) -> None:
        history = WhatIfHistory(history_file=str(tmp_path / "absent.json"))
        history.add(make_day(FIRST_DAY))

        await history.load_history()

        assert list(history.days) == ["2026-06-01"]

    @pytest.mark.asyncio
    async def test_a_day_added_during_the_load_wins_over_the_file(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "what_if.json"
        on_disk = WhatIfHistory(history_file=str(path))
        on_disk.add(make_day(FIRST_DAY, seed=1))
        on_disk.add(make_day(date(2026, 6, 2), seed=1))
        await on_disk.save_history()
        history = WhatIfHistory(history_file=str(path))
        newer = make_day(date(2026, 6, 2), seed=9)
        history.add(newer)

        await history.load_history()

        assert sorted(history.days) == ["2026-06-01", "2026-06-02"]
        assert history.days["2026-06-02"] is newer
