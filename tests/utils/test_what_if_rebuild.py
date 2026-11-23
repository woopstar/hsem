"""Tests for days rebuilt from hourly statistics (issue #1455).

A what-if can reach past the 90 measured days: a day without a record is
rebuilt from the hourly statistics of the house load and the PV, and priced
with the real hourly prices HSEM kept.  These tests pin that such a day is
only built when every hour has a real price and a load, that it is marked,
and that the load the replay derives from it is the load of the statistics.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.models.energy_backfill import MAX_HOUR_KWH, HourlyEnergy
from custom_components.hsem.planner.milp_optimizer import is_scipy_available
from custom_components.hsem.utils.performance_scoring import realized_slots
from custom_components.hsem.utils.what_if import (
    WhatIfRequest,
    WhatIfScenario,
    run_what_if,
)
from custom_components.hsem.utils.what_if_rebuild import (
    REASON_STATISTICS_INCOMPLETE,
    local_day_hours,
    priced_days,
    rebuild_day,
    rebuild_days,
)
from tests.utils.what_if_days import SITE

needs_scipy = pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available in this environment"
)

_TZ = ZoneInfo("Europe/Copenhagen")
_DAY = "2026-01-15"
_HOURS = local_day_hours(_DAY, _TZ)


def _prices(
    hours: tuple[datetime, ...] = _HOURS,
) -> dict[datetime, tuple[float, float]]:
    """Return a cheap night and an expensive evening for *hours*."""
    return {
        hour: (3.0 if 17 <= index < 21 else 0.5, 0.2)
        for index, hour in enumerate(hours)
    }


def _hourly(hours: tuple[datetime, ...] = _HOURS) -> HourlyEnergy:
    """Return 1 kWh of load every hour and 3 kWh of PV in four midday hours."""
    return HourlyEnergy(
        house_kwh=dict.fromkeys(hours, 1.0),
        pv_kwh={hour: 3.0 for hour in hours[10:14]},
    )


class TestLocalDayHours:
    def test_a_day_has_24_hours_from_local_midnight(self) -> None:
        assert len(_HOURS) == 24
        assert _HOURS[0] == datetime(2026, 1, 14, 23, 0, tzinfo=UTC)
        assert _HOURS[-1] == datetime(2026, 1, 15, 22, 0, tzinfo=UTC)

    @pytest.mark.parametrize(("day", "count"), [("2026-03-29", 23), ("2026-10-25", 25)])
    def test_a_day_the_clocks_change_is_shorter_or_longer(
        self, day: str, count: int
    ) -> None:
        hours = local_day_hours(day, _TZ)

        assert len(hours) == count
        assert hours[-1] - hours[0] == timedelta(hours=count - 1)


class TestPricedDays:
    def test_a_day_with_a_price_for_every_hour_is_priced(self) -> None:
        assert priced_days([_DAY], _TZ, _prices()) == {_DAY: _HOURS}

    def test_a_day_missing_one_hour_is_not(self) -> None:
        prices = _prices()
        del prices[_HOURS[7]]

        assert priced_days([_DAY, "2026-01-16"], _TZ, prices) == {}


class TestRebuildDay:
    def test_the_day_is_the_load_and_pv_with_an_idle_battery(self) -> None:
        day = rebuild_day(_DAY, _HOURS, _hourly(), _prices(), SITE)

        assert day is not None
        assert day.from_statistics
        assert day.limits is None
        assert day.day == _DAY
        assert day.start == _HOURS[0]
        assert day.slot_minutes == 60
        assert day.rated_kwh == pytest.approx(SITE.rated_kwh)
        # The battery is empty and idle all day.
        assert day.soc_pct == pytest.approx([SITE.min_soc_pct] * 25)
        assert day.grid_import_kwh[0] == pytest.approx(1.0)
        assert day.grid_export_kwh[0] == pytest.approx(0.0)
        assert day.grid_import_kwh[10] == pytest.approx(0.0)
        assert day.grid_export_kwh[10] == pytest.approx(2.0)
        assert day.import_price[18] == pytest.approx(3.0)
        assert day.export_price[18] == pytest.approx(0.2)
        # No PV statistics is no production, and is kept as not observed.
        assert day.pv_kwh[0] is None
        assert day.pv_kwh[10] == pytest.approx(3.0)

    def test_the_replay_derives_the_load_of_the_statistics(self) -> None:
        day = rebuild_day(_DAY, _HOURS, _hourly(), _prices(), SITE)
        assert day is not None

        slots = realized_slots(day.performance_slots(), SITE)

        assert [slot.net_load_kwh for slot in slots] == pytest.approx(
            [-2.0 if 10 <= index < 14 else 1.0 for index in range(24)]
        )
        assert all(
            slot.charged_kwh == pytest.approx(0.0)
            and slot.discharged_kwh == pytest.approx(0.0)
            for slot in slots
        )

    def test_negative_statistics_count_as_nothing(self) -> None:
        hourly = HourlyEnergy(
            house_kwh={**dict.fromkeys(_HOURS, 1.0), _HOURS[0]: -0.2},
            pv_kwh={_HOURS[0]: -0.1},
        )

        day = rebuild_day(_DAY, _HOURS, hourly, _prices(), SITE)

        assert day is not None
        assert day.grid_import_kwh[0] == pytest.approx(0.0)
        assert day.grid_export_kwh[0] == pytest.approx(0.0)
        assert day.pv_kwh[0] == pytest.approx(0.0)

    def test_an_hour_without_a_load_leaves_no_day(self) -> None:
        hourly = _hourly()
        house = dict(hourly.house_kwh)
        del house[_HOURS[3]]

        assert (
            rebuild_day(_DAY, _HOURS, HourlyEnergy(house_kwh=house), _prices(), SITE)
            is None
        )


class TestRebuildDays:
    def test_days_are_rebuilt_or_counted_as_incomplete(self) -> None:
        other = "2026-01-16"
        other_hours = local_day_hours(other, _TZ)
        prices = {**_prices(), **_prices(other_hours)}

        rebuilt, reasons = rebuild_days(
            {_DAY: _HOURS, other: other_hours}, _hourly(), prices, SITE
        )

        assert list(rebuilt) == [_DAY]
        assert reasons == {other: REASON_STATISTICS_INCOMPLETE}

    def test_a_meter_glitch_is_an_unknown_hour(self) -> None:
        hourly = HourlyEnergy(
            house_kwh={**dict.fromkeys(_HOURS, 1.0), _HOURS[5]: MAX_HOUR_KWH + 1.0}
        )

        rebuilt, reasons = rebuild_days({_DAY: _HOURS}, hourly, _prices(), SITE)

        assert rebuilt == {}
        assert reasons == {_DAY: REASON_STATISTICS_INCOMPLETE}


@needs_scipy
class TestReplay:
    def test_a_rebuilt_day_is_replayed_by_the_same_code(self) -> None:
        """No battery to start with: a larger one shifts more into the evening."""
        day = rebuild_day(_DAY, _HOURS, _hourly(), _prices(), SITE)
        assert day is not None

        response = run_what_if(
            WhatIfRequest(
                days=(_DAY,),
                recorded={_DAY: day},
                reasons={},
                site=SITE,
                cycle_cost_per_kwh=0.0,
                scenarios=(WhatIfScenario(usable_kwh=1.0),),
            )
        )

        assert response is not None
        assert response["days_compared"] == 1
        assert response["days_from_statistics"] == 1
        assert response["days_measured"] == 0
        row = response["scenarios"][0]
        # 9.5 kWh covers the four expensive hours, 1 kWh covers a quarter.
        assert row["grid_savings"] < 0.0
        assert row["from_statistics"]["grid_savings"] == pytest.approx(
            row["grid_savings"]
        )
        assert row["measured"]["days"] == 0
        assert response["days"][0]["source"] == "statistics"
        assert response["days"][0]["limits"] == "today"
