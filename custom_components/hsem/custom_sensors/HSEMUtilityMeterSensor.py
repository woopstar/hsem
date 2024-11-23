from homeassistant.components.utility_meter.sensor import UtilityMeterSensor
from homeassistant.components.sensor.const import SensorDeviceClass
from homeassistant.const import UnitOfEnergy
from custom_components.hsem.entity import HSEMEntity

class HSEMUtilityMeterSensor(UtilityMeterSensor, HSEMEntity):
    """Custom Utility Meter Sensor with device_info."""

    def __init__(self, *args, id=None, config_entry=None, **kwargs):
        UtilityMeterSensor.__init__(self, *args, **kwargs)
        HSEMEntity.__init__(self, config_entry)
        self._unique_id = id

    @property
    def unique_id(self):
        return self._unique_id

    @property
    def unit_of_measurement(self):
        return UnitOfEnergy.KILO_WATT_HOUR

    @property
    def device_class(self):
        return SensorDeviceClass.ENERGY

    async def async_added_to_hass(self):
        """Handle the sensor being added to Home Assistant."""
        await super().async_added_to_hass()

    async def async_will_remove_from_hass(self):
        """Entity being removed from hass."""
        await super().async_will_remove_from_hass()
