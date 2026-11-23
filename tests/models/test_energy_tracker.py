"""Tests for the energy tracker behind the monthly energy sensors (issue #1435)."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.models import energy_tracker as module
from custom_components.hsem.models.energy_tracker import (
    MAX_DAILY_LOG_DAYS,
    REASON_AC_BATTERY_POWER_NO_READINGS,
    REASON_AC_BATTERY_POWER_NOT_CONFIGURED,
    REASON_AC_BATTERY_POWER_UNIT_NOT_POWER,
    REASON_AC_SOC_OR_CAPACITY_MISSING,
    REASON_BATTERY_POWER_NOT_MEASURED,
    REASON_CAPACITY_UNKNOWN,
    REASON_CONSUMPTION_BELOW_MINIMUM,
    REASON_CONSUMPTION_NOT_MEASURED,
    REASON_GRID_IMPORT_NOT_MEASURED,
    REASON_IMPLAUSIBLE_RATIO,
    REASON_IMPORT_EXCEEDS_CONSUMPTION,
    REASON_NO_DATA,
    REASON_SOC_CHANGE_TOO_LARGE,
    REASON_THROUGHPUT_BELOW_MINIMUM,
    SCHEMA_VERSION,
    BatterySite,
    EnergyDayEntry,
    EnergySample,
    EnergyTracker,
    previous_month,
)

_TZ = ZoneInfo("Europe/Copenhagen")
_T0 = datetime(2026, 6, 10, 12, 0, tzinfo=_TZ)
_DAY = "2026-06-10"
#: A 10 kWh battery of which 9 kWh is usable, with easy-to-read efficiencies.
_SITE = BatterySite(
    rated_capacity_kwh=10.0,
    usable_capacity_kwh=9.0,
    charge_efficiency=0.8,
    discharge_efficiency=0.5,
    cycle_cost_per_kwh=0.5,
)


def _sample(minutes: float, **readings: float | None) -> EnergySample:
    return EnergySample(at=_T0 + timedelta(minutes=minutes), **readings)


def _feed(
    tracker: EnergyTracker,
    *samples: EnergySample,
    site: BatterySite = _SITE,
    gap: float = 600.0,
) -> list[bool]:
    return [tracker.accumulate(sample, site, max_gap_seconds=gap) for sample in samples]


#: The hours field that says a quantity was measured.
_HOURS = {
    "house_consumption_kwh": "house_hours",
    "paired_consumption_kwh": "house_hours",
    "grid_import_kwh": "grid_import_hours",
    "grid_export_kwh": "grid_export_hours",
    "pv_kwh": "pv_hours",
    "battery_charged_kwh": "battery_hours",
    "battery_discharged_kwh": "battery_hours",
    "wear_cost": "battery_hours",
}


def _day(day: str, **fields: float) -> EnergyDayEntry:
    """Return a day in which every given quantity was measured and priced."""
    for name, hours_name in _HOURS.items():
        if name in fields:
            fields.setdefault(hours_name, 1.0)
    fields.setdefault(
        "priced_throughput_kwh",
        fields.get("battery_charged_kwh", 0.0)
        + fields.get("battery_discharged_kwh", 0.0),
    )
    if "ac_charged_kwh" in fields:
        fields.setdefault("ac_power_hours", 1.0)
        fields.setdefault("ac_hours", 1.0)
    entry = EnergyDayEntry(day)
    for name, value in fields.items():
        setattr(entry, name, value)
    return entry


def _tracker(*entries: EnergyDayEntry) -> EnergyTracker:
    return EnergyTracker(daily_log={entry.date: entry for entry in entries})


class TestAccumulate:
    def test_the_first_sample_only_sets_a_baseline(self) -> None:
        tracker = EnergyTracker()

        assert _feed(tracker, _sample(0, grid_import_energy_kwh=100.0)) == [False]
        assert tracker.daily_log == {}

    def test_meter_deltas_and_house_power_become_the_days_energy(self) -> None:
        tracker = EnergyTracker()

        booked = _feed(
            tracker,
            _sample(
                0,
                grid_import_energy_kwh=100.0,
                grid_export_energy_kwh=20.0,
                pv_energy_kwh=50.0,
                house_power_w=1200.0,
            ),
            _sample(
                6,
                grid_import_energy_kwh=100.5,
                grid_export_energy_kwh=20.2,
                pv_energy_kwh=50.3,
                house_power_w=1800.0,
            ),
        )

        assert booked == [False, True]
        entry = tracker.daily_log[_DAY]
        assert entry.observed_hours == pytest.approx(0.1)
        assert entry.grid_import_kwh == pytest.approx(0.5)
        assert entry.grid_export_kwh == pytest.approx(0.2)
        assert entry.pv_kwh == pytest.approx(0.3)
        # 1.5 kW on average for a tenth of an hour.
        assert entry.house_consumption_kwh == pytest.approx(0.15)
        assert entry.paired_consumption_kwh == pytest.approx(0.15)
        assert entry.paired_import_kwh == pytest.approx(0.5)

    def test_pv_power_stands_in_for_a_missing_pv_meter(self) -> None:
        tracker = EnergyTracker()

        _feed(tracker, _sample(0, pv_power_w=600.0), _sample(6, pv_power_w=1200.0))

        assert tracker.daily_log[_DAY].pv_kwh == pytest.approx(0.09)

    def test_negative_pv_and_house_power_count_as_nothing(self) -> None:
        tracker = EnergyTracker()

        _feed(
            tracker,
            _sample(0, pv_power_w=-50.0, house_power_w=-100.0),
            _sample(6, pv_power_w=-50.0, house_power_w=-100.0),
        )

        entry = tracker.daily_log[_DAY]
        assert entry.pv_kwh == pytest.approx(0.0)
        assert entry.house_consumption_kwh == pytest.approx(0.0)

    def test_a_long_gap_is_not_booked_but_the_next_interval_is(self) -> None:
        tracker = EnergyTracker()

        booked = _feed(
            tracker,
            _sample(0, grid_import_energy_kwh=100.0),
            _sample(30, grid_import_energy_kwh=103.0),
            _sample(35, grid_import_energy_kwh=103.4),
        )

        assert booked == [False, False, True]
        assert tracker.daily_log[_DAY].grid_import_kwh == pytest.approx(0.4)

    def test_a_sample_that_is_not_later_is_not_booked(self) -> None:
        tracker = EnergyTracker()

        booked = _feed(
            tracker,
            _sample(0, grid_import_energy_kwh=100.0),
            _sample(0, grid_import_energy_kwh=100.5),
        )

        assert booked == [False, False]
        assert tracker.daily_log == {}

    def test_a_missing_import_reading_is_not_a_zero_import(self) -> None:
        """Consumption without a measured import must not look self-sufficient."""
        tracker = EnergyTracker()

        _feed(
            tracker,
            _sample(0, grid_import_energy_kwh=100.0, house_power_w=1000.0),
            _sample(6, house_power_w=1000.0),
        )

        entry = tracker.daily_log[_DAY]
        assert entry.house_consumption_kwh == pytest.approx(0.1)
        assert entry.grid_import_kwh == pytest.approx(0.0)
        assert entry.paired_consumption_kwh == pytest.approx(0.0)
        assert entry.paired_import_kwh == pytest.approx(0.0)

    def test_a_meter_running_backwards_is_left_out(self) -> None:
        tracker = EnergyTracker()

        _feed(
            tracker,
            _sample(0, grid_import_energy_kwh=100.0, grid_export_energy_kwh=5.0),
            _sample(6, grid_import_energy_kwh=90.0, grid_export_energy_kwh=5.5),
        )

        entry = tracker.daily_log[_DAY]
        assert entry.grid_import_kwh == pytest.approx(0.0)
        assert entry.grid_export_kwh == pytest.approx(0.5)

    def test_an_interval_is_booked_on_the_local_day_it_ends_in(self) -> None:
        tracker = EnergyTracker()
        before_midnight = datetime(2026, 6, 10, 23, 58, tzinfo=_TZ)

        _feed(
            tracker,
            EnergySample(at=before_midnight, grid_import_energy_kwh=10.0),
            EnergySample(
                at=before_midnight + timedelta(minutes=5), grid_import_energy_kwh=10.5
            ),
        )

        assert list(tracker.daily_log) == ["2026-06-11"]

    def test_a_new_day_drops_records_older_than_the_retention(self) -> None:
        kept = (_T0.date() - timedelta(days=MAX_DAILY_LOG_DAYS)).isoformat()
        dropped = (_T0.date() - timedelta(days=MAX_DAILY_LOG_DAYS + 1)).isoformat()
        tracker = _tracker(EnergyDayEntry(kept), EnergyDayEntry(dropped))

        _feed(tracker, _sample(0), _sample(5))

        assert sorted(tracker.daily_log) == [kept, _DAY]


class TestBattery:
    def test_battery_power_is_split_into_charge_and_discharge(self) -> None:
        tracker = EnergyTracker()

        _feed(
            tracker,
            _sample(0, battery_power_w=2000.0, soc_pct=50.0),
            _sample(6, battery_power_w=2000.0, soc_pct=52.0),
            _sample(12, battery_power_w=-3000.0, soc_pct=51.0),
        )

        entry = tracker.daily_log[_DAY]
        # 0.2 kWh at 2 kW, then 1 kW of charge and 1.5 kW of discharge on
        # average over the interval in which the sign changed.
        assert entry.battery_charged_kwh == pytest.approx(0.3)
        assert entry.battery_discharged_kwh == pytest.approx(0.15)
        assert entry.measured_charged_kwh == pytest.approx(0.3)
        assert entry.measured_discharged_kwh == pytest.approx(0.15)
        # 10 kWh rated, 50 % -> 51 %.
        assert entry.measured_stored_delta_kwh == pytest.approx(0.1)
        assert entry.wear_cost == pytest.approx(0.45 * 0.5)
        assert entry.usable_capacity_kwh == pytest.approx(9.0)
        assert entry.rated_capacity_kwh == pytest.approx(10.0)

    def test_power_without_soc_counts_for_cycles_but_not_for_the_loss(self) -> None:
        tracker = EnergyTracker()

        _feed(
            tracker,
            _sample(0, battery_power_w=-1000.0),
            _sample(6, battery_power_w=-1000.0),
        )

        entry = tracker.daily_log[_DAY]
        assert entry.battery_discharged_kwh == pytest.approx(0.1)
        assert entry.measured_discharged_kwh == pytest.approx(0.0)

    def test_without_power_the_flows_come_from_the_soc_change(self) -> None:
        tracker = EnergyTracker()

        _feed(
            tracker,
            _sample(0, soc_pct=50.0),
            _sample(6, soc_pct=60.0),
            _sample(12, soc_pct=40.0),
        )

        entry = tracker.daily_log[_DAY]
        # 1 kWh stored takes 1 / 0.8 on the way in; 2 kWh removed deliver 2 * 0.5.
        assert entry.battery_charged_kwh == pytest.approx(1.25)
        assert entry.battery_discharged_kwh == pytest.approx(1.0)
        assert entry.wear_cost == pytest.approx(2.25 * 0.5)
        assert entry.measured_charged_kwh == pytest.approx(0.0)
        assert entry.measured_stored_delta_kwh == pytest.approx(0.0)

    def test_without_power_and_soc_nothing_is_booked(self) -> None:
        tracker = EnergyTracker()

        _feed(tracker, _sample(0), _sample(6))

        entry = tracker.daily_log[_DAY]
        assert entry.battery_charged_kwh == pytest.approx(0.0)
        assert entry.battery_discharged_kwh == pytest.approx(0.0)

    def test_an_unknown_capacity_keeps_the_last_known_one(self) -> None:
        tracker = EnergyTracker()

        _feed(tracker, _sample(0, soc_pct=50.0), _sample(5, soc_pct=50.0))
        _feed(tracker, _sample(10, soc_pct=60.0), site=BatterySite())

        entry = tracker.daily_log[_DAY]
        assert entry.usable_capacity_kwh == pytest.approx(9.0)
        assert entry.rated_capacity_kwh == pytest.approx(10.0)
        # Without a rated capacity the SoC change is not an amount of energy.
        assert entry.battery_charged_kwh == pytest.approx(0.0)


class TestAcSideBattery:
    """The battery's flows on the AC side of the inverter (issue #1450)."""

    def test_ac_power_is_split_into_charge_and_discharge(self) -> None:
        tracker = EnergyTracker()

        _feed(
            tracker,
            _sample(0, battery_power_w=1900.0, battery_ac_power_w=2000.0, soc_pct=50.0),
            _sample(6, battery_power_w=1900.0, battery_ac_power_w=2000.0, soc_pct=52.0),
            _sample(
                12, battery_power_w=-3000.0, battery_ac_power_w=-2800.0, soc_pct=51.0
            ),
        )

        entry = tracker.daily_log[_DAY]
        assert entry.ac_charged_kwh == pytest.approx(0.3)
        assert entry.ac_discharged_kwh == pytest.approx(0.14)
        assert entry.ac_stored_delta_kwh == pytest.approx(0.1)
        assert entry.ac_power_hours == pytest.approx(0.2)
        assert entry.ac_hours == pytest.approx(0.2)
        # The battery-side figures are those of the battery power sensor.
        assert entry.measured_charged_kwh == pytest.approx(0.285)
        assert entry.measured_discharged_kwh == pytest.approx(0.15)
        assert entry.battery_charged_kwh == pytest.approx(0.285)

    def test_ac_power_alone_feeds_the_ac_side_only(self) -> None:
        """Cycles and wear still come from the SoC change."""
        tracker = EnergyTracker()

        _feed(
            tracker,
            _sample(0, battery_ac_power_w=2000.0, soc_pct=50.0),
            _sample(6, battery_ac_power_w=2000.0, soc_pct=51.0),
        )

        entry = tracker.daily_log[_DAY]
        assert entry.ac_charged_kwh == pytest.approx(0.2)
        assert entry.ac_stored_delta_kwh == pytest.approx(0.1)
        assert entry.measured_charged_kwh == pytest.approx(0.0)
        # 0.1 kWh stored takes 0.1 / 0.8 on the way in.
        assert entry.battery_charged_kwh == pytest.approx(0.125)

    @pytest.mark.parametrize(
        ("readings", "site"),
        [
            # No SoC.
            ({}, _SITE),
            # A SoC, but no rated capacity to turn it into energy.
            ({"soc_pct": 50.0}, BatterySite()),
        ],
    )
    def test_ac_power_without_a_stored_energy_change_is_not_booked(
        self, readings: dict[str, float], site: BatterySite
    ) -> None:
        tracker = EnergyTracker()
        tracker.set_ac_sensor_reason(REASON_AC_BATTERY_POWER_NO_READINGS)

        _feed(
            tracker,
            _sample(0, battery_ac_power_w=2000.0, **readings),
            _sample(6, battery_ac_power_w=2000.0, **readings),
            site=site,
        )

        entry = tracker.daily_log[_DAY]
        assert entry.ac_charged_kwh == pytest.approx(0.0)
        assert entry.ac_stored_delta_kwh == pytest.approx(0.0)
        assert entry.ac_power_hours == pytest.approx(0.1)
        assert entry.ac_hours == pytest.approx(0.0)
        summary = tracker.month_summary("2026-06")
        assert summary["ac_round_trip_loss"] is None
        assert summary["ac_round_trip_loss_reason"] == REASON_AC_SOC_OR_CAPACITY_MISSING

    def test_a_missing_ac_reading_is_not_a_zero_flow(self) -> None:
        tracker = EnergyTracker()

        _feed(
            tracker,
            _sample(0, battery_power_w=2000.0, battery_ac_power_w=2100.0, soc_pct=50.0),
            _sample(6, battery_power_w=2000.0, soc_pct=52.0),
            _sample(
                12, battery_power_w=2000.0, battery_ac_power_w=2100.0, soc_pct=54.0
            ),
        )

        entry = tracker.daily_log[_DAY]
        assert entry.ac_charged_kwh == pytest.approx(0.0)
        assert entry.ac_stored_delta_kwh == pytest.approx(0.0)
        assert entry.ac_power_hours == pytest.approx(0.0)
        assert entry.measured_charged_kwh == pytest.approx(0.4)

    def test_both_sides_are_published_side_by_side(self) -> None:
        """100 kWh in and 80 out on the AC side, 96 in and 86.4 out at the battery."""
        tracker = _tracker(
            _day(
                "2026-06-01",
                measured_charged_kwh=96.0,
                measured_discharged_kwh=86.4,
                ac_charged_kwh=100.0,
                ac_discharged_kwh=78.0,
                ac_stored_delta_kwh=2.0,
                rated_capacity_kwh=10.0,
            )
        )

        summary = tracker.month_summary("2026-06")

        assert summary["round_trip_efficiency"] == pytest.approx(90.0)
        assert summary["round_trip_loss"] == pytest.approx(10.0)
        assert summary["ac_round_trip_efficiency"] == pytest.approx(80.0)
        assert summary["ac_round_trip_loss"] == pytest.approx(20.0)
        # 2 kWh of stored-energy change, 15 % off at most, over 100 kWh.
        assert summary["ac_round_trip_loss_uncertainty"] == pytest.approx(0.3)
        assert summary["ac_round_trip_loss_reason"] is None
        assert summary["ac_round_trip_charged_kwh"] == pytest.approx(100.0)
        assert summary["ac_round_trip_discharged_kwh"] == pytest.approx(78.0)
        assert summary["ac_round_trip_stored_delta_kwh"] == pytest.approx(2.0)


class TestAcSideReasons:
    """Why the AC-side figure is unknown (issue #1450)."""

    _BATTERY_ONLY = {
        "measured_charged_kwh": 100.0,
        "measured_discharged_kwh": 90.0,
        "rated_capacity_kwh": 10.0,
    }

    def test_without_a_sensor_the_reason_is_not_configured(self) -> None:
        tracker = _tracker(_day("2026-06-01", **self._BATTERY_ONLY))

        summary = tracker.month_summary("2026-06")

        assert summary["round_trip_loss"] == pytest.approx(10.0)
        assert summary["ac_round_trip_loss"] is None
        assert summary["ac_round_trip_efficiency"] is None
        assert summary["ac_round_trip_loss_uncertainty"] is None
        assert (
            summary["ac_round_trip_loss_reason"]
            == REASON_AC_BATTERY_POWER_NOT_CONFIGURED
        )
        # A month without a record says the same: there is nothing to wait for.
        assert (
            tracker.month_summary("2026-05")["ac_round_trip_loss_reason"]
            == REASON_AC_BATTERY_POWER_NOT_CONFIGURED
        )

    def test_a_sensor_without_readings_is_told_apart_from_no_sensor(self) -> None:
        tracker = _tracker(_day("2026-06-01", **self._BATTERY_ONLY))

        assert tracker.set_ac_sensor_reason(REASON_AC_BATTERY_POWER_NO_READINGS)

        assert (
            tracker.month_summary("2026-06")["ac_round_trip_loss_reason"]
            == REASON_AC_BATTERY_POWER_NO_READINGS
        )
        # A month without any record has nothing recorded at all.
        assert (
            tracker.month_summary("2026-05")["ac_round_trip_loss_reason"]
            == REASON_NO_DATA
        )

    def test_a_unit_that_is_not_power_is_named(self) -> None:
        tracker = _tracker(_day("2026-06-01", **self._BATTERY_ONLY))

        tracker.set_ac_sensor_reason(REASON_AC_BATTERY_POWER_UNIT_NOT_POWER)

        for month in ("2026-05", "2026-06"):
            assert (
                tracker.month_summary(month)["ac_round_trip_loss_reason"]
                == REASON_AC_BATTERY_POWER_UNIT_NOT_POWER
            )

    def test_a_month_that_was_measured_does_not_carry_the_sensors_reason(self) -> None:
        """The sensor was removed after a month it measured."""
        tracker = _tracker(
            _day(
                "2026-06-01",
                ac_charged_kwh=100.0,
                ac_discharged_kwh=80.0,
                rated_capacity_kwh=10.0,
            )
        )

        summary = tracker.month_summary("2026-06")

        assert tracker.ac_sensor_reason == REASON_AC_BATTERY_POWER_NOT_CONFIGURED
        assert summary["ac_round_trip_loss"] == pytest.approx(20.0)
        assert summary["ac_round_trip_loss_reason"] is None

    def test_setting_the_reason_refreshes_what_the_sensors_publish(self) -> None:
        tracker = _tracker(_day("2026-06-01", **self._BATTERY_ONLY))
        today = date(2026, 6, 10)
        before = tracker.as_sensor_attributes(today)

        assert tracker.set_ac_sensor_reason(REASON_AC_BATTERY_POWER_NO_READINGS)
        assert not tracker.set_ac_sensor_reason(REASON_AC_BATTERY_POWER_NO_READINGS)

        after = tracker.as_sensor_attributes(today)
        assert (
            before["this_month"]["ac_round_trip_loss_reason"]
            == REASON_AC_BATTERY_POWER_NOT_CONFIGURED
        )
        assert (
            after["this_month"]["ac_round_trip_loss_reason"]
            == REASON_AC_BATTERY_POWER_NO_READINGS
        )


class TestAcSideGates:
    """The AC-side sensor is the user's pick, so its figure is checked more."""

    @pytest.mark.parametrize(
        ("fields", "reason"),
        [
            ({"ac_charged_kwh": 19.0, "ac_discharged_kwh": 15.0}, "below_minimum"),
            # Enough charged, too little discharged.
            ({"ac_charged_kwh": 100.0, "ac_discharged_kwh": 19.0}, "below_minimum"),
            (
                {
                    "ac_charged_kwh": 100.0,
                    "ac_discharged_kwh": 60.0,
                    "ac_stored_delta_kwh": 20.0,
                },
                "soc",
            ),
            ({"ac_charged_kwh": 100.0, "ac_discharged_kwh": 120.0}, "implausible"),
            # Half or less came out again.
            ({"ac_charged_kwh": 100.0, "ac_discharged_kwh": 50.0}, "implausible"),
            ({"ac_charged_kwh": 100.0, "ac_discharged_kwh": 30.0}, "implausible"),
        ],
    )
    def test_a_figure_that_fails_a_gate_is_unknown(
        self, fields: dict[str, float], reason: str
    ) -> None:
        expected = {
            "below_minimum": REASON_THROUGHPUT_BELOW_MINIMUM,
            "soc": REASON_SOC_CHANGE_TOO_LARGE,
            "implausible": REASON_IMPLAUSIBLE_RATIO,
        }[reason]
        tracker = _tracker(_day("2026-06-01", **fields))

        summary = tracker.month_summary("2026-06")

        assert summary["ac_round_trip_loss"] is None
        assert summary["ac_round_trip_efficiency"] is None
        assert summary["ac_round_trip_loss_reason"] == expected

    def test_just_above_half_is_a_measurement(self) -> None:
        tracker = _tracker(
            _day("2026-06-01", ac_charged_kwh=100.0, ac_discharged_kwh=51.0)
        )

        summary = tracker.month_summary("2026-06")

        assert summary["ac_round_trip_efficiency"] == pytest.approx(51.0)
        assert summary["ac_round_trip_loss_reason"] is None

    def test_a_sensor_without_a_sign_gives_no_loss_near_one_hundred(self) -> None:
        """The absolute power books every interval as charge."""
        tracker = EnergyTracker()
        samples = [
            _sample(6 * step, battery_ac_power_w=abs(power), soc_pct=soc)
            for step, (power, soc) in enumerate(
                [(5000.0, 20.0), (5000.0, 25.0), (-5000.0, 22.0), (-5000.0, 17.0)] * 150
            )
        ]

        _feed(tracker, *samples)

        days = list(tracker.daily_log.values())
        assert sum(day.ac_charged_kwh for day in days) > 200.0
        assert sum(day.ac_discharged_kwh for day in days) == pytest.approx(0.0)
        for month in {day.date[:7] for day in days}:
            summary = tracker.month_summary(month)
            assert summary["ac_round_trip_loss"] is None
            assert summary["ac_round_trip_loss_reason"] in (
                REASON_THROUGHPUT_BELOW_MINIMUM,
                REASON_SOC_CHANGE_TOO_LARGE,
            )

    @pytest.mark.parametrize("discharged", [19.0, 30.0, 50.0])
    def test_the_battery_side_keeps_the_gates_it_had(self, discharged: float) -> None:
        """What the AC side rejects is still published at the battery."""
        tracker = _tracker(
            _day(
                "2026-06-01",
                measured_charged_kwh=100.0,
                measured_discharged_kwh=discharged,
            )
        )

        summary = tracker.month_summary("2026-06")

        assert summary["round_trip_efficiency"] == pytest.approx(discharged)
        assert summary["round_trip_loss_reason"] is None


class TestAcSidePersistence:
    def test_the_ac_record_survives_a_round_trip_through_json(self) -> None:
        tracker = EnergyTracker()
        _feed(
            tracker,
            _sample(0, battery_ac_power_w=2000.0, soc_pct=50.0),
            _sample(6, battery_ac_power_w=2000.0, soc_pct=51.0),
        )

        restored = EnergyTracker.from_dict(json.loads(json.dumps(tracker.as_dict())))

        entry = restored.daily_log[_DAY]
        assert entry.ac_charged_kwh == pytest.approx(0.2)
        assert entry.ac_stored_delta_kwh == pytest.approx(0.1)
        assert entry.ac_hours == pytest.approx(0.1)
        assert entry.ac_power_hours == pytest.approx(0.1)
        assert restored._last_sample is not None
        assert restored._last_sample.battery_ac_power_w == pytest.approx(2000.0)

    def test_a_history_file_from_before_the_ac_figure_still_loads(self) -> None:
        tracker = EnergyTracker()
        _feed(
            tracker,
            _sample(0, battery_power_w=2000.0, soc_pct=50.0),
            _sample(6, battery_power_w=2000.0, soc_pct=51.0),
        )
        data = json.loads(json.dumps(tracker.as_dict()))
        del data["last_sample"]["battery_ac_power_w"]
        for day in data["daily_log"]:
            for name in [name for name in day if name.startswith("ac_")]:
                del day[name]

        restored = EnergyTracker.from_dict(data)

        entry = restored.daily_log[_DAY]
        assert entry.measured_charged_kwh == pytest.approx(0.2)
        assert entry.ac_charged_kwh == pytest.approx(0.0)
        assert entry.ac_hours == pytest.approx(0.0)
        assert restored._last_sample is not None
        assert restored._last_sample.battery_ac_power_w is None
        assert (
            restored.month_summary("2026-06")["ac_round_trip_loss_reason"]
            == REASON_AC_BATTERY_POWER_NOT_CONFIGURED
        )


class TestMonthSummary:
    def test_self_sufficiency_is_a_ratio_of_sums(self) -> None:
        """A 10 % day and a 90 % day are not 50 % when one is nine times larger."""
        tracker = _tracker(
            _day(
                "2026-06-01",
                house_consumption_kwh=10.0,
                paired_consumption_kwh=10.0,
                paired_import_kwh=9.0,
                grid_import_kwh=9.0,
            ),
            _day(
                "2026-06-02",
                house_consumption_kwh=90.0,
                paired_consumption_kwh=90.0,
                paired_import_kwh=9.0,
                grid_import_kwh=9.0,
            ),
        )

        summary = tracker.month_summary("2026-06")

        assert summary["self_sufficiency"] == pytest.approx(82.0)
        assert summary["self_sufficiency_reason"] is None
        assert summary["days"] == 2
        assert summary["house_consumption_kwh"] == pytest.approx(100.0)
        assert summary["grid_import_kwh"] == pytest.approx(18.0)

    def test_an_import_the_battery_charge_explains_is_zero_not_negative(self) -> None:
        """Grid energy charged into the battery is not house consumption."""
        tracker = _tracker(
            _day(
                "2026-06-01",
                house_consumption_kwh=10.0,
                paired_consumption_kwh=10.0,
                paired_import_kwh=25.0,
                battery_charged_kwh=16.0,
            )
        )

        summary = tracker.month_summary("2026-06")

        assert summary["self_sufficiency"] == pytest.approx(0.0)
        assert summary["self_sufficiency_reason"] is None

    def test_an_import_nothing_explains_is_unknown_not_zero(self) -> None:
        """House 300 kWh, an EV the house sensor leaves out, 550 kWh imported."""
        tracker = _tracker(
            _day(
                "2026-06-01",
                house_consumption_kwh=300.0,
                paired_consumption_kwh=300.0,
                paired_import_kwh=550.0,
            )
        )

        summary = tracker.month_summary("2026-06")

        assert summary["self_sufficiency"] is None
        assert summary["self_sufficiency_reason"] == REASON_IMPORT_EXCEEDS_CONSUMPTION

    def test_a_month_without_house_readings_has_no_share(self) -> None:
        tracker = _tracker(_day("2026-06-01", grid_import_kwh=12.0))

        summary = tracker.month_summary("2026-06")

        assert summary["self_sufficiency"] is None
        assert summary["self_sufficiency_reason"] == REASON_CONSUMPTION_NOT_MEASURED
        assert summary["house_consumption_kwh"] is None
        assert summary["grid_import_kwh"] == pytest.approx(12.0)

    def test_a_month_without_records_has_no_figures(self) -> None:
        summary = EnergyTracker().month_summary("2026-06")

        assert summary["days"] == 0
        for key in (
            "self_sufficiency",
            "battery_cycles",
            "battery_wear_cost",
            "round_trip_loss",
        ):
            assert summary[key] is None
            assert summary[f"{key}_reason"] == REASON_NO_DATA
        assert summary["round_trip_efficiency"] is None
        assert summary["usable_capacity_kwh"] is None
        assert summary["grid_import_kwh"] is None

    def test_too_little_consumption_gives_no_share(self) -> None:
        tracker = _tracker(_day("2026-06-01", house_consumption_kwh=0.5))

        summary = tracker.month_summary("2026-06")

        assert summary["self_sufficiency"] is None
        assert summary["self_sufficiency_reason"] == REASON_CONSUMPTION_BELOW_MINIMUM

    def test_consumption_without_a_measured_import_gives_no_share(self) -> None:
        tracker = _tracker(_day("2026-06-01", house_consumption_kwh=12.0))

        summary = tracker.month_summary("2026-06")

        assert summary["self_sufficiency"] is None
        assert summary["self_sufficiency_reason"] == REASON_GRID_IMPORT_NOT_MEASURED

    def test_cycles_are_the_discharge_over_the_latest_known_capacity(self) -> None:
        tracker = _tracker(
            _day("2026-06-01", battery_discharged_kwh=20.0, usable_capacity_kwh=10.0),
            _day("2026-06-02", battery_discharged_kwh=25.0, usable_capacity_kwh=9.0),
            _day("2026-06-03"),
        )

        summary = tracker.month_summary("2026-06")

        assert summary["battery_cycles"] == pytest.approx(5.0)
        assert summary["battery_cycles_reason"] is None
        assert summary["usable_capacity_kwh"] == pytest.approx(9.0)
        assert summary["battery_discharged_kwh"] == pytest.approx(45.0)

    def test_cycles_need_a_capacity(self) -> None:
        tracker = _tracker(_day("2026-06-01", battery_discharged_kwh=20.0))

        summary = tracker.month_summary("2026-06")

        assert summary["battery_cycles"] is None
        assert summary["battery_cycles_reason"] == REASON_CAPACITY_UNKNOWN

    def test_wear_cost_is_the_sum_of_the_days(self) -> None:
        tracker = _tracker(
            _day("2026-06-01", wear_cost=1.25),
            _day("2026-06-02", wear_cost=0.5),
            _day("2026-07-01", wear_cost=9.0),
        )

        summary = tracker.month_summary("2026-06")

        assert summary["battery_wear_cost"] == pytest.approx(1.75)
        assert summary["battery_wear_cost_reason"] is None

    @pytest.mark.parametrize(
        ("stored_delta", "efficiency"),
        [
            # 5 kWh more in the battery at the end: it counts as delivered.
            (5.0, 95.0),
            # 5 kWh less: that much of the discharge was charged earlier.
            (-5.0, 85.0),
            (0.0, 90.0),
        ],
    )
    def test_round_trip_loss_corrects_for_the_soc_difference(
        self, stored_delta: float, efficiency: float
    ) -> None:
        tracker = _tracker(
            _day(
                "2026-06-01",
                measured_charged_kwh=80.0,
                measured_discharged_kwh=10.0,
                measured_stored_delta_kwh=stored_delta,
                rated_capacity_kwh=10.0,
            ),
            _day("2026-06-02", measured_charged_kwh=20.0, measured_discharged_kwh=80.0),
        )

        summary = tracker.month_summary("2026-06")

        assert summary["round_trip_efficiency"] == pytest.approx(efficiency)
        assert summary["round_trip_loss"] == pytest.approx(100.0 - efficiency)
        assert summary["round_trip_loss_reason"] is None
        assert summary["round_trip_charged_kwh"] == pytest.approx(100.0)
        assert summary["round_trip_discharged_kwh"] == pytest.approx(90.0)
        assert summary["round_trip_stored_delta_kwh"] == pytest.approx(stored_delta)

    def test_round_trip_loss_needs_a_battery_power_reading(self) -> None:
        tracker = _tracker(
            _day("2026-06-01", battery_charged_kwh=50.0, battery_discharged_kwh=45.0)
        )

        summary = tracker.month_summary("2026-06")

        assert summary["round_trip_loss"] is None
        assert summary["round_trip_loss_reason"] == REASON_BATTERY_POWER_NOT_MEASURED

    @pytest.mark.parametrize(
        ("charged", "rated"),
        [
            # Below the absolute minimum of 20 kWh.
            (19.0, 5.0),
            # Above it, but less than five times a 15 kWh battery.
            (60.0, 15.0),
        ],
    )
    def test_round_trip_loss_needs_a_minimum_throughput(
        self, charged: float, rated: float
    ) -> None:
        tracker = _tracker(
            _day(
                "2026-06-01",
                measured_charged_kwh=charged,
                measured_discharged_kwh=charged * 0.9,
                rated_capacity_kwh=rated,
            )
        )

        summary = tracker.month_summary("2026-06")

        assert summary["round_trip_loss"] is None
        assert summary["round_trip_efficiency"] is None
        assert summary["round_trip_loss_reason"] == REASON_THROUGHPUT_BELOW_MINIMUM

    @pytest.mark.parametrize(
        ("discharged", "stored_delta"),
        [
            # More out than in.
            (120.0, 0.0),
            # Nothing delivered at all once the SoC difference is taken off.
            (1.0, -5.0),
        ],
    )
    def test_a_ratio_that_cannot_be_a_measurement_is_not_published(
        self, discharged: float, stored_delta: float
    ) -> None:
        tracker = _tracker(
            _day(
                "2026-06-01",
                measured_charged_kwh=100.0,
                measured_discharged_kwh=discharged,
                measured_stored_delta_kwh=stored_delta,
            )
        )

        summary = tracker.month_summary("2026-06")

        assert summary["round_trip_loss"] is None
        assert summary["round_trip_loss_reason"] == REASON_IMPLAUSIBLE_RATIO

    def test_an_efficiency_a_hair_above_one_is_no_loss(self) -> None:
        tracker = _tracker(
            _day(
                "2026-06-01",
                measured_charged_kwh=100.0,
                measured_discharged_kwh=101.0,
            )
        )

        summary = tracker.month_summary("2026-06")

        assert summary["round_trip_efficiency"] == pytest.approx(100.0)
        assert summary["round_trip_loss"] == pytest.approx(0.0)


class TestSensorAttributes:
    def test_previous_month_crosses_the_year(self) -> None:
        assert previous_month("2026-01") == "2025-12"
        assert previous_month("2026-03") == "2026-02"

    def test_current_and_previous_month_and_the_last_twelve(self) -> None:
        tracker = _tracker(
            # Twelve months back: outside the table.
            _day("2025-06-30", pv_kwh=1.0),
            _day("2025-07-01", pv_kwh=2.0),
            _day("2026-05-20", pv_kwh=3.0),
            _day("2026-05-21", pv_kwh=4.0),
            _day("2026-06-01", pv_kwh=5.0),
        )

        attributes = tracker.as_sensor_attributes(date(2026, 6, 10))

        assert attributes["month"] == "2026-06"
        assert attributes["this_month"]["pv_kwh"] == pytest.approx(5.0)
        assert attributes["previous_month"]["month"] == "2026-05"
        assert attributes["previous_month"]["pv_kwh"] == pytest.approx(7.0)
        assert [month["month"] for month in attributes["monthly"]] == [
            "2025-07",
            "2026-05",
            "2026-06",
        ]

    def test_a_new_installation_lists_the_current_month_only(self) -> None:
        attributes = EnergyTracker().as_sensor_attributes(date(2026, 6, 10))

        assert [month["month"] for month in attributes["monthly"]] == ["2026-06"]
        assert attributes["previous_month"]["days"] == 0

    def test_a_full_year_is_twelve_months(self) -> None:
        tracker = _tracker(
            *(
                EnergyDayEntry(f"{year}-{month:02d}-15")
                for year, month in [(2025, m) for m in range(1, 13)]
                + [(2026, m) for m in range(1, 7)]
            )
        )

        monthly = tracker.as_sensor_attributes(date(2026, 6, 10))["monthly"]

        assert len(monthly) == 12
        assert monthly[0]["month"] == "2025-07"
        assert monthly[-1]["month"] == "2026-06"


class TestPersistence:
    def _recorded(self) -> EnergyTracker:
        tracker = EnergyTracker()
        _feed(
            tracker,
            _sample(
                0,
                grid_import_energy_kwh=100.0,
                grid_export_energy_kwh=20.0,
                pv_power_w=500.0,
                house_power_w=1000.0,
                battery_power_w=1500.0,
                soc_pct=50.0,
            ),
            _sample(
                5,
                grid_import_energy_kwh=100.4,
                grid_export_energy_kwh=20.0,
                pv_power_w=500.0,
                house_power_w=1000.0,
                battery_power_w=1500.0,
                soc_pct=51.0,
            ),
        )
        return tracker

    def test_the_state_survives_a_round_trip_through_json(self) -> None:
        tracker = self._recorded()

        restored = EnergyTracker.from_dict(json.loads(json.dumps(tracker.as_dict())))

        assert restored.month_summary("2026-06") == tracker.month_summary("2026-06")
        assert restored._last_sample == tracker._last_sample
        assert tracker.as_dict()["schema"] == SCHEMA_VERSION

    def test_a_restart_shorter_than_the_gap_loses_nothing(self) -> None:
        tracker = self._recorded()
        restored = EnergyTracker.from_dict(json.loads(json.dumps(tracker.as_dict())))

        booked = _feed(restored, _sample(10, grid_import_energy_kwh=101.0))

        assert booked == [True]
        assert restored.daily_log[_DAY].grid_import_kwh == pytest.approx(1.0)

    def test_a_file_of_another_schema_is_not_read(self) -> None:
        data = self._recorded().as_dict()
        data["schema"] = SCHEMA_VERSION + 1

        restored = EnergyTracker.from_dict(data)

        assert restored.daily_log == {}
        assert restored._last_sample is None

    def test_entries_that_do_not_parse_are_left_out(self) -> None:
        data: dict[str, Any] = {
            "schema": SCHEMA_VERSION,
            "last_sample": {"at": "2026-06-10T12:00:00"},
            "daily_log": [
                "not a dict",
                {"date": "not a date"},
                # A day written before a quantity existed, with one bad value.
                {"date": "2026-06-01", "pv_kwh": 4.5, "grid_import_kwh": "nan"},
            ],
        }

        restored = EnergyTracker.from_dict(data)

        assert list(restored.daily_log) == ["2026-06-01"]
        entry = restored.daily_log["2026-06-01"]
        assert entry.pv_kwh == pytest.approx(4.5)
        assert entry.grid_import_kwh == pytest.approx(0.0)
        assert entry.wear_cost == pytest.approx(0.0)
        # A naive timestamp cannot be compared with the next sample.
        assert restored._last_sample is None

    def test_a_daily_log_that_is_not_a_list_is_ignored(self) -> None:
        restored = EnergyTracker.from_dict(
            {"schema": SCHEMA_VERSION, "daily_log": {"2026-06-01": {}}}
        )

        assert restored.daily_log == {}
        assert EnergySample.from_dict("not a dict") is None

    def test_save_is_due_after_the_interval(self) -> None:
        tracker = EnergyTracker()
        assert tracker.save_due(120.0) is False

        _feed(tracker, _sample(0))
        assert tracker.save_due(120.0) is True

        tracker._saved_at = _T0
        _feed(tracker, _sample(1))
        assert tracker.save_due(120.0) is False
        _feed(tracker, _sample(2))
        assert tracker.save_due(120.0) is True

    @pytest.mark.asyncio
    async def test_save_and_load_through_the_history_file(self, tmp_path: Path) -> None:
        tracker = self._recorded()
        tracker.history_file = str(tmp_path / ".storage" / "hsem_energy_history.json")

        assert await tracker.save_history() is True
        assert tracker.save_due(120.0) is False

        loaded = EnergyTracker(history_file=tracker.history_file)
        await loaded.load_history()

        assert loaded.history_file == tracker.history_file
        assert loaded.month_summary("2026-06") == tracker.month_summary("2026-06")

    @pytest.mark.asyncio
    async def test_nothing_is_saved_or_loaded_without_a_history_file(self) -> None:
        tracker = self._recorded()

        assert await tracker.save_history() is False
        await tracker.load_history()

        assert _DAY in tracker.daily_log

    @pytest.mark.asyncio
    async def test_a_missing_history_file_leaves_the_tracker_as_it_is(
        self, tmp_path: Path
    ) -> None:
        tracker = self._recorded()
        tracker.history_file = str(tmp_path / "absent.json")

        await tracker.load_history()

        assert _DAY in tracker.daily_log

    @pytest.mark.asyncio
    async def test_a_failed_write_stays_due(self, tmp_path: Path) -> None:
        tracker = self._recorded()
        tracker.history_file = str(tmp_path / "hsem_energy_history.json")

        with patch.object(module, "write_json_history_file", return_value=False):
            assert await tracker.save_history() is False

        assert tracker.save_due(120.0) is True
