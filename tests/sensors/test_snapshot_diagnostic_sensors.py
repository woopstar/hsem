"""Tests for the diagnostic sensors backed by the coordinator snapshot.

These sensors read ``CoordinatorData`` directly. They share one contract:
never poll, report a documented fallback before the first cycle, restore
their previous state across a restart, and expose a fixed attribute set
(with ``None``/empty placeholders while no snapshot exists).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN

from custom_components.hsem.const import FORCE_MODE_AUTO
from custom_components.hsem.coordinator_data import CoordinatorData
from custom_components.hsem.custom_sensors.battery_soc_sensor import (
    HSEMBatterySoCSensor,
)
from custom_components.hsem.custom_sensors.degraded_mode_sensor import (
    HSEMDegradedModeSensor,
)
from custom_components.hsem.custom_sensors.force_mode_sensor import HSEMForceModeSensor
from custom_components.hsem.custom_sensors.hardware_writes_sensor import (
    HSEMHardwareWritesSensor,
)
from custom_components.hsem.custom_sensors.last_updated_sensor import (
    HSEMLastUpdatedSensor,
)
from custom_components.hsem.custom_sensors.missing_entities_sensor import (
    HSEMMissingEntitiesSensor,
)
from custom_components.hsem.custom_sensors.net_consumption_sensor import (
    HSEMNetConsumptionSensor,
)
from custom_components.hsem.custom_sensors.next_update_sensor import (
    HSEMNextUpdateSensor,
)
from custom_components.hsem.custom_sensors.read_only_sensor import HSEMReadOnlySensor
from custom_components.hsem.custom_sensors.recommendation_interval_sensor import (
    HSEMRecommendationIntervalSensor,
)
from custom_components.hsem.custom_sensors.update_interval_sensor import (
    HSEMUpdateIntervalSensor,
)
from custom_components.hsem.entity import HSEMCoordinatorEntity
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.degraded_mode import DegradedMode

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


@dataclass(frozen=True)
class _Case:
    """One sensor's value contract."""

    sensor_cls: Any
    attribute: str
    data: CoordinatorData
    expected: Any
    cold_value: Any
    restored: str
    restored_value: Any

    @property
    def id(self) -> str:
        """Return a readable parametrisation id."""
        return str(self.sensor_cls.__name__)


_CASES = [
    _Case(
        HSEMReadOnlySensor,
        "state",
        CoordinatorData(cfg=SensorConfig(read_only=True)),
        STATE_ON,
        STATE_OFF,
        STATE_ON,
        STATE_ON,
    ),
    _Case(
        HSEMDegradedModeSensor,
        "state",
        CoordinatorData(
            live=_live(missing_entities=True, missing_entities_list=["solcast_today"])
        ),
        DegradedMode.Degraded.value,
        DegradedMode.OK.value,
        DegradedMode.Error.value,
        DegradedMode.Error.value,
    ),
    _Case(
        HSEMForceModeSensor,
        "state",
        CoordinatorData(live=_live(force_working_mode_state="batteries_charge_grid")),
        "batteries_charge_grid",
        FORCE_MODE_AUTO,
        "batteries_wait_mode",
        "batteries_wait_mode",
    ),
    _Case(
        HSEMUpdateIntervalSensor,
        "native_value",
        CoordinatorData(cfg=SensorConfig(update_interval=7)),
        7,
        None,
        "3",
        3,
    ),
    _Case(
        HSEMRecommendationIntervalSensor,
        "native_value",
        CoordinatorData(cfg=SensorConfig(recommendation_interval_minutes=15)),
        15,
        None,
        "60",
        60,
    ),
    _Case(
        HSEMNetConsumptionSensor,
        "native_value",
        CoordinatorData(live=_live(net_consumption_w=1234.5)),
        1234.5,
        None,
        "900.0",
        900.0,
    ),
    _Case(
        HSEMBatterySoCSensor,
        "native_value",
        CoordinatorData(live=_live(huawei_batteries_soc_pct=63.5)),
        63.5,
        None,
        "50.0",
        50.0,
    ),
    _Case(
        HSEMMissingEntitiesSensor,
        "state",
        CoordinatorData(live=_live(missing_entities_list=["sensor.a", "sensor.b"])),
        2,
        0,
        "4",
        4,
    ),
    _Case(
        HSEMLastUpdatedSensor,
        "state",
        CoordinatorData(last_updated="2026-06-01T12:00:00+00:00"),
        "2026-06-01T12:00:00+00:00",
        None,
        "2026-05-31T12:00:00+00:00",
        "2026-05-31T12:00:00+00:00",
    ),
    _Case(
        HSEMNextUpdateSensor,
        "state",
        CoordinatorData(next_update="2026-06-01T12:05:00+00:00"),
        "2026-06-01T12:05:00+00:00",
        None,
        "2026-05-31T12:05:00+00:00",
        "2026-05-31T12:05:00+00:00",
    ),
    _Case(
        HSEMHardwareWritesSensor,
        "state",
        CoordinatorData(
            live=_live(
                missing_entities=True,
                missing_entities_list=["batteries_state_of_capacity"],
            )
        ),
        "blocked",
        "allowed",
        "blocked",
        "blocked",
    ),
]


class TestSnapshotSensorContract:
    """Value, fallback, restore, and availability for each sensor."""

    @pytest.mark.parametrize("case", _CASES, ids=lambda c: c.id)
    def test_value_comes_from_the_snapshot(self, case: _Case) -> None:
        """A published snapshot drives the sensor value."""
        sensor = _sensor(case.sensor_cls, case.data)

        assert getattr(sensor, case.attribute) == case.expected
        assert sensor.available is True
        assert sensor.should_poll is False
        assert _ENTRY_ID in sensor.unique_id

    @pytest.mark.parametrize("case", _CASES, ids=lambda c: c.id)
    def test_documented_fallback_before_the_first_cycle(self, case: _Case) -> None:
        """Without a snapshot the sensor reports its documented default."""
        sensor = _sensor(case.sensor_cls, None)

        assert getattr(sensor, case.attribute) == case.cold_value
        assert sensor.available is False

    @pytest.mark.parametrize("case", _CASES, ids=lambda c: c.id)
    @pytest.mark.asyncio
    async def test_previous_state_is_restored(self, case: _Case) -> None:
        """A restored state carries the sensor until the first cycle."""
        sensor = _sensor(case.sensor_cls, None)

        await _restore(sensor, case.restored)

        assert getattr(sensor, case.attribute) == case.restored_value
        assert sensor.available is True

    @pytest.mark.parametrize("case", _CASES, ids=lambda c: c.id)
    @pytest.mark.parametrize("state", [STATE_UNAVAILABLE, STATE_UNKNOWN, None])
    @pytest.mark.asyncio
    async def test_unusable_previous_state_is_ignored(
        self, case: _Case, state: str | None
    ) -> None:
        """An unavailable/unknown/missing previous state is not restored."""
        sensor = _sensor(case.sensor_cls, None)

        await _restore(sensor, state)

        assert getattr(sensor, case.attribute) == case.cold_value
        assert sensor.available is False

    @pytest.mark.parametrize("case", _CASES, ids=lambda c: c.id)
    def test_attributes_are_reported_cold_and_warm(self, case: _Case) -> None:
        """Attributes exist before and after the first cycle."""
        cold = _sensor(case.sensor_cls, None).extra_state_attributes
        warm = _sensor(case.sensor_cls, case.data).extra_state_attributes

        assert isinstance(cold, dict)
        assert isinstance(warm, dict)
        assert cold
        assert warm


class TestNumericRestoreGuards:
    """Numeric sensors never turn an unparseable state into a number."""

    @pytest.mark.parametrize(
        "sensor_cls",
        [
            HSEMUpdateIntervalSensor,
            HSEMRecommendationIntervalSensor,
            HSEMNetConsumptionSensor,
            HSEMBatterySoCSensor,
            HSEMMissingEntitiesSensor,
        ],
        ids=lambda c: c.__name__,
    )
    @pytest.mark.asyncio
    async def test_unparseable_restored_state_is_dropped(self, sensor_cls: Any) -> None:
        """A non-numeric previous state falls back to the cold default."""
        sensor = _sensor(sensor_cls, None)

        await _restore(sensor, "not a number")

        value = sensor.native_value if hasattr(sensor, "native_value") else sensor.state
        assert value in (None, 0)


class TestSnapshotAttributeContent:
    """The attribute payloads mirror the snapshot they came from."""

    def test_read_only_reports_write_gating(self) -> None:
        """Read-only mode and hardware writes are mutually exclusive."""
        sensor = _sensor(
            HSEMReadOnlySensor,
            CoordinatorData(cfg=SensorConfig(read_only=True, update_interval=5)),
        )

        attributes = sensor.extra_state_attributes
        assert attributes["read_only"] is True
        assert attributes["hardware_writes_active"] is False
        assert attributes["update_interval_minutes"] == 5

    def test_force_mode_reports_the_active_override(self) -> None:
        """A forced mode exposes the entity and expiry driving it."""
        expiry = datetime(2026, 6, 1, 13, 0, tzinfo=UTC).isoformat()
        sensor = _sensor(
            HSEMForceModeSensor,
            CoordinatorData(
                live=_live(
                    force_working_mode_state="batteries_charge_grid",
                    force_working_mode="select.hsem_force_working_mode",
                ),
                override_expiry=expiry,
            ),
        )

        attributes = sensor.extra_state_attributes
        assert attributes["override_active"] is True
        assert attributes["force_mode_entity_id"] == "select.hsem_force_working_mode"
        assert attributes["override_expiry"] == expiry

    def test_recommendation_interval_reports_the_slot_count(self) -> None:
        """The planning horizon is expressed as a number of slots."""
        sensor = _sensor(
            HSEMRecommendationIntervalSensor,
            CoordinatorData(
                cfg=SensorConfig(
                    recommendation_interval_minutes=15,
                    recommendation_interval_length=24,
                )
            ),
        )

        attributes = sensor.extra_state_attributes
        assert attributes["recommendation_interval_length_hours"] == 24
        assert attributes["total_planning_slots"] == 96

    def test_net_consumption_reports_its_components(self) -> None:
        """House, solar, and EV-inclusive figures accompany the net value."""
        sensor = _sensor(
            HSEMNetConsumptionSensor,
            CoordinatorData(
                live=_live(
                    net_consumption_w=1000.0,
                    house_consumption_power_w=1500.0,
                    solar_production_power_w=500.0,
                    net_consumption_with_ev_w=4700.0,
                )
            ),
        )

        attributes = sensor.extra_state_attributes
        assert attributes["house_consumption_w"] == pytest.approx(1500.0)
        assert attributes["solar_production_w"] == pytest.approx(500.0)
        assert attributes["net_consumption_with_ev_w"] == pytest.approx(4700.0)
        assert attributes["ev_charging_active"] is False

    def test_battery_soc_reports_capacity_and_learner_metrics(self) -> None:
        """Learned capacity is exposed only once the learner has data."""
        learner = MagicMock()
        learner.learned_capacity_kwh = 9.8
        learner.sample_count = 42
        sensor = _sensor(
            HSEMBatterySoCSensor,
            CoordinatorData(
                live=_live(
                    huawei_batteries_soc_pct=63.5,
                    battery_current_capacity_kwh=6.3,
                    battery_usable_capacity_kwh=9.0,
                    huawei_batteries_rated_capacity_wh=10_000.0,
                ),
                capacity_learner=learner,
            ),
        )

        attributes = sensor.extra_state_attributes
        assert attributes["battery_current_capacity_kwh"] == pytest.approx(6.3)
        assert attributes["battery_usable_capacity_kwh"] == pytest.approx(9.0)
        assert attributes["battery_rated_capacity_wh"] == pytest.approx(10_000.0)
        assert attributes["learned_capacity_kwh"] == pytest.approx(9.8)
        assert attributes["capacity_samples"] == 42

    def test_battery_soc_reports_an_untrained_learner_as_such(self) -> None:
        """A fresh learner reports no learned capacity and no samples."""
        sensor = _sensor(
            HSEMBatterySoCSensor,
            CoordinatorData(live=_live(huawei_batteries_soc_pct=63.5)),
        )

        attributes = sensor.extra_state_attributes
        assert attributes["learned_capacity_kwh"] is None
        assert attributes["capacity_samples"] == 0
