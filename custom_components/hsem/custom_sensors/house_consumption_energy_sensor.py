import logging
from datetime import datetime

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.event import async_track_state_change_event

from ..const import DOMAIN, ICON
from ..entity import HSEMEntity
from ..utils.misc import async_resolve_entity_id_from_unique_id, convert_to_float

_LOGGER = logging.getLogger(__name__)


class HouseConsumptionEnergySensor(SensorEntity, HSEMEntity):
    _attr_icon = ICON
    _attr_has_entity_name = True

    def __init__(self, config_entry, hour_start, hour_end):
        super().__init__(config_entry)
        self._hour_start = hour_start
        self._hour_end = hour_end
        self._unique_id = (
            f"{DOMAIN}_house_consumption_energy_{hour_start:02d}_{hour_end:02d}"
        )
        self._hsem_power_sensor_entity = None
        self._hsem_power_sensor_state = 0.0
        self._config_entry = config_entry
        self._state = 0.0
        self._last_updated = None
        self._entity_is_tracked = False
        self._last_reset_date = None  # Holder styr på den sidste nulstillingsdato

    @property
    def name(self):
        return f"House Consumption {self._hour_start:02d}-{self._hour_end:02d} Energy"

    @property
    def unique_id(self):
        return self._unique_id

    @property
    def state(self):
        return self._state

    @property
    def unit_of_measurement(self):
        return "kWh"

    @property
    def state_class(self):
        return "total"

    @property
    def extra_state_attributes(self):
        return {
            "power_sensor_entity": self._hsem_power_sensor_entity,
            "power_sensor_state": self._hsem_power_sensor_state,
            "last_updated": self._last_updated,
            "unique_id": self._unique_id,
            "last_reset_date": self._last_reset_date,
        }

    async def async_added_to_hass(self):

        old_state = await self.async_get_last_state()
        if old_state is not None:
            try:
                self._state = round(convert_to_float(old_state.state), 2)
                self._last_reset_date = datetime.strptime(
                    old_state.attributes.get("last_reset_date"), "%Y-%m-%d"
                ).date()
                self._last_updated = old_state.attributes.get("last_updated", None)
            except (ValueError, TypeError):
                _LOGGER.warning(f"Invalid old state value for {self.name}")
                self._state = 0.0
                self._last_reset_date = datetime.now().date()
                self._last_updated = None

    async def _handle_update(self, event):
        now = datetime.now()

        # Tjek om vi skal nulstille (hvis dagen er ændret)
        if self._last_reset_date != now.date():
            _LOGGER.info(f"Nulstiller sensoren for en ny dag: {self.name}")
            self._state = 0.0  # Nulstil energiforbruget
            self._last_reset_date = now.date()

        # Find power sensor from unique id
        self._hsem_power_sensor_entity = await async_resolve_entity_id_from_unique_id(
            self,
            f"{DOMAIN}_house_consumption_power_{self._hour_start:02d}_{self._hour_end:02d}",
        )

        if not self._hsem_power_sensor_entity:
            _LOGGER.warning(f"Power sensor not found for {self.name}")
            return

        if self._hsem_power_sensor_entity and not self._entity_is_tracked:
            async_track_state_change_event(
                self.hass,
                [self._hsem_power_sensor_entity],
                self.async_update,
            )
            self._entity_is_tracked = True

        if self._hsem_power_sensor_entity:
            state = self.hass.states.get(self._hsem_power_sensor_entity)
            if state:
                self._hsem_power_sensor_state = round(convert_to_float(state.state), 2)
            else:
                _LOGGER.warning(f"Sensor {self._hsem_power_sensor_entity} not found.")
        state = None

        if now.hour == self._hour_start:
            if self._last_updated and self._hsem_power_sensor_state:
                # Beregn tidsintervallet i sekunder
                time_diff = (
                    now - datetime.fromisoformat(self._last_updated)
                ).total_seconds()

                # Konverter effekt til energi (W til kWh)
                self._state += (self._hsem_power_sensor_state * time_diff) / 3600000
                # Divider med 1000 for kW og 3600 for kWh

                # Round state to two decimals
                self._state = round(self._state, 2)
            else:
                _LOGGER.debug(f"First update for {self.name}, skipping accumulation.")
        else:
            self._state = 0.0

        # Update last update time
        self._last_updated = now.isoformat()

        # Update Home Assistant state
        self.async_write_ha_state()

    async def async_update(self, event=None):
        """Manually trigger the sensor update."""
        await self._handle_update(event=None)
