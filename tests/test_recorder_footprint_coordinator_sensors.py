"""Regression tests for issue #1099 — coordinator sensors' recorder footprint.

Coordinator-backed HSEM sensors publish large structured attributes (rejected
plans, EV charging slots, cost tables, daily histories, restore blobs) that
change on almost every cycle. HA's recorder stores the recorded attribute
subset in ``state_attributes`` and only reuses a row when that subset is
unchanged, so each of these structures produced a new multi-KB row per cycle.

Rule: a sensor's *recorded* attributes are small scalars only. Lists, dicts,
per-slot timestamps and restore blobs are listed in ``_unrecorded_attributes``
and stay available on the live state (dashboards read ``entity.attributes``
through apexcharts ``data_generator``, which bypasses history).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from homeassistant.const import MATCH_ALL

from custom_components.hsem.coordinator_data import CoordinatorData
from custom_components.hsem.custom_sensors.applier_status_sensor import (
    HSEMApplierStatusSensor,
)
from custom_components.hsem.custom_sensors.daily_plan_vs_actual_sensor import (
    HSEMDailyPlanVsActualSensor,
)
from custom_components.hsem.custom_sensors.ev_charger_current_limit_sensor import (
    HSEMEVChargerCurrentLimitSensor,
    HSEMEVSecondChargerCurrentLimitSensor,
)
from custom_components.hsem.custom_sensors.ev_optimal_charging_plan_sensor import (
    HSEMEVOptimalChargingPlanSensor,
)
from custom_components.hsem.custom_sensors.ev_second_optimal_charging_plan_sensor import (
    HSEMEVSecondOptimalChargingPlanSensor,
)
from custom_components.hsem.custom_sensors.ev_second_soc_economics_sensor import (
    HSEMEVSecondSoCEconomicsSensor,
)
from custom_components.hsem.custom_sensors.ev_soc_economics_sensor import (
    HSEMEVSoCEconomicsSensor,
)
from custom_components.hsem.custom_sensors.financial_sensors import (
    HSEMExportIncomeSensor,
    HSEMImportCostSensor,
    HSEMNetGridBalanceSensor,
)
from custom_components.hsem.custom_sensors.forecast_accuracy_sensor import (
    HSEMForecastAccuracySensor,
)
from custom_components.hsem.custom_sensors.next_update_sensor import (
    HSEMNextUpdateSensor,
)
from custom_components.hsem.custom_sensors.ocpp_sensors import (
    HSEMOCPPChargerSessionsSensor,
    HSEMOCPPChargerStatusSensor,
)
from custom_components.hsem.custom_sensors.plan_explanation_sensor import (
    HSEMPlanExplanationSensor,
)
from custom_components.hsem.custom_sensors.prediction_accuracy_sensor import (
    HSEMPredictionAccuracySensor,
)
from custom_components.hsem.custom_sensors.savings_sensor import HSEMSavingsSensor
from custom_components.hsem.custom_sensors.solar_confidence_sensor import (
    HSEMSolarConfidenceSensor,
)
from custom_components.hsem.models.daily_plan_vs_actual_tracker import (
    DailyPlanVsActualTracker,
)
from custom_components.hsem.models.daily_record import DailyRecord
from custom_components.hsem.models.financial_tracker import (
    FinancialDayEntry,
    FinancialTracker,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.plan_explanation import PlanExplanation
from custom_components.hsem.models.rejected_plan import RejectedPlan
from custom_components.hsem.models.savings_day import SavingsDay
from custom_components.hsem.models.savings_tracker import SavingsTracker
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.planner.ev_planner_models import (
    EVChargingPlan,
    EVChargingSlot,
)
from custom_components.hsem.planner.ev_soc_economics import (
    EVSoCEconomicsPoint,
    EVSoCEconomicsResult,
)
from custom_components.hsem.utils.forecast_tracker import ForecastTracker
from custom_components.hsem.utils.inverter_verify import (
    ApplyResult,
    ApplyStatus,
    CycleApplySummary,
)
from custom_components.hsem.utils.prediction_tracker import PredictionTracker
from custom_components.hsem.utils.recommendations import Recommendations
from custom_components.hsem.utils.solar_corrector import SolarForecastCorrector

_NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)

#: Recorded attributes must stay well below this after serialisation. The
#: plan explanation's ~30 scalar summaries are the largest (~1.4 KB).
_MAX_RECORDED_BYTES = 2048


def _entry() -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.options = {}
    entry.data = {}
    return entry


def _coordinator(data: CoordinatorData, **attributes: Any) -> MagicMock:
    coordinator = MagicMock()
    coordinator.last_update_success = True
    coordinator.data = data
    for name, value in attributes.items():
        setattr(coordinator, name, value)
    return coordinator


def _recorded(sensor: Any) -> dict[str, Any]:
    """Return the attribute subset HA's recorder would store for *sensor*."""
    attributes = sensor.extra_state_attributes or {}
    unrecorded = type(sensor)._unrecorded_attributes
    if MATCH_ALL in unrecorded:
        return {}
    return {k: v for k, v in attributes.items() if k not in unrecorded}


def _rec(start: datetime, ev_power_w: float = 7400.0) -> HourlyRecommendation:
    rec = HourlyRecommendation(
        start=start,
        end=start + timedelta(minutes=15),
        recommendation=Recommendations.BatteriesWaitMode.value,
        avg_house_consumption_kwh=0.3,
        avg_house_consumption_1d_kwh=0.3,
        avg_house_consumption_3d_kwh=0.3,
        avg_house_consumption_7d_kwh=0.3,
        avg_house_consumption_14d_kwh=0.3,
        batteries_charged_kwh=0.0,
        batteries_discharged_kwh=0.0,
        estimated_battery_capacity_kwh=5.0,
        estimated_battery_soc_pct=50.0,
        estimated_cost_currency=0.0,
        estimated_net_consumption_kwh=0.3,
        export_price=0.5,
        grid_export_kwh=0.0,
        grid_import_kwh=0.3,
        import_price=1.5,
        solcast_pv_estimate_kwh=0.0,
    )
    rec.ev_charger_calculated_power = ev_power_w
    rec.ev_second_charger_calculated_power = ev_power_w
    return rec


def _ev_plan() -> EVChargingPlan:
    slots = [
        EVChargingSlot(
            start=_NOW + timedelta(minutes=15 * i),
            end=_NOW + timedelta(minutes=15 * (i + 1)),
            estimated_charged_kwh=1.7,
            ac_load_kwh=1.85,
        )
        for i in range(32)
    ]
    return EVChargingPlan(
        state="charging",
        ev_connected=True,
        current_soc_pct=40.0,
        battery_capacity_kwh=60.0,
        charger_power_kw=7.4,
        total_kwh_needed=24.0,
        deadline=_NOW + timedelta(hours=8),
        charging_slots=slots,
        planned_load_by_slot={s.start.isoformat(): s.ac_load_kwh for s in slots},
        data_quality={"effective_deadline": _NOW.isoformat()},
    )


def _economics() -> EVSoCEconomicsResult:
    return EVSoCEconomicsResult(
        state="ready",
        current_soc_pct=40.0,
        points=[
            EVSoCEconomicsPoint(
                target_soc_pct=float(target),
                deadline_label=label,
                deadline=_NOW + timedelta(hours=hours),
                total_cost=12.5,
            )
            for target in range(50, 101, 10)
            for label, hours in (("07:00", 19), ("08:00", 20), ("16:00", 28))
        ],
    )


def _explanation() -> PlanExplanation:
    return PlanExplanation(
        winner_name="milp",
        selected_strategy="milp",
        summary="Charge from grid overnight, discharge at the evening peak.",
        constraints=["winter_month", "excess_export_enabled"],
        rejected_plans=[
            RejectedPlan(name=f"candidate_{i}", reason="higher cost") for i in range(8)
        ],
    )


def _plan_explanation_sensor() -> Any:
    data = CoordinatorData(
        cfg=SensorConfig(),
        live=LiveState(),
        plan_explanation=_explanation(),
        hourly_recommendation=_rec(_NOW),
    )
    return HSEMPlanExplanationSensor(_entry(), _coordinator(data, _ml_predictor=None))


def _ev_plan_sensor(sensor_cls: Any, field: str) -> Any:
    data = CoordinatorData()
    setattr(data, field, _ev_plan())
    return sensor_cls(_entry(), _coordinator(data))


def _economics_sensor(sensor_cls: Any, field: str) -> Any:
    data = CoordinatorData()
    setattr(data, field, _economics())
    return sensor_cls(_entry(), _coordinator(data))


def _current_limit_sensor(sensor_cls: Any) -> Any:
    recs = [_rec(_NOW + timedelta(minutes=15 * i)) for i in range(48)]
    data = CoordinatorData(
        cfg=SensorConfig(),
        live=LiveState(),
        hourly_recommendations=recs,
        hourly_recommendation=recs[0],
    )
    return sensor_cls(_entry(), _coordinator(data))


def _solar_confidence_sensor() -> Any:
    corrector = SolarForecastCorrector()
    for hour in range(6, 20):
        corrector.update_hour(hour, 1.0, 0.9)
        corrector.update_residual(1.0, 0.9)
    data = CoordinatorData(solar_hour_factors=dict(corrector.hour_factors))
    return HSEMSolarConfidenceSensor(
        _entry(), _coordinator(data, _solar_corrector=corrector)
    )


def _forecast_accuracy_sensor() -> Any:
    tracker = ForecastTracker()
    for i in range(24):
        tracker.get_or_create_record(
            _NOW + timedelta(minutes=15 * i), _NOW + timedelta(minutes=15 * (i + 1))
        )
    return HSEMForecastAccuracySensor(
        _entry(), _coordinator(CoordinatorData(), _forecast_tracker=tracker)
    )


def _prediction_accuracy_sensor() -> Any:
    tracker = PredictionTracker()
    tracker.action_mix = {"charge": 0.2, "discharge": 0.1, "idle": 0.7}
    return HSEMPredictionAccuracySensor(
        _entry(), _coordinator(CoordinatorData(), _prediction_tracker=tracker)
    )


def _savings_sensor() -> Any:
    tracker = SavingsTracker()
    for i in range(90):
        day = (_NOW - timedelta(days=i)).date().isoformat()
        tracker.daily[day] = SavingsDay(date=day, actual_savings=1.2)
    return HSEMSavingsSensor(
        _entry(), _coordinator(CoordinatorData(), _savings_tracker=tracker)
    )


def _daily_plan_vs_actual_sensor() -> Any:
    tracker = DailyPlanVsActualTracker()
    tracker.today = _NOW.date().isoformat()
    tracker.history = [
        DailyRecord(date=(_NOW - timedelta(days=i)).date().isoformat())
        for i in range(7, 0, -1)
    ]
    return HSEMDailyPlanVsActualSensor(
        _entry(), _coordinator(CoordinatorData(), _daily_tracker=tracker)
    )


def _financial_sensor(sensor_cls: Any) -> Any:
    tracker = FinancialTracker()
    tracker.today = _NOW.date().isoformat()
    for i in range(1, 366):
        day = (_NOW - timedelta(days=i)).date().isoformat()
        tracker.daily_log[day] = FinancialDayEntry(
            date=day, import_cost=3.2, export_income=1.1
        )
    return sensor_cls(
        _entry(), _coordinator(CoordinatorData(), _financial_tracker=tracker)
    )


def _applier_status_sensor() -> Any:
    summary = CycleApplySummary(
        results=[
            ApplyResult(
                entity_id=f"number.batteries_setting_{i}",
                desired="1",
                actual="0",
                status=ApplyStatus.FAILED,
                attempts=3,
                error_message="timeout",
            )
            for i in range(6)
        ]
    )
    return HSEMApplierStatusSensor(
        _entry(), _coordinator(CoordinatorData(apply_summary=summary))
    )


def _ocpp_sessions_sensor() -> Any:
    sessions = [{"transaction_id": i, "energy_kwh": 12.3} for i in range(40)]
    return HSEMOCPPChargerSessionsSensor(
        _entry(), _coordinator(CoordinatorData(ocpp_sessions=sessions))
    )


def _next_update_sensor() -> Any:
    data = CoordinatorData(
        cfg=SensorConfig(),
        last_updated=_NOW.isoformat(),
        next_update=(_NOW + timedelta(minutes=5)).isoformat(),
    )
    return HSEMNextUpdateSensor(_entry(), _coordinator(data))


_SENSOR_FACTORIES: dict[str, Any] = {
    "plan_explanation": _plan_explanation_sensor,
    "ev_plan": lambda: _ev_plan_sensor(
        HSEMEVOptimalChargingPlanSensor, "ev_charging_plan"
    ),
    "ev_second_plan": lambda: _ev_plan_sensor(
        HSEMEVSecondOptimalChargingPlanSensor, "ev_second_charging_plan"
    ),
    "ev_soc_economics": lambda: _economics_sensor(
        HSEMEVSoCEconomicsSensor, "ev_soc_economics"
    ),
    "ev_second_soc_economics": lambda: _economics_sensor(
        HSEMEVSecondSoCEconomicsSensor, "ev_second_soc_economics"
    ),
    "ev_current_limit": lambda: _current_limit_sensor(HSEMEVChargerCurrentLimitSensor),
    "ev_second_current_limit": lambda: _current_limit_sensor(
        HSEMEVSecondChargerCurrentLimitSensor
    ),
    "solar_confidence": _solar_confidence_sensor,
    "forecast_accuracy": _forecast_accuracy_sensor,
    "prediction_accuracy": _prediction_accuracy_sensor,
    "savings": _savings_sensor,
    "daily_plan_vs_actual": _daily_plan_vs_actual_sensor,
    "export_income": lambda: _financial_sensor(HSEMExportIncomeSensor),
    "import_cost": lambda: _financial_sensor(HSEMImportCostSensor),
    "net_grid_balance": lambda: _financial_sensor(HSEMNetGridBalanceSensor),
    "applier_status": _applier_status_sensor,
    "ocpp_sessions": _ocpp_sessions_sensor,
    "next_update": _next_update_sensor,
}


class TestRecordedAttributesAreSmallScalars:
    """Only small scalar attributes reach the recorder database."""

    @pytest.mark.parametrize("name", sorted(_SENSOR_FACTORIES))
    def test_no_structured_values_are_recorded(self, name: str) -> None:
        sensor = _SENSOR_FACTORIES[name]()
        assert sensor.extra_state_attributes, "fixture must publish attributes"

        recorded = _recorded(sensor)

        structured = {k for k, v in recorded.items() if isinstance(v, list | dict)}
        assert structured == set()
        assert not any(k.startswith("_") for k in recorded)

    @pytest.mark.parametrize("name", sorted(_SENSOR_FACTORIES))
    def test_recorded_attributes_are_bounded(self, name: str) -> None:
        recorded = _recorded(_SENSOR_FACTORIES[name]())

        assert len(json.dumps(recorded, default=str)) < _MAX_RECORDED_BYTES

    @pytest.mark.parametrize("name", sorted(_SENSOR_FACTORIES))
    def test_unrecorded_attributes_stay_on_the_live_state(self, name: str) -> None:
        """Dashboards and templates still see every attribute."""
        sensor = _SENSOR_FACTORIES[name]()
        attributes = sensor.extra_state_attributes

        unrecorded = type(sensor)._unrecorded_attributes - {MATCH_ALL}
        assert unrecorded & set(attributes), "an excluded attribute must be published"


class TestRecordedSizeDoesNotGrowWithStructures:
    """More candidates, slots or history days never enlarge recorded rows."""

    def test_plan_explanation_is_independent_of_candidate_count(self) -> None:
        small = _plan_explanation_sensor()
        large = _plan_explanation_sensor()
        large.coordinator.data.plan_explanation.rejected_plans *= 10

        assert json.dumps(_recorded(small), default=str) == json.dumps(
            _recorded(large), default=str
        )

    def test_ev_plan_is_independent_of_slot_count(self) -> None:
        sensor = _ev_plan_sensor(HSEMEVOptimalChargingPlanSensor, "ev_charging_plan")
        before = _recorded(sensor)
        sensor.coordinator.data.ev_charging_plan.charging_slots *= 4

        assert _recorded(sensor) == before


class TestStatusSensorsExcludeDynamicKeys:
    """Sensors with dynamic attribute keys exclude every attribute."""

    def test_ocpp_status_records_no_attributes(self) -> None:
        assert MATCH_ALL in HSEMOCPPChargerStatusSensor._unrecorded_attributes


class TestRestoreBlobsAreNotRecorded:
    """Restore payloads are persisted by RestoreEntity, never the recorder."""

    @pytest.mark.parametrize(
        ("sensor_cls", "key"),
        [
            (HSEMSolarConfidenceSensor, "_solar_corrector_data"),
            (HSEMForecastAccuracySensor, "_forecast_tracker_data"),
        ],
    )
    def test_blob_is_unrecorded(self, sensor_cls: Any, key: str) -> None:
        assert key in sensor_cls._unrecorded_attributes
