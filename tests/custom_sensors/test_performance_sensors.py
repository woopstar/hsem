"""Tests for the HSEM performance (KPI) sensors (issue #1182)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from homeassistant.components.sensor.const import SensorStateClass
from homeassistant.const import PERCENTAGE, EntityCategory

from custom_components.hsem.custom_sensors.performance_sensors import (
    PERFORMANCE_SENSORS,
    HSEMPerformanceSensor,
    build_performance_sensors,
)
from custom_components.hsem.devices import HSEMDevice
from custom_components.hsem.models.performance_record import (
    REASON_INSUFFICIENT_COVERAGE,
    REASON_POTENTIAL_BELOW_THRESHOLD,
    PerformanceScore,
)
from custom_components.hsem.models.performance_tracker import (
    REASON_NOT_EVALUATED,
    PerformanceTracker,
)

_MODULE = "custom_components.hsem.custom_sensors.performance_sensors"
_NOW = datetime(2026, 6, 10, 12, 0, tzinfo=ZoneInfo("Europe/Copenhagen"))
_START = datetime(2026, 6, 9, 10, 0, tzinfo=UTC)


def _score(
    *,
    realized: float = 3.4,
    oracle: float = 3.0,
    baseline: float = 5.0,
    potential: float = 2.0,
    reason: str | None = None,
) -> PerformanceScore:
    return PerformanceScore(
        start=_START,
        end=_START + timedelta(hours=24),
        slot_count=96,
        reason=reason,
        coverage=1.0,
        control_coverage=0.75,
        realized_cost=realized,
        oracle_cost=oracle,
        baseline_cost=baseline,
        potential=potential,
        start_kwh=4.0,
        realized_end_kwh=4.0,
        baseline_end_kwh=4.0,
    )


def _sensors(
    window: PerformanceScore | None, *, tracker: bool = True
) -> dict[str, HSEMPerformanceSensor]:
    coordinator = MagicMock()
    coordinator._performance_tracker = (
        PerformanceTracker(window=window) if tracker else None
    )
    coordinator.hass.config.currency = "DKK"
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    return {
        sensor._description.key: sensor
        for sensor in build_performance_sensors(entry, coordinator)
    }


class TestStates:
    def test_the_worked_example(self) -> None:
        """Baseline 5.00, realized 3.40, oracle 3.00 → 80 %, 1.60 and 0.40."""
        sensors = _sensors(_score())

        assert sensors["savings_capture"].native_value == pytest.approx(80.0)
        assert sensors["actual_benefit"].native_value == pytest.approx(1.6)
        assert sensors["missed_opportunity"].native_value == pytest.approx(0.4)

    def test_a_negative_capture_is_not_clamped(self) -> None:
        sensors = _sensors(_score(realized=6.0))

        assert sensors["savings_capture"].native_value == pytest.approx(-50.0)
        assert sensors["actual_benefit"].native_value == pytest.approx(-1.0)
        assert sensors["missed_opportunity"].native_value == pytest.approx(3.0)

    def test_capture_is_unknown_when_there_was_almost_nothing_to_gain(self) -> None:
        """The amounts stay valid on a flat-price day; the share does not."""
        sensors = _sensors(
            _score(realized=5.0, oracle=4.99, baseline=5.01, potential=0.02)
        )

        assert sensors["savings_capture"].native_value is None
        assert sensors["missed_opportunity"].native_value == pytest.approx(0.01)
        assert sensors["actual_benefit"].native_value == pytest.approx(0.01)
        attributes = sensors["savings_capture"].extra_state_attributes
        assert attributes is not None
        assert attributes["reason"] == REASON_POTENTIAL_BELOW_THRESHOLD

    def test_an_unscored_window_is_unknown_with_a_reason(self) -> None:
        sensors = _sensors(_score(reason=REASON_INSUFFICIENT_COVERAGE))

        for sensor in sensors.values():
            assert sensor.native_value is None
            attributes = sensor.extra_state_attributes
            assert attributes is not None
            assert attributes["reason"] == REASON_INSUFFICIENT_COVERAGE

    def test_unknown_before_the_first_evaluation(self) -> None:
        sensors = _sensors(None)

        for sensor in sensors.values():
            assert sensor.native_value is None
            attributes = sensor.extra_state_attributes
            assert attributes is not None
            assert attributes["reason"] == REASON_NOT_EVALUATED

    def test_unknown_without_a_tracker(self) -> None:
        sensors = _sensors(None, tracker=False)

        for sensor in sensors.values():
            assert sensor.native_value is None
            assert sensor.extra_state_attributes is None


class TestAttributes:
    def test_every_sensor_publishes_the_same_breakdown(self) -> None:
        sensors = _sensors(_score())

        with patch(f"{_MODULE}.hsem_now", return_value=_NOW):
            published = [sensor.extra_state_attributes for sensor in sensors.values()]

        assert published[0] == published[1] == published[2]
        attributes = published[0]
        assert attributes is not None
        assert attributes["realized_cost"] == pytest.approx(3.4)
        assert attributes["baseline_cost"] == pytest.approx(5.0)
        assert attributes["oracle_cost"] == pytest.approx(3.0)
        assert attributes["potential"] == pytest.approx(2.0)
        assert attributes["coverage"] == pytest.approx(1.0)
        assert attributes["control_coverage"] == pytest.approx(0.75)
        assert attributes["window_start"] == "2026-06-09T12:00:00+02:00"
        assert attributes["window_end"] == "2026-06-10T12:00:00+02:00"
        assert attributes["solver_status"] == "optimal"
        assert attributes["yesterday"] is None
        assert attributes["last_7_days"]["scored_days"] == 0
        assert attributes["last_30_days"]["days"] == 30

    def test_bulky_attributes_stay_out_of_the_recorder(self) -> None:
        """Rollups and notes live in the history file (issue #1099)."""
        assert HSEMPerformanceSensor._unrecorded_attributes == frozenset(
            {"yesterday", "last_7_days", "last_30_days", "notes"}
        )


class TestEntityDescription:
    def test_one_sensor_per_kpi(self) -> None:
        sensors = _sensors(_score())

        assert list(sensors) == [d.key for d in PERFORMANCE_SENSORS]
        assert list(sensors) == [
            "savings_capture",
            "actual_benefit",
            "missed_opportunity",
        ]

    def test_all_share_the_performance_prefix_on_the_financial_device(self) -> None:
        for key, sensor in _sensors(_score()).items():
            assert sensor.entity_id == f"sensor.hsem_performance_{key}"
            assert sensor.unique_id == f"hsem_test_entry_id_performance_{key}_sensor"
            assert sensor._attr_translation_key == f"performance_{key}"
            assert sensor._hsem_device is HSEMDevice.FINANCIAL
            assert sensor._attr_entity_category is EntityCategory.DIAGNOSTIC
            assert sensor._attr_has_entity_name is True
            assert sensor.should_poll is False

    def test_a_rolling_value_is_a_measurement_not_a_monetary_total(self) -> None:
        """Home Assistant allows the monetary device class only on totals."""
        for sensor in _sensors(_score()).values():
            assert sensor._attr_state_class is SensorStateClass.MEASUREMENT
            assert sensor.device_class is None

    def test_the_capture_is_a_percentage_and_the_amounts_are_money(self) -> None:
        sensors = _sensors(_score())

        assert sensors["savings_capture"].native_unit_of_measurement == PERCENTAGE
        assert sensors["actual_benefit"].native_unit_of_measurement == "DKK"
        assert sensors["missed_opportunity"].native_unit_of_measurement == "DKK"
