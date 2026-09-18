"""Tests for the tracker-backed diagnostic sensors.

Savings, daily plan-vs-actual, financial, prediction-accuracy,
forecast-accuracy, and effective-discharge-floor sensors all follow the same
contract: read a coordinator-owned tracker (or the published snapshot), fall
back to the restored state before the first cycle, never poll, and expose
their tracker's own dict as attributes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

from custom_components.hsem.coordinator_data import CoordinatorData
from custom_components.hsem.custom_sensors.daily_plan_vs_actual_sensor import (
    HSEMDailyPlanVsActualSensor,
)
from custom_components.hsem.custom_sensors.effective_discharge_floor_sensor import (
    HSEMEffectiveDischargeFloorSensor,
)
from custom_components.hsem.custom_sensors.financial_sensors import (
    HSEMExportIncomeSensor,
    HSEMImportCostSensor,
    HSEMNetGridBalanceSensor,
)
from custom_components.hsem.custom_sensors.forecast_accuracy_sensor import (
    HSEMForecastAccuracySensor,
)
from custom_components.hsem.custom_sensors.prediction_accuracy_sensor import (
    HSEMPredictionAccuracySensor,
)
from custom_components.hsem.custom_sensors.savings_sensor import HSEMSavingsSensor
from custom_components.hsem.entity import HSEMCoordinatorEntity
from custom_components.hsem.models.daily_plan_vs_actual_tracker import (
    DailyPlanVsActualTracker,
)
from custom_components.hsem.models.financial_tracker import FinancialTracker
from custom_components.hsem.models.savings_tracker import SavingsTracker
from custom_components.hsem.utils.forecast_summary import ForecastErrorSummary
from custom_components.hsem.utils.forecast_tracker import ForecastTracker
from custom_components.hsem.utils.prediction_tracker import PredictionTracker

_ENTRY_ID = "test_entry"


def _entry() -> MagicMock:
    """Return a minimal config entry."""
    entry = MagicMock()
    entry.entry_id = _ENTRY_ID
    entry.options = {}
    entry.data = {}
    return entry


def _coordinator(*, with_data: bool = True) -> Any:
    """Return a coordinator stand-in owning real trackers.

    The sensors only read ``data``, ``last_update_success``, and the tracker
    attributes, so a mock carrying real tracker instances exercises the real
    tracker maths without needing a running coordinator.
    """
    coordinator = MagicMock()
    coordinator.last_update_success = True
    coordinator.data = CoordinatorData() if with_data else None
    coordinator._savings_tracker = SavingsTracker()
    coordinator._daily_tracker = DailyPlanVsActualTracker()
    coordinator._financial_tracker = FinancialTracker()
    coordinator._prediction_tracker = PredictionTracker()
    coordinator._forecast_tracker = ForecastTracker()
    return coordinator


def _sensor(sensor_cls: Any, coordinator: Any) -> Any:
    """Construct a sensor bound to *coordinator*."""
    return sensor_cls(_entry(), coordinator)


async def _restore(
    sensor: Any, state: str | None, attributes: dict[str, Any] | None = None
) -> None:
    """Run ``async_added_to_hass`` with *state* as the restored state."""
    restored = (
        None
        if state is None
        else MagicMock(state=state, attributes=dict(attributes or {}))
    )
    sensor.async_get_last_state = AsyncMock(return_value=restored)
    with patch.object(HSEMCoordinatorEntity, "async_added_to_hass", AsyncMock()):
        await sensor.async_added_to_hass()


_ALL_SENSORS = [
    HSEMSavingsSensor,
    HSEMDailyPlanVsActualSensor,
    HSEMPredictionAccuracySensor,
    HSEMForecastAccuracySensor,
    HSEMEffectiveDischargeFloorSensor,
    HSEMExportIncomeSensor,
    HSEMImportCostSensor,
    HSEMNetGridBalanceSensor,
]


class TestSharedEntityContract:
    """Every tracker-backed diagnostic sensor behaves the same way."""

    @pytest.mark.parametrize("sensor_cls", _ALL_SENSORS, ids=lambda c: c.__name__)
    def test_is_push_driven_with_a_stable_unique_id(self, sensor_cls: Any) -> None:
        """Sensors never poll and key themselves on the config entry."""
        sensor = _sensor(sensor_cls, _coordinator())

        assert sensor.should_poll is False
        assert sensor.unique_id is not None
        assert _ENTRY_ID in sensor.unique_id
        assert sensor.entity_id.startswith("sensor.")

    @pytest.mark.parametrize("sensor_cls", _ALL_SENSORS, ids=lambda c: c.__name__)
    def test_unavailable_before_any_cycle_or_restore(self, sensor_cls: Any) -> None:
        """A cold start with no restored state reports unavailable."""
        coordinator = _coordinator(with_data=False)
        coordinator.last_update_success = False
        sensor = _sensor(sensor_cls, coordinator)

        assert sensor.available is False

    @pytest.mark.parametrize("sensor_cls", _ALL_SENSORS, ids=lambda c: c.__name__)
    @pytest.mark.asyncio
    async def test_restored_state_makes_the_sensor_available(
        self, sensor_cls: Any
    ) -> None:
        """A restored state carries the sensor until the first cycle lands."""
        coordinator = _coordinator(with_data=False)
        coordinator.last_update_success = False
        sensor = _sensor(sensor_cls, coordinator)

        await _restore(sensor, "1.5")

        assert sensor.available is True

    @pytest.mark.parametrize("sensor_cls", _ALL_SENSORS, ids=lambda c: c.__name__)
    @pytest.mark.parametrize("state", [STATE_UNAVAILABLE, STATE_UNKNOWN, None])
    @pytest.mark.asyncio
    async def test_unusable_restored_states_are_ignored(
        self, sensor_cls: Any, state: str | None
    ) -> None:
        """An unavailable/unknown/missing previous state is not restored."""
        coordinator = _coordinator(with_data=False)
        coordinator.last_update_success = False
        sensor = _sensor(sensor_cls, coordinator)

        await _restore(sensor, state)

        assert sensor.available is False


class TestSavingsSensor:
    """Savings state is today's actual savings; attributes are the full dict."""

    def test_reports_todays_actual_savings(self) -> None:
        """The tracker's rounded ``today_actual`` becomes the state."""
        coordinator = _coordinator()
        coordinator._savings_tracker.actual_savings = 12.3456
        coordinator._savings_tracker.accumulate(
            export_revenue_delta=1.2345,
            charge_savings_delta=0.0,
            baseline_cost_delta=0.0,
            switch_on=True,
        )
        sensor = _sensor(HSEMSavingsSensor, coordinator)

        assert sensor.native_value == pytest.approx(
            round(coordinator._savings_tracker.today_actual, 3)
        )
        assert sensor.extra_state_attributes == (coordinator._savings_tracker.as_dict())

    @pytest.mark.asyncio
    async def test_falls_back_to_the_restored_state(self) -> None:
        """Without a tracker the restored number is reported."""
        coordinator = _coordinator()
        del coordinator._savings_tracker
        sensor = _sensor(HSEMSavingsSensor, coordinator)
        await _restore(sensor, "7.5")

        assert sensor.native_value == pytest.approx(7.5)
        assert sensor.extra_state_attributes is None

    @pytest.mark.asyncio
    async def test_unparseable_restored_state_is_dropped(self) -> None:
        """A non-numeric previous state cannot become a numeric sensor value."""
        coordinator = _coordinator()
        del coordinator._savings_tracker
        sensor = _sensor(HSEMSavingsSensor, coordinator)
        await _restore(sensor, "not a number")

        assert sensor.native_value is None


class TestDailyPlanVsActualSensor:
    """The daily sensor reports today's net cost against the plan."""

    def test_reports_todays_net_cost(self) -> None:
        """State is the rounded actual net cost of today's record."""
        coordinator = _coordinator()
        tracker = coordinator._daily_tracker
        tracker.actual.grid_import_cost = 12.3456
        tracker.actual.grid_export_rev = 2.0
        sensor = _sensor(HSEMDailyPlanVsActualSensor, coordinator)

        assert sensor.native_value == pytest.approx(
            round(tracker.get_today_record().net_cost_actual, 3)
        )
        assert sensor.extra_state_attributes == tracker.as_sensor_attributes()

    @pytest.mark.asyncio
    async def test_falls_back_to_the_restored_state(self) -> None:
        """Without a tracker the restored number is reported."""
        coordinator = _coordinator()
        del coordinator._daily_tracker
        sensor = _sensor(HSEMDailyPlanVsActualSensor, coordinator)
        await _restore(sensor, "3.25")

        assert sensor.native_value == pytest.approx(3.25)
        assert sensor.extra_state_attributes is None

    @pytest.mark.asyncio
    async def test_non_numeric_restored_state_is_passed_through(self) -> None:
        """A non-numeric previous state is reported as-is."""
        coordinator = _coordinator()
        del coordinator._daily_tracker
        sensor = _sensor(HSEMDailyPlanVsActualSensor, coordinator)
        await _restore(sensor, "unavailable_earlier")

        assert sensor.native_value == "unavailable_earlier"


class TestFinancialSensors:
    """The three financial sensors read one shared tracker."""

    @staticmethod
    def _coordinator_with_totals() -> Any:
        """Return a coordinator whose financial tracker has totals."""
        coordinator = _coordinator()
        coordinator._financial_tracker = FinancialTracker(
            import_cost_total=40.1234, export_income_total=15.5678
        )
        return coordinator

    @pytest.mark.parametrize(
        ("sensor_cls", "expected"),
        [
            pytest.param(HSEMExportIncomeSensor, 15.568, id="export_income"),
            pytest.param(HSEMImportCostSensor, 40.123, id="import_cost"),
            pytest.param(HSEMNetGridBalanceSensor, -24.556, id="net_balance"),
        ],
    )
    def test_each_sensor_reports_its_own_total(
        self, sensor_cls: Any, expected: float
    ) -> None:
        """Income, cost, and their net balance come from the same tracker."""
        sensor = _sensor(sensor_cls, self._coordinator_with_totals())

        assert sensor.native_value == pytest.approx(expected)

    def test_period_rollups_are_exposed_as_attributes(self) -> None:
        """All three sensors share the tracker's period rollup attributes."""
        coordinator = self._coordinator_with_totals()
        sensor = _sensor(HSEMExportIncomeSensor, coordinator)

        assert sensor.extra_state_attributes == (
            coordinator._financial_tracker.as_sensor_attributes()
        )

    def test_no_tracker_means_no_attributes(self) -> None:
        """Before the tracker is initialised there is nothing to expose."""
        coordinator = _coordinator()
        del coordinator._financial_tracker
        sensor = _sensor(HSEMExportIncomeSensor, coordinator)

        assert sensor.extra_state_attributes is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "sensor_cls",
        [HSEMExportIncomeSensor, HSEMImportCostSensor, HSEMNetGridBalanceSensor],
        ids=lambda c: c.__name__,
    )
    async def test_falls_back_to_the_restored_state(self, sensor_cls: Any) -> None:
        """Without a tracker the restored number is reported."""
        coordinator = _coordinator()
        del coordinator._financial_tracker
        sensor = _sensor(sensor_cls, coordinator)
        await _restore(sensor, "9.75")

        assert sensor.native_value == pytest.approx(9.75)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "sensor_cls",
        [HSEMExportIncomeSensor, HSEMImportCostSensor, HSEMNetGridBalanceSensor],
        ids=lambda c: c.__name__,
    )
    async def test_unparseable_restored_state_is_dropped(self, sensor_cls: Any) -> None:
        """A non-numeric previous state cannot become a currency value."""
        coordinator = _coordinator()
        del coordinator._financial_tracker
        sensor = _sensor(sensor_cls, coordinator)
        await _restore(sensor, "not a number")

        assert sensor.native_value is None


class TestPredictionAccuracySensor:
    """The scorecard reports SoC mean absolute error over 7 days."""

    def test_reports_the_seven_day_soc_mae(self) -> None:
        """A tracker with an MAE publishes it, rounded, plus its scorecard."""
        coordinator = _coordinator()
        tracker = MagicMock(spec=PredictionTracker)
        tracker.soc_mae_7d = 1.23456
        tracker.soc_mae_30d = 2.34567
        tracker.solar_mape = 12.3456
        tracker.load_mae_kwh = 0.23456
        tracker.action_mix = {"charge": 2, "idle": 1}
        tracker.records = [object(), object(), object()]
        coordinator._prediction_tracker = tracker
        sensor = _sensor(HSEMPredictionAccuracySensor, coordinator)

        assert sensor.native_value == pytest.approx(1.235)
        attributes = sensor.extra_state_attributes
        assert attributes["soc_mae_7d"] == pytest.approx(1.2346)
        assert attributes["soc_mae_30d"] == pytest.approx(2.3457)
        assert attributes["solar_mape"] == pytest.approx(12.35)
        assert attributes["load_mae_kwh"] == pytest.approx(0.2346)
        assert attributes["action_mix"] == {"charge": 2, "idle": 1}
        assert attributes["records_count"] == 3

    @pytest.mark.asyncio
    async def test_no_records_yet_falls_back_to_restored_state(self) -> None:
        """Before any scored slot the restored value carries the sensor."""
        coordinator = _coordinator()
        tracker = MagicMock(spec=PredictionTracker)
        tracker.soc_mae_7d = None
        coordinator._prediction_tracker = tracker
        sensor = _sensor(HSEMPredictionAccuracySensor, coordinator)
        await _restore(sensor, "2.5")

        assert sensor.native_value == pytest.approx(2.5)

    def test_no_attributes_before_the_first_cycle(self) -> None:
        """Without a published snapshot there are no attributes."""
        coordinator = _coordinator(with_data=False)
        sensor = _sensor(HSEMPredictionAccuracySensor, coordinator)

        assert sensor.extra_state_attributes is None
        assert sensor.native_value is None

    def test_missing_tracker_reports_nothing(self) -> None:
        """A coordinator without the scorecard yields no attributes."""
        coordinator = _coordinator()
        del coordinator._prediction_tracker
        sensor = _sensor(HSEMPredictionAccuracySensor, coordinator)

        assert sensor.extra_state_attributes is None
        assert sensor.native_value is None

    @pytest.mark.asyncio
    async def test_non_numeric_restored_state_is_passed_through(self) -> None:
        """A non-numeric previous state is reported unchanged."""
        coordinator = _coordinator(with_data=False)
        sensor = _sensor(HSEMPredictionAccuracySensor, coordinator)
        await _restore(sensor, "warming_up")

        assert sensor.native_value == "warming_up"

    def test_available_follows_the_coordinator(self) -> None:
        """A healthy coordinator makes the sensor available on its own."""
        coordinator = _coordinator()
        sensor = _sensor(HSEMPredictionAccuracySensor, coordinator)

        assert sensor.available is True


class TestForecastAccuracySensor:
    """The forecast sensor reports PV mean absolute error in kWh."""

    def test_reports_the_pv_mae_once_slots_are_finalised(self) -> None:
        """A summary with finalised slots publishes its rounded PV MAE."""
        coordinator = _coordinator()
        tracker = MagicMock(spec=ForecastTracker)
        tracker.summary = ForecastErrorSummary(
            window_slots=4, finalised_count=4, mae_pv_kwh=0.123456
        )
        coordinator._forecast_tracker = tracker
        sensor = _sensor(HSEMForecastAccuracySensor, coordinator)

        assert sensor.native_value == pytest.approx(0.123)
        assert sensor.native_unit_of_measurement == "kWh"

    def test_no_finalised_slots_reports_nothing(self) -> None:
        """An empty window has no error to report."""
        coordinator = _coordinator()
        tracker = MagicMock(spec=ForecastTracker)
        tracker.summary = ForecastErrorSummary(window_slots=0)
        coordinator._forecast_tracker = tracker
        sensor = _sensor(HSEMForecastAccuracySensor, coordinator)

        assert sensor.native_value is None

    @pytest.mark.asyncio
    async def test_before_the_first_cycle_uses_the_restored_state(self) -> None:
        """Without a snapshot the restored number is reported."""
        coordinator = _coordinator(with_data=False)
        sensor = _sensor(HSEMForecastAccuracySensor, coordinator)
        await _restore(sensor, "0.4")

        assert sensor.native_value == pytest.approx(0.4)


def _forecast_tracker_with_finalised_slot() -> ForecastTracker:
    """Return a tracker holding one finalised slot with real actuals."""
    tracker = ForecastTracker()
    start = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
    end = start + timedelta(minutes=15)
    tracker.get_or_create_record(start, end)
    tracker.set_forecasts(
        start=start, pv_kwh=1.0, load_kwh=0.5, observed_at=start - timedelta(hours=1)
    )
    tracker.freeze_forecasts(start)
    tracker.accumulate_power_interval(
        start, end, pv_power_w=2000.0, load_power_w=1000.0, max_gap_seconds=3600.0
    )
    tracker.finalise_past_records(end + timedelta(minutes=1))
    return tracker


class TestForecastAccuracyAttributes:
    """Attributes carry the summary, the latest slot, and restore payload."""

    def test_latest_finalised_slot_is_summarised(self) -> None:
        """The most recent finalised slot is broken out for the UI."""
        coordinator = _coordinator()
        coordinator._forecast_tracker = _forecast_tracker_with_finalised_slot()
        sensor = _sensor(HSEMForecastAccuracySensor, coordinator)

        attributes = sensor.extra_state_attributes
        assert attributes["latest_pv_forecast_kwh"] == pytest.approx(1.0)
        # 15 minutes at 2 kW PV / 1 kW load.
        assert attributes["latest_pv_actual_kwh"] == pytest.approx(0.5)
        assert attributes["latest_load_forecast_kwh"] == pytest.approx(0.5)
        assert attributes["latest_load_actual_kwh"] == pytest.approx(0.25)
        assert attributes["restored_unfinalised_count"] == 0
        assert "_forecast_tracker_data" in attributes

    def test_no_attributes_before_the_first_cycle(self) -> None:
        """Without a published snapshot there is nothing to expose."""
        coordinator = _coordinator(with_data=False)
        sensor = _sensor(HSEMForecastAccuracySensor, coordinator)

        assert sensor.extra_state_attributes is None

    def test_missing_tracker_reports_nothing(self) -> None:
        """A coordinator without the tracker yields no state or attributes."""
        coordinator = _coordinator()
        del coordinator._forecast_tracker
        sensor = _sensor(HSEMForecastAccuracySensor, coordinator)

        assert sensor.native_value is None
        assert sensor.extra_state_attributes is None

    @pytest.mark.asyncio
    async def test_tracker_state_is_restored_from_attributes(self) -> None:
        """The persisted tracker payload is loaded back on restart."""
        source = _forecast_tracker_with_finalised_slot()
        payload = source.to_persistence_dict(
            now=datetime(2026, 6, 1, 10, 20, tzinfo=UTC), max_records=24
        )
        coordinator = _coordinator()
        coordinator._forecast_tracker = ForecastTracker()
        sensor = _sensor(HSEMForecastAccuracySensor, coordinator)

        await _restore(sensor, "0.25", {"_forecast_tracker_data": payload})

        assert coordinator._forecast_tracker.records

    @pytest.mark.asyncio
    async def test_malformed_tracker_payload_restores_nothing(self) -> None:
        """A payload that is not a record mapping is ignored."""
        coordinator = _coordinator()
        sensor = _sensor(HSEMForecastAccuracySensor, coordinator)

        await _restore(sensor, "0.25", {"_forecast_tracker_data": "not a dict"})

        assert coordinator._forecast_tracker.records == []

    @pytest.mark.asyncio
    async def test_failed_restore_is_logged_not_raised(self) -> None:
        """A tracker that raises while loading must not break setup."""
        coordinator = _coordinator()
        coordinator._forecast_tracker.load_from_dict = MagicMock(  # type: ignore[method-assign]  # force failure
            side_effect=RuntimeError("corrupt")
        )
        sensor = _sensor(HSEMForecastAccuracySensor, coordinator)

        with patch(
            "custom_components.hsem.custom_sensors.forecast_accuracy_sensor._LOGGER"
        ) as logger:
            await _restore(sensor, "0.25", {"_forecast_tracker_data": {"records": []}})

        logger.exception.assert_called_once()

    @pytest.mark.asyncio
    async def test_unfinalised_restored_slots_are_reported(self) -> None:
        """Slots still open at shutdown are counted after a restore."""
        source = _forecast_tracker_with_finalised_slot()
        open_start = datetime(2026, 6, 1, 11, 0, tzinfo=UTC)
        source.get_or_create_record(open_start, open_start + timedelta(minutes=15))
        payload = source.to_persistence_dict(now=open_start, max_records=24)
        coordinator = _coordinator()
        sensor = _sensor(HSEMForecastAccuracySensor, coordinator)

        await _restore(sensor, "0.25", {"_forecast_tracker_data": payload})

        assert coordinator._forecast_tracker.restored_unfinalised_keys

    def test_nothing_to_report_before_the_first_cycle(self) -> None:
        """No snapshot and no restored state means no value at all."""
        coordinator = _coordinator(with_data=False)
        sensor = _sensor(HSEMForecastAccuracySensor, coordinator)

        assert sensor.native_value is None

    @pytest.mark.asyncio
    async def test_restore_without_tracker_payload_is_a_noop(self) -> None:
        """An old restored state without the payload just sets the value."""
        coordinator = _coordinator()
        sensor = _sensor(HSEMForecastAccuracySensor, coordinator)

        await _restore(sensor, "0.25")

        assert coordinator._forecast_tracker.records == []


class TestEffectiveDischargeFloorSensor:
    """The floor sensor distinguishes disabled, unavailable, and a live floor."""

    def test_reports_the_floor_and_its_diagnostics(self) -> None:
        """A computed floor is published to one decimal with its diagnostics."""
        coordinator = _coordinator()
        coordinator.data = CoordinatorData(
            effective_discharge_floor_pct=17.25,
            effective_discharge_floor_diag={
                "safety_margin": 2.0,
                "bridge_duration_hours": 6.0,
                "reserve_kwh": 3.5,
                "next_refill_slot": "2026-06-01T10:00:00+00:00",
                "refill_type": "solar",
            },
        )
        sensor = _sensor(HSEMEffectiveDischargeFloorSensor, coordinator)

        assert sensor.state == "17.2"
        attributes = sensor.extra_state_attributes
        assert attributes["enabled"] is True
        assert attributes["effective_floor_pct"] == pytest.approx(17.25)
        assert attributes["reserve_kwh"] == pytest.approx(3.5)
        assert attributes["refill_type"] == "solar"

    def test_disabled_floor_is_reported_as_disabled(self) -> None:
        """A published snapshot without a floor means the feature is off."""
        coordinator = _coordinator()
        sensor = _sensor(HSEMEffectiveDischargeFloorSensor, coordinator)

        assert sensor.state == "disabled"
        assert sensor.extra_state_attributes["enabled"] is False

    def test_no_snapshot_is_unavailable(self) -> None:
        """Before the first cycle the sensor reports unavailable."""
        coordinator = _coordinator(with_data=False)
        sensor = _sensor(HSEMEffectiveDischargeFloorSensor, coordinator)

        assert sensor.state == STATE_UNAVAILABLE
        assert sensor.extra_state_attributes == {
            "enabled": False,
            "effective_floor_pct": None,
            "safety_margin": None,
            "bridge_duration_hours": None,
            "reserve_kwh": None,
            "next_refill_slot": None,
        }

    @pytest.mark.asyncio
    async def test_restored_floor_survives_until_the_first_cycle(self) -> None:
        """A restored floor is reported while the coordinator warms up."""
        coordinator = _coordinator(with_data=False)
        sensor = _sensor(HSEMEffectiveDischargeFloorSensor, coordinator)
        await _restore(sensor, "21.5")

        assert sensor.state == "21.5"
