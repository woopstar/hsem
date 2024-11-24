from homeassistant.components.sensor.const import SensorDeviceClass, SensorStateClass
from homeassistant.components.utility_meter.sensor import UtilityMeterSensor
from homeassistant.const import UnitOfEnergy

from custom_components.hsem.entity import HSEMEntity


class HSEMUtilityMeterSensor(UtilityMeterSensor, HSEMEntity):
    """Custom Utility Meter Sensor with device_info."""

    _attr_icon = "mdi:counter"

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

    @property
    def state_class(self):
        return SensorStateClass.TOTAL

    async def async_added_to_hass(self):
        """Handle the sensor being added to Home Assistant."""
        await super().async_added_to_hass()

    async def async_will_remove_from_hass(self):
        """Entity being removed from hass."""
        await super().async_will_remove_from_hass()
