from homeassistant.components.statistics.sensor import StatisticsSensor

from custom_components.hsem.entity import HSEMEntity


class HSEMStatisticsSensor(StatisticsSensor, HSEMEntity):
    """Custom Statistics Sensor with device_info."""

    _attr_icon = "mdi:calculator"

    def __init__(self, *args, id=None, config_entry=None, **kwargs):
        StatisticsSensor.__init__(self, *args, **kwargs)
        HSEMEntity.__init__(self, config_entry)
        self._unique_id = id

    @property
    def unique_id(self):
        return self._unique_id

    async def async_added_to_hass(self):
        """Handle the sensor being added to Home Assistant."""
        await super().async_added_to_hass()

    async def async_will_remove_from_hass(self):
        """Entity being removed from hass."""
        await super().async_will_remove_from_hass()
