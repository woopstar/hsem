"""Module for the HouseConsumptionPowerSensor class.

This module defines the HouseConsumptionPowerSensor class, which represents a sensor
that tracks power consumption per hour block in a Home Assistant environment.

Classes:
    HouseConsumptionPowerSensor: A sensor entity that tracks power consumption per hour block.

Functions:
    set_hsem_house_consumption_power(value): Sets the house consumption power entity.
    set_hsem_house_power_includes_ev_charger_power(value): Sets whether house power includes EV charger power.
    set_hsem_ev_charger_power(value): Sets the EV charger power entity.
    name: Returns the name of the sensor.
    unit_of_measurement: Returns the unit of measurement for the sensor.
    device_class: Returns the device class of the sensor.
    unique_id: Returns the unique ID of the sensor.
    state: Returns the current state of the sensor.
    extra_state_attributes: Returns the extra state attributes of the sensor.
    _update_settings(): Fetches updated settings from config_entry options.
    async_added_to_hass(): Handles when the sensor is added to Home Assistant.
    _async_handle_update(event): Handles updates to the source sensor.
    async_update(event=None): Manually triggers the sensor update.
"""

import logging
from datetime import timedelta
from typing import Any

import homeassistant.util.dt as dt_util
import voluptuous as vol
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.sensor.const import SensorDeviceClass, SensorStateClass
from homeassistant.components.utility_meter.const import (
    DATA_TARIFF_SENSORS,
    DATA_UTILITY,
)
from homeassistant.const import UnitOfPower, UnitOfTime
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.custom_sensors.avg_sensor import HSEMAvgSensor
from custom_components.hsem.custom_sensors.integration_sensor import (
    HSEMIntegrationSensor,
)
from custom_components.hsem.custom_sensors.utility_meter_sensor import (
    HSEMUtilityMeterSensor,
)
from custom_components.hsem.entity import HSEMEntity
from custom_components.hsem.utils.misc import (
    async_resolve_entity_id_from_unique_id,
    convert_to_float,
    get_config_value,
    ha_get_entity_state_and_convert,
)
from custom_components.hsem.utils.sensornames import (
    get_energy_average_sensor_entity_id,
    get_energy_average_sensor_name,
    get_energy_average_sensor_unique_id,
    get_house_consumption_power_sensor_entity_id,
    get_house_consumption_power_sensor_name,
    get_house_consumption_power_sensor_unique_id,
    get_integral_sensor_entity_id,
    get_integral_sensor_name,
    get_integral_sensor_unique_id,
    get_utility_meter_sensor_entity_id,
    get_utility_meter_sensor_name,
    get_utility_meter_sensor_unique_id,
)

_LOGGER = logging.getLogger(__name__)


class HSEMHouseConsumptionPowerSensor(SensorEntity, HSEMEntity, RestoreEntity):
    """Representation of a sensor that tracks power consumption per hour block."""

    _attr_icon = "mdi:flash"
    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    # List all attributes to exclude from recording, except state and last_updated
    _unrecorded_attributes = frozenset(
        [
            "status",
            "description",
            "unique_id",
            "house_consumption_power_entity",
            "house_consumption_power_state",
            "ev_charger_power_entity",
            "ev_charger_power_state",
            "house_power_includes_ev_charger_power",
            "hour_start",
            "hour_end",
        ]
    )

    def __init__(self, config_entry, hour_start, hour_end, async_add_entities) -> None:
        super().__init__(config_entry)
        self._available = False
        self._missing_input_entities = True
        self._hsem_house_consumption_power = None
        self._hsem_house_consumption_power_state = 0.0
        self._hsem_ev_charger_power = None
        self._hsem_ev_charger_power_state = 0.0
        self._hsem_house_power_includes_ev_charger_power = None
        self._hour_start = hour_start
        self._hour_end = hour_end
        self._attr_unique_id = get_house_consumption_power_sensor_unique_id(
            hour_start, hour_end
        )
        self.entity_id = get_house_consumption_power_sensor_entity_id(
            hour_start, hour_end
        )
        self._state = None
        self._state_previous = None
        self._config_entry = config_entry
        self._last_updated = None
        # Track which derived sensors have already been created to avoid duplicate adds.
        self._derived_sensors_created: set[str] = set()
        self._tracked_entities = set()
        self._async_add_entities = async_add_entities
        self._name = get_house_consumption_power_sensor_name(
            self._hour_start, self._hour_end
        )
        self._update_settings()

    @property
    def name(self) -> str:
        return self._name

    @property
    def unique_id(self) -> str | None:
        return self._attr_unique_id

    @property
    def state(self) -> float | None:
        return self._state

    @property
    def should_poll(self) -> bool:
        return True

    @property
    def available(self) -> bool:
        return self._available

    @property
    def extra_state_attributes(self) -> dict[str, Any]:

        if self._missing_input_entities:
            return {
                "status": "error",
                "description": "Some of the required input sensors from the config flow is missing or not reporting a state. Check your configuration and make sure input sensors are configured correctly.",
                "last_updated": self._last_updated,
                "unique_id": self._attr_unique_id,
            }

        """Return the state attributes."""
        return {
            "house_consumption_power_entity": self._hsem_house_consumption_power,
            "house_consumption_power_state": round(
                self._hsem_house_consumption_power_state, 2
            ),
            "ev_charger_power_entity": self._hsem_ev_charger_power,
            "ev_charger_power_state": round(self._hsem_ev_charger_power_state, 2),
            "house_power_includes_ev_charger_power": self._hsem_house_power_includes_ev_charger_power,
            "hour_start": self._hour_start,
            "hour_end": self._hour_end,
            "last_updated": self._last_updated,
            "unique_id": self._attr_unique_id,
        }

    async def async_update(self, event=None) -> None:
        """Manually trigger the sensor update."""
        await self._async_handle_update(event)

    async def async_options_updated(self, config_entry) -> None:
        """Handle options update from configuration change."""

        self._update_settings()

        await self._async_handle_update(None)

    async def async_added_to_hass(self) -> None:
        """Handle when sensor is added to Home Assistant.

        We restore ``last_updated`` from the previous run for informational
        purposes, but do NOT restore the power reading itself.  The state is
        intentionally left as ``None`` here and will be set (or remain ``None``)
        by the first ``_async_handle_update`` call depending on whether the
        current hour matches this sensor's active window.  This prevents a
        stale power value from being fed into the IntegrationSensor after a
        restart when the active window has already passed.
        """
        old_state = await self.async_get_last_state()
        if old_state is not None:
            self._last_updated = old_state.attributes.get("last_updated", None)

        # Initial update — sets self._state to a real value or None based on
        # whether the current hour is inside this sensor's active window.
        await self._async_handle_update(None)

        await super().async_added_to_hass()

    def _update_settings(self) -> None:
        """Fetch updated settings from config_entry options."""
        self._hsem_house_consumption_power = get_config_value(
            self._config_entry, "hsem_house_consumption_power"
        )

        self._hsem_ev_charger_power = get_config_value(
            self._config_entry, "hsem_ev_charger_power"
        )

        if self._hsem_ev_charger_power == vol.UNDEFINED:
            self._hsem_ev_charger_power = None

        self._hsem_house_power_includes_ev_charger_power = get_config_value(
            self._config_entry, "hsem_house_power_includes_ev_charger_power"
        )

    async def _async_handle_update(self, event=None) -> None:
        """Handle updates to the source sensor.

        Power is only measured inside the active hour window
        (``now.hour == self._hour_start``).  Outside that window the state is
        ``None``, which HA exposes as ``unknown``.  This causes the downstream
        ``IntegrationSensor`` to pause accumulation and the ``UtilityMeterSensor``
        to stop counting — preventing cross-hour contamination of the energy
        reading for this slot.
        """
        now = dt_util.now()

        # Ensure config flow settings are reloaded if it changed.
        self._update_settings()

        # Track state changes for the source sensors. Also if they change.
        await self._async_track_entities()

        if now.hour == self._hour_start:
            # Active window: measure the current power.
            await self._async_fetch_sensor_states()

            if isinstance(
                self._hsem_ev_charger_power_state, (int, float)
            ) and isinstance(self._hsem_house_consumption_power_state, (int, float)):
                if self._hsem_house_power_includes_ev_charger_power:
                    self._state = round(
                        float(
                            self._hsem_house_consumption_power_state
                            - self._hsem_ev_charger_power_state
                        ),
                        2,
                    )
                else:
                    self._state = round(
                        float(self._hsem_house_consumption_power_state), 2
                    )
        else:
            # Outside the active window: reset to None so the integral sensor
            # does not keep accumulating stale power for this hour slot.
            self._state = None

        # Ensure derived sensors exist — create them only once; skip if already present.
        await self._async_add_integral_sensor()
        await self._async_add_utility_meter_sensor()
        await self._async_add_energy_average_sensors(1)
        await self._async_add_energy_average_sensors(3)
        await self._async_add_energy_average_sensors(7)
        await self._async_add_energy_average_sensors(14)

        # Available only inside the active hour window (when state is non-None).
        self._available = self._state is not None

        if self._state_previous is None or self._state_previous != self._state:
            self._last_updated = now.isoformat()
            self._state_previous = self._state

            # Trigger an update in Home Assistant
            self.async_write_ha_state()

    async def _async_track_entities(self) -> None:
        # Track state changes for the source sensor
        if self._hsem_house_consumption_power:
            if self._hsem_house_consumption_power not in self._tracked_entities:
                _LOGGER.debug(
                    f"Starting to track state changes for {self._hsem_house_consumption_power}"
                )
                async_track_state_change_event(
                    self.hass,
                    [self._hsem_house_consumption_power],
                    self._async_handle_update,
                )
                self._tracked_entities.add(self._hsem_house_consumption_power)

        if self._hsem_ev_charger_power:
            if self._hsem_ev_charger_power not in self._tracked_entities:
                _LOGGER.debug(
                    f"Starting to track state changes for {self._hsem_ev_charger_power}"
                )
                async_track_state_change_event(
                    self.hass, [self._hsem_ev_charger_power], self._async_handle_update
                )
                self._tracked_entities.add(self._hsem_ev_charger_power)

    async def _async_fetch_sensor_states(self) -> None:
        # Update the state of the sensor for house consumption power

        self._missing_input_entities = False

        try:
            if self._hsem_house_consumption_power:
                raw = ha_get_entity_state_and_convert(
                    self, self._hsem_house_consumption_power, "float"
                )
                self._hsem_house_consumption_power_state = convert_to_float(raw)

            # Update the state of the sensor for EV charger power
            if self._hsem_ev_charger_power:
                raw_ev = ha_get_entity_state_and_convert(
                    self, self._hsem_ev_charger_power, "float"
                )
                self._hsem_ev_charger_power_state = convert_to_float(raw_ev)

        except (HomeAssistantError, ValueError, TypeError) as exc:
            _LOGGER.warning(
                "Sensor read failed for entity_id=%s (operation=_async_fetch_sensor_states): "
                "%s: %s",
                self._hsem_house_consumption_power or self._hsem_ev_charger_power,
                type(exc).__name__,
                repr(exc),
            )
            self._missing_input_entities = True

    async def _async_add_integral_sensor(self) -> None:
        """Add an integral sensor dynamically to convert power to energy.

        Skips creation when the entity already exists in the registry so that
        an HA restart does not destroy the historical energy data accumulated
        by the IntegrationSensor.
        """
        integral_sensor_unique_id = get_integral_sensor_unique_id(
            self._hour_start, self._hour_end
        )

        # Short-circuit: already created in this run or already in the registry.
        if integral_sensor_unique_id in self._derived_sensors_created:
            return

        integral_sensor_exists = await async_resolve_entity_id_from_unique_id(
            self, integral_sensor_unique_id
        )
        if integral_sensor_exists:
            # Entity survived the restart — no need to recreate it.
            self._derived_sensors_created.add(integral_sensor_unique_id)
            return

        # Resolve the source power sensor entity before creating the integral sensor.
        power_sensor_unique_id = get_house_consumption_power_sensor_unique_id(
            self._hour_start, self._hour_end
        )
        source_entity = await async_resolve_entity_id_from_unique_id(
            self, power_sensor_unique_id
        )
        if not source_entity:
            return

        integral_sensor_name = get_integral_sensor_name(
            self._hour_start, self._hour_end
        )
        integral_sensor_entity_id = get_integral_sensor_entity_id(
            self._hour_start, self._hour_end
        )

        _LOGGER.debug(
            "Adding integral sensor %s for %s", integral_sensor_name, source_entity
        )

        integral_sensor = HSEMIntegrationSensor(
            integration_method="left",
            name=integral_sensor_name,
            round_digits=2,
            source_entity=source_entity,
            unique_id=integral_sensor_unique_id,
            unit_prefix="k",
            unit_time=UnitOfTime.HOURS,
            max_sub_interval=timedelta(minutes=0),
            hass=self.hass,
            e_id=integral_sensor_entity_id,
            id=integral_sensor_unique_id,
            config_entry=self._config_entry,
        )

        self._async_add_entities([integral_sensor])
        self._derived_sensors_created.add(integral_sensor_unique_id)

    async def _async_add_utility_meter_sensor(self) -> None:
        """Add a utility meter sensor dynamically.

        Skips creation when the entity already exists in the registry so that
        the accumulated daily totals are preserved across HA restarts.  The
        utility meter must track the *energy* integral sensor (kWh), not the
        raw power sensor (W).
        """
        utility_meter_unique_id = get_utility_meter_sensor_unique_id(
            self._hour_start, self._hour_end
        )

        # Short-circuit: already created in this run or already in the registry.
        if utility_meter_unique_id in self._derived_sensors_created:
            return

        utility_meter_exists = await async_resolve_entity_id_from_unique_id(
            self, utility_meter_unique_id
        )
        if utility_meter_exists:
            self._derived_sensors_created.add(utility_meter_unique_id)
            return

        # The utility meter must track the *energy* integral sensor, not the raw
        # power sensor.  Resolve the integral sensor entity first.
        integral_sensor_unique_id = get_integral_sensor_unique_id(
            self._hour_start, self._hour_end
        )
        source_entity = await async_resolve_entity_id_from_unique_id(
            self, integral_sensor_unique_id
        )
        if not source_entity:
            return

        # Ensure DATA_UTILITY structure exists in hass.data
        if DATA_UTILITY not in self.hass.data:
            self.hass.data[DATA_UTILITY] = {}
        if source_entity not in self.hass.data[DATA_UTILITY]:
            self.hass.data[DATA_UTILITY][source_entity] = {DATA_TARIFF_SENSORS: []}

        utility_meter_name = get_utility_meter_sensor_name(
            self._hour_start, self._hour_end
        )
        utility_meter_entity_id = get_utility_meter_sensor_entity_id(
            self._hour_start, self._hour_end
        )

        _LOGGER.debug(
            "Adding utility meter sensor %s for %s", utility_meter_name, source_entity
        )

        utility_meter_sensor = HSEMUtilityMeterSensor(
            cron_pattern=None,
            delta_values=False,
            meter_offset=timedelta(hours=int(self._hour_start)),
            meter_type="daily",
            name=utility_meter_name,
            net_consumption=True,
            parent_meter=source_entity,
            periodically_resetting=True,
            source_entity=source_entity,
            tariff_entity=None,
            tariff=None,
            unique_id=utility_meter_unique_id,
            hass=self.hass,
            sensor_always_available=True,
            id=utility_meter_unique_id,
            e_id=utility_meter_entity_id,
            config_entry=self._config_entry,
        )

        utility_meter_sensor.entity_id = utility_meter_entity_id

        self._async_add_entities([utility_meter_sensor])
        self._derived_sensors_created.add(utility_meter_unique_id)

        # Register with the HA utility-meter book-keeping so the daily-reset
        # event reaches this sensor.
        self.hass.data[DATA_UTILITY][source_entity][DATA_TARIFF_SENSORS].append(
            utility_meter_sensor
        )

    async def _async_add_energy_average_sensors(self, avg: int) -> None:
        """Add a template sensor for the energy average over a given number of days.

        Skips creation when the entity already exists in the registry so that
        the rolling daily measurements are preserved across HA restarts.
        """
        avg_energy_sensor_unique_id = get_energy_average_sensor_unique_id(
            self._hour_start, self._hour_end, avg
        )

        # Short-circuit: already created in this run or already in the registry.
        if avg_energy_sensor_unique_id in self._derived_sensors_created:
            return

        avg_energy_sensor_exists = await async_resolve_entity_id_from_unique_id(
            self, avg_energy_sensor_unique_id
        )
        if avg_energy_sensor_exists:
            self._derived_sensors_created.add(avg_energy_sensor_unique_id)
            return

        utility_meter_unique_id = get_utility_meter_sensor_unique_id(
            self._hour_start, self._hour_end
        )
        utility_meter_entity_id = await async_resolve_entity_id_from_unique_id(
            self, utility_meter_unique_id
        )
        if not utility_meter_entity_id:
            return

        avg_energy_sensor_name = get_energy_average_sensor_name(
            self._hour_start, self._hour_end, avg
        )
        avg_energy_sensor_entity_id = get_energy_average_sensor_entity_id(
            self._hour_start, self._hour_end, avg
        )

        _LOGGER.debug("Creating new average energy sensor '%s'", avg_energy_sensor_name)

        avg_sensor = HSEMAvgSensor(
            config_entry=self._config_entry,
            hour_start=self._hour_start,
            hour_end=self._hour_end,
            avg=avg,
            tracked_entity=utility_meter_entity_id,
            name=avg_energy_sensor_name,
            unique_id=avg_energy_sensor_unique_id,
            entity_id=avg_energy_sensor_entity_id,
        )

        self._async_add_entities([avg_sensor])
        self._derived_sensors_created.add(avg_energy_sensor_unique_id)
