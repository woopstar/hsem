from __future__ import annotations

from typing import Any

from homeassistant.components.integration.sensor import IntegrationSensor
from homeassistant.components.sensor.const import SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigEntry

from custom_components.hsem.entity import HSEMEntity


class HSEMIntegrationSensor(IntegrationSensor, HSEMEntity):
    """Custom Integration Sensor (power → energy) with HSEM device_info.

    Uses ``state_class=TOTAL_INCREASING`` so that Home Assistant's energy
    dashboard and long-term statistics correctly treat it as a monotonically
    increasing energy accumulator that resets periodically.
    """

    _attr_icon = "mdi:chart-histogram"

    def __init__(
        self,
        *args: Any,
        id: str,
        e_id: str,
        config_entry: ConfigEntry | None = None,
        **kwargs: Any,
    ) -> None:
        IntegrationSensor.__init__(self, *args, **kwargs)
        HSEMEntity.__init__(self, config_entry)
        self._attr_unique_id = id
        self.entity_id = e_id

    @property
    def state_class(self) -> SensorStateClass:
        """Return TOTAL_INCREASING so the energy dashboard integrates correctly."""
        return SensorStateClass.TOTAL_INCREASING

    @property
    def device_class(self) -> SensorDeviceClass:
        """Return ENERGY device class."""
        return SensorDeviceClass.ENERGY

    @property
    def unique_id(self) -> str | None:
        return self._attr_unique_id

    @property
    def should_poll(self) -> bool:
        return True
