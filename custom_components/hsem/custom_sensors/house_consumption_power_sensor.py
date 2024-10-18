import logging
from datetime import datetime

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.event import async_track_state_change_event

from ..const import DEFAULT_HSEM_HOUSE_POWER_INCLUDES_EV_CHARGER_POWER, DOMAIN, ICON
from ..entity import HSEMEntity
from ..utils.misc import convert_to_boolean, convert_to_float, get_config_value

_LOGGER = logging.getLogger(__name__)


class HouseConsumptionPowerSensor(SensorEntity, HSEMEntity):
    """Representation of a sensor that tracks power consumption per hour block."""

    _attr_icon = ICON
    _attr_has_entity_name = True

    def __init__(self, config_entry, hour_start, hour_end):
        super().__init__(config_entry)
        self._hsem_house_consumption_power = None
        self._hsem_house_consumption_power_state = 0.0
        self._hsem_ev_charger_power = None
        self._hsem_ev_charger_power_state = 0.0
        self._hsem_house_power_includes_ev_charger_power = None
        self._hsem_house_power_includes_ev_charger_power_state = (
            DEFAULT_HSEM_HOUSE_POWER_INCLUDES_EV_CHARGER_POWER
        )
        self._hour_start = hour_start
        self._hour_end = hour_end
        self._unique_id = (
            f"{DOMAIN}_house_consumption_power_{hour_start:02d}_{hour_end:02d}"
        )
        self._state = None
        self._config_entry = config_entry
        self._last_updated = None
        self._update_settings()

    def set_hsem_house_consumption_power(self, value):
        self._hsem_house_consumption_power = value

    def set_hsem_house_power_includes_ev_charger_power(self, value):
        self._hsem_house_power_includes_ev_charger_power = value

    def set_hsem_ev_charger_power(self, value):
        self._hsem_ev_charger_power = value

    @property
    def name(self):
        return f"House Consumption {self._hour_start:02d}-{self._hour_end:02d} Hourly Power"

    @property
    def unit_of_measurement(self):
        return "W"

    @property
    def device_class(self):
        return "power"

    @property
    def unique_id(self):
        return self._unique_id

    @property
    def state(self):
        return self._state

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        return {
            "house_consumption_power_entity": self._hsem_house_consumption_power,
            "house_consumption_power_state": self._hsem_house_consumption_power_state,
            "ev_charger_power_entity": self._hsem_ev_charger_power,
            "ev_charger_power_state": self._hsem_ev_charger_power_state,
            "house_power_includes_ev_charger_power": self._hsem_house_power_includes_ev_charger_power,
            "hour_start": self._hour_start,
            "hour_end": self._hour_end,
            "last_updated": self._last_updated,
            "unique_id": self._unique_id,
        }

    def _update_settings(self):
        """Fetch updated settings from config_entry options."""
        self.set_hsem_house_consumption_power(
            get_config_value(self._config_entry, "hsem_house_consumption_power")
        )
        self.set_hsem_ev_charger_power(
            get_config_value(self._config_entry, "hsem_ev_charger_power")
        )
        self.set_hsem_house_power_includes_ev_charger_power(
            get_config_value(
                self._config_entry, "hsem_house_power_includes_ev_charger_power"
            )
        )

    async def async_added_to_hass(self):
        """Handle when sensor is added to Home Assistant."""
        await super().async_added_to_hass()

        old_state = await self.async_get_last_state()
        if old_state is not None:
            self._state = old_state.state
            self._last_updated = old_state.attributes.get("last_updated", None)
        else:
            self._state = 0.0

        # Track state changes for the source sensor
        if self._hsem_house_consumption_power:
            _LOGGER.info(
                f"Starting to track state changes for {self._hsem_house_consumption_power}"
            )
            async_track_state_change_event(
                self.hass, [self._hsem_house_consumption_power], self._handle_update
            )

        if self._hsem_ev_charger_power:
            _LOGGER.info(
                f"Starting to track state changes for {self._hsem_ev_charger_power}"
            )
            async_track_state_change_event(
                self.hass, [self._hsem_ev_charger_power], self._handle_update
            )

    async def _handle_update(self, event):
        """Handle updates to the source sensor."""
        now = datetime.now()

        if self._hsem_house_consumption_power:
            state = self.hass.states.get(self._hsem_house_consumption_power)
            if state:
                self._hsem_house_consumption_power_state = round(
                    convert_to_float(state.state), 2
                )
            else:
                _LOGGER.warning(
                    f"Sensor {self._hsem_house_consumption_power} not found."
                )
        state = None

        if self._hsem_ev_charger_power:
            state = self.hass.states.get(self._hsem_ev_charger_power)
            if state:
                self._hsem_ev_charger_power_state = round(
                    convert_to_float(state.state), 2
                )
            else:
                _LOGGER.warning(f"Sensor {self._hsem_ev_charger_power} not found.")
        state = None

        if now.hour == self._hour_start:
            if self._hsem_house_power_includes_ev_charger_power:
                self._state = float(
                    self._hsem_house_consumption_power_state
                    - self._hsem_ev_charger_power_state
                )
            else:
                self._state = float(self._hsem_house_consumption_power_state)
        else:
            self._state = 0.0

        _LOGGER.debug(f"Updated state for {self._unique_id}: {self._state}")

        # Update last update time
        self._last_updated = now.isoformat()

        # Trigger an update in Home Assistant
        self.async_write_ha_state()

    async def async_update(self):
        """Manually trigger the sensor update."""
        await self._handle_update(event=None)
