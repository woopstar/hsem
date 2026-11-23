"""Tests for the recorded days behind ``hsem.what_if`` (issue #1432).

A what-if replays whole days long after the performance tracker has dropped
their slots.  These tests pin that a day survives the trip into the compact
record and back, that a damaged file costs only the damaged entries, and that
the history stays bounded.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from custom_components.hsem.models.performance_record import PerformanceSlot
from custom_components.hsem.models.what_if_history import (
    MAX_HISTORY_DAYS,
    MAX_RECORDED_DAYS,
    SCHEMA_VERSION,
    DayLimits,
    RecordedDay,
    WhatIfHistory,
)
from tests.utils.what_if_days import FIRST_DAY, SITE, make_day

_START = datetime(2026, 6, 9, 22, 0, tzinfo=UTC)
_SOURCE = {
    "import_entity": "sensor.import_price",
    "export_entity": "sensor.export_price",
    "import_unit": "DKK/kWh",
    "export_unit": "DKK/kWh",
}


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


class TestMatches:
    """Two records of the same readings are the same day (issue #1454)."""

    def test_a_day_read_back_from_disk_matches_the_day_it_was_written_from(
        self,
    ) -> None:
        """The file is rounded, the fresh record is not."""
        fresh = replace(
            make_day(FIRST_DAY, minutes=15),
            rated_kwh=10.0 / 3.0,
            charge_efficiency=0.97123456789,
        )
        stored = RecordedDay.from_dict(json.loads(json.dumps(fresh.as_dict())))

        assert stored is not None
        # Rounding did move the readings.
        assert stored.rated_kwh != pytest.approx(fresh.rated_kwh, abs=1e-9)
        assert stored.matches(fresh)
        assert fresh.matches(stored)
        assert fresh.matches(fresh)

    def test_no_day_matches_nothing(self) -> None:
        assert not make_day(FIRST_DAY).matches(None)

    @pytest.mark.parametrize(
        "changes",
        [
            {"day": "2026-07-01"},
            {"start": datetime(2026, 6, 1, 1, 0, tzinfo=UTC)},
            {"slot_minutes": 30},
            {"rated_kwh": 10.001},
            {"charge_efficiency": 0.96999},
            {"discharge_efficiency": 0.97001},
        ],
    )
    def test_a_changed_field_does_not_match(self, changes: dict[str, Any]) -> None:
        day = make_day(FIRST_DAY)

        assert not day.matches(replace(day, **changes))
        assert not replace(day, **changes).matches(day)

    @pytest.mark.parametrize(
        ("series", "change"),
        [
            ("soc_pct", 0.001),
            ("grid_import_kwh", 0.001),
            ("grid_export_kwh", 0.001),
            ("import_price", 0.0001),
            ("export_price", 0.0001),
            ("pv_kwh", 0.001),
        ],
    )
    def test_one_changed_reading_does_not_match(
        self, series: str, change: float
    ) -> None:
        day = make_day(FIRST_DAY)
        values = list(getattr(day, series))
        values[5] += change
        changes: dict[str, Any] = {series: tuple(values)}

        assert not day.matches(replace(day, **changes))

    def test_a_reading_that_went_missing_does_not_match(self) -> None:
        day = make_day(FIRST_DAY)
        without = replace(day, pv_kwh=(None, *day.pv_kwh[1:]))

        assert not day.matches(without)
        assert not without.matches(day)
        assert without.matches(without)

    def test_a_day_of_another_length_does_not_match(self) -> None:
        day = make_day(FIRST_DAY)
        shorter = replace(day, pv_kwh=day.pv_kwh[:-1])

        assert not day.matches(shorter)

    def test_a_difference_below_what_the_file_keeps_matches(self) -> None:
        day = make_day(FIRST_DAY)
        values = list(day.grid_import_kwh)
        values[5] += 0.00004

        assert day.matches(replace(day, grid_import_kwh=tuple(values)))


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


class TestDayLimits:
    """Issue #1455: a day keeps the SoC and power limits it was recorded with."""

    def test_a_new_day_holds_the_limits_of_its_site(self) -> None:
        site = replace(SITE, min_soc_pct=12.0, max_soc_pct=95.0, max_charge_kw=3.5)

        day = RecordedDay.from_slots("2026-06-10", _slots(), site)

        assert day.limits is not None
        assert day.limits.as_tuple() == pytest.approx((12.0, 95.0, 3.5, 5.0))
        assert not day.from_statistics

    def test_a_site_without_capacity_gives_no_limits(self) -> None:
        site = replace(SITE, min_soc_pct=50.0, max_soc_pct=50.0)

        assert RecordedDay.from_slots("2026-06-10", _slots(), site).limits is None

    def test_the_limits_survive_json(self) -> None:
        day = RecordedDay.from_slots(
            "2026-06-10", _slots(), replace(SITE, min_soc_pct=100.0 / 3.0)
        )

        restored = RecordedDay.from_dict(json.loads(json.dumps(day.as_dict())))

        assert restored is not None
        assert restored.limits is not None
        assert restored.limits.min_soc_pct == pytest.approx(33.3333)
        assert restored.matches(day)

    def test_a_day_replaces_today_s_limits_with_its_own(self) -> None:
        recorded = replace(SITE, min_soc_pct=20.0, max_soc_pct=90.0, max_charge_kw=2.0)
        day = RecordedDay.from_slots("2026-06-10", _slots(), recorded)
        today = replace(SITE, rated_kwh=20.0, export_fee_per_kwh=0.1)

        site = day.site_limits(today)

        assert site.min_soc_pct == pytest.approx(20.0)
        assert site.max_soc_pct == pytest.approx(90.0)
        assert site.max_charge_kw == pytest.approx(2.0)
        assert site.max_discharge_kw == pytest.approx(SITE.max_discharge_kw)
        # Everything that is not a stored limit stays as it is today.
        assert site.rated_kwh == pytest.approx(20.0)
        assert site.export_fee_per_kwh == pytest.approx(0.1)

    def test_a_day_without_limits_keeps_today_s(self) -> None:
        day = make_day(FIRST_DAY)

        assert day.limits is None
        assert day.site_limits(SITE) is SITE

    def test_a_changed_limit_is_a_changed_day(self) -> None:
        day = RecordedDay.from_slots("2026-06-10", _slots(), SITE)
        moved = RecordedDay.from_slots(
            "2026-06-10", _slots(), replace(SITE, min_soc_pct=10.0)
        )

        assert not day.matches(moved)
        # A day that gained limits is rewritten with them.
        assert not day.matches(replace(day, limits=None))
        assert not replace(day, limits=None).matches(day)

    @pytest.mark.parametrize(
        "change",
        [
            {"min_soc_pct": None},
            {"max_soc_pct": "high"},
            {"max_charge_kw": -1.0},
            {"max_discharge_kw": float("nan")},
            {"min_soc_pct": 90.0, "max_soc_pct": 10.0},
        ],
    )
    def test_unusable_limits_are_dropped_and_the_day_is_kept(
        self, change: dict[str, Any]
    ) -> None:
        data = RecordedDay.from_slots("2026-06-10", _slots(), SITE).as_dict()
        data.update(change)

        restored = RecordedDay.from_dict(json.loads(json.dumps(data)))

        assert restored is not None
        assert restored.limits is None

    def test_from_dict_reads_complete_limits(self) -> None:
        limits = DayLimits.from_dict(
            {
                "min_soc_pct": 5,
                "max_soc_pct": 100,
                "max_charge_kw": 5,
                "max_discharge_kw": 4,
            }
        )

        assert limits is not None
        assert limits.as_tuple() == pytest.approx((5.0, 100.0, 5.0, 4.0))


class TestOldFile:
    """Issue #1455: a version 1 file has days without limits and no prices."""

    def _version_one(self) -> dict[str, Any]:
        days = []
        for offset in range(2):
            day = RecordedDay.from_slots(
                (FIRST_DAY + timedelta(days=offset)).isoformat(), _slots(), SITE
            ).as_dict()
            for name in DayLimits(0.0, 1.0, 0.0, 0.0).as_dict():
                del day[name]
            days.append(day)
        return {"schema": 1, "days": days}

    def test_its_days_load_without_limits(self) -> None:
        history = WhatIfHistory.from_dict(self._version_one())

        assert sorted(history.days) == ["2026-06-01", "2026-06-02"]
        assert all(day.limits is None for day in history.days.values())
        assert history.prices == {}

    @pytest.mark.asyncio
    async def test_it_is_read_from_disk_and_written_as_the_new_version(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "what_if.json"
        path.write_text(json.dumps(self._version_one()), encoding="utf-8")
        history = WhatIfHistory(history_file=str(path))

        await history.load_history()
        assert len(history.days) == 2
        history.add(RecordedDay.from_slots("2026-06-10", _slots(), SITE))
        await history.save_history()

        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["schema"] == SCHEMA_VERSION == 2
        assert "min_soc_pct" not in saved["days"][0]
        assert saved["days"][2]["min_soc_pct"] == pytest.approx(SITE.min_soc_pct)


class TestPrices:
    """Issue #1455: the hourly prices a day rebuilt from statistics needs."""

    _HOUR = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)

    def _hours(self, count: int) -> dict[datetime, tuple[float, float]]:
        return {
            self._HOUR + timedelta(hours=index): (1.0 + index, 0.5 + index)
            for index in range(count)
        }

    def test_only_hours_before_the_given_instant_are_kept(self) -> None:
        history = WhatIfHistory()

        changed = history.add_prices(
            self._hours(5), self._HOUR + timedelta(hours=3), _SOURCE
        )

        assert changed is True
        assert sorted(history.prices) == sorted(self._hours(3))

    def test_a_held_hour_is_not_replaced_and_changes_nothing(self) -> None:
        history = WhatIfHistory()
        history.add_prices(self._hours(3), self._HOUR + timedelta(days=1), _SOURCE)

        changed = history.add_prices(
            {self._HOUR: (9.0, 9.0)}, self._HOUR + timedelta(days=1), _SOURCE
        )

        assert changed is False
        assert history.prices[self._HOUR] == pytest.approx((1.0, 0.5))

    def test_hours_older_than_the_longest_history_are_dropped(self) -> None:
        history = WhatIfHistory()
        history.add_prices(self._hours(3), self._HOUR + timedelta(days=1), _SOURCE)
        later = self._HOUR + timedelta(days=MAX_HISTORY_DAYS + 1, hours=2)

        changed = history.add_prices({}, later, _SOURCE)

        assert changed is True
        assert sorted(history.prices) == [self._HOUR + timedelta(hours=2)]

    @pytest.mark.asyncio
    async def test_they_are_saved_and_restored(self, tmp_path: Path) -> None:
        path = tmp_path / "what_if.json"
        history = WhatIfHistory(history_file=str(path))
        history.add_prices(
            {self._HOUR: (1.0 / 3.0, -0.25)}, self._HOUR + timedelta(days=1), _SOURCE
        )
        await history.save_history()

        restored = WhatIfHistory(history_file=str(path))
        restored.prices[self._HOUR + timedelta(hours=1)] = (2.0, 1.0)
        await restored.load_history()

        assert restored.prices[self._HOUR] == pytest.approx((0.33333, -0.25))
        # An hour added while the file was being read is kept.
        assert restored.prices[self._HOUR + timedelta(hours=1)] == pytest.approx(
            (2.0, 1.0)
        )

    def test_a_damaged_price_row_costs_only_itself(self) -> None:
        stamp = int(self._HOUR.timestamp())

        history = WhatIfHistory.from_dict(
            {
                "schema": SCHEMA_VERSION,
                "prices": [
                    [stamp, 1.5],
                    "broken",
                    [stamp, "a", 1.0],
                    [-5, 1.0, 1.0],
                    [1e300, 1.0, 1.0],
                    [stamp, 1.5, 0.5],
                ],
            }
        )

        assert history.prices == {self._HOUR: pytest.approx((1.5, 0.5))}


class TestPriceSource:
    """Issue #1455: kept prices belong to one price source, as the price history's."""

    _HOUR = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    _LATER = datetime(2026, 6, 3, 0, 0, tzinfo=UTC)

    def _history(self) -> WhatIfHistory:
        history = WhatIfHistory()
        history.add_prices({self._HOUR: (1.0, 0.5)}, self._LATER, _SOURCE)
        return history

    @pytest.mark.parametrize(
        "change",
        [{"import_entity": "sensor.other_price"}, {"export_unit": "øre/kWh"}],
    )
    def test_another_entity_or_unit_drops_the_kept_prices(
        self, change: dict[str, str]
    ) -> None:
        history = self._history()
        new_hour = self._HOUR + timedelta(hours=5)

        changed = history.add_prices(
            {new_hour: (120.0, 50.0)}, self._LATER, {**_SOURCE, **change}
        )

        assert changed is True
        assert history.prices == {new_hour: pytest.approx((120.0, 50.0))}
        assert history.price_source == {**_SOURCE, **change}

    def test_a_source_change_alone_is_a_change_to_save(self) -> None:
        history = self._history()

        changed = history.add_prices(
            {}, self._LATER, {**_SOURCE, "import_entity": "sensor.other_price"}
        )

        assert changed is True
        assert history.prices == {}

    def test_an_unknown_unit_keeps_the_prices_and_the_known_unit(self) -> None:
        """A price sensor that is down reports no unit: not another source."""
        history = self._history()

        changed = history.add_prices(
            {}, self._LATER, {**_SOURCE, "import_unit": "", "export_unit": ""}
        )

        assert changed is False
        assert list(history.prices) == [self._HOUR]
        assert history.price_source == _SOURCE

    def test_the_kept_prices_are_only_given_to_their_own_source(self) -> None:
        history = self._history()

        assert list(history.prices_for(_SOURCE)) == [self._HOUR]
        assert history.prices_for({**_SOURCE, "export_entity": "sensor.x"}) == {}

    @pytest.mark.asyncio
    async def test_the_source_is_saved_and_restored(self, tmp_path: Path) -> None:
        path = tmp_path / "what_if.json"
        history = self._history()
        history.history_file = str(path)
        await history.save_history()

        restored = WhatIfHistory(history_file=str(path))
        await restored.load_history()

        assert restored.price_source == _SOURCE
        assert list(restored.prices) == [self._HOUR]

    @pytest.mark.asyncio
    async def test_prices_on_disk_of_another_source_are_not_merged(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "what_if.json"
        on_disk = self._history()
        on_disk.history_file = str(path)
        await on_disk.save_history()
        history = WhatIfHistory(history_file=str(path))
        other = {**_SOURCE, "import_entity": "sensor.other_price"}
        new_hour = self._HOUR + timedelta(hours=1)
        history.add_prices({new_hour: (2.0, 1.0)}, self._LATER, other)

        await history.load_history()

        assert list(history.prices) == [new_hour]
        assert history.price_source == other
