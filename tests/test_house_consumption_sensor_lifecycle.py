"""Regression tests for house-consumption sensor lifecycle (issue #XXX).

Verifies that:
1. Derived sensors (integral, utility meter, avg) are NOT removed on HA restart.
2. Derived sensors are created only when they are missing from the registry.
3. The power sensor reports ``None`` (unknown) outside its active hour window so
   the IntegrationSensor pauses accumulation — preventing cross-hour energy
   contamination.
4. The power sensor reports a real value inside its active hour window.
5. The sensor is *available* only inside the active window (state is not None).
6. Sensor metadata is correct: device_class, state_class, unit_of_measurement.
7. The utility meter source is an *energy* sensor (the integral), not the power
   sensor.
8. HSEMIntegrationSensor uses TOTAL_INCREASING state class.
9. HSEMAvgSensor carries device_class=ENERGY.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.sensor.const import SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfEnergy, UnitOfPower

from custom_components.hsem.custom_sensors.avg_sensor import HSEMAvgSensor
from custom_components.hsem.custom_sensors.house_consumption_power_sensor import (
    HSEMHouseConsumptionPowerSensor,
)
from custom_components.hsem.custom_sensors.integration_sensor import (
    HSEMIntegrationSensor,
)
from custom_components.hsem.custom_sensors.utility_meter_sensor import (
    HSEMUtilityMeterSensor,
)
from custom_components.hsem.utils.sensornames import (
    get_energy_average_sensor_unique_id,
    get_house_consumption_power_sensor_unique_id,
    get_integral_sensor_unique_id,
    get_utility_meter_sensor_unique_id,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_config_entry(**overrides) -> MagicMock:
    """Return a minimal config-entry mock."""
    import voluptuous as vol

    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    entry.options = {
        "hsem_house_consumption_power": "sensor.house_power",
        "hsem_ev_charger_power": vol.UNDEFINED,
        "hsem_house_power_includes_ev_charger_power": False,
        **overrides,
    }
    entry.data = {}
    return entry


def _make_sensor(
    hour_start: int = 14, *, config_entry=None
) -> tuple[
    HSEMHouseConsumptionPowerSensor,
    list,
]:
    """Construct a sensor and capture entities added via async_add_entities."""
    if config_entry is None:
        config_entry = _mock_config_entry()

    added: list = []

    def fake_add(entities, update_before_add=False):
        added.extend(entities)

    sensor = HSEMHouseConsumptionPowerSensor(
        config_entry=config_entry,
        hour_start=hour_start,
        hour_end=(hour_start + 1) % 24,
        async_add_entities=fake_add,
    )
    return sensor, added


def _attach_hass(sensor: HSEMHouseConsumptionPowerSensor) -> MagicMock:
    """Attach a minimal fake hass to the sensor."""
    hass = MagicMock()
    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=None)
    hass.data = {}
    sensor.hass = hass
    # Suppress async_write_ha_state globally on the sensor to avoid HA bootstrap.
    sensor.async_write_ha_state = MagicMock()
    return hass


# ---------------------------------------------------------------------------
# 1. Metadata correctness
# ---------------------------------------------------------------------------


class TestPowerSensorMetadata:
    """HSEMHouseConsumptionPowerSensor must carry the correct HA metadata."""

    def test_device_class_is_power(self) -> None:
        sensor, _ = _make_sensor()
        assert sensor._attr_device_class == SensorDeviceClass.POWER

    def test_state_class_is_measurement(self) -> None:
        sensor, _ = _make_sensor()
        assert sensor._attr_state_class == SensorStateClass.MEASUREMENT

    def test_unit_is_watt(self) -> None:
        sensor, _ = _make_sensor()
        assert sensor._attr_native_unit_of_measurement == UnitOfPower.WATT

    def test_unique_id_format(self) -> None:
        sensor, _ = _make_sensor(hour_start=7)
        expected = get_house_consumption_power_sensor_unique_id(7, 8)
        assert sensor.unique_id == expected


class TestIntegrationSensorMetadata:
    """HSEMIntegrationSensor must use TOTAL_INCREASING and ENERGY device class.

    Metadata is exposed as @property overrides (required because IntegrationSensor
    defines these as properties itself), so we verify via the property descriptors
    rather than class-level _attr_* attributes.
    """

    def test_state_class_is_total_increasing(self) -> None:
        # Verify the property is defined on the class and returns TOTAL_INCREASING.
        prop = HSEMIntegrationSensor.__dict__.get("state_class")
        assert prop is not None and isinstance(prop, property), (
            "state_class must be a @property on HSEMIntegrationSensor"
        )
        # Spot-check by calling it via the descriptor protocol with a minimal mock.
        mock_instance = MagicMock(spec=HSEMIntegrationSensor)
        result = HSEMIntegrationSensor.state_class.fget(mock_instance)
        assert result == SensorStateClass.TOTAL_INCREASING

    def test_device_class_is_energy(self) -> None:
        prop = HSEMIntegrationSensor.__dict__.get("device_class")
        assert prop is not None and isinstance(prop, property), (
            "device_class must be a @property on HSEMIntegrationSensor"
        )
        mock_instance = MagicMock(spec=HSEMIntegrationSensor)
        result = HSEMIntegrationSensor.device_class.fget(mock_instance)
        assert result == SensorDeviceClass.ENERGY


class TestAvgSensorMetadata:
    """HSEMAvgSensor must carry device_class=ENERGY via @property."""

    def test_device_class_is_energy(self) -> None:
        prop = HSEMAvgSensor.__dict__.get("device_class")
        assert prop is not None and isinstance(prop, property), (
            "device_class must be a @property on HSEMAvgSensor"
        )
        mock_instance = MagicMock(spec=HSEMAvgSensor)
        result = HSEMAvgSensor.device_class.fget(mock_instance)
        assert result == SensorDeviceClass.ENERGY

    def test_state_class_is_measurement(self) -> None:
        prop = HSEMAvgSensor.__dict__.get("state_class")
        assert prop is not None and isinstance(prop, property)
        mock_instance = MagicMock(spec=HSEMAvgSensor)
        result = HSEMAvgSensor.state_class.fget(mock_instance)
        assert result == SensorStateClass.MEASUREMENT

    def test_unit_is_kwh(self) -> None:
        prop = HSEMAvgSensor.__dict__.get("unit_of_measurement")
        assert prop is not None and isinstance(prop, property)
        mock_instance = MagicMock(spec=HSEMAvgSensor)
        result = HSEMAvgSensor.unit_of_measurement.fget(mock_instance)
        assert result == UnitOfEnergy.KILO_WATT_HOUR


# ---------------------------------------------------------------------------
# 2. Active / inactive window — state and availability
# ---------------------------------------------------------------------------


class TestPowerSensorActiveWindow:
    """State must be None outside the active hour; real value inside it."""

    @pytest.mark.asyncio
    async def test_state_is_none_outside_active_hour(self) -> None:
        """Update triggered at hour 3 while sensor covers hour 14 → None."""
        sensor, _ = _make_sensor(hour_start=14)
        _attach_hass(sensor)

        fake_now = MagicMock()
        fake_now.hour = 3  # NOT the active window
        fake_now.isoformat.return_value = "2026-05-12T03:00:00"

        with (
            patch(
                "custom_components.hsem.custom_sensors.house_consumption_power_sensor.dt_util.now",
                return_value=fake_now,
            ),
            patch(
                "custom_components.hsem.custom_sensors.house_consumption_power_sensor.async_resolve_entity_id_from_unique_id",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            await sensor._async_handle_update()

        assert sensor._state is None

    @pytest.mark.asyncio
    async def test_sensor_unavailable_outside_active_hour(self) -> None:
        sensor, _ = _make_sensor(hour_start=14)
        _attach_hass(sensor)

        fake_now = MagicMock()
        fake_now.hour = 5
        fake_now.isoformat.return_value = "2026-05-12T05:00:00"

        with (
            patch(
                "custom_components.hsem.custom_sensors.house_consumption_power_sensor.dt_util.now",
                return_value=fake_now,
            ),
            patch(
                "custom_components.hsem.custom_sensors.house_consumption_power_sensor.async_resolve_entity_id_from_unique_id",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            await sensor._async_handle_update()

        assert sensor._available is False

    @pytest.mark.asyncio
    async def test_state_set_inside_active_hour(self) -> None:
        """Update at hour 14 with sensor covering 14-15 → state is set."""
        config_entry = _mock_config_entry(
            hsem_house_power_includes_ev_charger_power=False,
        )
        sensor, _ = _make_sensor(hour_start=14, config_entry=config_entry)
        _attach_hass(sensor)

        fake_now = MagicMock()
        fake_now.hour = 14  # INSIDE the active window
        fake_now.isoformat.return_value = "2026-05-12T14:05:00"

        async def fake_fetch():
            sensor._hsem_house_consumption_power_state = 1500.0
            sensor._hsem_ev_charger_power_state = 0.0
            sensor._missing_input_entities = False

        with (
            patch(
                "custom_components.hsem.custom_sensors.house_consumption_power_sensor.dt_util.now",
                return_value=fake_now,
            ),
            patch.object(sensor, "_async_fetch_sensor_states", new=fake_fetch),
            patch(
                "custom_components.hsem.custom_sensors.house_consumption_power_sensor.async_resolve_entity_id_from_unique_id",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            await sensor._async_handle_update()

        assert sensor._state == pytest.approx(1500.0)
        assert sensor._available is True

    @pytest.mark.asyncio
    async def test_ev_subtraction_inside_active_hour(self) -> None:
        """EV power is subtracted when house power includes EV charger."""
        config_entry = _mock_config_entry(
            hsem_house_power_includes_ev_charger_power=True,
        )
        sensor, _ = _make_sensor(hour_start=8, config_entry=config_entry)
        _attach_hass(sensor)

        fake_now = MagicMock()
        fake_now.hour = 8
        fake_now.isoformat.return_value = "2026-05-12T08:10:00"

        async def fake_fetch():
            sensor._hsem_house_consumption_power_state = 2000.0
            sensor._hsem_ev_charger_power_state = 700.0
            sensor._missing_input_entities = False

        with (
            patch(
                "custom_components.hsem.custom_sensors.house_consumption_power_sensor.dt_util.now",
                return_value=fake_now,
            ),
            patch.object(sensor, "_async_fetch_sensor_states", new=fake_fetch),
            patch(
                "custom_components.hsem.custom_sensors.house_consumption_power_sensor.async_resolve_entity_id_from_unique_id",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            await sensor._async_handle_update()

        assert sensor._state == pytest.approx(1300.0)

    @pytest.mark.asyncio
    async def test_state_resets_to_none_when_hour_passes(self) -> None:
        """State must reset to None when the active window is over."""
        sensor, _ = _make_sensor(hour_start=10)
        _attach_hass(sensor)

        # First: inside the window
        fake_now_inside = MagicMock()
        fake_now_inside.hour = 10
        fake_now_inside.isoformat.return_value = "2026-05-12T10:30:00"

        async def fake_fetch():
            sensor._hsem_house_consumption_power_state = 800.0
            sensor._hsem_ev_charger_power_state = 0.0
            sensor._missing_input_entities = False

        with (
            patch(
                "custom_components.hsem.custom_sensors.house_consumption_power_sensor.dt_util.now",
                return_value=fake_now_inside,
            ),
            patch.object(sensor, "_async_fetch_sensor_states", new=fake_fetch),
            patch(
                "custom_components.hsem.custom_sensors.house_consumption_power_sensor.async_resolve_entity_id_from_unique_id",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            await sensor._async_handle_update()

        assert sensor._state == pytest.approx(800.0)

        # Then: outside the window (next hour)
        fake_now_outside = MagicMock()
        fake_now_outside.hour = 11
        fake_now_outside.isoformat.return_value = "2026-05-12T11:00:00"

        with (
            patch(
                "custom_components.hsem.custom_sensors.house_consumption_power_sensor.dt_util.now",
                return_value=fake_now_outside,
            ),
            patch(
                "custom_components.hsem.custom_sensors.house_consumption_power_sensor.async_resolve_entity_id_from_unique_id",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            await sensor._async_handle_update()

        assert sensor._state is None
        assert sensor._available is False


# ---------------------------------------------------------------------------
# 3. Derived sensor lifecycle — no deletion on restart
# ---------------------------------------------------------------------------


class TestDerivedSensorLifecycle:
    """Derived sensors must be created once and NOT deleted on restart."""

    @pytest.mark.asyncio
    async def test_integral_sensor_created_when_missing(self) -> None:
        """An integral sensor is added to HA when it is not in the registry.

        We mock ``HSEMIntegrationSensor.__init__`` to avoid bootstrapping a real
        HA instance (IntegrationSensor.__init__ calls async_entity_id_to_device).
        """
        sensor, added = _make_sensor(hour_start=6)
        _attach_hass(sensor)

        fake_now = MagicMock()
        fake_now.hour = 6
        fake_now.isoformat.return_value = "2026-05-12T06:00:00"

        integral_uid = get_integral_sensor_unique_id(6, 7)
        power_uid = get_house_consumption_power_sensor_unique_id(6, 7)

        async def mock_resolve(self_inner, uid):
            if uid == power_uid:
                return "sensor.hsem_house_consumption_power_06_07"
            return None

        async def fake_fetch():
            sensor._hsem_house_consumption_power_state = 500.0
            sensor._hsem_ev_charger_power_state = 0.0
            sensor._missing_input_entities = False

        def fake_integration_init(
            self_inner, *args, id, e_id, config_entry=None, **kwargs
        ):
            # Minimal init — skip the real HA bootstrap.
            self_inner._attr_unique_id = id
            self_inner.entity_id = e_id

        with (
            patch(
                "custom_components.hsem.custom_sensors.house_consumption_power_sensor.dt_util.now",
                return_value=fake_now,
            ),
            patch.object(sensor, "_async_fetch_sensor_states", new=fake_fetch),
            patch(
                "custom_components.hsem.custom_sensors.house_consumption_power_sensor.async_resolve_entity_id_from_unique_id",
                side_effect=mock_resolve,
            ),
            patch.object(HSEMIntegrationSensor, "__init__", fake_integration_init),
        ):
            await sensor._async_handle_update()

        integral_sensors = [e for e in added if isinstance(e, HSEMIntegrationSensor)]
        assert len(integral_sensors) == 1
        assert integral_sensors[0]._attr_unique_id == integral_uid

    @pytest.mark.asyncio
    async def test_integral_sensor_not_recreated_when_already_exists(self) -> None:
        """When the integral sensor already exists, it must NOT be re-added."""
        sensor, added = _make_sensor(hour_start=9)
        _attach_hass(sensor)

        fake_now = MagicMock()
        fake_now.hour = 9
        fake_now.isoformat.return_value = "2026-05-12T09:00:00"

        power_uid = get_house_consumption_power_sensor_unique_id(9, 10)
        integral_uid = get_integral_sensor_unique_id(9, 10)
        utility_uid = get_utility_meter_sensor_unique_id(9, 10)

        # Include the four avg sensor UIDs so they also appear as already-existing.
        avg_uids = {
            get_energy_average_sensor_unique_id(9, 10, d): d for d in (1, 3, 7, 14)
        }
        all_existing = {power_uid, integral_uid, utility_uid} | set(avg_uids)

        async def mock_resolve(self_inner, uid):
            if uid in all_existing:
                return f"sensor.existing_{uid}"
            return None

        async def fake_fetch():
            sensor._hsem_house_consumption_power_state = 900.0
            sensor._hsem_ev_charger_power_state = 0.0
            sensor._missing_input_entities = False

        with (
            patch(
                "custom_components.hsem.custom_sensors.house_consumption_power_sensor.dt_util.now",
                return_value=fake_now,
            ),
            patch.object(sensor, "_async_fetch_sensor_states", new=fake_fetch),
            patch(
                "custom_components.hsem.custom_sensors.house_consumption_power_sensor.async_resolve_entity_id_from_unique_id",
                side_effect=mock_resolve,
            ),
        ):
            await sensor._async_handle_update()

        assert len(added) == 0, (
            "No sensor must be re-added when all derived sensors already exist in the registry"
        )
        assert integral_uid in sensor._derived_sensors_created

    @pytest.mark.asyncio
    async def test_derived_sensors_not_added_twice_within_same_run(self) -> None:
        """Calling _async_handle_update twice must not add the integral sensor twice."""
        sensor, added = _make_sensor(hour_start=16)
        _attach_hass(sensor)

        power_uid = get_house_consumption_power_sensor_unique_id(16, 17)
        utility_uid = get_utility_meter_sensor_unique_id(16, 17)

        async def mock_resolve(self_inner, uid):
            # Power and utility meter exist; integral does not on first call.
            # After first update, integral is in _derived_sensors_created so
            # async_resolve is never called for it again.
            if uid == power_uid:
                return "sensor.hsem_house_consumption_power_16_17"
            if uid == utility_uid:
                return "sensor.existing_utility"
            return None  # integral missing

        fake_now = MagicMock()
        fake_now.hour = 16
        fake_now.isoformat.return_value = "2026-05-12T16:05:00"

        async def fake_fetch():
            sensor._hsem_house_consumption_power_state = 1200.0
            sensor._hsem_ev_charger_power_state = 0.0
            sensor._missing_input_entities = False

        def fake_integration_init(
            self_inner, *args, id, e_id, config_entry=None, **kwargs
        ):
            self_inner._attr_unique_id = id
            self_inner.entity_id = e_id

        with (
            patch(
                "custom_components.hsem.custom_sensors.house_consumption_power_sensor.dt_util.now",
                return_value=fake_now,
            ),
            patch.object(sensor, "_async_fetch_sensor_states", new=fake_fetch),
            patch(
                "custom_components.hsem.custom_sensors.house_consumption_power_sensor.async_resolve_entity_id_from_unique_id",
                side_effect=mock_resolve,
            ),
            patch.object(HSEMIntegrationSensor, "__init__", fake_integration_init),
        ):
            await sensor._async_handle_update()
            await sensor._async_handle_update()

        integral_sensors = [e for e in added if isinstance(e, HSEMIntegrationSensor)]
        assert len(integral_sensors) == 1, (
            "Integral sensor must be added exactly once even after multiple update calls"
        )

    @pytest.mark.asyncio
    async def test_utility_meter_source_is_integral_not_power(self) -> None:
        """The utility meter must track the integral (energy) sensor, not power.

        We mock ``HSEMUtilityMeterSensor.__init__`` to capture the ``source_entity``
        argument without bootstrapping a real HA instance.
        """
        sensor, added = _make_sensor(hour_start=20)
        _attach_hass(sensor)

        power_uid = get_house_consumption_power_sensor_unique_id(20, 21)
        integral_uid = get_integral_sensor_unique_id(20, 21)
        utility_uid = get_utility_meter_sensor_unique_id(20, 21)

        integral_entity_id = "sensor.hsem_house_consumption_energy_integral_20_21"

        async def mock_resolve(self_inner, uid):
            if uid == power_uid:
                return "sensor.hsem_house_consumption_power_20_21"
            if uid == integral_uid:
                return integral_entity_id
            if uid == utility_uid:
                return None  # Not yet created
            return None

        fake_now = MagicMock()
        fake_now.hour = 20
        fake_now.isoformat.return_value = "2026-05-12T20:00:00"

        async def fake_fetch():
            sensor._hsem_house_consumption_power_state = 300.0
            sensor._hsem_ev_charger_power_state = 0.0
            sensor._missing_input_entities = False

        captured_source: list[str] = []

        def fake_utility_init(
            self_inner, *args, id, e_id, config_entry=None, source_entity=None, **kwargs
        ):
            self_inner._attr_unique_id = id
            self_inner.entity_id = e_id
            self_inner._source_entity = source_entity
            captured_source.append(source_entity)

        with (
            patch(
                "custom_components.hsem.custom_sensors.house_consumption_power_sensor.dt_util.now",
                return_value=fake_now,
            ),
            patch.object(sensor, "_async_fetch_sensor_states", new=fake_fetch),
            patch(
                "custom_components.hsem.custom_sensors.house_consumption_power_sensor.async_resolve_entity_id_from_unique_id",
                side_effect=mock_resolve,
            ),
            patch.object(HSEMUtilityMeterSensor, "__init__", fake_utility_init),
        ):
            await sensor._async_handle_update()

        assert len(captured_source) == 1
        assert captured_source[0] == integral_entity_id, (
            f"Utility meter source should be the integral sensor "
            f"({integral_entity_id}), not the power sensor"
        )

    @pytest.mark.asyncio
    async def test_utility_meter_not_recreated_on_restart(self) -> None:
        """After restart the utility meter exists in registry — must NOT be re-added."""
        sensor, added = _make_sensor(hour_start=0)
        _attach_hass(sensor)

        integral_uid = get_integral_sensor_unique_id(0, 1)
        utility_uid = get_utility_meter_sensor_unique_id(0, 1)

        async def mock_resolve(self_inner, uid):
            # Both already present after restart.
            if uid == integral_uid:
                return "sensor.hsem_house_consumption_energy_integral_00_01"
            if uid == utility_uid:
                return "sensor.hsem_house_consumption_energy_00_01_utility_meter"
            return None

        fake_now = MagicMock()
        fake_now.hour = 5  # Outside active window — just tests derived sensor path
        fake_now.isoformat.return_value = "2026-05-12T05:00:00"

        with (
            patch(
                "custom_components.hsem.custom_sensors.house_consumption_power_sensor.dt_util.now",
                return_value=fake_now,
            ),
            patch(
                "custom_components.hsem.custom_sensors.house_consumption_power_sensor.async_resolve_entity_id_from_unique_id",
                side_effect=mock_resolve,
            ),
            patch.object(sensor, "async_write_ha_state", MagicMock()),
        ):
            await sensor._async_handle_update()

        utility_sensors = [e for e in added if isinstance(e, HSEMUtilityMeterSensor)]
        assert len(utility_sensors) == 0, (
            "Utility meter must NOT be re-added when it already exists after restart"
        )
        assert utility_uid in sensor._derived_sensors_created

    @pytest.mark.asyncio
    async def test_avg_sensors_not_recreated_on_restart(self) -> None:
        """After restart all four avg sensors exist — none should be re-added."""
        sensor, added = _make_sensor(hour_start=12)
        _attach_hass(sensor)

        integral_uid = get_integral_sensor_unique_id(12, 13)
        utility_uid = get_utility_meter_sensor_unique_id(12, 13)
        avg_uids = {
            get_energy_average_sensor_unique_id(12, 13, d): d for d in (1, 3, 7, 14)
        }

        async def mock_resolve(self_inner, uid):
            if uid == integral_uid:
                return "sensor.hsem_integral_12_13"
            if uid == utility_uid:
                return "sensor.hsem_utility_12_13"
            if uid in avg_uids:
                return f"sensor.hsem_avg_{uid}"
            return None

        fake_now = MagicMock()
        fake_now.hour = 6  # Outside active window
        fake_now.isoformat.return_value = "2026-05-12T06:00:00"

        with (
            patch(
                "custom_components.hsem.custom_sensors.house_consumption_power_sensor.dt_util.now",
                return_value=fake_now,
            ),
            patch(
                "custom_components.hsem.custom_sensors.house_consumption_power_sensor.async_resolve_entity_id_from_unique_id",
                side_effect=mock_resolve,
            ),
            patch.object(sensor, "async_write_ha_state", MagicMock()),
        ):
            await sensor._async_handle_update()

        avg_sensors = [e for e in added if isinstance(e, HSEMAvgSensor)]
        assert len(avg_sensors) == 0, (
            "Average sensors must NOT be re-added when they already exist after restart"
        )
        for uid in avg_uids:
            assert uid in sensor._derived_sensors_created


# ---------------------------------------------------------------------------
# 4. async_added_to_hass — no stale power restored on restart
# ---------------------------------------------------------------------------


class TestRestartBehaviour:
    """On HA restart the power state must NOT be restored from the previous run."""

    @pytest.mark.asyncio
    async def test_state_not_restored_from_previous_run(self) -> None:
        """async_added_to_hass must NOT set a numeric state from old_state."""
        sensor, _ = _make_sensor(hour_start=18)
        _attach_hass(sensor)

        fake_old_state = MagicMock()
        fake_old_state.state = "1234.5"
        fake_old_state.attributes = {"last_updated": "2026-05-11T18:30:00"}

        fake_now = MagicMock()
        fake_now.hour = 3  # Outside active window at restart time
        fake_now.isoformat.return_value = "2026-05-12T03:00:00"

        with (
            patch.object(
                sensor,
                "async_get_last_state",
                new=AsyncMock(return_value=fake_old_state),
            ),
            patch(
                "custom_components.hsem.custom_sensors.house_consumption_power_sensor.dt_util.now",
                return_value=fake_now,
            ),
            patch(
                "custom_components.hsem.custom_sensors.house_consumption_power_sensor.async_resolve_entity_id_from_unique_id",
                new_callable=AsyncMock,
                return_value=None,
            ),
            # Suppress the full HA entity/restore chain which requires a bootstrapped runtime.
            patch(
                "custom_components.hsem.entity.HSEMEntity.async_added_to_hass",
                new=AsyncMock(),
            ),
            patch(
                "homeassistant.helpers.restore_state.RestoreEntity.async_added_to_hass",
                new=AsyncMock(),
            ),
        ):
            await sensor.async_added_to_hass()

        # After restart outside the active window the state must be None,
        # NOT the previously measured 1234.5.
        assert sensor._state is None, (
            "Power state must not be restored on restart — it must be None "
            "when outside the active hour window"
        )

    @pytest.mark.asyncio
    async def test_last_updated_is_read_from_previous_run(self) -> None:
        """async_added_to_hass must read last_updated from the restored state.

        Note: ``_last_updated`` is subsequently overwritten by the first
        ``_async_handle_update`` call (when ``_state_previous`` is None).
        This test verifies the attribute is loaded from old_state *before* the
        update cycle runs, by patching ``_async_handle_update`` to a no-op.
        """
        sensor, _ = _make_sensor(hour_start=18)
        _attach_hass(sensor)

        saved_ts = "2026-05-11T18:30:00"
        fake_old_state = MagicMock()
        fake_old_state.state = "800.0"
        fake_old_state.attributes = {"last_updated": saved_ts}

        with (
            patch.object(
                sensor,
                "async_get_last_state",
                new=AsyncMock(return_value=fake_old_state),
            ),
            # Suppress the update cycle so _last_updated is not overwritten.
            patch.object(sensor, "_async_handle_update", new=AsyncMock()),
            patch(
                "custom_components.hsem.entity.HSEMEntity.async_added_to_hass",
                new=AsyncMock(),
            ),
            patch(
                "homeassistant.helpers.restore_state.RestoreEntity.async_added_to_hass",
                new=AsyncMock(),
            ),
        ):
            await sensor.async_added_to_hass()

        assert sensor._last_updated == saved_ts
