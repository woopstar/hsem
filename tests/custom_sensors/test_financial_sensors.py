"""Tests for the HSEM financial sensors (export income, import cost, net grid balance)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from homeassistant.components.sensor.const import SensorDeviceClass, SensorStateClass

from custom_components.hsem.custom_sensors.financial_sensors import (
    HSEMExportIncomeSensor,
    HSEMImportCostSensor,
    HSEMNetGridBalanceSensor,
)
from custom_components.hsem.models.financial_tracker import FinancialTracker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_coordinator(
    export_income_total: float = 0.0,
    import_cost_total: float = 0.0,
    last_update_success: bool = True,
) -> MagicMock:
    """Create a mock coordinator with a financial tracker."""
    tracker = FinancialTracker(
        import_cost_total=import_cost_total,
        export_income_total=export_income_total,
    )
    coordinator = MagicMock()
    coordinator._financial_tracker = tracker
    coordinator.last_update_success = last_update_success
    coordinator.data = MagicMock()
    return coordinator


def _make_mock_config_entry() -> MagicMock:
    """Create a mock config entry."""
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    return entry


# ---------------------------------------------------------------------------
# Export Income Sensor Tests
# ---------------------------------------------------------------------------


class TestExportIncomeSensor:
    """Tests for :class:`HSEMExportIncomeSensor`."""

    def test_native_value_returns_total(self) -> None:
        """native_value returns the cumulative export income."""
        coordinator = _make_mock_coordinator(export_income_total=123.456)
        sensor = HSEMExportIncomeSensor(_make_mock_config_entry(), coordinator)
        assert sensor.native_value == pytest.approx(123.456)

    def test_native_value_none_when_no_tracker(self) -> None:
        """native_value returns None when tracker is unavailable."""
        coordinator = MagicMock()
        coordinator._financial_tracker = None
        coordinator.last_update_success = False
        sensor = HSEMExportIncomeSensor(_make_mock_config_entry(), coordinator)
        assert sensor.native_value is None

    def test_device_class_is_monetary(self) -> None:
        """Device class is MONETARY."""
        sensor = HSEMExportIncomeSensor(
            _make_mock_config_entry(), _make_mock_coordinator()
        )
        assert sensor._attr_device_class == SensorDeviceClass.MONETARY

    def test_state_class_is_total_increasing(self) -> None:
        """State class is TOTAL_INCREASING."""
        sensor = HSEMExportIncomeSensor(
            _make_mock_config_entry(), _make_mock_coordinator()
        )
        assert sensor._attr_state_class == SensorStateClass.TOTAL_INCREASING

    def test_should_poll_is_false(self) -> None:
        """Sensor is coordinator-driven, not polled."""
        sensor = HSEMExportIncomeSensor(
            _make_mock_config_entry(), _make_mock_coordinator()
        )
        assert sensor.should_poll is False

    def test_available_when_coordinator_success(self) -> None:
        """available is True when the coordinator last updated successfully."""
        coordinator = _make_mock_coordinator(last_update_success=True)
        sensor = HSEMExportIncomeSensor(_make_mock_config_entry(), coordinator)
        assert sensor.available is True

    def test_unavailable_when_coordinator_failed(self) -> None:
        """available is False when the coordinator has not succeeded."""
        coordinator = _make_mock_coordinator(last_update_success=False)
        sensor = HSEMExportIncomeSensor(_make_mock_config_entry(), coordinator)
        assert sensor.available is False

    def test_extra_state_attributes_includes_periods(self) -> None:
        """Attributes include today, last_7_days, etc."""
        coordinator = _make_mock_coordinator(
            export_income_total=100.0, import_cost_total=50.0
        )
        sensor = HSEMExportIncomeSensor(_make_mock_config_entry(), coordinator)
        attrs = sensor.extra_state_attributes
        assert attrs is not None
        assert "today" in attrs
        assert "last_7_days" in attrs
        assert "last_30_days" in attrs
        assert "this_month" in attrs
        assert "this_year" in attrs
        assert "daily" in attrs


# ---------------------------------------------------------------------------
# Import Cost Sensor Tests
# ---------------------------------------------------------------------------


class TestImportCostSensor:
    """Tests for :class:`HSEMImportCostSensor`."""

    def test_native_value_returns_total(self) -> None:
        """native_value returns the cumulative import cost."""
        coordinator = _make_mock_coordinator(import_cost_total=56.789)
        sensor = HSEMImportCostSensor(_make_mock_config_entry(), coordinator)
        assert sensor.native_value == pytest.approx(56.789)

    def test_device_class_is_monetary(self) -> None:
        """Device class is MONETARY."""
        sensor = HSEMImportCostSensor(
            _make_mock_config_entry(), _make_mock_coordinator()
        )
        assert sensor._attr_device_class == SensorDeviceClass.MONETARY

    def test_state_class_is_total_increasing(self) -> None:
        """State class is TOTAL_INCREASING."""
        sensor = HSEMImportCostSensor(
            _make_mock_config_entry(), _make_mock_coordinator()
        )
        assert sensor._attr_state_class == SensorStateClass.TOTAL_INCREASING

    def test_should_poll_is_false(self) -> None:
        """Sensor is coordinator-driven."""
        sensor = HSEMImportCostSensor(
            _make_mock_config_entry(), _make_mock_coordinator()
        )
        assert sensor.should_poll is False


# ---------------------------------------------------------------------------
# Net Grid Balance Sensor Tests
# ---------------------------------------------------------------------------


class TestNetGridBalanceSensor:
    """Tests for :class:`HSEMNetGridBalanceSensor`."""

    def test_native_value_is_export_minus_import(self) -> None:
        """native_value = export_income_total - import_cost_total."""
        coordinator = _make_mock_coordinator(
            export_income_total=100.0, import_cost_total=60.0
        )
        sensor = HSEMNetGridBalanceSensor(_make_mock_config_entry(), coordinator)
        assert sensor.native_value == pytest.approx(40.0)

    def test_native_value_can_be_negative(self) -> None:
        """Net balance can be negative (more import cost than export income)."""
        coordinator = _make_mock_coordinator(
            export_income_total=10.0, import_cost_total=100.0
        )
        sensor = HSEMNetGridBalanceSensor(_make_mock_config_entry(), coordinator)
        assert sensor.native_value == pytest.approx(-90.0)

    def test_device_class_is_monetary(self) -> None:
        """Device class is MONETARY."""
        sensor = HSEMNetGridBalanceSensor(
            _make_mock_config_entry(), _make_mock_coordinator()
        )
        assert sensor._attr_device_class == SensorDeviceClass.MONETARY

    def test_state_class_is_measurement(self) -> None:
        """State class is MEASUREMENT (not total_increasing)."""
        sensor = HSEMNetGridBalanceSensor(
            _make_mock_config_entry(), _make_mock_coordinator()
        )
        assert sensor._attr_state_class == SensorStateClass.MEASUREMENT

    def test_should_poll_is_false(self) -> None:
        """Sensor is coordinator-driven."""
        sensor = HSEMNetGridBalanceSensor(
            _make_mock_config_entry(), _make_mock_coordinator()
        )
        assert sensor.should_poll is False
