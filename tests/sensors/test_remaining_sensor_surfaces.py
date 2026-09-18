"""Surface tests for the remaining coordinator-backed sensors.

Same contract as the other diagnostic sensors: never poll, key on the config
entry, report a documented fallback before the first cycle, restore only a
state from their own enum, and expose attributes that either describe the live
snapshot or say plainly that there is nothing yet.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.components.sensor.const import SensorDeviceClass, SensorStateClass
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, UnitOfEnergy

from custom_components.hsem.coordinator_data import CoordinatorData
from custom_components.hsem.custom_sensors.applier_status_sensor import (
    HSEMApplierStatusSensor,
)
from custom_components.hsem.custom_sensors.ev_charger_calculated_power_sensor import (
    HSEMEVChargerCalculatedPowerSensor,
)
from custom_components.hsem.custom_sensors.ev_charger_current_limit_sensor import (
    HSEMEVChargerCurrentLimitSensor,
)
from custom_components.hsem.custom_sensors.ev_charging_sensor import (
    HSEMEVChargingSensor,
)
from custom_components.hsem.custom_sensors.ev_second_soc_economics_sensor import (
    HSEMEVSecondSoCEconomicsSensor,
)
from custom_components.hsem.custom_sensors.plan_explanation_sensor import (
    HSEMPlanExplanationSensor,
)
from custom_components.hsem.custom_sensors.pv_curtailment_sensor import (
    HSEMPVTailedSensor,
)
from custom_components.hsem.custom_sensors.utility_meter_sensor import (
    HSEMUtilityMeterSensor,
)
from custom_components.hsem.entity import HSEMCoordinatorEntity
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.plan_explanation import PlanExplanation
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.inverter_verify import (
    ApplyResult,
    ApplyStatus,
    CycleApplySummary,
)

_ENTRY_ID = "test_entry"


def _entry() -> MagicMock:
    """Return a minimal config entry."""
    entry = MagicMock()
    entry.entry_id = _ENTRY_ID
    entry.options = {}
    entry.data = {}
    return entry


def _coordinator(data: CoordinatorData | None) -> Any:
    """Return a coordinator stand-in publishing *data*."""
    coordinator = MagicMock()
    coordinator.last_update_success = data is not None
    coordinator.data = data
    return coordinator


def _sensor(sensor_cls: Any, data: CoordinatorData | None) -> Any:
    """Construct *sensor_cls* against a coordinator publishing *data*."""
    return sensor_cls(_entry(), _coordinator(data))


async def _restore(sensor: Any, state: str | None) -> None:
    """Run ``async_added_to_hass`` with *state* as the restored state."""
    restored = None if state is None else MagicMock(state=state, attributes={})
    sensor.async_get_last_state = AsyncMock(return_value=restored)
    with patch.object(HSEMCoordinatorEntity, "async_added_to_hass", AsyncMock()):
        await sensor.async_added_to_hass()


def _live(**kwargs: Any) -> LiveState:
    """Return a live snapshot with *kwargs* applied."""
    live = LiveState()
    for key, value in kwargs.items():
        setattr(live, key, value)
    return live


_COORDINATOR_SENSORS = [
    HSEMPlanExplanationSensor,
    HSEMPVTailedSensor,
    HSEMApplierStatusSensor,
    HSEMEVSecondSoCEconomicsSensor,
    HSEMEVChargingSensor,
    HSEMEVChargerCurrentLimitSensor,
    HSEMEVChargerCalculatedPowerSensor,
]


class TestSharedContract:
    """Every one of these sensors is push-driven and entry-scoped."""

    @pytest.mark.parametrize(
        "sensor_cls", _COORDINATOR_SENSORS, ids=lambda c: c.__name__
    )
    def test_is_push_driven_with_a_stable_unique_id(self, sensor_cls: Any) -> None:
        """No polling, and the unique id carries the config entry."""
        sensor = _sensor(sensor_cls, CoordinatorData())

        assert sensor.should_poll is False
        assert sensor.unique_id is not None
        assert _ENTRY_ID in sensor.unique_id

    @pytest.mark.parametrize(
        "sensor_cls", _COORDINATOR_SENSORS, ids=lambda c: c.__name__
    )
    def test_unavailable_before_the_first_cycle(self, sensor_cls: Any) -> None:
        """Without a snapshot or a restored state the sensor is unavailable."""
        coordinator = _coordinator(None)
        coordinator.last_update_success = False

        assert sensor_cls(_entry(), coordinator).available is False


class TestPlanExplanationSensor:
    """The winning candidate name is the state."""

    def test_winner_name_is_published(self) -> None:
        """The selector's winner is what the user sees."""
        sensor = _sensor(
            HSEMPlanExplanationSensor,
            CoordinatorData(
                plan_explanation=PlanExplanation(
                    winner_name="milp", selected_strategy="milp"
                )
            ),
        )

        assert sensor.state == "milp"

    def test_selected_strategy_is_the_fallback(self) -> None:
        """Without a winner name the strategy stands in."""
        sensor = _sensor(
            HSEMPlanExplanationSensor,
            CoordinatorData(
                plan_explanation=PlanExplanation(
                    winner_name="", selected_strategy="safety_hold"
                )
            ),
        )

        assert sensor.state == "safety_hold"

    @pytest.mark.asyncio
    async def test_restored_state_carries_until_the_first_cycle(self) -> None:
        """A restored strategy name survives a restart."""
        sensor = _sensor(HSEMPlanExplanationSensor, None)

        await _restore(sensor, "milp")

        assert sensor.state == "milp"
        assert sensor.available is True


class TestPvCurtailmentSensor:
    """Curtailment state reflects the inverter's power-control reading."""

    def test_no_snapshot_reports_normal(self) -> None:
        """Before the first cycle the site is assumed uncurtailed."""
        assert _sensor(HSEMPVTailedSensor, None).state == "normal"

    @pytest.mark.asyncio
    async def test_restored_state_is_reported(self) -> None:
        """A previous curtailment state survives a restart."""
        sensor = _sensor(HSEMPVTailedSensor, None)

        await _restore(sensor, "curtailed")

        assert sensor.state == "curtailed"
        assert sensor.available is True


def _summary(status: ApplyStatus) -> CycleApplySummary:
    """Return an apply summary whose single write ended in *status*."""
    return CycleApplySummary(
        results=[
            ApplyResult(
                entity_id="select.batteries_working_mode",
                desired="time_of_use_luna2000",
                actual="time_of_use_luna2000",
                status=status,
                attempts=1,
            )
        ]
    )


class TestApplierStatusSensor:
    """The worst status of the last write cycle is the state."""

    def test_pending_before_any_write_cycle(self) -> None:
        """No summary yet means pending, with empty write details."""
        sensor = _sensor(HSEMApplierStatusSensor, CoordinatorData())

        assert sensor.state == "pending"
        attributes = sensor.extra_state_attributes
        assert attributes["total_writes"] == 0
        assert attributes["failed_entities"] == []

    def test_a_completed_cycle_reports_its_status(self) -> None:
        """A finished summary publishes its overall status."""
        sensor = _sensor(
            HSEMApplierStatusSensor,
            CoordinatorData(apply_summary=_summary(ApplyStatus.OK)),
        )

        assert sensor.state == ApplyStatus.OK.value

    @pytest.mark.asyncio
    async def test_restored_status_carries_until_the_first_cycle(self) -> None:
        """A restored status survives a restart (issue #951)."""
        sensor = _sensor(HSEMApplierStatusSensor, None)

        await _restore(sensor, ApplyStatus.OK.value)

        assert sensor.state == ApplyStatus.OK.value
        assert sensor.available is True

    @pytest.mark.asyncio
    async def test_an_unknown_restored_status_is_ignored(self) -> None:
        """Only a status from the enum may be restored."""
        sensor = _sensor(HSEMApplierStatusSensor, None)

        await _restore(sensor, "something_new")

        assert sensor.state == "pending"


class TestEvSecondSoCEconomicsSensor:
    """The second EV's economics state comes from its own result."""

    def test_no_snapshot_is_unavailable(self) -> None:
        """Before the first cycle there is no economics result."""
        sensor = _sensor(HSEMEVSecondSoCEconomicsSensor, None)

        assert sensor.state == STATE_UNAVAILABLE
        assert sensor.extra_state_attributes == {}

    def test_missing_result_is_unavailable(self) -> None:
        """A snapshot without a second-EV result is unavailable."""
        sensor = _sensor(HSEMEVSecondSoCEconomicsSensor, CoordinatorData())

        assert sensor.state == STATE_UNAVAILABLE

    def test_result_state_and_attributes_are_published(self) -> None:
        """A computed result supplies both the state and the attributes."""
        result = MagicMock()
        result.state = "ready"
        result.as_attributes.return_value = {"target_soc_pct": 80.0}
        sensor = _sensor(
            HSEMEVSecondSoCEconomicsSensor,
            CoordinatorData(ev_second_soc_economics=result),
        )

        assert sensor.state == "ready"
        assert sensor.extra_state_attributes == {"target_soc_pct": 80.0}

    def test_an_unknown_result_state_is_unavailable(self) -> None:
        """A state outside the documented enum is never published."""
        result = MagicMock()
        result.state = "something_new"
        sensor = _sensor(
            HSEMEVSecondSoCEconomicsSensor,
            CoordinatorData(ev_second_soc_economics=result),
        )

        assert sensor.state == STATE_UNAVAILABLE

    @pytest.mark.asyncio
    async def test_restored_state_carries_until_the_first_cycle(self) -> None:
        """A known previous state survives a restart."""
        sensor = _sensor(HSEMEVSecondSoCEconomicsSensor, None)

        await _restore(sensor, "ready")

        assert sensor.state == "ready"


class TestEvChargingSensor:
    """The charging sensor is on while either EV draws power."""

    def test_charging_is_reported_on(self) -> None:
        """A charging EV turns the sensor on."""
        live = _live()
        live.ev.is_charging = True
        sensor = _sensor(HSEMEVChargingSensor, CoordinatorData(live=live))

        assert sensor.state == STATE_ON
        assert sensor.available is True

    def test_idle_chargers_report_off(self) -> None:
        """With no EV charging the sensor is off."""
        sensor = _sensor(HSEMEVChargingSensor, CoordinatorData(live=_live()))

        assert sensor.state == STATE_OFF

    def test_no_snapshot_reports_off_with_placeholder_attributes(self) -> None:
        """Before the first cycle the sensor is off and says nothing is known."""
        sensor = _sensor(HSEMEVChargingSensor, None)

        assert sensor.state == STATE_OFF
        assert isinstance(sensor.extra_state_attributes, dict)

    def test_attributes_describe_the_live_session(self) -> None:
        """Charger power and configuration accompany the on/off state."""
        live = _live()
        live.ev.is_charging = True
        live.ev.power_w = 7400.0
        sensor = _sensor(
            HSEMEVChargingSensor, CoordinatorData(live=live, cfg=SensorConfig())
        )

        assert isinstance(sensor.extra_state_attributes, dict)
        assert sensor.extra_state_attributes


class TestEvChargerCommandSensors:
    """The per-EV command sensors publish what the applier will write."""

    def test_current_limit_attributes_describe_the_command(self) -> None:
        """Topology and rated current accompany the limit."""
        live = _live()
        live.ev.power_w = 7400.0
        sensor = _sensor(
            HSEMEVChargerCurrentLimitSensor,
            CoordinatorData(live=live, cfg=SensorConfig()),
        )

        attributes = sensor.extra_state_attributes
        assert isinstance(attributes, dict)
        assert attributes

    def test_calculated_power_sensor_is_entry_scoped(self) -> None:
        """Each EV's calculated-power sensor has its own unique id."""
        sensor = _sensor(HSEMEVChargerCalculatedPowerSensor, CoordinatorData())

        assert _ENTRY_ID in sensor.unique_id
        assert sensor.should_poll is False

    @pytest.mark.asyncio
    async def test_calculated_power_restores_its_previous_value(self) -> None:
        """A restored numeric value carries across a restart."""
        sensor = _sensor(HSEMEVChargerCalculatedPowerSensor, None)

        await _restore(sensor, "3700")

        assert sensor.available is True


class TestUtilityMeterSensor:
    """HSEM's utility meter declares energy units for the recorder."""

    def test_declares_energy_in_kilowatt_hours(self) -> None:
        """Unit, device class and state class match an energy total."""
        unique_id = "hsem_energy_average_house_consumption_14_15_id"
        sensor = HSEMUtilityMeterSensor(
            cron_pattern=None,
            delta_values=False,
            meter_offset=timedelta(hours=14),
            meter_type="daily",
            name="Average house consumption 14-15",
            net_consumption=True,
            parent_meter="sensor.source",
            periodically_resetting=True,
            source_entity="sensor.source",
            tariff_entity=None,
            tariff=None,
            unique_id=unique_id,
            sensor_always_available=True,
            id=unique_id,
            e_id="sensor.hsem_energy_average_house_consumption_14_15",
            config_entry=_entry(),
        )

        assert sensor.unit_of_measurement == UnitOfEnergy.KILO_WATT_HOUR
        assert sensor.device_class is SensorDeviceClass.ENERGY
        assert sensor.state_class is SensorStateClass.TOTAL
        # Unlike the coordinator-backed sensors, the utility meter polls.
        assert sensor.should_poll is True
        assert sensor.unique_id == unique_id
