"""Diagnostic sensor that exposes the timestamp of the last completed HSEM update cycle.

The state is the ISO-format timestamp of the most recently completed coordinator
cycle.  Together with the Next Update sensor this gives a complete picture of
update timing without digging into working-mode sensor attributes.

The sensor is a *diagnostic* entity (``entity_category = EntityCategory.DIAGNOSTIC``)
so it appears in the *Diagnostic* section of the device page and is excluded from
the default Lovelace dashboard.

This sensor subscribes to :class:`~custom_components.hsem.coordinator.HSEMDataUpdateCoordinator`
and updates automatically after every coordinator cycle.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.coordinator import (
    CoordinatorData,
    HSEMDataUpdateCoordinator,
)
from custom_components.hsem.entity import HSEMCoordinatorEntity, HSEMEntity
from custom_components.hsem.utils.sensornames import (
    get_last_updated_sensor_entity_id,
    get_last_updated_sensor_name,
    get_last_updated_sensor_unique_id,
)


class HSEMLastUpdatedSensor(
    HSEMCoordinatorEntity,
    SensorEntity,
    HSEMEntity,
    RestoreEntity,
):
    """Diagnostic sensor exposing the timestamp of the last HSEM update cycle.

    State is the ISO-format string of the most recently completed coordinator
    cycle, or ``None`` until the first cycle has completed.

    The sensor subscribes to the shared coordinator and is updated automatically
    after every coordinator cycle.
    """

    _attr_icon = "mdi:clock-check-outline"
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        config_entry,
        coordinator: HSEMDataUpdateCoordinator,
    ) -> None:
        """Initialise the last-updated sensor.

        Args:
            config_entry: The HSEM config entry.
            coordinator: The shared :class:`HSEMDataUpdateCoordinator`.
        """
        HSEMCoordinatorEntity.__init__(self, coordinator)
        HSEMEntity.__init__(self, config_entry)

        self._config_entry = config_entry

        self._attr_unique_id = get_last_updated_sensor_unique_id()
        self.entity_id = get_last_updated_sensor_entity_id()
        self._name = get_last_updated_sensor_name()

        self._restored_state: str | None = None

    # ------------------------------------------------------------------
    # HA entity properties
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        """Return the display name."""
        return self._name

    @property
    def unique_id(self) -> str | None:
        """Return the unique ID."""
        return self._attr_unique_id

    @property
    def state(self) -> str | None:
        """Return the last-updated ISO timestamp."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None:
            return self._restored_state
        return data.last_updated

    @property
    def should_poll(self) -> bool:
        """No polling — driven by the coordinator."""
        return False

    @property
    def available(self) -> bool:
        """True once the coordinator has completed at least one successful cycle."""
        return (
            self.coordinator.last_update_success and self.coordinator.data is not None
        ) or self._restored_state is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the next-update timestamp as context."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None:
            return {"next_update": None}
        return {"next_update": data.next_update}

    # ------------------------------------------------------------------
    # HA lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Restore previous state and register coordinator listener."""
        await super().async_added_to_hass()
        restored = await self.async_get_last_state()
        if restored is not None and restored.state not in {
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
            None,
        }:
            self._restored_state = restored.state
