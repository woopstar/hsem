import logging
from datetime import timedelta

from homeassistant.components.integration.sensor import IntegrationSensor
from homeassistant.const import UnitOfTime

from custom_components.hsem.utils.misc import async_resolve_entity_id_from_unique_id, async_remove_entity_from_ha
from custom_components.hsem.utils.sensornames import get_integral_sensor_name, get_integral_sensor_unique_id
from custom_components.hsem.const import DOMAIN

_LOGGER = logging.getLogger(__name__)

async def add_integral_sensor(self):
    """Add an integral sensor dynamically to convert power to energy."""

    # Create the name and unique id for the integral sensor
    integral_sensor_name = get_integral_sensor_name(self._hour_start, self._hour_end)
    integral_sensor_unique_id = get_integral_sensor_unique_id(self._hour_start, self._hour_end)

    # Resolve the source power sensor entity
    source_entity = await async_resolve_entity_id_from_unique_id(self, self._unique_id)

    if not source_entity:
        return

    # Check if the integral sensor already exists
    integral_sensor_exists = await async_resolve_entity_id_from_unique_id(self, integral_sensor_unique_id)

    if integral_sensor_exists:
        if integral_sensor_unique_id not in self._has_been_removed:
            if await async_remove_entity_from_ha(self, integral_sensor_unique_id):
                self._has_been_removed.append(integral_sensor_unique_id)
                _LOGGER.info(f"Successfully removed '{integral_sensor_name}'.")
    else:
        _LOGGER.warning(f"Adding integral sensor {integral_sensor_name} for {source_entity}")

        # Create the integral sensor using the left Reimann method
        integral_sensor = IntegrationSensor(
            integration_method="left",
            name=integral_sensor_name,
            round_digits=2,
            source_entity=source_entity,
            unique_id=integral_sensor_unique_id,
            unit_prefix="k",
            unit_time=UnitOfTime.HOURS,
            max_sub_interval=timedelta(minutes=1),
            device_info=None,
        )

        # Add the integral sensor to Home Assistant
        async_add_entities = self.hass.data[DOMAIN].get(self._config_entry.entry_id)
        if async_add_entities:
            async_add_entities([integral_sensor])
        else:
            _LOGGER.error(f"Could not add integral sensor for {source_entity}")
