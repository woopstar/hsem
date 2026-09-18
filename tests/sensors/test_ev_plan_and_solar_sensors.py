"""Tests for the EV charging-plan sensors and the solar-confidence sensor.

The two EV plan sensors are per-EV views of the planner's ``EVChargingPlan``
and must only ever publish a state from their documented enum. The solar
confidence sensor publishes the mean per-hour correction factor and carries
the corrector's serialised state across a restart.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

from custom_components.hsem.coordinator_data import CoordinatorData
from custom_components.hsem.custom_sensors.ev_optimal_charging_plan_sensor import (
    HSEMEVOptimalChargingPlanSensor,
)
from custom_components.hsem.custom_sensors.ev_second_optimal_charging_plan_sensor import (
    HSEMEVSecondOptimalChargingPlanSensor,
)
from custom_components.hsem.custom_sensors.solar_confidence_sensor import (
    HSEMSolarConfidenceSensor,
)
from custom_components.hsem.entity import HSEMCoordinatorEntity
from custom_components.hsem.planner.ev_planner_models import EVChargingPlan
from custom_components.hsem.utils.solar_corrector import SolarForecastCorrector

_ENTRY_ID = "test_entry"
_SOLAR_MODULE = "custom_components.hsem.custom_sensors.solar_confidence_sensor"


def _entry() -> MagicMock:
    """Return a minimal config entry."""
    entry = MagicMock()
    entry.entry_id = _ENTRY_ID
    entry.options = {}
    entry.data = {}
    return entry


def _coordinator(data: CoordinatorData | None, **attributes: Any) -> Any:
    """Return a coordinator stand-in publishing *data*."""
    coordinator = MagicMock()
    coordinator.last_update_success = data is not None
    coordinator.data = data
    for name, value in attributes.items():
        setattr(coordinator, name, value)
    return coordinator


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


def _plan(state: str = "charging") -> EVChargingPlan:
    """Return an EV charging plan in *state*."""
    return EVChargingPlan(
        state=state,
        ev_connected=True,
        current_soc_pct=40.0,
        target_soc_pct=80.0,
        battery_capacity_kwh=60.0,
        charger_power_kw=11.0,
        total_kwh_needed=24.0,
        deadline=datetime(2026, 6, 1, 7, 0, tzinfo=UTC),
    )


def _plan_data(field: str, plan: EVChargingPlan) -> CoordinatorData:
    """Return a snapshot carrying *plan* on the named per-EV field."""
    data = CoordinatorData()
    setattr(data, field, plan)
    return data


_EV_SENSORS = [
    (HSEMEVOptimalChargingPlanSensor, "ev_charging_plan"),
    (HSEMEVSecondOptimalChargingPlanSensor, "ev_second_charging_plan"),
]
_EV_IDS = ["primary", "second"]


class TestEvChargingPlanSensors:
    """Each EV plan sensor reads only its own plan."""

    @pytest.mark.parametrize(("sensor_cls", "field"), _EV_SENSORS, ids=_EV_IDS)
    def test_publishes_the_plan_state_and_attributes(
        self, sensor_cls: Any, field: str
    ) -> None:
        """The plan's state and serialised attributes are published."""
        plan = _plan()
        sensor = sensor_cls(_entry(), _coordinator(_plan_data(field, plan)))

        assert sensor.state == "charging"
        assert sensor.extra_state_attributes == plan.as_attributes()
        assert sensor.available is True
        assert sensor.should_poll is False
        assert _ENTRY_ID in sensor.unique_id

    @pytest.mark.parametrize(("sensor_cls", "field"), _EV_SENSORS, ids=_EV_IDS)
    def test_other_evs_plan_is_ignored(self, sensor_cls: Any, field: str) -> None:
        """A plan belonging to the other EV never leaks into this sensor."""
        other_field = (
            "ev_second_charging_plan"
            if field == "ev_charging_plan"
            else "ev_charging_plan"
        )
        sensor = sensor_cls(_entry(), _coordinator(_plan_data(other_field, _plan())))

        assert sensor.state == STATE_UNAVAILABLE
        assert sensor.extra_state_attributes == {}

    @pytest.mark.parametrize(("sensor_cls", "field"), _EV_SENSORS, ids=_EV_IDS)
    def test_unknown_plan_state_is_reported_unavailable(
        self, sensor_cls: Any, field: str
    ) -> None:
        """A state outside the documented enum is never published."""
        sensor = sensor_cls(
            _entry(),
            _coordinator(_plan_data(field, _plan(state="something_new"))),
        )

        assert sensor.state == STATE_UNAVAILABLE

    @pytest.mark.parametrize(("sensor_cls", "field"), _EV_SENSORS, ids=_EV_IDS)
    def test_no_snapshot_is_unavailable(self, sensor_cls: Any, field: str) -> None:
        """Before the first cycle the sensor is unavailable and empty."""
        sensor = sensor_cls(_entry(), _coordinator(None))

        assert sensor.state == STATE_UNAVAILABLE
        assert sensor.extra_state_attributes == {}
        assert sensor.available is False

    @pytest.mark.parametrize(("sensor_cls", "field"), _EV_SENSORS, ids=_EV_IDS)
    @pytest.mark.asyncio
    async def test_known_previous_state_is_restored(
        self, sensor_cls: Any, field: str
    ) -> None:
        """A previous state from the enum survives a restart."""
        sensor = sensor_cls(_entry(), _coordinator(None))

        await _restore(sensor, "waiting")

        assert sensor.state == "waiting"

    @pytest.mark.parametrize(("sensor_cls", "field"), _EV_SENSORS, ids=_EV_IDS)
    @pytest.mark.parametrize("state", ["something_new", STATE_UNKNOWN, None])
    @pytest.mark.asyncio
    async def test_unusable_previous_state_is_ignored(
        self, sensor_cls: Any, field: str, state: str | None
    ) -> None:
        """An unknown previous state is not restored."""
        sensor = sensor_cls(_entry(), _coordinator(None))

        await _restore(sensor, state)

        assert sensor.state == STATE_UNAVAILABLE


class TestSolarConfidenceSensor:
    """The confidence sensor averages the corrector's per-hour factors."""

    @staticmethod
    def _corrector() -> SolarForecastCorrector:
        """Return a corrector that has learned two hours."""
        corrector = SolarForecastCorrector()
        corrector.update_hour(10, 1.0, 0.8)
        corrector.update_hour(11, 1.0, 1.2)
        corrector.update_residual(1.0, 0.8)
        return corrector

    def test_reports_the_mean_hour_factor(self) -> None:
        """State is the mean of the published per-hour factors."""
        sensor = HSEMSolarConfidenceSensor(
            _entry(),
            _coordinator(
                CoordinatorData(solar_hour_factors={10: 0.8, 11: 1.2}),
                _solar_corrector=self._corrector(),
            ),
        )

        assert sensor.native_value == pytest.approx(1.0)
        assert sensor.available is True

    def test_no_factors_yet_reports_nothing(self) -> None:
        """Before any hour is learned there is no factor to average."""
        sensor = HSEMSolarConfidenceSensor(
            _entry(), _coordinator(CoordinatorData(), _solar_corrector=None)
        )

        assert sensor.native_value is None

    def test_attributes_carry_the_corrector_state(self) -> None:
        """Factors, confidence, residuals, and the restore payload are exposed."""
        corrector = self._corrector()
        sensor = HSEMSolarConfidenceSensor(
            _entry(),
            _coordinator(
                CoordinatorData(solar_hour_factors={10: 0.8}),
                _solar_corrector=corrector,
            ),
        )

        attributes = sensor.extra_state_attributes
        assert attributes is not None
        assert json.loads(attributes["hour_factors"]) == {"10": 0.8}
        assert attributes["confidence"] == pytest.approx(corrector.confidence)
        assert attributes["residual_count"] == 1
        assert attributes["_solar_corrector_data"] == corrector.to_dict()

    def test_attributes_without_a_corrector_use_neutral_defaults(self) -> None:
        """A missing corrector reports a neutral confidence and no payload."""
        sensor = HSEMSolarConfidenceSensor(
            _entry(), _coordinator(CoordinatorData(), _solar_corrector=None)
        )

        attributes = sensor.extra_state_attributes
        assert attributes is not None
        assert attributes["confidence"] == pytest.approx(0.50)
        assert attributes["residual_count"] == 0
        assert attributes["processed_through"] is None
        assert "_solar_corrector_data" not in attributes

    def test_no_attributes_before_the_first_cycle(self) -> None:
        """Without a snapshot there is nothing to expose."""
        sensor = HSEMSolarConfidenceSensor(_entry(), _coordinator(None))

        assert sensor.extra_state_attributes is None
        assert sensor.available is False

    @pytest.mark.asyncio
    async def test_restores_state_and_corrector_payload(self) -> None:
        """A restart restores both the value and the corrector's own state."""
        corrector = SolarForecastCorrector()
        sensor = HSEMSolarConfidenceSensor(
            _entry(), _coordinator(None, _solar_corrector=corrector)
        )

        await _restore(
            sensor, "0.95", {"_solar_corrector_data": self._corrector().to_dict()}
        )

        assert sensor.native_value == pytest.approx(0.95)
        assert corrector.hour_factors

    @pytest.mark.asyncio
    async def test_unparseable_previous_state_is_ignored(self) -> None:
        """A non-numeric previous state is not restored."""
        sensor = HSEMSolarConfidenceSensor(_entry(), _coordinator(None))

        await _restore(sensor, "unavailable_earlier")

        assert sensor.native_value is None

    @pytest.mark.asyncio
    async def test_failed_corrector_restore_is_logged_not_raised(self) -> None:
        """A corrector that raises while loading must not break setup."""
        corrector = SolarForecastCorrector()
        corrector.load_from_dict = MagicMock(side_effect=RuntimeError("corrupt"))  # type: ignore[method-assign]  # force failure
        sensor = HSEMSolarConfidenceSensor(
            _entry(), _coordinator(None, _solar_corrector=corrector)
        )

        with patch(f"{_SOLAR_MODULE}._LOGGER") as logger:
            await _restore(sensor, "0.95", {"_solar_corrector_data": {"bad": True}})

        logger.exception.assert_called_once()

    @pytest.mark.asyncio
    async def test_restore_without_a_corrector_payload_is_a_noop(self) -> None:
        """An older restored state without the payload only sets the value."""
        corrector = SolarForecastCorrector()
        sensor = HSEMSolarConfidenceSensor(
            _entry(), _coordinator(None, _solar_corrector=corrector)
        )

        await _restore(sensor, "0.95")

        assert sensor.native_value == pytest.approx(0.95)
        assert corrector.hour_factors == {}

    @pytest.mark.asyncio
    async def test_nothing_restored_leaves_the_sensor_cold(self) -> None:
        """No previous state at all leaves the sensor unavailable."""
        sensor = HSEMSolarConfidenceSensor(_entry(), _coordinator(None))

        await _restore(sensor, None)

        assert sensor.native_value is None
        assert sensor.available is False
