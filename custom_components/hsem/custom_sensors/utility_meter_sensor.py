"""Custom HSEM utility meter sensor with HSEM device metadata.

Wraps Home Assistant's built-in :class:`UtilityMeterSensor` so that all
per-hour energy meters appear on the HSEM device page.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor.const import SensorDeviceClass, SensorStateClass
from homeassistant.components.utility_meter.sensor import UtilityMeterSensor
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy

from custom_components.hsem.entity import HSEMEntity


class HSEMUtilityMeterSensor(UtilityMeterSensor, HSEMEntity):
    """Custom Utility Meter Sensor with HSEM device_info.

    Tracks daily energy accumulation per hour block with
    ``state_class=TOTAL`` so the energy dashboard can sum
    across hours correctly.
    """

    _attr_icon = "mdi:counter"

    def __init__(
        self,
        *args: Any,
        id: str,
        e_id: str,
        config_entry: ConfigEntry | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the HSEM utility meter sensor.

        Args:
            *args: Positional arguments forwarded to :class:`UtilityMeterSensor`.
            id: Unique ID for the HA entity registry.
            e_id: Entity ID for the sensor.
            config_entry: The HSEM config entry (required).
            **kwargs: Keyword arguments forwarded to :class:`UtilityMeterSensor`.
        """
        UtilityMeterSensor.__init__(self, *args, **kwargs)
        assert config_entry is not None, (
            "config_entry is required for HSEMUtilityMeterSensor"
        )
        HSEMEntity.__init__(self, config_entry)
        self._attr_unique_id = id
        self.entity_id = e_id

    @property
    def unique_id(self) -> str | None:
        return self._attr_unique_id

    @property  # type: ignore[misc]  # HA stub declares unit_of_measurement as @final
    def unit_of_measurement(self) -> str:
        return UnitOfEnergy.KILO_WATT_HOUR

    @property
    def device_class(self) -> SensorDeviceClass:
        return SensorDeviceClass.ENERGY

    @property
    def state_class(self) -> SensorStateClass:
        return SensorStateClass.TOTAL

    @property
    def should_poll(self) -> bool:
        return True
