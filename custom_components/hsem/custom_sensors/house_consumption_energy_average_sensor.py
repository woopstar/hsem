import logging
from collections import deque
from datetime import datetime, timedelta

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.event import async_track_state_change_event

from ..const import DOMAIN, ICON
from ..entity import HSEMEntity
from ..utils.misc import async_resolve_entity_id_from_unique_id, convert_to_float

_LOGGER = logging.getLogger(__name__)


class HouseConsumptionEnergyAverageSensor(SensorEntity, HSEMEntity):
    _attr_icon = ICON
    _attr_has_entity_name = True

    def __init__(
        self, config_entry, hour_start, hour_end, sampling_size=5040, max_age_days=7
    ):
        super().__init__(config_entry)
        self._hour_start = hour_start
        self._hour_end = hour_end
        self._max_age = timedelta(days=max_age_days)
        self._unique_id = f"{DOMAIN}_house_consumption_energy_avg_{hour_start:02d}_{hour_end:02d}_{self._max_age.days}d"
        self._hsem_energy_sensor_entity = None
        self._hsem_energy_sensor_state = 0.0
        self._config_entry = config_entry
        self._state = 0.0
        self._samples = deque(maxlen=sampling_size)
        self._last_updated = None
        self._entity_is_tracked = False

    @property
    def name(self):
        return f"House Consumption {self._hour_start:02d}-{self._hour_end:02d} Energy Average {self._max_age.days}d"

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
        return "measurement"

    @property
    def extra_state_attributes(self):
        return {
            "last_updated": self._last_updated,
            "unique_id": self._unique_id,
            "energy_sensor_entity": self._hsem_energy_sensor_entity,
            "energy_sensor_state": self._hsem_energy_sensor_state,
            "sampling_size": len(self._samples),
            "samples": self._samples,
            "max_age_days": self._max_age.days,
        }

    async def async_update(self):

        # Find energy sensor from unique id
        self._hsem_energy_sensor_entity = await async_resolve_entity_id_from_unique_id(
            self,
            f"{DOMAIN}_house_consumption_energy_{self._hour_start:02d}_{self._hour_end:02d}",
        )

        if not self._hsem_energy_sensor_entity:
            _LOGGER.warning(f"Energy sensor not found for {self.name}")
            return

        if self._hsem_energy_sensor_entity and not self._entity_is_tracked:
            async_track_state_change_event(
                self.hass,
                [self._hsem_energy_sensor_entity],
                self.async_update,
            )
            self._entity_is_tracked = True

        if self._hsem_energy_sensor_entity:
            state = self.hass.states.get(self._hsem_energy_sensor_entity)
            if state:
                self._hsem_energy_sensor_state = round(convert_to_float(state.state), 2)
            else:
                _LOGGER.warning(f"Sensor {self._hsem_energy_sensor_entity} not found.")
        state = None

        now = datetime.now()

        # Tilføj den nye værdi og tidspunkt til samples
        if self._hsem_energy_sensor_state:
            self._samples.append((now, self._hsem_energy_sensor_state))

            # Fjern gamle samples uden for max age
            self._samples = deque(
                [
                    (timestamp, value)
                    for timestamp, value in self._samples
                    if now - timestamp <= self._max_age
                ],
                maxlen=self._samples.maxlen,
            )

            # Beregn gennemsnittet af sampleværdierne
            if self._samples:
                values = [value for _, value in self._samples]
                self._state = round(sum(values) / len(values), 6)
            else:
                self._state = 0.0  # Ingen samples, så ingen gennemsnit

        # Update last update time
        self._last_updated = now.isoformat()

        # Trigger an update in Home Assistant
        self.async_write_ha_state()

    async def async_added_to_hass(self):
        await super().async_added_to_hass()

        old_state = await self.async_get_last_state()
        if old_state is not None:
            try:
                self._state = round(convert_to_float(old_state.state), 2)
                self._last_updated = old_state.attributes.get("last_updated", None)
                # self._samples = old_state.attributes.get("samples")
            except (ValueError, TypeError):
                _LOGGER.warning(f"Invalid old state value for {self.name}")
                self._state = 0.0
