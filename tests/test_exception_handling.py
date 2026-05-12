"""Tests for P0-09 — broad exception-handler removal.

Covers:
- Sensor read failure: specific exceptions propagate correctly / mark missing
  entities when ``ha_get_entity_state_and_convert`` raises.
- Inverter write failure: ``async_set_grid_export_power_pct``,
  ``async_set_tou_periods``, and ``async_set_forcible_discharge`` raise
  ``HomeAssistantError`` on ``ServiceNotFound``, ``ServiceValidationError``,
  ``vol.Invalid``, and propagate ``HomeAssistantError`` directly.
- ``async_set_number_value`` / ``async_set_select_option`` raise on HA errors.
- ``avg_sensor._async_store_utility_meter_value`` degrades cleanly on read error.
- ``house_consumption_power_sensor._async_fetch_sensor_states`` sets the
  missing-entities flag and logs on read error.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceNotFound,
    ServiceValidationError,
)

from custom_components.hsem.utils.misc import (
    EntityNotFoundError,
    async_set_number_value,
    async_set_select_option,
    ha_get_entity_state_and_convert,
)
from datetime import UTC


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_hass_with_state(entity_id: str, state_value: str) -> MagicMock:
    """Return a minimal mock ``hass`` with one entity in the state machine."""
    hass = MagicMock()
    state = MagicMock()
    state.state = state_value
    hass.states.get.return_value = state
    return hass


def _make_sensor(entity_id: str, state_value: str) -> MagicMock:
    """Return a mock sensor that has ``.hass`` and ``.entity_id``."""
    sensor = MagicMock()
    sensor.hass = _make_hass_with_state(entity_id, state_value)
    sensor.entity_id = "sensor.hsem_working_mode"
    return sensor


def _make_inverter_sensor(hass: MagicMock) -> MagicMock:
    """Return a mock sensor with the given ``hass``."""
    sensor = MagicMock()
    sensor.hass = hass
    sensor.entity_id = "sensor.hsem_working_mode"
    return sensor


# ---------------------------------------------------------------------------
# ha_get_entity_state_and_convert — sensor read errors
# ---------------------------------------------------------------------------


class TestSensorReadFailures:
    """Verify that sensor read helpers raise specific, typed exceptions."""

    def test_missing_entity_raises_entity_not_found(self):
        """A completely missing entity raises ``EntityNotFoundError``."""
        sensor = MagicMock()
        sensor.hass = MagicMock()
        sensor.hass.states.get.return_value = None

        with pytest.raises(EntityNotFoundError, match="not found"):
            ha_get_entity_state_and_convert(sensor, "sensor.missing", "float")

    def test_unknown_state_raises_entity_not_found(self):
        """An entity in 'unknown' state raises ``EntityNotFoundError`` for float."""
        sensor = _make_sensor("sensor.bad", "unknown")

        with pytest.raises(EntityNotFoundError, match="unknown"):
            ha_get_entity_state_and_convert(sensor, "sensor.bad", "float")

    def test_unavailable_state_raises_entity_not_found(self):
        """An entity in 'unavailable' state raises ``EntityNotFoundError``."""
        sensor = _make_sensor("sensor.offline", "unavailable")

        with pytest.raises(EntityNotFoundError, match="unavailable"):
            ha_get_entity_state_and_convert(sensor, "sensor.offline", "float")

    def test_non_numeric_float_raises_entity_not_found(self):
        """A non-numeric string for a float entity raises ``EntityNotFoundError``."""
        sensor = _make_sensor("sensor.weird", "not-a-number")

        with pytest.raises(EntityNotFoundError, match="cannot be converted to float"):
            ha_get_entity_state_and_convert(sensor, "sensor.weird", "float")

    def test_valid_float_returns_value(self):
        """A numeric entity state converts cleanly to a rounded float."""
        sensor = _make_sensor("sensor.soc", "83.5")

        result = ha_get_entity_state_and_convert(sensor, "sensor.soc", "float", 1)

        assert result == pytest.approx(83.5)

    def test_entity_not_found_is_homeassistant_error(self):
        """``EntityNotFoundError`` must be a subclass of ``HomeAssistantError``."""
        assert issubclass(EntityNotFoundError, HomeAssistantError)


# ---------------------------------------------------------------------------
# state_collector._read() — marks missing entities on read failure
# ---------------------------------------------------------------------------


class TestStateCollectorReadMissingOnError:
    """Verify that _read() inside async_collect_live_state marks missing entities."""

    @pytest.mark.asyncio
    async def test_entity_not_found_marks_missing(self):
        """When an entity is missing, _read() records it in live.missing_entities."""
        from custom_components.hsem.custom_sensors.state_collector import (
            async_collect_live_state,
        )
        from custom_components.hsem.custom_sensors.config_reader import (
            build_sensor_config,
        )
        from tests.sensors.test_state_collector import _make_config_entry

        # Build a minimal config
        cfg = build_sensor_config(_make_config_entry())

        # hass always returns None (all entities missing)
        hass = MagicMock()
        hass.states.get.return_value = None

        sensor = MagicMock()
        sensor.hass = hass
        sensor.entity_id = "sensor.hsem_test"

        # async_resolve_entity_id_from_unique_id must return a string
        with (
            patch(
                "custom_components.hsem.custom_sensors.state_collector"
                ".async_resolve_entity_id_from_unique_id",
                new_callable=AsyncMock,
                return_value="select.hsem_force_working_mode",
            ),
            patch(
                "custom_components.hsem.custom_sensors.state_collector"
                "._register_listeners",
                new_callable=AsyncMock,
            ),
        ):
            live, _ = await async_collect_live_state(sensor, cfg, None, set())

        assert live.missing_entities is True

    @pytest.mark.asyncio
    async def test_homeassistant_error_on_read_marks_missing(self):
        """A HomeAssistantError from ha_get_entity_state_and_convert marks missing."""
        from custom_components.hsem.custom_sensors.state_collector import (
            async_collect_live_state,
        )
        from custom_components.hsem.custom_sensors.config_reader import (
            build_sensor_config,
        )
        from tests.sensors.test_state_collector import _make_config_entry

        cfg = build_sensor_config(_make_config_entry())

        hass = MagicMock()
        # Return a real-looking state so get() is truthy, but state.state is 'unavailable'
        bad_state = MagicMock()
        bad_state.state = "unavailable"
        hass.states.get.return_value = bad_state

        sensor = MagicMock()
        sensor.hass = hass
        sensor.entity_id = "sensor.hsem_test"

        with (
            patch(
                "custom_components.hsem.custom_sensors.state_collector"
                ".async_resolve_entity_id_from_unique_id",
                new_callable=AsyncMock,
                return_value="select.hsem_force_working_mode",
            ),
            patch(
                "custom_components.hsem.custom_sensors.state_collector"
                "._register_listeners",
                new_callable=AsyncMock,
            ),
        ):
            live, _ = await async_collect_live_state(sensor, cfg, None, set())

        # Critical battery sensors are unavailable → missing_entities
        assert live.missing_entities is True


# ---------------------------------------------------------------------------
# async_set_number_value — inverter write failures
# ---------------------------------------------------------------------------


class TestAsyncSetNumberValueFailures:
    """Verify that ``async_set_number_value`` raises on HA service errors."""

    @pytest.mark.asyncio
    async def test_service_not_found_raises(self):
        """ServiceNotFound must bubble up from async_set_number_value."""
        sensor = MagicMock()
        sensor.hass.states.get.return_value = MagicMock()  # entity exists
        sensor.hass.services.async_call = AsyncMock(
            side_effect=ServiceNotFound("number", "set_value")
        )

        # Patch the logger to prevent ServiceNotFound.__str__ from calling
        # async_get_hass() outside the HA event loop during log formatting.
        with (
            patch("custom_components.hsem.utils.misc._LOGGER"),
            pytest.raises(ServiceNotFound),
        ):
            await async_set_number_value(sensor, "number.charge_power", 3000)

    @pytest.mark.asyncio
    async def test_service_validation_error_raises(self):
        """ServiceValidationError must bubble up from async_set_number_value."""
        sensor = MagicMock()
        sensor.hass.states.get.return_value = MagicMock()
        sensor.hass.services.async_call = AsyncMock(
            side_effect=ServiceValidationError("number", "set_value")
        )

        with pytest.raises(ServiceValidationError):
            await async_set_number_value(sensor, "number.charge_power", 9999)

    @pytest.mark.asyncio
    async def test_homeassistant_error_raises(self):
        """HomeAssistantError must bubble up from async_set_number_value."""
        sensor = MagicMock()
        sensor.hass.states.get.return_value = MagicMock()
        sensor.hass.services.async_call = AsyncMock(
            side_effect=HomeAssistantError("inverter rejected write")
        )

        with pytest.raises(HomeAssistantError, match="inverter rejected write"):
            await async_set_number_value(sensor, "number.charge_power", 500)

    @pytest.mark.asyncio
    async def test_missing_entity_exits_early(self):
        """When the entity is absent, async_set_number_value returns without raising."""
        sensor = MagicMock()
        sensor.hass.states.get.return_value = None  # entity missing
        sensor.hass.services.async_call = AsyncMock()

        # Must NOT raise
        await async_set_number_value(sensor, "number.nonexistent", 100)
        sensor.hass.services.async_call.assert_not_called()


# ---------------------------------------------------------------------------
# async_set_select_option — inverter write failures
# ---------------------------------------------------------------------------


class TestAsyncSetSelectOptionFailures:
    """Verify that ``async_set_select_option`` raises on HA service errors."""

    @pytest.mark.asyncio
    async def test_service_not_found_raises(self):
        """ServiceNotFound must propagate from async_set_select_option."""
        sensor = MagicMock()
        sensor.hass.states.get.return_value = MagicMock()
        sensor.hass.services.async_call = AsyncMock(
            side_effect=ServiceNotFound("select", "select_option")
        )

        with (
            patch("custom_components.hsem.utils.misc._LOGGER"),
            pytest.raises(ServiceNotFound),
        ):
            await async_set_select_option(sensor, "select.working_mode", "TimeOfUse")

    @pytest.mark.asyncio
    async def test_homeassistant_error_raises(self):
        """HomeAssistantError must propagate from async_set_select_option."""
        sensor = MagicMock()
        sensor.hass.states.get.return_value = MagicMock()
        sensor.hass.services.async_call = AsyncMock(
            side_effect=HomeAssistantError("mode change rejected")
        )

        with pytest.raises(HomeAssistantError, match="mode change rejected"):
            await async_set_select_option(
                sensor, "select.working_mode", "Maximise Self Consumption"
            )

    @pytest.mark.asyncio
    async def test_missing_entity_exits_early(self):
        """When entity is absent, async_set_select_option returns without raising."""
        sensor = MagicMock()
        sensor.hass.states.get.return_value = None
        sensor.hass.services.async_call = AsyncMock()

        await async_set_select_option(sensor, "select.missing", "SomeMode")
        sensor.hass.services.async_call.assert_not_called()


# ---------------------------------------------------------------------------
# async_set_grid_export_power_pct — inverter write failures
# ---------------------------------------------------------------------------


class TestAsyncSetGridExportPowerPctFailures:
    """Verify that ``async_set_grid_export_power_pct`` raises specific exceptions."""

    @pytest.mark.asyncio
    async def test_service_not_found_raises_homeassistant_error(self):
        """ServiceNotFound must bubble up (it IS a HomeAssistantError)."""
        from custom_components.hsem.utils.huawei import async_set_grid_export_power_pct

        hass = MagicMock()
        hass.services.has_service.return_value = True
        hass.services.async_call = AsyncMock(
            side_effect=ServiceNotFound(
                "huawei_solar", "set_maximum_feed_grid_power_percent"
            )
        )
        sensor = _make_inverter_sensor(hass)

        with (
            patch("custom_components.hsem.utils.huawei._LOGGER"),
            pytest.raises(ServiceNotFound),
        ):
            await async_set_grid_export_power_pct(sensor, "device_abc", 80)

    @pytest.mark.asyncio
    async def test_vol_invalid_raises_homeassistant_error(self):
        """``vol.Invalid`` must be caught and re-raised as ``HomeAssistantError``."""
        from custom_components.hsem.utils.huawei import async_set_grid_export_power_pct

        hass = MagicMock()
        hass.services.has_service.return_value = True
        hass.services.async_call = AsyncMock(
            side_effect=vol.Invalid("power_percentage must be in [0,100]")
        )
        sensor = _make_inverter_sensor(hass)

        with pytest.raises(HomeAssistantError, match="Invalid input data"):
            await async_set_grid_export_power_pct(sensor, "device_abc", 999)

    @pytest.mark.asyncio
    async def test_service_validation_error_raises(self):
        """ServiceValidationError must propagate from grid export setter."""
        from custom_components.hsem.utils.huawei import async_set_grid_export_power_pct

        hass = MagicMock()
        hass.services.has_service.return_value = True
        hass.services.async_call = AsyncMock(
            side_effect=ServiceValidationError(
                "huawei_solar", "set_maximum_feed_grid_power_percent"
            )
        )
        sensor = _make_inverter_sensor(hass)

        with pytest.raises(ServiceValidationError):
            await async_set_grid_export_power_pct(sensor, "device_abc", 50)


# ---------------------------------------------------------------------------
# async_set_tou_periods — inverter write failures
# ---------------------------------------------------------------------------


class TestAsyncSetTouPeriodsFailures:
    """Verify that ``async_set_tou_periods`` raises specific exceptions."""

    @pytest.mark.asyncio
    async def test_service_not_found_raises(self):
        """ServiceNotFound propagates from async_set_tou_periods."""
        from custom_components.hsem.utils.huawei import async_set_tou_periods

        hass = MagicMock()
        hass.services.has_service.return_value = True
        hass.services.async_call = AsyncMock(
            side_effect=ServiceNotFound("huawei_solar", "set_tou_periods")
        )
        sensor = _make_inverter_sensor(hass)

        with (
            patch("custom_components.hsem.utils.huawei._LOGGER"),
            pytest.raises(ServiceNotFound),
        ):
            await async_set_tou_periods(sensor, "bat_device_1", ["00:00-06:00/100/1/0"])

    @pytest.mark.asyncio
    async def test_vol_multiple_invalid_raises_homeassistant_error(self):
        """``vol.MultipleInvalid`` (subclass of vol.Invalid) wraps as HomeAssistantError."""
        from custom_components.hsem.utils.huawei import async_set_tou_periods

        hass = MagicMock()
        hass.services.has_service.return_value = True
        hass.services.async_call = AsyncMock(
            side_effect=vol.MultipleInvalid([vol.Invalid("bad periods format")])
        )
        sensor = _make_inverter_sensor(hass)

        with pytest.raises(HomeAssistantError, match="Invalid input data"):
            await async_set_tou_periods(sensor, "bat_device_1", ["garbage"])

    @pytest.mark.asyncio
    async def test_service_missing_exits_early(self):
        """When the service does not exist, the function returns without calling it."""
        from custom_components.hsem.utils.huawei import async_set_tou_periods

        hass = MagicMock()
        hass.services.has_service.return_value = False
        hass.services.async_call = AsyncMock()
        sensor = _make_inverter_sensor(hass)

        # Should NOT raise — early return path
        await async_set_tou_periods(sensor, "bat_device_2", ["00:00-06:00/100/1/0"])
        hass.services.async_call.assert_not_called()


# ---------------------------------------------------------------------------
# async_set_forcible_discharge — inverter write failures
# ---------------------------------------------------------------------------


class TestAsyncSetForcibleDischargeFailures:
    """Verify that ``async_set_forcible_discharge`` raises specific exceptions."""

    @pytest.mark.asyncio
    async def test_service_not_found_raises(self):
        """ServiceNotFound propagates from async_set_forcible_discharge."""
        from custom_components.hsem.utils.huawei import async_set_forcible_discharge

        hass = MagicMock()
        hass.services.has_service.return_value = True
        hass.services.async_call = AsyncMock(
            side_effect=ServiceNotFound("huawei_solar", "set_forcible_discharge")
        )
        sensor = _make_inverter_sensor(hass)

        with (
            patch("custom_components.hsem.utils.huawei._LOGGER"),
            pytest.raises(ServiceNotFound),
        ):
            await async_set_forcible_discharge(sensor, "bat_device_3", 20, 2500)

    @pytest.mark.asyncio
    async def test_invalid_target_soc_raises_value_error(self):
        """Out-of-range target_soc raises ``ValueError`` before calling HA."""
        from custom_components.hsem.utils.huawei import async_set_forcible_discharge

        hass = MagicMock()
        hass.services.has_service.return_value = True
        hass.services.async_call = AsyncMock()
        sensor = _make_inverter_sensor(hass)

        with pytest.raises(ValueError, match="target_soc"):
            await async_set_forcible_discharge(sensor, "bat_device_3", 150, 2000)
        hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_power_raises_value_error(self):
        """Negative power raises ``ValueError`` before calling HA."""
        from custom_components.hsem.utils.huawei import async_set_forcible_discharge

        hass = MagicMock()
        hass.services.has_service.return_value = True
        hass.services.async_call = AsyncMock()
        sensor = _make_inverter_sensor(hass)

        with pytest.raises(ValueError, match="power"):
            await async_set_forcible_discharge(sensor, "bat_device_3", 20, -100)
        hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_homeassistant_error_propagates(self):
        """HomeAssistantError from the HA service call propagates unchanged."""
        from custom_components.hsem.utils.huawei import async_set_forcible_discharge

        hass = MagicMock()
        hass.services.has_service.return_value = True
        hass.services.async_call = AsyncMock(
            side_effect=HomeAssistantError("inverter offline")
        )
        sensor = _make_inverter_sensor(hass)

        with pytest.raises(HomeAssistantError, match="inverter offline"):
            await async_set_forcible_discharge(sensor, "bat_device_3", 20, 2500)

    @pytest.mark.asyncio
    async def test_service_missing_exits_early(self):
        """When the service does not exist, the function returns without calling it."""
        from custom_components.hsem.utils.huawei import async_set_forcible_discharge

        hass = MagicMock()
        hass.services.has_service.return_value = False
        hass.services.async_call = AsyncMock()
        sensor = _make_inverter_sensor(hass)

        await async_set_forcible_discharge(sensor, "bat_device_4", 20, 1000)
        hass.services.async_call.assert_not_called()


# ---------------------------------------------------------------------------
# house_consumption_power_sensor — missing-entities flag on read failure
# ---------------------------------------------------------------------------


class TestHouseConsumptionSensorReadFailure:
    """Verify _async_fetch_sensor_states marks missing_entities on read error."""

    @pytest.mark.asyncio
    async def test_entity_not_found_sets_missing_flag(self):
        """EntityNotFoundError during sensor read must set _missing_input_entities."""
        from custom_components.hsem.custom_sensors.house_consumption_power_sensor import (
            HSEMHouseConsumptionPowerSensor,
        )

        # Build a minimal mock sensor instance — we only need the private method
        sensor = MagicMock(spec=HSEMHouseConsumptionPowerSensor)
        sensor.hass = MagicMock()
        sensor._hsem_house_consumption_power = "sensor.house_power"
        sensor._hsem_ev_charger_power = None
        sensor._missing_input_entities = False

        # Patch ha_get_entity_state_and_convert to raise EntityNotFoundError
        with patch(
            "custom_components.hsem.custom_sensors.house_consumption_power_sensor"
            ".ha_get_entity_state_and_convert",
            side_effect=EntityNotFoundError("sensor.house_power not found"),
        ):
            # Call the real method on our mock instance
            await HSEMHouseConsumptionPowerSensor._async_fetch_sensor_states(sensor)

        assert sensor._missing_input_entities is True

    @pytest.mark.asyncio
    async def test_homeassistant_error_sets_missing_flag(self):
        """HomeAssistantError during sensor read must set _missing_input_entities."""
        from custom_components.hsem.custom_sensors.house_consumption_power_sensor import (
            HSEMHouseConsumptionPowerSensor,
        )

        sensor = MagicMock(spec=HSEMHouseConsumptionPowerSensor)
        sensor.hass = MagicMock()
        sensor._hsem_house_consumption_power = "sensor.house_power"
        sensor._hsem_ev_charger_power = None
        sensor._missing_input_entities = False

        with patch(
            "custom_components.hsem.custom_sensors.house_consumption_power_sensor"
            ".ha_get_entity_state_and_convert",
            side_effect=HomeAssistantError("state machine error"),
        ):
            await HSEMHouseConsumptionPowerSensor._async_fetch_sensor_states(sensor)

        assert sensor._missing_input_entities is True

    @pytest.mark.asyncio
    async def test_successful_read_clears_missing_flag(self):
        """A successful read must clear _missing_input_entities."""
        from custom_components.hsem.custom_sensors.house_consumption_power_sensor import (
            HSEMHouseConsumptionPowerSensor,
        )

        sensor = MagicMock(spec=HSEMHouseConsumptionPowerSensor)
        sensor.hass = MagicMock()
        sensor._hsem_house_consumption_power = "sensor.house_power"
        sensor._hsem_ev_charger_power = None
        sensor._missing_input_entities = True  # start dirty

        with (
            patch(
                "custom_components.hsem.custom_sensors.house_consumption_power_sensor"
                ".ha_get_entity_state_and_convert",
                return_value=1250.0,
            ),
            patch(
                "custom_components.hsem.custom_sensors.house_consumption_power_sensor"
                ".convert_to_float",
                return_value=1250.0,
            ),
        ):
            await HSEMHouseConsumptionPowerSensor._async_fetch_sensor_states(sensor)

        assert sensor._missing_input_entities is False


# ---------------------------------------------------------------------------
# avg_sensor — graceful degradation on read failure
# ---------------------------------------------------------------------------


class TestAvgSensorReadFailure:
    """Verify _async_store_utility_meter_value degrades cleanly on read error."""

    @pytest.mark.asyncio
    async def test_entity_not_found_stores_nothing(self):
        """EntityNotFoundError on read leaves the measurement entry absent."""
        from custom_components.hsem.custom_sensors.avg_sensor import HSEMAvgSensor

        sensor = MagicMock(spec=HSEMAvgSensor)
        sensor.hass = MagicMock()
        sensor._tracked_entity = "sensor.daily_kwh"
        sensor._measurements = {}
        sensor._average = 14

        with patch(
            "custom_components.hsem.custom_sensors.avg_sensor"
            ".ha_get_entity_state_and_convert",
            side_effect=EntityNotFoundError("sensor.daily_kwh not found"),
        ):
            await HSEMAvgSensor._async_store_utility_meter_value(sensor)

        # No measurement should have been added
        assert sensor._measurements == {}

    @pytest.mark.asyncio
    async def test_homeassistant_error_stores_nothing(self):
        """HomeAssistantError on read leaves the measurement entry absent."""
        from custom_components.hsem.custom_sensors.avg_sensor import HSEMAvgSensor

        sensor = MagicMock(spec=HSEMAvgSensor)
        sensor.hass = MagicMock()
        sensor._tracked_entity = "sensor.daily_kwh"
        sensor._measurements = {}
        sensor._average = 14

        with patch(
            "custom_components.hsem.custom_sensors.avg_sensor"
            ".ha_get_entity_state_and_convert",
            side_effect=HomeAssistantError("entity unavailable"),
        ):
            await HSEMAvgSensor._async_store_utility_meter_value(sensor)

        assert sensor._measurements == {}

    @pytest.mark.asyncio
    async def test_successful_read_stores_measurement(self):
        """A successful read must store a rounded measurement keyed by today."""
        from custom_components.hsem.custom_sensors.avg_sensor import HSEMAvgSensor
        from datetime import datetime

        sensor = MagicMock(spec=HSEMAvgSensor)
        sensor.hass = MagicMock()
        sensor._tracked_entity = "sensor.daily_kwh"
        sensor._measurements = {}
        sensor._average = 14
        sensor._async_cleanup_old_measurements = AsyncMock()

        fake_now = datetime(2024, 6, 15, 14, 30, tzinfo=UTC)

        with (
            patch(
                "custom_components.hsem.custom_sensors.avg_sensor.dt_util.now",
                return_value=fake_now,
            ),
            patch(
                "custom_components.hsem.custom_sensors.avg_sensor"
                ".ha_get_entity_state_and_convert",
                return_value=7.43,
            ),
        ):
            await HSEMAvgSensor._async_store_utility_meter_value(sensor)

        assert sensor._measurements.get("2024-06-15") == pytest.approx(7.43)
