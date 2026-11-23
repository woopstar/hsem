"""Tests for the daily energy records built from statistics (issue #1450)."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.models.energy_backfill import (
    MAX_HOUR_KWH,
    HourlyEnergy,
    build_backfill_days,
    drop_implausible_hours,
)
from custom_components.hsem.models.energy_tracker import (
    REASON_BATTERY_POWER_NOT_MEASURED,
    REASON_CYCLE_COST_UNKNOWN,
    BatterySite,
    EnergyDayEntry,
    EnergyTracker,
)

_CPH = ZoneInfo("Europe/Copenhagen")
_HOUR = timedelta(hours=1)
#: 12:00 local time on 1 May 2026.
_NOON = datetime(2026, 5, 1, 10, 0, tzinfo=UTC)
_SITE = BatterySite(
    rated_capacity_kwh=10.0, usable_capacity_kwh=9.0, cycle_cost_per_kwh=0.1
)


def _measured(day: str) -> EnergyDayEntry:
    return EnergyDayEntry(day, observed_hours=24.0, house_consumption_kwh=7.0)


class TestBuildDays:
    def test_an_hour_adds_every_quantity_it_has(self) -> None:
        hourly = HourlyEnergy(
            house_kwh={_NOON: 2.0, _NOON + _HOUR: 1.0},
            grid_import_kwh={_NOON: 0.5},
            grid_export_kwh={_NOON: 0.25},
            pv_kwh={_NOON: 3.0},
            battery_net_kwh={_NOON: 1.5, _NOON + _HOUR: -0.5},
        )

        (day,) = build_backfill_days(hourly, _CPH, _SITE)

        assert day.date == "2026-05-01"
        assert day.backfilled is True
        assert day.observed_hours == pytest.approx(2.0)
        assert day.house_consumption_kwh == pytest.approx(3.0)
        assert day.house_hours == pytest.approx(2.0)
        assert day.grid_import_kwh == pytest.approx(0.5)
        assert day.grid_import_hours == pytest.approx(1.0)
        assert day.grid_export_kwh == pytest.approx(0.25)
        assert day.grid_export_hours == pytest.approx(1.0)
        assert day.pv_kwh == pytest.approx(3.0)
        assert day.pv_hours == pytest.approx(1.0)
        # Only the hour with both readings pairs consumption with import.
        assert day.paired_consumption_kwh == pytest.approx(2.0)
        assert day.paired_import_kwh == pytest.approx(0.5)
        assert day.battery_charged_kwh == pytest.approx(1.5)
        assert day.battery_discharged_kwh == pytest.approx(0.5)
        assert day.battery_hours == pytest.approx(2.0)
        assert day.wear_cost == pytest.approx(0.2)
        assert day.priced_throughput_kwh == pytest.approx(2.0)
        assert day.usable_capacity_kwh == pytest.approx(9.0)
        assert day.rated_capacity_kwh == pytest.approx(10.0)

    def test_nothing_is_added_to_the_measured_round_trip(self) -> None:
        hourly = HourlyEnergy(battery_net_kwh={_NOON: 4.0})

        (day,) = build_backfill_days(hourly, _CPH, _SITE)

        assert day.measured_charged_kwh == pytest.approx(0.0)
        assert day.measured_discharged_kwh == pytest.approx(0.0)
        assert day.measured_stored_delta_kwh == pytest.approx(0.0)

    def test_a_quantity_without_statistics_has_no_hours(self) -> None:
        (day,) = build_backfill_days(HourlyEnergy(pv_kwh={_NOON: 1.0}), _CPH, _SITE)

        assert day.house_hours == pytest.approx(0.0)
        assert day.grid_import_hours == pytest.approx(0.0)
        assert day.grid_export_hours == pytest.approx(0.0)
        assert day.battery_hours == pytest.approx(0.0)

    def test_an_hour_is_booked_on_the_local_day_it_starts_in(self) -> None:
        late = datetime(2026, 5, 1, 22, 0, tzinfo=UTC)  # 00:00 on 2 May, local
        hourly = HourlyEnergy(house_kwh={late - _HOUR: 1.0, late: 2.0})

        first, second = build_backfill_days(hourly, _CPH, _SITE)

        assert (first.date, second.date) == ("2026-05-01", "2026-05-02")
        assert first.house_consumption_kwh == pytest.approx(1.0)
        assert second.house_consumption_kwh == pytest.approx(2.0)

    def test_negative_energy_counts_as_nothing(self) -> None:
        hourly = HourlyEnergy(
            house_kwh={_NOON: -1.0},
            grid_import_kwh={_NOON: -1.0},
            grid_export_kwh={_NOON: -1.0},
            pv_kwh={_NOON: -1.0},
        )

        (day,) = build_backfill_days(hourly, _CPH, _SITE)

        assert day.house_consumption_kwh == pytest.approx(0.0)
        assert day.grid_import_kwh == pytest.approx(0.0)
        assert day.paired_import_kwh == pytest.approx(0.0)
        assert day.grid_export_kwh == pytest.approx(0.0)
        assert day.pv_kwh == pytest.approx(0.0)

    def test_without_a_cycle_cost_the_throughput_is_not_priced(self) -> None:
        hourly = HourlyEnergy(battery_net_kwh={_NOON: 4.0})

        (day,) = build_backfill_days(hourly, _CPH, BatterySite())

        assert day.priced_throughput_kwh == pytest.approx(0.0)
        tracker = EnergyTracker()
        tracker.apply_backfill([day], date(2026, 5, 2))
        summary = tracker.month_summary("2026-05")
        assert summary["battery_wear_cost"] is None
        assert summary["battery_wear_cost_reason"] == REASON_CYCLE_COST_UNKNOWN

    def test_no_statistics_give_no_days(self) -> None:
        assert build_backfill_days(HourlyEnergy(), _CPH, _SITE) == []


class TestMeterGlitches:
    def test_a_meter_that_reset_and_came_back_is_not_booked(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The meter reported 0 and returned: the statistics sum jumps by the
        # whole meter reading in the hour it came back.
        hourly = HourlyEnergy(
            house_kwh={_NOON: 2.0, _NOON + _HOUR: 1.0},
            grid_import_kwh={_NOON: 0.5, _NOON + _HOUR: 48123.4},
        )

        with caplog.at_level(logging.INFO, logger="custom_components.hsem"):
            (day,) = build_backfill_days(hourly, _CPH, _SITE)

        assert day.grid_import_kwh == pytest.approx(0.5)
        assert day.grid_import_hours == pytest.approx(1.0)
        # The other quantity keeps the hour, without an import to pair with.
        assert day.house_hours == pytest.approx(2.0)
        assert day.paired_consumption_kwh == pytest.approx(2.0)
        assert day.paired_import_kwh == pytest.approx(0.5)
        assert "1 hourly values above 50 kWh dropped" in caplog.text

    def test_every_quantity_is_capped_in_both_directions(self) -> None:
        over = MAX_HOUR_KWH + 0.1
        hourly = HourlyEnergy(
            house_kwh={_NOON: over},
            grid_import_kwh={_NOON: over},
            grid_export_kwh={_NOON: over},
            pv_kwh={_NOON: over},
            battery_net_kwh={_NOON: over, _NOON + _HOUR: -over},
        )

        plausible, dropped = drop_implausible_hours(hourly)

        assert dropped == 6
        assert plausible.hours() == []
        assert build_backfill_days(hourly, _CPH, _SITE) == []

    def test_an_hour_at_the_limit_is_kept_without_a_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        hourly = HourlyEnergy(
            grid_import_kwh={_NOON: MAX_HOUR_KWH},
            battery_net_kwh={_NOON: -MAX_HOUR_KWH},
        )

        with caplog.at_level(logging.INFO, logger="custom_components.hsem"):
            (day,) = build_backfill_days(hourly, _CPH, _SITE)

        assert drop_implausible_hours(hourly) == (hourly, 0)
        assert day.grid_import_kwh == pytest.approx(MAX_HOUR_KWH)
        assert day.battery_discharged_kwh == pytest.approx(MAX_HOUR_KWH)
        assert "dropped" not in caplog.text


class TestApply:
    def test_days_before_the_first_measured_day_are_added(self) -> None:
        tracker = EnergyTracker(daily_log={"2026-05-03": _measured("2026-05-03")})
        days = [EnergyDayEntry(f"2026-05-0{day}") for day in (1, 2, 3, 4)]

        added = tracker.apply_backfill(days, date(2026, 5, 5))

        assert added == 2
        assert sorted(tracker.daily_log) == ["2026-05-01", "2026-05-02", "2026-05-03"]
        assert tracker.daily_log["2026-05-01"].backfilled is True
        assert tracker.backfill_complete is True

    def test_a_measured_day_is_never_changed(self) -> None:
        measured = _measured("2026-05-03")
        tracker = EnergyTracker(daily_log={"2026-05-03": measured})
        statistics = EnergyDayEntry("2026-05-03", house_consumption_kwh=99.0)

        assert tracker.apply_backfill([statistics], date(2026, 5, 5)) == 0

        assert tracker.daily_log["2026-05-03"] is measured
        assert measured.backfilled is False
        assert measured.house_consumption_kwh == pytest.approx(7.0)

    def test_today_is_not_backfilled_into_an_empty_history(self) -> None:
        tracker = EnergyTracker()
        days = [EnergyDayEntry("2026-05-04"), EnergyDayEntry("2026-05-05")]

        assert tracker.apply_backfill(days, date(2026, 5, 5)) == 1
        assert list(tracker.daily_log) == ["2026-05-04"]

    def test_a_backfilled_day_is_not_backfilled_again(self) -> None:
        tracker = EnergyTracker()
        tracker.apply_backfill(
            [EnergyDayEntry("2026-05-01", pv_kwh=1.0)], date(2026, 5, 5)
        )

        added = tracker.apply_backfill(
            [EnergyDayEntry("2026-05-01", pv_kwh=9.0), EnergyDayEntry("2026-05-02")],
            date(2026, 5, 5),
        )

        assert added == 1
        assert tracker.daily_log["2026-05-01"].pv_kwh == pytest.approx(1.0)

    def test_days_older_than_the_log_keeps_are_dropped(self) -> None:
        tracker = EnergyTracker()

        tracker.apply_backfill(
            [EnergyDayEntry("2025-04-29"), EnergyDayEntry("2025-05-10")],
            date(2026, 5, 5),
        )

        assert list(tracker.daily_log) == ["2025-05-10"]

    def test_nothing_to_add_still_closes_the_backfill(self) -> None:
        tracker = EnergyTracker()

        assert tracker.apply_backfill([], date(2026, 5, 5)) == 0
        assert tracker.backfill_complete is True


class TestPublished:
    def _tracker(self) -> EnergyTracker:
        tracker = EnergyTracker(daily_log={"2026-05-03": _measured("2026-05-03")})
        hourly = HourlyEnergy(
            house_kwh={_NOON - 24 * _HOUR: 4.0, _NOON: 5.0, _NOON + 24 * _HOUR: 6.0},
            grid_import_kwh={_NOON: 1.0},
            battery_net_kwh={_NOON: -3.0},
        )
        tracker.apply_backfill(
            build_backfill_days(hourly, _CPH, _SITE), date(2026, 5, 5)
        )
        return tracker

    def test_a_month_counts_its_backfilled_days(self) -> None:
        tracker = self._tracker()

        april = tracker.month_summary("2026-04")
        may = tracker.month_summary("2026-05")

        assert (april["days"], april["backfilled_days"]) == (1, 1)
        assert (may["days"], may["backfilled_days"]) == (3, 2)
        assert tracker.month_summary("2026-03")["backfilled_days"] == 0
        assert april["house_consumption_kwh"] == pytest.approx(4.0)
        assert april["grid_import_kwh"] is None
        assert may["house_consumption_kwh"] == pytest.approx(18.0)
        assert may["battery_cycles"] == pytest.approx(0.33)
        assert may["battery_wear_cost"] == pytest.approx(0.3)
        assert may["round_trip_loss"] is None
        assert may["round_trip_loss_reason"] == REASON_BATTERY_POWER_NOT_MEASURED

    def test_the_attributes_give_the_backfilled_range(self) -> None:
        attributes = self._tracker().as_sensor_attributes(date(2026, 5, 5))

        assert attributes["backfilled_days"] == 3
        assert attributes["backfilled_from"] == "2026-04-30"
        assert attributes["backfilled_until"] == "2026-05-02"
        assert [month["month"] for month in attributes["monthly"]] == [
            "2026-04",
            "2026-05",
        ]

    def test_a_history_without_backfilled_days_has_no_range(self) -> None:
        tracker = EnergyTracker(daily_log={"2026-05-03": _measured("2026-05-03")})

        attributes = tracker.as_sensor_attributes(date(2026, 5, 5))

        assert attributes["backfilled_days"] == 0
        assert attributes["backfilled_from"] is None
        assert attributes["backfilled_until"] is None

    def test_the_marks_survive_a_save_and_a_load(self) -> None:
        restored = EnergyTracker.from_dict(self._tracker().as_dict())

        assert restored.backfill_complete is True
        assert restored.daily_log["2026-05-01"].backfilled is True
        assert restored.daily_log["2026-05-03"].backfilled is False

    def test_a_file_from_before_the_backfill_is_not_backfilled_yet(self) -> None:
        data = EnergyTracker(
            daily_log={"2026-05-03": _measured("2026-05-03")}
        ).as_dict()
        del data["backfill_complete"]
        del data["daily_log"][0]["backfilled"]

        restored = EnergyTracker.from_dict(data)

        assert restored.backfill_complete is False
        assert restored.daily_log["2026-05-03"].backfilled is False
