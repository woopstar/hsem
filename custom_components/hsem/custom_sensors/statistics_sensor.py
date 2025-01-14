from homeassistant.components.sensor.const import SensorStateClass
from homeassistant.components.statistics.sensor import StatisticsSensor
from homeassistant.const import UnitOfEnergy

from custom_components.hsem.entity import HSEMEntity


class HSEMStatisticsSensor(StatisticsSensor, HSEMEntity):
    """Custom Statistics Sensor with device_info."""

    _attr_icon = "mdi:calculator"

    def __init__(self, *args, id: str, e_id: str, config_entry=None, **kwargs):
        StatisticsSensor.__init__(self, *args, **kwargs)
        HSEMEntity.__init__(self, config_entry)
        self._attr_unique_id = id
        self.entity_id = e_id

    @property
    def unit_of_measurement(self):
        return UnitOfEnergy.KILO_WATT_HOUR

    @property
    def state_class(self):
        return SensorStateClass.MEASUREMENT

    @property
    def unique_id(self):
        return self._attr_unique_id

    @property
    def should_poll(self):
        return True
