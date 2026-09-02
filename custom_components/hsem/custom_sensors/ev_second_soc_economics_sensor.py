"""Sensor exposing the HSEM second EV SoC economics cost/feasibility table.

Mirrors :class:`~HSEMEVSoCEconomicsSensor` but reads
``coordinator.data.ev_second_soc_economics`` instead of the primary table.

See :mod:`ev_soc_economics_sensor` for full documentation.
"""

from __future__ import annotations

from typing import Any, override

from homeassistant.components.sensor import SensorEntity
from homeassistant.components.sensor.const import SensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.coordinator import (
    CoordinatorData,
    HSEMDataUpdateCoordinator,
)
from custom_components.hsem.entity import HSEMCoordinatorEntity, HSEMEntity
from custom_components.hsem.utils.sensornames.ev import (
    get_ev_second_soc_economics_sensor_entity_id,
    get_ev_second_soc_economics_sensor_name,
    get_ev_second_soc_economics_sensor_unique_id,
)

_VALID_STATES = {
    "not_connected",
    "ready",
    "smart_charging_disabled",
    STATE_UNAVAILABLE,
}


class HSEMEVSecondSoCEconomicsSensor(
    HSEMCoordinatorEntity,
    RestoreEntity,
    SensorEntity,
    HSEMEntity,
):
    """Sensor exposing the HSEM second EV SoC economics state and cost table."""

    _attr_icon = "mdi:cash-multiple"
    _attr_has_entity_name = True
    _attr_translation_key = "ev_second_soc_economics"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = sorted(_VALID_STATES)
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: HSEMDataUpdateCoordinator,
    ) -> None:
        """Initialise the second EV SoC economics sensor."""
        HSEMCoordinatorEntity.__init__(self, coordinator)
        HSEMEntity.__init__(self, config_entry)

        self._config_entry = config_entry
        self._attr_unique_id = get_ev_second_soc_economics_sensor_unique_id(
            config_entry.entry_id
        )
        self.entity_id = get_ev_second_soc_economics_sensor_entity_id()
        self._name = get_ev_second_soc_economics_sensor_name()

        self._restored_state: str | None = None

    @property
    @override
    def name(self) -> str:
        """Return the display name."""
        return self._name

    @property
    @override
    def unique_id(self) -> str | None:
        """Return the unique ID."""
        return self._attr_unique_id

    @property  # type: ignore[misc]  # HA stub declares state as @final
    @override
    def state(self) -> str:
        """Return the current second EV SoC economics computation state."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None:
            return self._restored_state or STATE_UNAVAILABLE
        result = data.ev_second_soc_economics
        if result is None:
            return STATE_UNAVAILABLE
        state = result.state
        return state if state in _VALID_STATES else STATE_UNAVAILABLE

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return second EV SoC economics attributes."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None or data.ev_second_soc_economics is None:
            return {}
        return data.ev_second_soc_economics.as_attributes()

    @property
    @override
    def available(self) -> bool:
        """Return True when the coordinator has data."""
        return self.coordinator.data is not None

    @property
    @override
    def should_poll(self) -> bool:
        """Return False — this sensor is coordinator-driven."""
        return False

    @override
    async def async_added_to_hass(self) -> None:
        """Restore previous state on startup."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in _VALID_STATES:
            self._restored_state = last.state
