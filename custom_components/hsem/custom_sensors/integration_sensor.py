from homeassistant.components.integration.sensor import IntegrationSensor
from homeassistant.components.sensor.const import SensorDeviceClass, SensorStateClass

from custom_components.hsem.entity import HSEMEntity


class HSEMIntegrationSensor(IntegrationSensor, HSEMEntity):
    """Custom Integration Sensor with device_info."""

    _attr_icon = "mdi:chart-histogram"

    def __init__(self, *args, config_entry=None, **kwargs):
        IntegrationSensor.__init__(self, *args, **kwargs)
        HSEMEntity.__init__(self, config_entry)

    @property
    def state_class(self):
        return SensorStateClass.TOTAL

    @property
    def device_class(self):
        return SensorDeviceClass.ENERGY

    @property
    def unique_id(self):
        return self._attr_unique_id
