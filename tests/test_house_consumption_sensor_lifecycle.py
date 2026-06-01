"""Regression tests for house-consumption sensor lifecycle.

Key invariants verified here:

1. Derived sensors (integral, utility meter, avg) are always created as fresh
   entity instances after an HA restart.  The entity registry entry surviving
   a restart does NOT prevent instance creation — HA needs a live instance to
   bind to the registry entry and restore state.

2. Within the same HA session, ``_derived_sensors_created`` prevents duplicate
   entity creation on repeated update cycles.

3. The power sensor state is reset to ``None`` at the start of every update
   cycle so that a failed sensor fetch inside the active window clears stale
   power instead of leaving it for the IntegrationSensor to accumulate.

4. Power is only measured inside the active hour window.  Outside the window
   the state remains ``None`` (integral pauses, utility meter stops).

5. Sensor metadata: device_class, state_class, unit_of_measurement are correct.

6. The utility meter source is the energy (integral) sensor, not the power sensor.

7. Previous state is restored on restart (via RestoreEntity) so HA does not
   show ``unknown`` immediately.  The sensor is marked unavailable until the
   first live measurement so the integral does not accumulate the restored value.
"""

from __future__ import annotations

from typing import Any
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
    get_house_consumption_power_sensor_unique_id,
    get_integral_sensor_entity_id,
    get_integral_sensor_unique_id,
    get_utility_meter_sensor_entity_id,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_config_entry(**overrides: Any) -> MagicMock:
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
    hour_start: int = 14, *, config_entry: MagicMock | None = None
) -> tuple[
    HSEMHouseConsumptionPowerSensor,
    list,
]:
    """Construct a sensor and capture entities added via async_add_entities."""
    if config_entry is None:
        config_entry = _mock_config_entry()

    added: list = []

    def fake_add(entities, _update_before_add=False):
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
    sensor.async_write_ha_state = MagicMock()  # type: ignore[method-assign]  # test monkey-patch
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
        expected = get_house_consumption_power_sensor_unique_id("test_entry_id", 7, 8)
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
        result = HSEMIntegrationSensor.state_class.fget(mock_instance)  # type: ignore[attr-defined]  # mock attribute set in test
        assert result == SensorStateClass.TOTAL_INCREASING

    def test_device_class_is_energy(self) -> None:
        prop = HSEMIntegrationSensor.__dict__.get("device_class")
        assert prop is not None and isinstance(prop, property), (
            "device_class must be a @property on HSEMIntegrationSensor"
        )
        mock_instance = MagicMock(spec=HSEMIntegrationSensor)
        result = HSEMIntegrationSensor.device_class.fget(mock_instance)  # type: ignore[attr-defined]  # mock attribute set in test
        assert result == SensorDeviceClass.ENERGY


class TestAvgSensorMetadata:
    """HSEMAvgSensor must carry device_class=ENERGY via @property."""

    def test_device_class_is_energy(self) -> None:
        prop = HSEMAvgSensor.__dict__.get("device_class")
        assert prop is not None and isinstance(prop, property), (
            "device_class must be a @property on HSEMAvgSensor"
        )
        mock_instance = MagicMock(spec=HSEMAvgSensor)
        result = HSEMAvgSensor.device_class.fget(mock_instance)  # type: ignore[attr-defined]  # mock attribute set in test
        assert result == SensorDeviceClass.ENERGY

    def test_state_class_is_measurement(self) -> None:
        prop = HSEMAvgSensor.__dict__.get("state_class")
        assert prop is not None and isinstance(prop, property)
        mock_instance = MagicMock(spec=HSEMAvgSensor)
        result = HSEMAvgSensor.state_class.fget(mock_instance)  # type: ignore[attr-defined]  # mock attribute set in test
        assert result == SensorStateClass.MEASUREMENT

    def test_unit_is_kwh(self) -> None:
        prop = HSEMAvgSensor.__dict__.get("unit_of_measurement")
        assert prop is not None and isinstance(prop, property)
        mock_instance = MagicMock(spec=HSEMAvgSensor)
        result = HSEMAvgSensor.unit_of_measurement.fget(mock_instance)  # type: ignore[attr-defined]  # mock attribute set in test
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
            patch.object(HSEMIntegrationSensor, "__init__", _fake_integration_init),
            patch.object(HSEMUtilityMeterSensor, "__init__", _fake_utility_init),
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
            patch.object(HSEMIntegrationSensor, "__init__", _fake_integration_init),
            patch.object(HSEMUtilityMeterSensor, "__init__", _fake_utility_init),
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
            patch.object(HSEMIntegrationSensor, "__init__", _fake_integration_init),
            patch.object(HSEMUtilityMeterSensor, "__init__", _fake_utility_init),
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
            patch.object(HSEMIntegrationSensor, "__init__", _fake_integration_init),
            patch.object(HSEMUtilityMeterSensor, "__init__", _fake_utility_init),
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
            patch.object(HSEMIntegrationSensor, "__init__", _fake_integration_init),
            patch.object(HSEMUtilityMeterSensor, "__init__", _fake_utility_init),
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
        ):
            await sensor._async_handle_update()

        assert sensor._state is None
        assert sensor._available is False

    @pytest.mark.asyncio
    async def test_fetch_failure_inside_active_window_clears_state(self) -> None:
        """A failed sensor fetch inside the active window must set state=None.

        This prevents the IntegrationSensor from continuing to accumulate the
        last valid power reading as if it were still live.
        """
        sensor, _ = _make_sensor(hour_start=10)
        _attach_hass(sensor)

        fake_now = MagicMock()
        fake_now.hour = 10
        fake_now.isoformat.return_value = "2026-05-12T10:05:00"

        # First update: successful measurement.
        async def fake_fetch_ok():
            sensor._hsem_house_consumption_power_state = 750.0
            sensor._hsem_ev_charger_power_state = 0.0
            sensor._missing_input_entities = False

        with (
            patch(
                "custom_components.hsem.custom_sensors.house_consumption_power_sensor.dt_util.now",
                return_value=fake_now,
            ),
            patch.object(sensor, "_async_fetch_sensor_states", new=fake_fetch_ok),
            patch.object(HSEMIntegrationSensor, "__init__", _fake_integration_init),
            patch.object(HSEMUtilityMeterSensor, "__init__", _fake_utility_init),
        ):
            await sensor._async_handle_update()

        assert sensor._state == pytest.approx(750.0)

        # Second update: fetch fails (entity unavailable / network error).
        async def fake_fetch_fail():
            sensor._missing_input_entities = True
            # Stale values remain in the float fields — the guard should ignore them.

        with (
            patch(
                "custom_components.hsem.custom_sensors.house_consumption_power_sensor.dt_util.now",
                return_value=fake_now,
            ),
            patch.object(sensor, "_async_fetch_sensor_states", new=fake_fetch_fail),
        ):
            await sensor._async_handle_update()

        assert sensor._state is None, (
            "Failed fetch inside the active window must clear state to None "
            "so the IntegrationSensor pauses accumulation"
        )
        assert sensor._available is False

    @pytest.mark.asyncio
    async def test_stale_power_not_kept_after_fetch_failure(self) -> None:
        """State is reset to None at the start of every cycle.

        Even if _async_fetch_sensor_states leaves stale float values in the
        internal state fields (because it only sets _missing_input_entities=True
        without zeroing them), the ``not self._missing_input_entities`` guard
        must prevent those stale values from being committed to _state.
        """
        sensor, _ = _make_sensor(hour_start=15)
        _attach_hass(sensor)

        # Pre-load stale internal values that a real exception would leave behind.
        sensor._hsem_house_consumption_power_state = 999.9
        sensor._hsem_ev_charger_power_state = 0.0

        fake_now = MagicMock()
        fake_now.hour = 15
        fake_now.isoformat.return_value = "2026-05-12T15:10:00"

        # Fetch sets missing flag but does NOT zero the stale float fields.
        async def failing_fetch():
            sensor._missing_input_entities = True

        with (
            patch(
                "custom_components.hsem.custom_sensors.house_consumption_power_sensor.dt_util.now",
                return_value=fake_now,
            ),
            patch.object(sensor, "_async_fetch_sensor_states", new=failing_fetch),
            patch.object(HSEMIntegrationSensor, "__init__", _fake_integration_init),
            patch.object(HSEMUtilityMeterSensor, "__init__", _fake_utility_init),
        ):
            await sensor._async_handle_update()

        assert sensor._state is None, (
            "Stale internal float values must not be committed to _state when "
            "_missing_input_entities is True"
        )


# ---------------------------------------------------------------------------
# 3. Derived sensor lifecycle
# ---------------------------------------------------------------------------


def _fake_integration_init(
    self_inner: Any,
    *args: Any,
    id: Any,
    e_id: Any,
    config_entry: Any = None,
    **kwargs: Any,
) -> None:
    """Minimal HSEMIntegrationSensor init that bypasses the real HA bootstrap."""
    self_inner._attr_unique_id = id
    self_inner.entity_id = e_id


def _fake_utility_init(
    self_inner: Any,
    *args: Any,
    id: Any,
    e_id: Any,
    config_entry: Any = None,
    source_entity: Any = None,
    _parent_meter: Any = None,
    **kwargs: Any,
) -> None:
    """Minimal HSEMUtilityMeterSensor init that bypasses the real HA bootstrap."""
    self_inner._attr_unique_id = id
    self_inner.entity_id = e_id
    self_inner._source_entity = source_entity


class TestDerivedSensorLifecycle:
    """Derived sensors are always created as new instances on every HA start.

    The per-runtime ``_derived_sensors_created`` set is the only gate — the
    entity registry is NOT consulted.  This ensures HA can bind the fresh
    instance to the existing registry entry and restore sensor state.
    """

    @pytest.mark.asyncio
    async def test_integral_sensor_created_on_first_boot(self) -> None:
        """Integral sensor is added the first time _async_handle_update runs."""
        sensor, added = _make_sensor(hour_start=6)
        _attach_hass(sensor)

        fake_now = MagicMock()
        fake_now.hour = 6
        fake_now.isoformat.return_value = "2026-05-12T06:00:00"
        integral_uid = get_integral_sensor_unique_id("test_entry_id", 6, 7)

        async def fake_fetch():
            sensor._hsem_house_consumption_power_state = 500.0
            sensor._hsem_ev_charger_power_state = 0.0
            sensor._missing_input_entities = False

        with (
            patch(
                "custom_components.hsem.custom_sensors.house_consumption_power_sensor.dt_util.now",
                return_value=fake_now,
            ),
            patch.object(sensor, "_async_fetch_sensor_states", new=fake_fetch),
            patch.object(HSEMIntegrationSensor, "__init__", _fake_integration_init),
            patch.object(HSEMUtilityMeterSensor, "__init__", _fake_utility_init),
        ):
            await sensor._async_handle_update()

        integral_sensors = [e for e in added if isinstance(e, HSEMIntegrationSensor)]
        assert len(integral_sensors) == 1
        assert integral_sensors[0]._attr_unique_id == integral_uid

    @pytest.mark.asyncio
    async def test_integral_sensor_created_after_restart_despite_registry_entry(
        self,
    ) -> None:
        """A registry entry for the integral sensor must NOT prevent instance creation.

        This is the key lifecycle fix: after restart ``_derived_sensors_created``
        is empty (new runtime), so a new entity instance is always created.  HA
        binds it to the existing registry entry and IntegrationSensor restores.
        """
        sensor, added = _make_sensor(hour_start=9)
        _attach_hass(sensor)

        # _derived_sensors_created is empty — simulates fresh HA restart.
        assert len(sensor._derived_sensors_created) == 0

        integral_uid = get_integral_sensor_unique_id("test_entry_id", 9, 10)

        fake_now = MagicMock()
        fake_now.hour = 5  # Outside active window — just tests derived sensor path
        fake_now.isoformat.return_value = "2026-05-12T05:00:00"

        with (
            patch(
                "custom_components.hsem.custom_sensors.house_consumption_power_sensor.dt_util.now",
                return_value=fake_now,
            ),
            patch.object(HSEMIntegrationSensor, "__init__", _fake_integration_init),
            patch.object(HSEMUtilityMeterSensor, "__init__", _fake_utility_init),
        ):
            await sensor._async_handle_update()

        integral_sensors = [e for e in added if isinstance(e, HSEMIntegrationSensor)]
        assert len(integral_sensors) == 1, (
            "Integral sensor instance must be created after restart even when a "
            "registry entry exists — the registry entry alone does not give HA a "
            "live entity instance"
        )
        assert integral_uid in sensor._derived_sensors_created

    @pytest.mark.asyncio
    async def test_all_derived_sensors_created_on_restart(self) -> None:
        """All 6 derived sensors (1 integral + 1 utility meter + 4 avg) are created
        on restart, regardless of any registry entries."""
        sensor, added = _make_sensor(hour_start=3)
        _attach_hass(sensor)

        fake_now = MagicMock()
        fake_now.hour = 5
        fake_now.isoformat.return_value = "2026-05-12T05:00:00"

        with (
            patch(
                "custom_components.hsem.custom_sensors.house_consumption_power_sensor.dt_util.now",
                return_value=fake_now,
            ),
            patch.object(HSEMIntegrationSensor, "__init__", _fake_integration_init),
            patch.object(HSEMUtilityMeterSensor, "__init__", _fake_utility_init),
        ):
            await sensor._async_handle_update()

        assert len([e for e in added if isinstance(e, HSEMIntegrationSensor)]) == 1
        assert len([e for e in added if isinstance(e, HSEMUtilityMeterSensor)]) == 1
        assert len([e for e in added if isinstance(e, HSEMAvgSensor)]) == 4

    @pytest.mark.asyncio
    async def test_derived_sensors_not_added_twice_within_same_session(self) -> None:
        """Within the same HA session, _async_handle_update called twice must
        not add derived sensors a second time."""
        sensor, added = _make_sensor(hour_start=16)
        _attach_hass(sensor)

        fake_now = MagicMock()
        fake_now.hour = 16
        fake_now.isoformat.return_value = "2026-05-12T16:05:00"

        async def fake_fetch():
            sensor._hsem_house_consumption_power_state = 1200.0
            sensor._hsem_ev_charger_power_state = 0.0
            sensor._missing_input_entities = False

        with (
            patch(
                "custom_components.hsem.custom_sensors.house_consumption_power_sensor.dt_util.now",
                return_value=fake_now,
            ),
            patch.object(sensor, "_async_fetch_sensor_states", new=fake_fetch),
            patch.object(HSEMIntegrationSensor, "__init__", _fake_integration_init),
            patch.object(HSEMUtilityMeterSensor, "__init__", _fake_utility_init),
        ):
            await sensor._async_handle_update()
            await sensor._async_handle_update()

        # Each type created exactly once.
        assert len([e for e in added if isinstance(e, HSEMIntegrationSensor)]) == 1, (
            "Integral sensor must be added exactly once per HA session"
        )
        assert len([e for e in added if isinstance(e, HSEMUtilityMeterSensor)]) == 1, (
            "Utility meter must be added exactly once per HA session"
        )
        assert len([e for e in added if isinstance(e, HSEMAvgSensor)]) == 4, (
            "Average sensors must be added exactly once per HA session"
        )

    @pytest.mark.asyncio
    async def test_utility_meter_source_is_integral_not_power(self) -> None:
        """The utility meter must track the energy (integral) sensor, not the power
        sensor.  Source entity_id is now derived deterministically."""
        sensor, _ = _make_sensor(hour_start=20)
        _attach_hass(sensor)

        expected_source = get_integral_sensor_entity_id(20, 21)

        fake_now = MagicMock()
        fake_now.hour = 5
        fake_now.isoformat.return_value = "2026-05-12T05:00:00"

        captured_source: list[str] = []

        def capturing_utility_init(
            self_inner: Any,
            *args: Any,
            id: Any,
            e_id: Any,
            config_entry: Any = None,
            source_entity: Any = None,
            _parent_meter: Any = None,
            **kwargs: Any,
        ) -> None:
            self_inner._attr_unique_id = id
            self_inner.entity_id = e_id
            self_inner._source_entity = source_entity
            captured_source.append(source_entity)

        with (
            patch(
                "custom_components.hsem.custom_sensors.house_consumption_power_sensor.dt_util.now",
                return_value=fake_now,
            ),
            patch.object(HSEMIntegrationSensor, "__init__", _fake_integration_init),
            patch.object(HSEMUtilityMeterSensor, "__init__", capturing_utility_init),
        ):
            await sensor._async_handle_update()

        assert len(captured_source) == 1
        assert captured_source[0] == expected_source, (
            f"Utility meter source should be the integral sensor "
            f"({expected_source}), not the power sensor"
        )

    @pytest.mark.asyncio
    async def test_avg_sensor_tracked_entity_is_utility_meter(self) -> None:
        """Each avg sensor must track the utility meter, not any other entity."""
        sensor, added = _make_sensor(hour_start=7)
        _attach_hass(sensor)

        expected_tracked = get_utility_meter_sensor_entity_id(7, 8)

        fake_now = MagicMock()
        fake_now.hour = 5
        fake_now.isoformat.return_value = "2026-05-12T05:00:00"

        with (
            patch(
                "custom_components.hsem.custom_sensors.house_consumption_power_sensor.dt_util.now",
                return_value=fake_now,
            ),
            patch.object(HSEMIntegrationSensor, "__init__", _fake_integration_init),
            patch.object(HSEMUtilityMeterSensor, "__init__", _fake_utility_init),
        ):
            await sensor._async_handle_update()

        avg_sensors = [e for e in added if isinstance(e, HSEMAvgSensor)]
        assert len(avg_sensors) == 4
        for avg_s in avg_sensors:
            assert avg_s._tracked_entity == expected_tracked, (
                f"Avg sensor {avg_s._attr_unique_id} must track the utility meter "
                f"({expected_tracked})"
            )


# ---------------------------------------------------------------------------
# 4. async_added_to_hass — state restore and availability on restart
# ---------------------------------------------------------------------------


class TestRestartBehaviour:
    """On HA restart the previous power state is restored for display, but the
    sensor is marked unavailable until the first live measurement so that the
    IntegrationSensor does not accumulate the restored value as active power."""

    @pytest.mark.asyncio
    async def test_state_restored_from_previous_run(self) -> None:
        """async_added_to_hass must restore the previous numeric state.

        We suppress ``_async_handle_update`` with a no-op so we can inspect
        ``_state`` exactly as it was set by the restore step, before the update
        cycle would clear it (when outside the active window).
        """
        sensor, _ = _make_sensor(hour_start=18)
        _attach_hass(sensor)

        fake_old_state = MagicMock()
        fake_old_state.state = "1234.5"
        fake_old_state.attributes = {"last_updated": "2026-05-11T18:30:00"}

        with (
            patch.object(
                sensor,
                "async_get_last_state",
                new=AsyncMock(return_value=fake_old_state),
            ),
            # No-op the update cycle so we can inspect the restored value directly.
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

        # The restore step sets _state; the update cycle is suppressed so the
        # value is visible here.
        assert sensor._state == pytest.approx(1234.5), (
            "Previous state must be restored by async_added_to_hass"
        )
        # _available must be False after restore (set explicitly before the update
        # cycle) so the IntegrationSensor does not accumulate the restored value.
        assert sensor._available is False

    @pytest.mark.asyncio
    async def test_sensor_unavailable_after_restore_outside_active_window(self) -> None:
        """After restore + first update outside the active window, sensor is unavailable.

        The restored state is cleared to ``None`` by the reset-at-start-of-cycle
        in ``_async_handle_update``, preventing the IntegrationSensor from
        accumulating it.
        """
        sensor, _ = _make_sensor(hour_start=18)
        _attach_hass(sensor)

        fake_old_state = MagicMock()
        fake_old_state.state = "1234.5"
        fake_old_state.attributes = {"last_updated": "2026-05-11T18:30:00"}

        fake_now = MagicMock()
        fake_now.hour = 3  # Outside active window
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
            patch.object(HSEMIntegrationSensor, "__init__", _fake_integration_init),
            patch.object(HSEMUtilityMeterSensor, "__init__", _fake_utility_init),
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

        # After the update cycle outside the active window, state is None and
        # sensor is unavailable — so the integral sensor won't accumulate it.
        assert sensor._state is None
        assert sensor._available is False

    @pytest.mark.asyncio
    async def test_last_updated_is_read_from_previous_run(self) -> None:
        """async_added_to_hass must read last_updated from the restored state.

        ``_last_updated`` is subsequently overwritten by the first
        ``_async_handle_update`` call (when ``_state_previous`` is None).
        This test verifies the attribute is loaded before the update cycle runs.
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

    @pytest.mark.asyncio
    async def test_no_state_restored_when_no_previous_state(self) -> None:
        """When there is no previous state (first boot), _state stays None."""
        sensor, _ = _make_sensor(hour_start=18)
        _attach_hass(sensor)

        fake_now = MagicMock()
        fake_now.hour = 3
        fake_now.isoformat.return_value = "2026-05-12T03:00:00"

        with (
            patch.object(
                sensor,
                "async_get_last_state",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "custom_components.hsem.custom_sensors.house_consumption_power_sensor.dt_util.now",
                return_value=fake_now,
            ),
            patch.object(HSEMIntegrationSensor, "__init__", _fake_integration_init),
            patch.object(HSEMUtilityMeterSensor, "__init__", _fake_utility_init),
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

        assert sensor._state is None
        assert sensor._available is False
