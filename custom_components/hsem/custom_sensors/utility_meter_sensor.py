from __future__ import annotations

from typing import Any

from homeassistant.components.sensor.const import SensorDeviceClass, SensorStateClass
from homeassistant.components.utility_meter.sensor import UtilityMeterSensor
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy

from custom_components.hsem.entity import HSEMEntity


class HSEMUtilityMeterSensor(UtilityMeterSensor, HSEMEntity):
    """Custom Utility Meter Sensor with device_info."""

    _attr_icon = "mdi:counter"

    def __init__(
        self,
        *args: Any,
        id: str,
        e_id: str,
        config_entry: ConfigEntry | None = None,
        **kwargs: Any,
    ) -> None:
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
    def device_class(self) -> str:
        return SensorDeviceClass.ENERGY

    @property
    def state_class(self) -> str:
        return SensorStateClass.TOTAL

    @property
    def should_poll(self) -> bool:
        return True
