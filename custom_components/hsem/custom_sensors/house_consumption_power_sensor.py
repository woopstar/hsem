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
        # Unsubscribe callbacks registered by async_track_* helpers.
        self._unsub_callbacks: list = []
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

        Restores the previous state so HA does not show ``unknown`` immediately
        after a restart.  The sensor is marked **unavailable** after restore so
        that the downstream ``IntegrationSensor`` pauses accumulation until the
        first live measurement is taken inside the active hour window.  Once the
        active window is entered, ``_async_handle_update`` sets a real value and
        flips ``_available`` to ``True``.

        The ``else`` branch in ``_async_handle_update`` will reset ``_state``
        to ``None`` (and ``_available`` to ``False``) as soon as a tick fires
        outside the active window — so the restored value is only visible
        briefly on restart and never feeds stale data into the energy integral.
        """
        old_state = await self.async_get_last_state()
        if old_state is not None:
            restored = convert_to_float(old_state.state)
            if restored is not None:
                self._state = round(restored, 2)
            self._last_updated = old_state.attributes.get("last_updated", None)

        # Mark unavailable so the IntegrationSensor does not accumulate the
        # restored value as live power before the first real measurement.
        self._available = False

        # Initial update — will set a live value (and available=True) when
        # the current hour matches the active window, or reset state to None
        # (and available=False) when outside it.
        await self._async_handle_update(None)

        await super().async_added_to_hass()

    async def async_will_remove_from_hass(self) -> None:
        """Cancel all tracked listeners when the entity is removed."""
        for unsub in self._unsub_callbacks:
            unsub()
        self._unsub_callbacks.clear()
        await super().async_will_remove_from_hass()

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

        ``_state`` is reset to ``None`` at the start of every cycle so that a
        failed sensor fetch inside the active window never leaves stale power
        in place for the ``IntegrationSensor`` to accumulate.

        Power is only measured inside the active hour window
        (``now.hour == self._hour_start``).  Outside that window the state
        remains ``None``, which HA exposes as ``unknown``, causing the
        ``IntegrationSensor`` to pause accumulation and the
        ``UtilityMeterSensor`` to stop counting — preventing cross-hour
        contamination of the energy reading for this slot.
        """
        now = dt_util.now()

        # Ensure config flow settings are reloaded if it changed.
        self._update_settings()

        # Track state changes for the source sensors. Also if they change.
        await self._async_track_entities()

        # Always reset state to None before attempting to measure.  This ensures
        # that a failed fetch inside the active window clears any stale value
        # rather than leaving the previous reading in place for the IntegrationSensor
        # to continue accumulating.
        self._state = None

        if now.hour == self._hour_start:
            # Active window: measure the current power.
            await self._async_fetch_sensor_states()

            if (
                not self._missing_input_entities
                and isinstance(self._hsem_ev_charger_power_state, (int, float))
                and isinstance(self._hsem_house_consumption_power_state, (int, float))
            ):
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
            # else: fetch failed — _state remains None, integral pauses

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
                unsub = async_track_state_change_event(
                    self.hass,
                    [self._hsem_house_consumption_power],
                    self._async_handle_update,
                )
                self._unsub_callbacks.append(unsub)
                self._tracked_entities.add(self._hsem_house_consumption_power)

        if self._hsem_ev_charger_power:
            if self._hsem_ev_charger_power not in self._tracked_entities:
                _LOGGER.debug(
                    f"Starting to track state changes for {self._hsem_ev_charger_power}"
                )
                unsub = async_track_state_change_event(
                    self.hass, [self._hsem_ev_charger_power], self._async_handle_update
                )
                self._unsub_callbacks.append(unsub)
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

        Uses only the per-runtime ``_derived_sensors_created`` set to prevent
        duplicate creation within the same HA session.  The entity registry is
        NOT consulted here: the registry entry survives restarts but the entity
        instance does not, so after a restart we must always create a fresh
        instance with the same stable ``unique_id``.  HA will bind the new
        instance to the existing registry entry automatically, allowing
        ``IntegrationSensor`` to restore its accumulated state.
        """
        integral_sensor_unique_id = get_integral_sensor_unique_id(
            self._hour_start, self._hour_end
        )

        # Per-runtime guard — skip if already created in this HA session.
        if integral_sensor_unique_id in self._derived_sensors_created:
            return

        # The source entity_id is deterministic — derive it directly rather than
        # resolving via the registry so this also works on first boot.
        source_entity = get_house_consumption_power_sensor_entity_id(
            self._hour_start, self._hour_end
        )

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

        Uses only the per-runtime ``_derived_sensors_created`` set to prevent
        duplicate creation within the same HA session.  The utility meter must
        track the *energy* integral sensor (kWh), not the raw power sensor (W).
        The source entity_id is derived deterministically — no registry lookup
        needed.
        """
        utility_meter_unique_id = get_utility_meter_sensor_unique_id(
            self._hour_start, self._hour_end
        )

        # Per-runtime guard — skip if already created in this HA session.
        if utility_meter_unique_id in self._derived_sensors_created:
            return

        # Source is always the integral sensor — derive its entity_id directly.
        source_entity = get_integral_sensor_entity_id(self._hour_start, self._hour_end)

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

        Uses only the per-runtime ``_derived_sensors_created`` set to prevent
        duplicate creation within the same HA session.  The tracked utility-meter
        entity_id is derived deterministically — no registry lookup needed.
        """
        avg_energy_sensor_unique_id = get_energy_average_sensor_unique_id(
            self._hour_start, self._hour_end, avg
        )

        # Per-runtime guard — skip if already created in this HA session.
        if avg_energy_sensor_unique_id in self._derived_sensors_created:
            return

        # Tracked entity is always the utility meter — derive its entity_id directly.
        utility_meter_entity_id = get_utility_meter_sensor_entity_id(
            self._hour_start, self._hour_end
        )

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
