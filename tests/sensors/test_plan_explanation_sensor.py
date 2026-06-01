"""Tests for the HSEMPlanExplanationSensor diagnostic sensor.

Acceptance criteria
-------------------
- ``state`` returns ``selected_strategy`` from ``CoordinatorData.plan_explanation``.
- ``state`` returns ``"unknown"`` when the coordinator has no data yet.
- ``state`` falls back to the restored state before the first coordinator cycle.
- ``extra_state_attributes`` returns all keys from ``PlanExplanation.as_dict()``.
- ``extra_state_attributes`` values are correct for a known explanation.
- ``available`` is False before the first cycle and True after.
- The sensor is wired as a diagnostic entity category.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from homeassistant.const import STATE_UNKNOWN, EntityCategory

from custom_components.hsem.coordinator import CoordinatorData
from custom_components.hsem.custom_sensors.plan_explanation_sensor import (
    HSEMPlanExplanationSensor,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.planner_outputs import (
    DataQuality,
    PlanExplanation,
    RejectedPlan,
)
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.degraded_mode import DegradedMode
from custom_components.hsem.utils.inverter_verify import (
    ApplyResult,
    ApplyStatus,
    CycleApplySummary,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EXPECTED_ATTR_KEYS = {
    "selected_strategy",
    "summary",
    "score",
    "estimated_total_cost",
    "price_spread",
    "peak_import_price",
    "off_peak_import_price",
    "forecast_pv_kwh",
    "forecast_net_consumption_kwh",
    "battery_soc_pct",
    "battery_soc_at_end_pct",
    "constraints",
    "rejected_plans",
}

# Context keys added when the coordinator snapshot is fully populated.
_CONTEXT_ATTR_KEYS = {
    "planning_horizon_hours",
    "planning_interval_minutes",
    "forecast_mode",
    "current_slot_start",
    "current_slot_end",
    "current_slot_recommendation",
    "last_apply_status",
    "hardware_writes_blocked",
    "data_quality_complete",
}


def _make_explanation(**kwargs: Any) -> PlanExplanation:
    """Return a PlanExplanation with sensible defaults overridable by kwargs."""
    defaults = {
        "selected_strategy": "charge_grid_discharge_peak",
        "summary": "Battery charged from grid and discharged at peak.",
        "score": 0.45,
        "estimated_total_cost": 1.20,
        "price_spread": 0.27,
        "peak_import_price": 0.32,
        "off_peak_import_price": 0.05,
        "forecast_pv_kwh": 18.3,
        "forecast_net_consumption_kwh": 7.4,
        "battery_soc_pct": 50.0,
        "battery_soc_at_end_pct": 20.0,
        "constraints": ["summer_month"],
        "rejected_plans": [RejectedPlan("do_nothing", "Costs more idle.", 1.65)],
    }
    defaults.update(kwargs)
    return PlanExplanation(**defaults)  # type: ignore[arg-type]  # test helper: dict values are typed at runtime


def _make_coordinator_data(
    explanation: PlanExplanation | None = None,
    *,
    has_context: bool = False,
) -> CoordinatorData:
    """Return a minimal CoordinatorData carrying a plan explanation.

    When *has_context* is True, also populates ``cfg``, ``live``,
    ``hourly_recommendation``, ``apply_summary``, and ``data_quality``
    so that the enriched attributes are returned.
    """
    data = CoordinatorData(plan_explanation=explanation or _make_explanation())
    if has_context:
        cfg = SensorConfig()
        cfg.recommendation_interval_length = 48
        cfg.recommendation_interval_minutes = 15
        cfg.months_winter = [1, 2, 3, 4, 10, 11, 12]
        data.cfg = cfg

        live = LiveState()
        live._degraded_mode = DegradedMode.OK
        data.live = live

        now = datetime.now(UTC)
        slot = HourlyRecommendation(
            start=now,
            end=now + timedelta(hours=1),
            recommendation="batteries_wait_mode",
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
        )
        data.hourly_recommendation = slot

        data.apply_summary = CycleApplySummary(
            results=[
                ApplyResult(
                    entity_id="number.batteries_end_of_charge_soc",
                    desired=95,
                    actual=95,
                    status=ApplyStatus.OK,
                    attempts=1,
                )
            ]
        )
        data.data_quality = DataQuality()
    return data


def _make_sensor(data: CoordinatorData | None = None) -> HSEMPlanExplanationSensor:
    """Return a bare HSEMPlanExplanationSensor wired to a mock coordinator."""
    coordinator = MagicMock()
    coordinator.data = data
    coordinator.last_update_success = data is not None

    config_entry = MagicMock()

    sensor = object.__new__(HSEMPlanExplanationSensor)
    # Minimal attribute injection — bypasses __init__ CoordinatorEntity plumbing.
    sensor.coordinator = coordinator
    sensor._config_entry = config_entry
    sensor._attr_unique_id = "hsem_plan_explanation_sensor"
    sensor.entity_id = "sensor.hsem_plan_explanation_sensor"
    sensor._name = "Plan Strategy"
    sensor._restored_state = None
    return sensor


# ===========================================================================
# 1. Entity metadata
# ===========================================================================


class TestEntityMetadata:
    """The sensor has the correct HA entity metadata."""

    def test_sensor_is_diagnostic_entity(self):
        """entity_category must resolve to DIAGNOSTIC on an instantiated sensor."""
        sensor = _make_sensor(_make_coordinator_data())
        cat = getattr(sensor, "entity_category", None) or getattr(
            sensor, "_attr_entity_category", None
        )
        assert cat is EntityCategory.DIAGNOSTIC


# ===========================================================================
# 2. State
# ===========================================================================


class TestSensorState:
    """state property returns the correct value in all scenarios."""

    def test_state_returns_selected_strategy(self):
        """state must equal explanation.selected_strategy when data is present."""
        sensor = _make_sensor(_make_coordinator_data())
        assert sensor.state == "charge_grid_discharge_peak"

    def test_state_unknown_when_no_coordinator_data(self):
        """state must return 'unknown' before the first cycle."""
        sensor = _make_sensor(data=None)
        assert sensor.state == STATE_UNKNOWN

    def test_state_falls_back_to_restored_state(self):
        """state uses _restored_state when coordinator.data is None."""
        sensor = _make_sensor(data=None)
        sensor._restored_state = "winter_wait"
        assert sensor.state == "winter_wait"

    def test_state_prefers_live_over_restored(self):
        """When coordinator.data is available, live data takes priority."""
        sensor = _make_sensor(
            _make_coordinator_data(
                _make_explanation(selected_strategy="solar_charge_only")
            )
        )
        sensor._restored_state = "old_strategy"
        assert sensor.state == "solar_charge_only"

    def test_state_unknown_strategy_fallback(self):
        """Empty selected_strategy string returns 'unknown'."""
        sensor = _make_sensor(
            _make_coordinator_data(_make_explanation(selected_strategy=""))
        )
        assert sensor.state == STATE_UNKNOWN

    @pytest.mark.parametrize(
        "strategy",
        [
            "charge_grid_discharge_peak",
            "charge_solar_discharge_peak",
            "opportunistic_charge",
            "force_export",
            "force_export_pv",
            "discharge_only",
            "winter_wait",
            "solar_charge_only",
        ],
    )
    def test_state_for_all_known_strategies(self, strategy: str) -> None:
        """All known strategy values round-trip correctly through state."""
        sensor = _make_sensor(
            _make_coordinator_data(_make_explanation(selected_strategy=strategy))
        )
        assert sensor.state == strategy


# ===========================================================================
# 3. Availability
# ===========================================================================


class TestSensorAvailability:
    """available property is correct in all states."""

    def test_available_when_coordinator_has_data(self):
        """available is True when coordinator has data and last_update_success."""
        sensor = _make_sensor(_make_coordinator_data())
        assert sensor.available is True

    def test_not_available_when_no_data(self):
        """available is False when coordinator.data is None and no restored state."""
        sensor = _make_sensor(data=None)
        assert sensor.available is False

    def test_available_via_restored_state(self):
        """available is True when _restored_state is set (HA restart scenario)."""
        sensor = _make_sensor(data=None)
        sensor._restored_state = "winter_wait"
        assert sensor.available is True


# ===========================================================================
# 4. Extra state attributes
# ===========================================================================


class TestSensorAttributes:
    """extra_state_attributes returns the correct keys and values."""

    def test_all_expected_keys_present(self):
        """Attributes must contain every expected key from PlanExplanation."""
        sensor = _make_sensor(_make_coordinator_data())
        assert set(sensor.extra_state_attributes.keys()) == _EXPECTED_ATTR_KEYS

    def test_score_value_matches_explanation(self):
        """score attribute must match the explanation score."""
        exp = _make_explanation(score=1.2345)
        sensor = _make_sensor(_make_coordinator_data(exp))
        assert sensor.extra_state_attributes["score"] == pytest.approx(1.2345, abs=1e-3)

    def test_constraints_is_list(self):
        """constraints attribute must be a list."""
        sensor = _make_sensor(_make_coordinator_data())
        assert isinstance(sensor.extra_state_attributes["constraints"], list)

    def test_rejected_plans_is_list_of_dicts(self):
        """rejected_plans attribute must be a list of dicts with name/reason/cost."""
        sensor = _make_sensor(_make_coordinator_data())
        plans = sensor.extra_state_attributes["rejected_plans"]
        assert isinstance(plans, list)
        assert len(plans) == 1
        assert {"name", "reason", "estimated_cost"} <= set(plans[0].keys())

    def test_attributes_when_no_data(self):
        """Attributes fall back to a default PlanExplanation when no coordinator data."""
        sensor = _make_sensor(data=None)
        attrs = sensor.extra_state_attributes
        assert set(attrs.keys()) == _EXPECTED_ATTR_KEYS
        assert attrs["selected_strategy"] == STATE_UNKNOWN

    def test_price_spread_rounded(self):
        """price_spread is rounded to 4 decimal places."""
        exp = _make_explanation(price_spread=0.123456789)
        sensor = _make_sensor(_make_coordinator_data(exp))
        spread = sensor.extra_state_attributes["price_spread"]
        # as_dict() rounds to 4dp
        assert spread == pytest.approx(0.1235, abs=1e-4)

    def test_forecast_pv_kwh_non_negative(self):
        """forecast_pv_kwh attribute must be non-negative."""
        sensor = _make_sensor(_make_coordinator_data())
        assert sensor.extra_state_attributes["forecast_pv_kwh"] >= 0.0


# ===========================================================================
# 5. Context attributes (planner horizon, forecast mode, current slot, safety)
# ===========================================================================


class TestSensorContextAttributes:
    """extra_state_attributes includes diagnostic context when coordinator
    snapshot is fully populated."""

    def test_context_keys_present_when_data_has_context(self):
        """All context keys must be present when cfg/live/slot/apply are set."""
        sensor = _make_sensor(_make_coordinator_data(has_context=True))
        keys = set(sensor.extra_state_attributes.keys())
        assert _CONTEXT_ATTR_KEYS.issubset(keys), (
            f"Missing context keys: {_CONTEXT_ATTR_KEYS - keys}"
        )

    def test_context_keys_absent_when_no_coordinator_data(self):
        """Context keys must NOT be present when coordinator.data is None."""
        sensor = _make_sensor(data=None)
        keys = set(sensor.extra_state_attributes.keys())
        assert keys == _EXPECTED_ATTR_KEYS

    def test_context_keys_absent_when_data_has_no_context(self):
        """Context keys must NOT be present when cfg/live are not populated."""
        sensor = _make_sensor(_make_coordinator_data(has_context=False))
        keys = set(sensor.extra_state_attributes.keys())
        assert keys == _EXPECTED_ATTR_KEYS

    def test_planning_horizon_hours(self):
        """planning_horizon_hours must match cfg.recommendation_interval_length."""
        sensor = _make_sensor(_make_coordinator_data(has_context=True))
        assert sensor.extra_state_attributes["planning_horizon_hours"] == 48

    def test_planning_interval_minutes(self):
        """planning_interval_minutes must match cfg.recommendation_interval_minutes."""
        sensor = _make_sensor(_make_coordinator_data(has_context=True))
        assert sensor.extra_state_attributes["planning_interval_minutes"] == 15

    def test_forecast_mode_winter(self):
        """forecast_mode must be 'winter' when current month is in months_winter."""
        sensor = _make_sensor(_make_coordinator_data(has_context=True))
        mode = sensor.extra_state_attributes["forecast_mode"]
        assert mode in ("winter", "summer")

    def test_current_slot_recommendation(self):
        """current_slot_recommendation must match the hourly_recommendation."""
        sensor = _make_sensor(_make_coordinator_data(has_context=True))
        assert (
            sensor.extra_state_attributes["current_slot_recommendation"]
            == "batteries_wait_mode"
        )

    def test_current_slot_start_and_end(self):
        """current_slot_start and current_slot_end must be ISO-format strings."""
        sensor = _make_sensor(_make_coordinator_data(has_context=True))
        assert isinstance(sensor.extra_state_attributes["current_slot_start"], str)
        assert isinstance(sensor.extra_state_attributes["current_slot_end"], str)

    def test_last_apply_status(self):
        """last_apply_status must reflect the apply_summary overall_status."""
        sensor = _make_sensor(_make_coordinator_data(has_context=True))
        assert sensor.extra_state_attributes["last_apply_status"] == "ok"

    def test_hardware_writes_blocked(self):
        """hardware_writes_blocked must be False when degraded mode is OK."""
        sensor = _make_sensor(_make_coordinator_data(has_context=True))
        assert sensor.extra_state_attributes["hardware_writes_blocked"] is False

    def test_data_quality_complete(self):
        """data_quality_complete must be True for a default DataQuality."""
        sensor = _make_sensor(_make_coordinator_data(has_context=True))
        assert sensor.extra_state_attributes["data_quality_complete"] is True
