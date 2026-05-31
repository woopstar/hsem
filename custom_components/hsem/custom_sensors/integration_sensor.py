"""Custom HSEM integration sensor that converts power (W) to energy (kWh).

Wraps Home Assistant's built-in :class:`IntegrationSensor` with HSEM device
metadata so that all derived energy sensors appear on the HSEM device page.
"""

from __future__ import annotations

from typing import Any, override

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
        """Initialize the HSEM integration sensor.

        Args:
            *args: Positional arguments forwarded to :class:`IntegrationSensor`.
            id: Unique ID for the HA entity registry.
            e_id: Entity ID for the sensor.
            config_entry: The HSEM config entry (required).
            **kwargs: Keyword arguments forwarded to :class:`IntegrationSensor`.
        """
        IntegrationSensor.__init__(self, *args, **kwargs)
        assert config_entry is not None, (
            "config_entry is required for HSEMIntegrationSensor"
        )
        HSEMEntity.__init__(self, config_entry)
        self._attr_unique_id = id
        self.entity_id = e_id

    @property
    @override
    def state_class(self) -> SensorStateClass:
        """Return TOTAL_INCREASING so the energy dashboard integrates correctly."""
        return SensorStateClass.TOTAL_INCREASING

    @property
    @override
    def device_class(self) -> SensorDeviceClass:
        """Return ENERGY device class."""
        return SensorDeviceClass.ENERGY

    @property
    @override
    def unique_id(self) -> str | None:
        return self._attr_unique_id

    @property
    @override
    def should_poll(self) -> bool:
        return True
