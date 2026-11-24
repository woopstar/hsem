"""Tests for ``reason_kind`` and ``unknown_reason`` on the sensors (issue #1482)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

from custom_components.hsem.custom_sensors.battery_efficiency_sensor import (
    HSEMBatteryEfficiencySensor,
)
from custom_components.hsem.custom_sensors.house_consumption_power_sensor import (
    HSEMHouseConsumptionPowerSensor,
)
from custom_components.hsem.custom_sensors.prediction_accuracy_sensor import (
    HSEMPredictionAccuracySensor,
)
from custom_components.hsem.models.energy_tracker import EnergyTracker
from custom_components.hsem.models.performance_record import (
    REASON_ENERGY_BALANCE_RESIDUAL,
    REASON_INSUFFICIENT_COVERAGE,
)
from custom_components.hsem.sensor import async_setup_entry
from custom_components.hsem.unknown_sensor_monitor import UnknownSensorMonitor
from custom_components.hsem.utils.efficiency_learner import (
    STATUS_IMPLAUSIBLE,
    STORAGE_VERSION,
    EfficiencyLearner,
    EfficiencyStep,
)
from custom_components.hsem.utils.forecast_tracker import ForecastTracker
from custom_components.hsem.utils.prediction_tracker import PredictionTracker
from custom_components.hsem.utils.solar_corrector import SolarForecastCorrector
from tests.coordinator_fixtures import make_real_coordinator
from tests.custom_sensors.test_forecast_accuracy_sensor import (
    _sensor as _forecast_sensor,
)
from tests.custom_sensors.test_monthly_energy_sensors import (
    _MODULE as _MONTHLY_MODULE,
    _NOW,
    _recorded,
    _sensors as _monthly_sensors,
)
from tests.custom_sensors.test_performance_sensors import (
    _MODULE as _PERFORMANCE_MODULE,
    _score,
    _sensors as _performance_sensors,
)
from tests.custom_sensors.test_solar_confidence_sensor import (
    _sensor as _solar_sensor,
)
from tests.sensors.test_tracker_diagnostic_sensors import (
    _coordinator as _tracker_coordinator,
    _sensor as _tracker_sensor,
)
from tests.test_coordinator_failed_retry import _RetryHarness
from tests.test_entity_gating_config import _collector, _make_sensor_config_entry


def _efficiency_sensor(learner: EfficiencyLearner | None) -> Any:
    coordinator = MagicMock()
    coordinator._efficiency_learner = learner
    coordinator.data = None
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    return HSEMBatteryEfficiencySensor(entry, coordinator)


def _learned(charge_stored: float = 10.0) -> EfficiencyLearner:
    """Return a learner with 20 kWh measured per direction.

    Charge stores *charge_stored* of 12.5 kWh measured; discharge delivers
    9 of 10 kWh stored.  With the default that is 80 % x 90 % = 72 %.
    """
    return EfficiencyLearner(
        charge_total=EfficiencyStep(40, 12.5, charge_stored),
        discharge_total=EfficiencyStep(40, 9.0, 10.0),
    )


class TestMonthlyEnergySensors:
    def test_a_value_has_no_kind(self) -> None:
        sensors = _monthly_sensors(_recorded())

        with patch(f"{_MONTHLY_MODULE}.hsem_now", return_value=_NOW):
            sensor = sensors["self_sufficiency"]
            assert sensor.native_value is not None
            assert (sensor.extra_state_attributes or {})["reason_kind"] is None
            assert sensor.unknown_reason is None

    def test_each_sensor_gives_the_kind_of_its_own_reason(self) -> None:
        sensors = _monthly_sensors(EnergyTracker())

        with patch(f"{_MONTHLY_MODULE}.hsem_now", return_value=_NOW):
            waiting = sensors["round_trip_loss"]
            assert waiting.unknown_reason == "no_data"
            assert (waiting.extra_state_attributes or {})["reason_kind"] == "waiting"
            optional = sensors["ac_round_trip_loss"]
            assert optional.unknown_reason == "ac_battery_power_not_configured"
            assert (optional.extra_state_attributes or {})["reason_kind"] == (
                "not_configured"
            )

    def test_without_a_tracker_there_is_no_reason(self) -> None:
        sensors = _monthly_sensors(None)

        assert sensors["round_trip_loss"].unknown_reason is None


class TestPerformanceSensors:
    def test_a_value_has_no_kind(self) -> None:
        sensors = _performance_sensors(_score())

        with patch(f"{_PERFORMANCE_MODULE}.hsem_now", return_value=_NOW):
            for sensor in sensors.values():
                assert sensor.unknown_reason is None
                assert (sensor.extra_state_attributes or {})["reason_kind"] is None

    def test_before_the_first_evaluation_every_sensor_waits(self) -> None:
        sensors = _performance_sensors(None)

        with patch(f"{_PERFORMANCE_MODULE}.hsem_now", return_value=_NOW):
            for sensor in sensors.values():
                assert sensor.unknown_reason == "not_evaluated"
                assert (sensor.extra_state_attributes or {})["reason_kind"] == (
                    "waiting"
                )

    @pytest.mark.parametrize(
        ("reason", "kind"),
        [
            (REASON_INSUFFICIENT_COVERAGE, "waiting"),
            (REASON_ENERGY_BALANCE_RESIDUAL, "fault"),
        ],
    )
    def test_an_unscored_window_gives_every_sensor_its_reason(
        self, reason: str, kind: str
    ) -> None:
        sensors = _performance_sensors(
            _score(reason=reason), hsem_ev_planned_load_enabled=True
        )

        with patch(f"{_PERFORMANCE_MODULE}.hsem_now", return_value=_NOW):
            for sensor in sensors.values():
                assert sensor.unknown_reason == reason
                assert (sensor.extra_state_attributes or {})["reason_kind"] == kind

    def test_the_kind_is_the_sensors_own(self) -> None:
        """The split failed: its two sensors are faulty, the others have a value."""
        sensors = _performance_sensors(
            _score(pv_as_realized=None), hsem_ev_planned_load_enabled=True
        )

        with patch(f"{_PERFORMANCE_MODULE}.hsem_now", return_value=_NOW):
            kinds = {
                key: (sensor.extra_state_attributes or {})["reason_kind"]
                for key, sensor in sensors.items()
            }
            reasons = {key: sensor.unknown_reason for key, sensor in sensors.items()}

        assert kinds == {
            "savings_capture": None,
            "actual_benefit": None,
            "missed_opportunity": None,
            "battery_utilisation": "fault",
            "solar_utilisation": "fault",
            "ev_charging_efficiency": "waiting",
        }
        assert reasons["battery_utilisation"] == "breakdown_unsolved"
        assert reasons["ev_charging_efficiency"] == "no_ev_session"

    def test_a_capture_without_a_potential_waits(self) -> None:
        sensors = _performance_sensors(_score(baseline=3.4, potential=0.0))

        with patch(f"{_PERFORMANCE_MODULE}.hsem_now", return_value=_NOW):
            sensor = sensors["savings_capture"]
            assert sensor.native_value is None
            assert sensor.unknown_reason == "potential_below_threshold"
            assert (sensor.extra_state_attributes or {})["reason_kind"] == "waiting"
            # The amounts are known all the same.
            assert sensors["actual_benefit"].unknown_reason is None

    def test_without_a_tracker_there_is_no_reason(self) -> None:
        sensors = _performance_sensors(None, tracker=False)

        assert sensors["savings_capture"].unknown_reason is None


class TestBatteryEfficiencySensor:
    def test_learning_is_waiting(self) -> None:
        sensor = _efficiency_sensor(EfficiencyLearner())

        assert sensor.native_value is None
        assert sensor.unknown_reason == "learning"
        assert sensor.extra_state_attributes["reason_kind"] == "waiting"

    def test_an_implausible_round_trip_is_a_fault(self) -> None:
        sensor = _efficiency_sensor(_learned(charge_stored=20.0))

        assert sensor.native_value is None
        assert sensor.unknown_reason == STATUS_IMPLAUSIBLE
        assert sensor.extra_state_attributes["reason_kind"] == "fault"

    def test_a_value_has_no_kind(self) -> None:
        sensor = _efficiency_sensor(_learned())

        assert sensor.native_value == pytest.approx(72.0)
        assert sensor.unknown_reason is None
        assert sensor.extra_state_attributes["reason_kind"] is None

    def test_without_a_learner_there_is_no_reason(self) -> None:
        assert _efficiency_sensor(None).unknown_reason is None


class TestAccuracySensors:
    """Unknown until the first comparison: they say so instead of nothing."""

    def test_forecast_accuracy_waits_for_a_finalised_slot(self) -> None:
        sensor = _forecast_sensor(tracker=ForecastTracker())

        assert sensor.native_value is None
        assert sensor.unknown_reason == "no_finalised_slots"
        attributes = sensor.extra_state_attributes
        assert attributes is not None
        assert attributes["reason"] == "no_finalised_slots"
        assert attributes["reason_kind"] == "waiting"

    def test_forecast_accuracy_with_a_value_has_no_reason(self) -> None:
        sensor = _forecast_sensor(tracker=ForecastTracker())
        sensor._restored_state = "0.25"
        sensor.coordinator.data = None  # type: ignore[assignment]

        assert sensor.native_value == pytest.approx(0.25)
        assert sensor.unknown_reason is None

    def test_solar_confidence_waits_for_a_factor(self) -> None:
        sensor = _solar_sensor(corrector=SolarForecastCorrector())

        assert sensor.native_value is None
        assert sensor.unknown_reason == "no_solar_factors"
        attributes = sensor.extra_state_attributes
        assert attributes is not None
        assert attributes["reason"] == "no_solar_factors"
        assert attributes["reason_kind"] == "waiting"

    def test_solar_confidence_with_a_value_has_no_reason(self) -> None:
        sensor = _solar_sensor(
            corrector=SolarForecastCorrector(),
            data=SimpleNamespace(solar_hour_factors={10: 0.9, 11: 1.1}),
        )

        assert sensor.native_value == pytest.approx(1.0)
        attributes = sensor.extra_state_attributes
        assert attributes is not None
        assert attributes["reason"] is None
        assert attributes["reason_kind"] is None

    def test_prediction_accuracy_waits_for_a_record(self) -> None:
        sensor = _tracker_sensor(HSEMPredictionAccuracySensor, _tracker_coordinator())

        assert sensor.native_value is None
        assert sensor.unknown_reason == "no_prediction_records"
        attributes = sensor.extra_state_attributes
        assert attributes["reason"] == "no_prediction_records"
        assert attributes["reason_kind"] == "waiting"

    def test_prediction_accuracy_with_a_value_has_no_reason(self) -> None:
        coordinator = _tracker_coordinator()
        tracker = PredictionTracker()
        tracker.soc_mae_7d = 1.5
        coordinator._prediction_tracker = tracker
        sensor = _tracker_sensor(HSEMPredictionAccuracySensor, coordinator)

        assert sensor.unknown_reason is None
        assert sensor.extra_state_attributes["reason_kind"] is None


class TestEfficiencyLearnerSchemaVersion:
    _MODULE = "custom_components.hsem.utils.efficiency_learner"

    @pytest.mark.parametrize("version", [1, None])
    def test_a_file_of_another_version_is_logged_once(self, version: Any) -> None:
        data = _learned().as_dict()
        data["version"] = version
        if version is None:
            del data["version"]

        with patch(f"{self._MODULE}.async_log") as async_log:
            EfficiencyLearner().restore(data)

        assert async_log.call_args_list == [
            call(
                "info",
                "Battery efficiency history of schema version %s is not read "
                "(this version reads %s). The measurements start again.",
                version,
                STORAGE_VERSION,
            )
        ]

    def test_a_file_of_this_version_is_not_logged(self) -> None:
        with patch(f"{self._MODULE}.async_log") as async_log:
            EfficiencyLearner().restore(_learned().as_dict())

        async_log.assert_not_called()


class TestWiring:
    @pytest.mark.asyncio
    async def test_the_platform_hands_its_sensors_to_the_monitor(self) -> None:
        """The hourly consumption sensors, unavailable by design, are left out."""
        config_entry = _make_sensor_config_entry()
        added, add_entities = _collector()

        await async_setup_entry(MagicMock(), config_entry, add_entities)

        watch = config_entry.runtime_data.coordinator.unknown_sensor_monitor.watch
        watch.assert_called_once()
        watched = watch.call_args.args[0]
        assert any(isinstance(e, HSEMBatteryEfficiencySensor) for e in watched)
        assert any(isinstance(e, HSEMHouseConsumptionPowerSensor) for e in added)
        assert not any(isinstance(e, HSEMHouseConsumptionPowerSensor) for e in watched)

    @pytest.mark.asyncio
    async def test_the_coordinator_checks_once_per_cycle(self) -> None:
        harness: Any = _RetryHarness(failures=0, pending_during_cycle=False)
        harness.hass = MagicMock()
        harness._cfg = MagicMock()
        harness._entity_availability_tracker = MagicMock()
        harness.unknown_sensor_monitor = MagicMock()

        await harness.run()
        await harness.run()

        assert (
            harness.unknown_sensor_monitor.check.call_args_list
            == [call(harness.hass)] * 2
        )

    @pytest.mark.asyncio
    async def test_a_coordinator_without_a_monitor_runs_its_cycle(self) -> None:
        harness: Any = _RetryHarness(failures=0, pending_during_cycle=False)
        harness.hass = MagicMock()
        harness._cfg = MagicMock()
        harness._entity_availability_tracker = MagicMock()

        await harness.run()

        assert harness.cycles == 1

    def test_a_new_coordinator_owns_a_monitor(self) -> None:
        coordinator = make_real_coordinator()

        assert isinstance(coordinator.unknown_sensor_monitor, UnknownSensorMonitor)
