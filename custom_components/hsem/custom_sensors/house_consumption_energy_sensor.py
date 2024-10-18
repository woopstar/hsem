import logging
from datetime import datetime

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.event import async_track_state_change_event

from ..const import DOMAIN, ICON
from ..entity import HSEMEntity
from ..utils.misc import async_resolve_entity_id_from_unique_id

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
        self._config_entry = config_entry
        self._state = 0.0
        self._last_updated = None
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
            "last_updated": self._last_updated,
            "unique_id": self._unique_id,
            "last_reset_date": self._last_reset_date,
        }

    async def async_update(self):
        current_time = datetime.now()

        # Tjek om vi skal nulstille (hvis dagen er ændret)
        if self._last_reset_date != current_time.date():
            _LOGGER.info(f"Nulstiller sensoren for en ny dag: {self.name}")
            self._state = 0.0  # Nulstil energiforbruget
            self._last_reset_date = current_time.date()

        # Slå power sensoren op
        self._hsem_power_sensor_entity = await async_resolve_entity_id_from_unique_id(
            self,
            f"{DOMAIN}_house_consumption_power_{self._hour_start:02d}_{self._hour_end:02d}",
        )

        if not self._hsem_power_sensor_entity:
            _LOGGER.warning(f"Power sensor not found for {self.name}")
            return

        # Hent power-sensorens nuværende værdi
        power_sensor_state = self.hass.states.get(self._hsem_power_sensor_entity)
        if power_sensor_state is None:
            _LOGGER.warning(
                f"Power sensor {self._hsem_power_sensor_entity} not ready or not found. Skipping update."
            )
            return

        try:
            power_value = float(power_sensor_state.state)

            if self._last_updated:
                # Beregn tidsintervallet i sekunder
                time_diff = (current_time - self._last_updated).total_seconds()
                # Konverter effekt til energi (W til kWh)
                self._state += (
                    power_value * time_diff
                ) / 3600000  # Divider med 3600 for kW og 1000 for kWh
            else:
                _LOGGER.debug(f"First update for {self.name}, skipping accumulation.")

            # Sæt sidste opdateringstidspunkt
            self._last_updated = current_time

            # Runde state til 6 decimaler
            self._state = round(self._state, 6)

        except ValueError:
            _LOGGER.warning(
                f"Invalid value from power sensor {self._hsem_power_sensor_entity}: {power_sensor_state.state}"
            )

        # Update Home Assistant state
        self.async_write_ha_state()

    async def async_added_to_hass(self):
        old_state = await self.async_get_last_state()
        if old_state is not None:
            try:
                self._state = float(old_state.state)
                self._last_reset_date = datetime.strptime(
                    old_state.attributes.get("last_reset_date"), "%Y-%m-%d"
                ).date()
            except (ValueError, TypeError):
                _LOGGER.warning(f"Invalid old state value for {self.name}")
                self._state = 0.0
                self._last_reset_date = datetime.now().date()

        # Track power-sensorens tilstand
        if self._hsem_power_sensor_entity:
            async_track_state_change_event(
                self.hass,
                [self._hsem_power_sensor_entity],
                self.async_update,
            )
