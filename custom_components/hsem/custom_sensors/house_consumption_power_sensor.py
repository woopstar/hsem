import logging
from datetime import datetime

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.event import async_track_state_change_event

from ..const import DOMAIN, ICON
from ..entity import HSEMEntity
from ..utils.misc import get_config_value

_LOGGER = logging.getLogger(__name__)


class HouseConsumptionPowerSensor(SensorEntity, HSEMEntity):
    """Representation of a sensor that tracks power consumption per hour block."""

    _attr_icon = ICON
    _attr_has_entity_name = True

    def __init__(
        self, config_entry, hour_start, hour_end, hsem_house_consumption_power
    ):
        super().__init__(config_entry)
        self._hsem_house_consumption_power = hsem_house_consumption_power
        self._hour_start = hour_start
        self._hour_end = hour_end
        self._unique_id = (
            f"{DOMAIN}_house_consumption_power_{hour_start:02d}_{hour_end:02d}"
        )
        self._state = None
        self._config_entry = config_entry
        self._last_updated = None
        self._update_settings()

    @property
    def name(self):
        return f"House Consumption {self._hour_start:02d}-{self._hour_end:02d} Power"

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
            "hour_start": self._hour_start,
            "hour_end": self._hour_end,
            "last_updated": self._last_updated,
            "unique_id": self._unique_id,
        }

    def _update_settings(self):
        """Fetch updated settings from config_entry options."""
        self._hsem_house_consumption_power = get_config_value(
            self._config_entry, "hsem_house_consumption_power"
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
        _LOGGER.info(
            f"Starting to track state changes for {self._hsem_house_consumption_power}"
        )
        async_track_state_change_event(
            self.hass, [self._hsem_house_consumption_power], self._handle_update
        )

    async def _handle_update(self, event):
        """Handle updates to the source sensor."""
        current_hour = datetime.now().hour

        if current_hour == self._hour_start:
            state = self.hass.states.get(self._hsem_house_consumption_power)

            if state and state.state:
                try:
                    self._state = float(state.state)
                except ValueError:
                    _LOGGER.warning(
                        f"Invalid state value from {self._hsem_house_consumption_power}: {state.state}"
                    )
                    self._state = 0.0

            self._last_updated = datetime.now().isoformat()
            _LOGGER.debug(f"Updated state for {self._unique_id}: {self._state}")
            self.async_write_ha_state()

    async def async_update(self):
        """Manually trigger the sensor update."""
        await self._handle_update(event=None)
