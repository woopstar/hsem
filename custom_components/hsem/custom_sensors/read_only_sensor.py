"""Diagnostic sensor that exposes whether HSEM is operating in read-only (dry-run) mode.

When ``read_only`` is ``True`` no commands are sent to the inverter or battery
hardware regardless of the planner recommendation.  This sensor makes that
state clearly visible on the device page so users can tell at a glance why
hardware writes are not happening.

The sensor is a *diagnostic* entity (``entity_category = EntityCategory.DIAGNOSTIC``)
so it appears in the *Diagnostic* section of the device page and is excluded
from the default Lovelace dashboard.

This sensor subscribes to :class:`~custom_components.hsem.coordinator.HSEMDataUpdateCoordinator`
and updates automatically after every coordinator cycle without any additional
polling.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF, STATE_ON, EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.coordinator import (
    CoordinatorData,
    HSEMDataUpdateCoordinator,
)
from custom_components.hsem.entity import HSEMCoordinatorEntity, HSEMEntity
from custom_components.hsem.utils.sensornames import (
    get_read_only_sensor_entity_id,
    get_read_only_sensor_name,
    get_read_only_sensor_unique_id,
)


class HSEMReadOnlySensor(
    HSEMCoordinatorEntity,
    RestoreEntity,
    SensorEntity,
    HSEMEntity,
):
    """Diagnostic sensor exposing whether HSEM is in read-only mode.

    State is ``"on"`` when read-only mode is active (no hardware writes),
    ``"off"`` when HSEM is allowed to write to the inverter / battery.

    The sensor subscribes to the shared coordinator and is updated
    automatically after every coordinator cycle.
    """

    _attr_icon = "mdi:eye-off-outline"
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: HSEMDataUpdateCoordinator,
    ) -> None:
        """Initialise the read-only mode sensor.

        Args:
            config_entry: The HSEM config entry.
            coordinator: The shared :class:`HSEMDataUpdateCoordinator`.
        """
        HSEMCoordinatorEntity.__init__(self, coordinator)
        HSEMEntity.__init__(self, config_entry)

        self._config_entry = config_entry

        self._attr_unique_id = get_read_only_sensor_unique_id()
        self.entity_id = get_read_only_sensor_entity_id()
        self._name = get_read_only_sensor_name()

        # Restored state used before the first coordinator cycle completes.
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

    @property  # type: ignore[misc]  # HA stub declares state as @final
    def state(self) -> str:
        """Return ``'on'`` when read-only mode is active, ``'off'`` otherwise."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None or data.cfg is None:
            return self._restored_state or STATE_OFF
        return STATE_ON if bool(data.cfg.read_only) else STATE_OFF

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
        """Return diagnostic attributes visible on the entity detail page."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None or data.cfg is None:
            return {
                "read_only": False,
                "hardware_writes_active": True,
            }
        return {
            "read_only": bool(data.cfg.read_only),
            "hardware_writes_active": not bool(data.cfg.read_only),
            "update_interval_minutes": data.cfg.update_interval,
        }

    # ------------------------------------------------------------------
    # HA lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Restore previous state and register coordinator listener."""
        await super().async_added_to_hass()
        restored = await self.async_get_last_state()
        if restored is not None and restored.state in {STATE_ON, STATE_OFF}:
            self._restored_state = restored.state
