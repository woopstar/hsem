from homeassistant.components.sensor.const import SensorDeviceClass, SensorStateClass
from homeassistant.components.utility_meter.sensor import UtilityMeterSensor
from homeassistant.const import UnitOfEnergy

from custom_components.hsem.entity import HSEMEntity


class HSEMUtilityMeterSensor(UtilityMeterSensor, HSEMEntity):
    """Custom Utility Meter Sensor with device_info."""

    _attr_icon = "mdi:counter"

    def __init__(self, *args, id: str, e_id: str, config_entry=None, **kwargs):
        UtilityMeterSensor.__init__(self, *args, **kwargs)
        HSEMEntity.__init__(self, config_entry)
        self._attr_unique_id = id
        self.entity_id = e_id

    @property
    def unique_id(self):
        return self._attr_unique_id

    @property
    def unit_of_measurement(self):
        return UnitOfEnergy.KILO_WATT_HOUR

    @property
    def device_class(self):
        return SensorDeviceClass.ENERGY

    @property
    def state_class(self):
        return SensorStateClass.TOTAL

    @property
    def should_poll(self):
        return True
