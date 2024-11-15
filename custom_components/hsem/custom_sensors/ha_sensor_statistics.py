import logging
from datetime import timedelta

from homeassistant.components.statistics.sensor import StatisticsSensor

from custom_components.hsem.const import DOMAIN
from custom_components.hsem.utils.misc import (
    async_remove_entity_from_ha,
    async_resolve_entity_id_from_unique_id,
)
from custom_components.hsem.utils.sensornames import (
    get_energy_average_sensor_name,
    get_energy_average_sensor_unique_id,
    get_utility_meter_sensor_unique_id,
)

_LOGGER = logging.getLogger(__name__)


async def add_energy_average_sensors(self, avg=3):
    # Create the name and unique id for the avg sensor
    avg_energy_sensor_name = get_energy_average_sensor_name(
        self._hour_start, self._hour_end, avg
    )
    avg_energy_sensor_unique_id = get_energy_average_sensor_unique_id(
        self._hour_start, self._hour_end, avg
    )
    utility_meter_unique_id = get_utility_meter_sensor_unique_id(
        self._hour_start, self._hour_end
    )

    # find the energy sensor from the unique id
    source_entity = await async_resolve_entity_id_from_unique_id(
        self, utility_meter_unique_id
    )

    if not source_entity:
        return

    # Check if the avg sensor already exists
    avg_energy_sensor_entity_id = await async_resolve_entity_id_from_unique_id(
        self, avg_energy_sensor_unique_id
    )

    if avg_energy_sensor_entity_id:
        if avg_energy_sensor_unique_id not in self._has_been_removed:
            if await async_remove_entity_from_ha(self, avg_energy_sensor_unique_id):
                _LOGGER.info(
                    f"Successfully removed '{avg_energy_sensor_name}' before re-adding."
                )
                self._has_been_removed.append(avg_energy_sensor_unique_id)
    else:
        # If sensor does not exist, create it and add to Home Assistant
        _LOGGER.warning(
            f"Creating new average energy sensor '{avg_energy_sensor_name}' for '{source_entity}'."
        )

        avg_sensor = StatisticsSensor(
            hass=self.hass,
            source_entity_id=source_entity,
            name=avg_energy_sensor_name,
            unique_id=avg_energy_sensor_unique_id,
            state_characteristic="mean",
            samples_max_buffer_size=(24 * 60 * avg),  # Sampling size
            samples_max_age=timedelta(days=avg),  # Max age
            samples_keep_last=False,
            precision=2,
            percentile=50,
        )

        async_add_entities = self.hass.data[DOMAIN].get(self._config_entry.entry_id)
        if async_add_entities:
            async_add_entities([avg_sensor])
        else:
            _LOGGER.error("Could not add avg sensor for {energy_sensor}")
