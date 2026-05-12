"""Diagnostic sensor that exposes whether a force-working-mode override is active.

When the user sets the force-mode select to anything other than ``"auto"``, HSEM
bypasses the planner entirely and sends the manual working mode directly to the
inverter.  This sensor makes that override clearly visible on the device page.

The state is the current value of the force-mode select entity:

- ``"auto"`` — no override; the planner controls the hardware.
- Any other string — the named mode is being forced (e.g. ``"batteries_charge_grid"``).

The sensor is a *diagnostic* entity (``entity_category = EntityCategory.DIAGNOSTIC``)
so it appears in the *Diagnostic* section of the device page and is excluded from
the default Lovelace dashboard.

This sensor subscribes to :class:`~custom_components.hsem.coordinator.HSEMDataUpdateCoordinator`
and updates automatically after every coordinator cycle.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from custom_components.hsem.coordinator import (
    CoordinatorData,
    HSEMDataUpdateCoordinator,
)
from custom_components.hsem.entity import HSEMEntity
from custom_components.hsem.utils.sensornames import (
    get_force_mode_sensor_entity_id,
    get_force_mode_sensor_name,
    get_force_mode_sensor_unique_id,
)


class HSEMForceModeSensor(
    CoordinatorEntity[HSEMDataUpdateCoordinator],
    SensorEntity,
    HSEMEntity,
    RestoreEntity,
):
    """Diagnostic sensor exposing the current force-working-mode state.

    State is the raw value of the force-mode select: ``"auto"`` when no override
    is active, otherwise the name of the forced working mode.

    The sensor subscribes to the shared coordinator and is updated automatically
    after every coordinator cycle.
    """

    _attr_icon = "mdi:hand-pointing-right"
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        config_entry,
        coordinator: HSEMDataUpdateCoordinator,
    ) -> None:
        """Initialise the force-mode sensor.

        Args:
            config_entry: The HSEM config entry.
            coordinator: The shared :class:`HSEMDataUpdateCoordinator`.
        """
        CoordinatorEntity.__init__(self, coordinator)
        HSEMEntity.__init__(self, config_entry)

        self._config_entry = config_entry

        self._attr_unique_id = get_force_mode_sensor_unique_id()
        self.entity_id = get_force_mode_sensor_entity_id()
        self._name = get_force_mode_sensor_name()

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
    def state(self) -> str:
        """Return the force-mode select value (``'auto'`` when not overriding)."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None or data.live is None:
            return self._restored_state or "auto"
        return data.live.force_working_mode_state

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
        """Return whether the override is active and the resolved entity ID."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None or data.live is None:
            return {
                "override_active": False,
                "force_mode_entity_id": None,
            }
        live = data.live
        return {
            "override_active": live.is_forced_mode,
            "force_mode_entity_id": live.force_working_mode,
        }

    # ------------------------------------------------------------------
    # HA lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Restore previous state and register coordinator listener."""
        await super().async_added_to_hass()
        restored = await self.async_get_last_state()
        if restored is not None and restored.state not in {
            "unavailable",
            "unknown",
            None,
        }:
            self._restored_state = restored.state
