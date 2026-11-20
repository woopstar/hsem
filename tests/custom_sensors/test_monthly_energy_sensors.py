"""Tests for the monthly energy sensors (issue #1435)."""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from homeassistant.components.sensor.const import SensorStateClass
from homeassistant.const import PERCENTAGE, EntityCategory

from custom_components.hsem.custom_sensors.monthly_energy_sensors import (
    MONTHLY_ENERGY_SENSORS,
    UNIT_CYCLES,
    HSEMMonthlyEnergySensor,
    build_monthly_energy_sensors,
)
from custom_components.hsem.devices import HSEMDevice
from custom_components.hsem.models.energy_tracker import (
    REASON_NO_DATA,
    REASON_THROUGHPUT_BELOW_MINIMUM,
    EnergyDayEntry,
    EnergyTracker,
)

_MODULE = "custom_components.hsem.custom_sensors.monthly_energy_sensors"
_NOW = datetime(2026, 6, 10, 12, 0, tzinfo=ZoneInfo("Europe/Copenhagen"))


def _day(date: str, *, charged: float = 60.0) -> EnergyDayEntry:
    """Return a day that is 75 % self-sufficient with a 10 % round-trip loss."""
    return EnergyDayEntry(
        date,
        observed_hours=24.0,
        house_consumption_kwh=20.0,
        grid_import_kwh=5.0,
        grid_export_kwh=3.0,
        pv_kwh=18.0,
        battery_charged_kwh=charged,
        battery_discharged_kwh=charged * 0.9,
        wear_cost=1.5,
        paired_consumption_kwh=20.0,
        paired_import_kwh=5.0,
        measured_charged_kwh=charged,
        measured_discharged_kwh=charged * 0.9,
        usable_capacity_kwh=9.0,
        rated_capacity_kwh=10.0,
    )


def _sensors(tracker: EnergyTracker | None) -> dict[str, HSEMMonthlyEnergySensor]:
    coordinator = MagicMock()
    coordinator._energy_tracker = tracker
    coordinator.hass.config.currency = "DKK"
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    return {
        sensor._description.key: sensor
        for sensor in build_monthly_energy_sensors(entry, coordinator)
    }


def _recorded() -> EnergyTracker:
    return EnergyTracker(
        daily_log={
            entry.date: entry
            for entry in (_day("2026-05-30", charged=10.0), _day("2026-06-09"))
        }
    )


class TestEntities:
    def test_one_sensor_per_kpi(self) -> None:
        sensors = _sensors(EnergyTracker())

        assert list(sensors) == [d.key for d in MONTHLY_ENERGY_SENSORS]
        assert {key: sensor.entity_id for key, sensor in sensors.items()} == {
            "self_sufficiency": "sensor.hsem_monthly_self_sufficiency",
            "battery_cycles": "sensor.hsem_monthly_battery_cycles",
            "battery_wear_cost": "sensor.hsem_monthly_battery_wear_cost",
            "round_trip_loss": "sensor.hsem_monthly_round_trip_loss",
        }
        assert len({sensor.unique_id for sensor in sensors.values()}) == 4
        for key, sensor in sensors.items():
            assert sensor.unique_id == f"hsem_test_entry_id_monthly_{key}_sensor"
            assert sensor.translation_key == f"monthly_{key}"
            assert sensor.state_class is SensorStateClass.MEASUREMENT
            assert sensor.entity_category is EntityCategory.DIAGNOSTIC
            assert sensor.device_class is None
            assert sensor.should_poll is False
            assert sensor._hsem_device is HSEMDevice.FINANCIAL

    def test_units(self) -> None:
        sensors = _sensors(EnergyTracker())

        assert sensors["self_sufficiency"].native_unit_of_measurement == PERCENTAGE
        assert sensors["round_trip_loss"].native_unit_of_measurement == PERCENTAGE
        assert sensors["battery_cycles"].native_unit_of_measurement == UNIT_CYCLES
        assert sensors["battery_wear_cost"].native_unit_of_measurement == "DKK"


class TestValues:
    def test_the_state_is_the_current_month(self) -> None:
        sensors = _sensors(_recorded())

        with patch(f"{_MODULE}.hsem_now", return_value=_NOW):
            values = {key: sensor.native_value for key, sensor in sensors.items()}
            reasons = {
                key: (sensor.extra_state_attributes or {})["reason"]
                for key, sensor in sensors.items()
            }

        assert values == {
            "self_sufficiency": pytest.approx(75.0),
            "battery_cycles": pytest.approx(6.0),
            "battery_wear_cost": pytest.approx(1.5),
            "round_trip_loss": pytest.approx(10.0),
        }
        assert set(reasons.values()) == {None}

    def test_previous_month_and_the_month_table_are_attributes(self) -> None:
        sensor = _sensors(_recorded())["round_trip_loss"]

        with patch(f"{_MODULE}.hsem_now", return_value=_NOW):
            attributes = sensor.extra_state_attributes

        assert attributes is not None
        assert attributes["month"] == "2026-06"
        assert attributes["this_month"]["round_trip_loss"] == pytest.approx(10.0)
        previous = attributes["previous_month"]
        assert previous["month"] == "2026-05"
        assert previous["self_sufficiency"] == pytest.approx(75.0)
        # 10 kWh charged in May is too little to measure a loss with.
        assert previous["round_trip_loss"] is None
        assert previous["round_trip_loss_reason"] == REASON_THROUGHPUT_BELOW_MINIMUM
        assert [month["month"] for month in attributes["monthly"]] == [
            "2026-05",
            "2026-06",
        ]

    def test_an_unknown_value_carries_its_reason(self) -> None:
        sensors = _sensors(EnergyTracker())

        with patch(f"{_MODULE}.hsem_now", return_value=_NOW):
            for sensor in sensors.values():
                assert sensor.native_value is None
                assert (sensor.extra_state_attributes or {})["reason"] == REASON_NO_DATA

    def test_without_a_tracker_there_is_nothing_to_report(self) -> None:
        sensors = _sensors(None)

        for sensor in sensors.values():
            assert sensor.native_value is None
            assert sensor.extra_state_attributes is None


class TestRecorderFootprint:
    def test_only_small_scalars_are_recorded(self) -> None:
        """The month tables stay out of the recorder (issue #1099)."""
        sensor = _sensors(_recorded())["self_sufficiency"]

        with patch(f"{_MODULE}.hsem_now", return_value=_NOW):
            attributes = sensor.extra_state_attributes or {}
        recorded = {
            key: value
            for key, value in attributes.items()
            if key not in HSEMMonthlyEnergySensor._unrecorded_attributes
        }

        assert set(recorded) == {"month", "reason"}
        assert not any(isinstance(value, (list, dict)) for value in recorded.values())
        assert len(json.dumps(recorded)) < 2048
        assert {"this_month", "previous_month", "monthly"} <= set(attributes)
