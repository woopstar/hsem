import logging
from datetime import timedelta

from homeassistant.components.integration.sensor import IntegrationSensor
from homeassistant.const import UnitOfTime

from custom_components.hsem.entity import HSEMEntity
from custom_components.hsem.const import DOMAIN
from custom_components.hsem.utils.misc import (
    async_remove_entity_from_ha,
    async_resolve_entity_id_from_unique_id,
)
from custom_components.hsem.utils.sensornames import (
    get_house_consumption_power_sensor_unique_id,
    get_integral_sensor_name,
    get_integral_sensor_unique_id,
)

_LOGGER = logging.getLogger(__name__)

class HSEMIntegrationSensor(IntegrationSensor, HSEMEntity):
    """Custom Integration Sensor with device_info."""

    def __init__(self, *args, config_entry=None, **kwargs):
        IntegrationSensor.__init__(self, *args, **kwargs)
        HSEMEntity.__init__(self, config_entry)

async def add_integral_sensor(self):
    """Add an integral sensor dynamically to convert power to energy."""

    # Create the name and unique id for the integral sensor
    integral_sensor_name = get_integral_sensor_name(self._hour_start, self._hour_end)
    integral_sensor_unique_id = get_integral_sensor_unique_id(
        self._hour_start, self._hour_end
    )
    power_sensor_unique_id = get_house_consumption_power_sensor_unique_id(
        self._hour_start, self._hour_end
    )

    # Resolve the source power sensor entity
    source_entity = await async_resolve_entity_id_from_unique_id(
        self, power_sensor_unique_id
    )

    if not source_entity:
        return

    # Check if the integral sensor already exists
    integral_sensor_exists = await async_resolve_entity_id_from_unique_id(
        self, integral_sensor_unique_id
    )

    if integral_sensor_exists:
        if integral_sensor_unique_id not in self._has_been_removed:
            if await async_remove_entity_from_ha(self, integral_sensor_unique_id):
                self._has_been_removed.append(integral_sensor_unique_id)
                _LOGGER.info(f"Successfully removed '{integral_sensor_name}'.")
    else:
        _LOGGER.warning(
            f"Adding integral sensor {integral_sensor_name} for {source_entity}"
        )

        # Create the integral sensor using the left Reimann method
        integral_sensor = HSEMIntegrationSensor(
            integration_method="left",
            name=integral_sensor_name,
            round_digits=2,
            source_entity=source_entity,
            unique_id=integral_sensor_unique_id,
            unit_prefix="k",
            unit_time=UnitOfTime.HOURS,
            max_sub_interval=timedelta(minutes=1),
            device_info=None,
            config_entry=self._config_entry,
        )

        # Add the integral sensor to Home Assistant
        async_add_entities = self.hass.data[DOMAIN].get(self._config_entry.entry_id)
        if async_add_entities:
            async_add_entities([integral_sensor])
        else:
            _LOGGER.error(f"Could not add integral sensor for {source_entity}")
