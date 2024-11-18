import logging
from datetime import timedelta

from homeassistant.components.utility_meter.const import (
    DATA_TARIFF_SENSORS,
    DATA_UTILITY,
)
from homeassistant.components.utility_meter.sensor import UtilityMeterSensor

from custom_components.hsem.entity import HSEMEntity
from custom_components.hsem.const import DOMAIN
from custom_components.hsem.utils.misc import (
    async_remove_entity_from_ha,
    async_resolve_entity_id_from_unique_id,
)
from custom_components.hsem.utils.sensornames import (
    get_integral_sensor_unique_id,
    get_utility_meter_sensor_name,
    get_utility_meter_sensor_unique_id,
)

_LOGGER = logging.getLogger(__name__)


class HSEMUtilityMeterSensor(UtilityMeterSensor, HSEMEntity):
    """Custom Utility Meter Sensor with device_info."""

    def __init__(self, *args, config_entry=None, **kwargs):
        UtilityMeterSensor.__init__(self, *args, **kwargs)
        HSEMEntity.__init__(self, config_entry)

async def add_utility_meter_sensor(self):
    """Add a utility meter sensor dynamically."""

    # Create the name and unique id for the avg sensor
    utility_meter_name = get_utility_meter_sensor_name(self._hour_start, self._hour_end)
    utility_meter_unique_id = get_utility_meter_sensor_unique_id(
        self._hour_start, self._hour_end
    )
    integral_sensor_unique_id = get_integral_sensor_unique_id(
        self._hour_start, self._hour_end
    )

    # Resolve the source entity (sensor) that the utility meter should track
    source_entity = await async_resolve_entity_id_from_unique_id(
        self, integral_sensor_unique_id
    )

    if not source_entity:
        return

    # Ensure DATA_UTILITY structure exists in hass.data
    if DATA_UTILITY not in self.hass.data:
        self.hass.data[DATA_UTILITY] = {}

    # Ensure the entry_id exists in DATA_UTILITY
    if source_entity not in self.hass.data[DATA_UTILITY]:
        self.hass.data[DATA_UTILITY][source_entity] = {DATA_TARIFF_SENSORS: []}

    # Check if the utility meter already exists
    utility_meter_exists = await async_resolve_entity_id_from_unique_id(
        self, utility_meter_unique_id
    )

    if utility_meter_exists:
        if utility_meter_unique_id not in self._has_been_removed:
            if await async_remove_entity_from_ha(self, utility_meter_unique_id):
                _LOGGER.info(
                    f"Successfully removed '{utility_meter_name}' before re-adding."
                )
                self._has_been_removed.append(utility_meter_unique_id)
    else:
        _LOGGER.warning(
            f"Adding utility meter sensor {utility_meter_name} for {source_entity}"
        )

        # Create the utility meter sensor with the given cycle and source
        utility_meter_sensor = HSEMUtilityMeterSensor(
            cron_pattern=None,
            delta_values=False,
            meter_offset=timedelta(hours=0),
            meter_type="daily",
            name=utility_meter_name,
            net_consumption=False,
            parent_meter=source_entity,
            periodically_resetting=True,
            source_entity=source_entity,
            tariff_entity=None,
            tariff=None,
            unique_id=utility_meter_unique_id,
            device_info=None,
            sensor_always_available=True,
            config_entry=self._config_entry,
        )

        # Add the utility meter to Home Assistant
        async_add_entities = self.hass.data[DOMAIN].get(self._config_entry.entry_id)
        if async_add_entities:
            async_add_entities([utility_meter_sensor])

            # Append the newly created sensor to DATA_TARIFF_SENSORS
            self.hass.data[DATA_UTILITY][source_entity][DATA_TARIFF_SENSORS].append(
                utility_meter_sensor
            )
        else:
            _LOGGER.error(f"Could not add utility meter sensor for {source_entity}")
