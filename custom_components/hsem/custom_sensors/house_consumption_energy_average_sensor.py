import logging
from collections import deque
from datetime import datetime, timedelta

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.event import async_track_state_change_event

from ..const import DOMAIN, ICON
from ..entity import HSEMEntity
from ..utils.misc import async_resolve_entity_id_from_unique_id

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
        self._config_entry = config_entry
        self._state = 0.0
        self._samples = deque(maxlen=sampling_size)
        self._last_updated = None

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
            "sampling_size": len(self._samples),
            "max_age_days": self._max_age.days,
        }

    async def async_update(self):
        # Slå energisensoren op
        self._hsem_energy_sensor_entity = await async_resolve_entity_id_from_unique_id(
            self,
            f"{DOMAIN}_house_consumption_energy_{self._hour_start:02d}_{self._hour_end:02d}",
        )

        if not self._hsem_energy_sensor_entity:
            _LOGGER.warning(f"Energy sensor not found for {self.name}")
            return

        # Hent energisensorens nuværende værdi
        energy_sensor_state = self.hass.states.get(self._hsem_energy_sensor_entity)
        if energy_sensor_state is None:
            _LOGGER.warning(
                f"Energy sensor {self._hsem_energy_sensor_entity} not ready or not found. Skipping update."
            )
            return

        try:
            energy_value = float(energy_sensor_state.state)
            current_time = datetime.now()

            # Tilføj den nye værdi og tidspunkt til samples
            self._samples.append((current_time, energy_value))

            # Fjern gamle samples uden for max age
            self._samples = deque(
                [
                    (timestamp, value)
                    for timestamp, value in self._samples
                    if current_time - timestamp <= self._max_age
                ],
                maxlen=self._samples.maxlen,
            )

            # Beregn gennemsnittet af sampleværdierne
            if self._samples:
                values = [value for _, value in self._samples]
                self._state = round(sum(values) / len(values), 6)
            else:
                self._state = 0.0  # Ingen samples, så ingen gennemsnit

            # Opdater sidste opdateringstidspunkt
            self._last_updated = current_time

        except ValueError:
            _LOGGER.warning(
                f"Invalid value from energy sensor {self._hsem_energy_sensor_entity}: {energy_sensor_state.state}"
            )

        # Update Home Assistant state
        self.async_write_ha_state()

    async def async_added_to_hass(self):
        await super().async_added_to_hass()

        old_state = await self.async_get_last_state()
        if old_state is not None:
            try:
                self._state = float(old_state.state)
                self._last_updated = datetime.now()
            except (ValueError, TypeError):
                _LOGGER.warning(f"Invalid old state value for {self.name}")
                self._state = 0.0

        # Track energisensorens tilstand
        if self._hsem_energy_sensor_entity:
            async_track_state_change_event(
                self.hass,
                [self._hsem_energy_sensor_entity],
                self.async_update,
            )
