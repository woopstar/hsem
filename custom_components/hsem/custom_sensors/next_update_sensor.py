"""Diagnostic sensor that exposes the timestamp of the next scheduled HSEM update.

The state is the ISO-format timestamp of the next coordinator cycle.  Useful for
automations that need to know when HSEM will next refresh and for debugging timing
issues (e.g. verifying the update interval is applied correctly).

The sensor is a *diagnostic* entity (``entity_category = EntityCategory.DIAGNOSTIC``)
so it appears in the *Diagnostic* section of the device page and is excluded from
the default Lovelace dashboard.

This sensor subscribes to :class:`~custom_components.hsem.coordinator.HSEMDataUpdateCoordinator`
and updates automatically after every coordinator cycle.
"""

from __future__ import annotations

from typing import Any, override

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.coordinator import (
    CoordinatorData,
    HSEMDataUpdateCoordinator,
)
from custom_components.hsem.entity import HSEMCoordinatorEntity, HSEMEntity
from custom_components.hsem.utils.sensornames import (
    get_next_update_sensor_entity_id,
    get_next_update_sensor_name,
    get_next_update_sensor_unique_id,
)


class HSEMNextUpdateSensor(
    HSEMCoordinatorEntity,
    RestoreEntity,
    SensorEntity,
    HSEMEntity,
):
    """Diagnostic sensor exposing the next scheduled HSEM update timestamp.

    State is the ISO-format string of the next coordinator cycle, or ``None``
    until the first cycle has completed.

    The sensor subscribes to the shared coordinator and is updated automatically
    after every coordinator cycle.
    """

    _attr_icon = "mdi:clock-outline"
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: HSEMDataUpdateCoordinator,
    ) -> None:
        """Initialise the next-update sensor.

        Args:
            config_entry: The HSEM config entry.
            coordinator: The shared :class:`HSEMDataUpdateCoordinator`.
        """
        HSEMCoordinatorEntity.__init__(self, coordinator)
        HSEMEntity.__init__(self, config_entry)

        self._config_entry = config_entry

        self._attr_unique_id = get_next_update_sensor_unique_id()
        self.entity_id = get_next_update_sensor_entity_id()
        self._name = get_next_update_sensor_name()

        self._restored_state: str | None = None

    # ------------------------------------------------------------------
    # HA entity properties
    # ------------------------------------------------------------------

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
    def state(self) -> str | None:
        """Return the next-update ISO timestamp."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None:
            return self._restored_state
        return data.next_update

    @property
    @override
    def should_poll(self) -> bool:
        """No polling — driven by the coordinator."""
        return False

    @property
    @override
    def available(self) -> bool:
        """True once the coordinator has completed at least one successful cycle."""
        return (
            self.coordinator.last_update_success and self.coordinator.data is not None
        ) or self._restored_state is not None

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return diagnostic attributes."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None:
            return {"last_updated": None, "update_interval_minutes": None}
        return {
            "last_updated": data.last_updated,
            "update_interval_minutes": (
                data.cfg.update_interval if data.cfg is not None else None
            ),
        }

    # ------------------------------------------------------------------
    # HA lifecycle
    # ------------------------------------------------------------------

    @override
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
