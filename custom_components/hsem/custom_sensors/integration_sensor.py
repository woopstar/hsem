from homeassistant.components.integration.sensor import IntegrationSensor
from homeassistant.components.sensor.const import SensorDeviceClass, SensorStateClass

from custom_components.hsem.entity import HSEMEntity


class HSEMIntegrationSensor(IntegrationSensor, HSEMEntity):
    """Custom Integration Sensor with device_info."""

    _attr_icon = "mdi:chart-histogram"

    def __init__(self, *args, id=None, config_entry=None, **kwargs):
        IntegrationSensor.__init__(self, *args, **kwargs)
        HSEMEntity.__init__(self, config_entry)
        self._unique_id = id

    @property
    def state_class(self):
        return SensorStateClass.TOTAL

    @property
    def device_class(self):
        return SensorDeviceClass.ENERGY

    @property
    def unique_id(self):
        return self._unique_id

    async def async_added_to_hass(self):
        """Handle the sensor being added to Home Assistant."""
        await super().async_added_to_hass()

    async def async_will_remove_from_hass(self):
        """Entity being removed from hass."""
        await super().async_will_remove_from_hass()
