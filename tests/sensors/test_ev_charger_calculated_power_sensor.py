"""Tests for the EV charger calculated power sensors.

Acceptance criteria
-------------------
- ``native_value`` equals ``ev_charger_calculated_power`` (primary) and
  ``ev_second_charger_calculated_power`` (second) from the current
  ``HourlyRecommendation``.
- ``native_value`` returns ``None`` when the coordinator has no data or no
  active slot, falling back to the restored state when available.
- ``available`` is False before the first cycle and True after.
- ``extra_state_attributes`` expose the active slot window and planned load.
- The sensors are wired as diagnostic power-measurement entities.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from homeassistant.components.sensor.const import SensorDeviceClass, SensorStateClass
from homeassistant.const import EntityCategory, UnitOfPower

from custom_components.hsem.coordinator import CoordinatorData
from custom_components.hsem.custom_sensors.ev_charger_calculated_power_sensor import (
    HSEMEVChargerCalculatedPowerSensor,
    HSEMEVChargerCalculatedPowerSensorBase,
    HSEMEVSecondChargerCalculatedPowerSensor,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_rec(
    *,
    ev_power: float = 11000.0,
    ev_second_power: float = 0.0,
) -> HourlyRecommendation:
    """Return a minimal HourlyRecommendation for the current slot."""
    now = datetime.now(UTC)
    return HourlyRecommendation(
        start=now,
        end=now + timedelta(minutes=15),
        recommendation="ev_smart_charging",
        avg_house_consumption_kwh=0.0,
        avg_house_consumption_1d_kwh=0.0,
        avg_house_consumption_3d_kwh=0.0,
        avg_house_consumption_7d_kwh=0.0,
        avg_house_consumption_14d_kwh=0.0,
        batteries_charged_kwh=0.0,
        batteries_discharged_kwh=0.0,
        estimated_battery_capacity_kwh=0.0,
        estimated_battery_soc_pct=50.0,
        estimated_cost_currency=0.0,
        estimated_net_consumption_kwh=0.0,
        export_price=0.0,
        grid_export_kwh=0.0,
        grid_import_kwh=0.0,
        import_price=0.0,
        solcast_pv_estimate_kwh=0.0,
        ev_charger_calculated_power=ev_power,
        ev_second_charger_calculated_power=ev_second_power,
        ev_total_planned_load_kwh=2.75,
    )


def _make_sensor(
    cls: type[HSEMEVChargerCalculatedPowerSensorBase],
    data: CoordinatorData | None,
) -> HSEMEVChargerCalculatedPowerSensorBase:
    """Return a bare sensor wired to a mock coordinator."""
    coordinator = MagicMock()
    coordinator.data = data
    coordinator.last_update_success = data is not None

    sensor: HSEMEVChargerCalculatedPowerSensorBase = object.__new__(cls)
    sensor.coordinator = coordinator
    sensor._config_entry = MagicMock()
    sensor._restored_state = None
    return sensor


# ===========================================================================
# 1. Entity metadata
# ===========================================================================


class TestEntityMetadata:
    """The sensors have the correct HA entity metadata."""

    @pytest.mark.parametrize(
        "cls",
        [HSEMEVChargerCalculatedPowerSensor, HSEMEVSecondChargerCalculatedPowerSensor],
    )
    def test_sensor_is_diagnostic_power_measurement(self, cls):
        """Both sensors must be diagnostic W power-measurement entities."""
        sensor = _make_sensor(cls, None)
        assert sensor._attr_entity_category is EntityCategory.DIAGNOSTIC
        assert sensor._attr_device_class is SensorDeviceClass.POWER
        assert sensor._attr_state_class is SensorStateClass.MEASUREMENT
        assert sensor._attr_native_unit_of_measurement == UnitOfPower.WATT

    def test_primary_translation_key(self):
        """Primary sensor must use the ev_charger_calculated_power translation key."""
        sensor = _make_sensor(HSEMEVChargerCalculatedPowerSensor, None)
        assert sensor._attr_translation_key == "ev_charger_calculated_power"

    def test_second_translation_key(self):
        """Second sensor must use the ev_second_charger_calculated_power key."""
        sensor = _make_sensor(HSEMEVSecondChargerCalculatedPowerSensor, None)
        assert sensor._attr_translation_key == "ev_second_charger_calculated_power"


# ===========================================================================
# 2. Native value
# ===========================================================================


class TestNativeValue:
    """native_value reflects the planner's calculated power per charger."""

    def test_primary_returns_ev_charger_calculated_power(self):
        """Primary sensor state must equal ev_charger_calculated_power."""
        data = CoordinatorData(hourly_recommendation=_make_rec(ev_power=11000.0))
        sensor = _make_sensor(HSEMEVChargerCalculatedPowerSensor, data)
        assert sensor.native_value == pytest.approx(11000.0)

    def test_second_returns_ev_second_charger_calculated_power(self):
        """Second sensor state must equal ev_second_charger_calculated_power."""
        data = CoordinatorData(hourly_recommendation=_make_rec(ev_second_power=3700.0))
        sensor = _make_sensor(HSEMEVSecondChargerCalculatedPowerSensor, data)
        assert sensor.native_value == pytest.approx(3700.0)

    def test_primary_zero_when_no_charging_planned(self):
        """Primary sensor reports 0 W when the planner allocates no EV power."""
        data = CoordinatorData(hourly_recommendation=_make_rec(ev_power=0.0))
        sensor = _make_sensor(HSEMEVChargerCalculatedPowerSensor, data)
        assert sensor.native_value == pytest.approx(0.0)

    def test_none_when_no_coordinator_data(self):
        """native_value must be None before the first coordinator cycle."""
        sensor = _make_sensor(HSEMEVChargerCalculatedPowerSensor, None)
        assert sensor.native_value is None

    def test_none_when_no_active_slot(self):
        """native_value must be None when there is no active slot."""
        sensor = _make_sensor(HSEMEVChargerCalculatedPowerSensor, CoordinatorData())
        assert sensor.native_value is None

    def test_falls_back_to_restored_state(self):
        """native_value uses the restored state when no data is available."""
        sensor = _make_sensor(HSEMEVChargerCalculatedPowerSensor, None)
        sensor._restored_state = "7400"
        assert sensor.native_value == pytest.approx(7400.0)

    def test_invalid_restored_state_returns_none(self):
        """A non-numeric restored state must not crash the sensor."""
        sensor = _make_sensor(HSEMEVChargerCalculatedPowerSensor, None)
        sensor._restored_state = "not-a-number"
        assert sensor.native_value is None


# ===========================================================================
# 3. Availability
# ===========================================================================


class TestAvailability:
    """available reflects coordinator health and restore state."""

    def test_unavailable_before_first_cycle(self):
        """Sensor must be unavailable before the first successful cycle."""
        sensor = _make_sensor(HSEMEVChargerCalculatedPowerSensor, None)
        assert sensor.available is False

    def test_available_after_cycle(self):
        """Sensor must be available once the coordinator has data."""
        data = CoordinatorData(hourly_recommendation=_make_rec())
        sensor = _make_sensor(HSEMEVChargerCalculatedPowerSensor, data)
        assert sensor.available is True

    def test_available_with_restored_state(self):
        """A restored state keeps the sensor available across restarts."""
        sensor = _make_sensor(HSEMEVChargerCalculatedPowerSensor, None)
        sensor._restored_state = "11000"
        assert sensor.available is True


# ===========================================================================
# 4. Attributes
# ===========================================================================


class TestAttributes:
    """extra_state_attributes expose the active slot context."""

    def test_attributes_with_active_slot(self):
        """Attributes include slot window and total planned EV load."""
        rec = _make_rec()
        data = CoordinatorData(hourly_recommendation=rec)
        sensor = _make_sensor(HSEMEVChargerCalculatedPowerSensor, data)
        attrs = sensor.extra_state_attributes
        assert attrs["slot_start"] == rec.start.isoformat()
        assert attrs["slot_end"] == rec.end.isoformat()
        assert attrs["ev_total_planned_load_kwh"] == pytest.approx(2.75)

    def test_attributes_without_data(self):
        """Attributes are None-filled when no active slot exists."""
        sensor = _make_sensor(HSEMEVChargerCalculatedPowerSensor, None)
        attrs = sensor.extra_state_attributes
        assert attrs["slot_start"] is None
        assert attrs["slot_end"] is None
        assert attrs["ev_total_planned_load_kwh"] is None
