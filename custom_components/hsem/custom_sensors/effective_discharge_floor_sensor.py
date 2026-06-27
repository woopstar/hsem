"""Diagnostic sensor exposing the effective dynamic discharge floor.

When the dynamic discharge floor feature is enabled, this sensor shows the
effective minimum SoC percentage that the planner is using as the export floor.
When disabled, the sensor state is ``"disabled"``.

The sensor is a *diagnostic* entity (``entity_category = EntityCategory.DIAGNOSTIC``)
so it appears in the *Diagnostic* section of the device page and is excluded
from the default Lovelace dashboard.
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
from custom_components.hsem.utils.sensornames.diagnostics import (
    get_effective_discharge_floor_sensor_entity_id,
    get_effective_discharge_floor_sensor_name,
    get_effective_discharge_floor_sensor_unique_id,
)


class HSEMEffectiveDischargeFloorSensor(
    HSEMCoordinatorEntity,
    RestoreEntity,
    SensorEntity,
    HSEMEntity,
):
    """Diagnostic sensor exposing the effective dynamic discharge floor.

    State is the effective floor SoC percentage (e.g. ``12.5``) when the
    dynamic discharge floor feature is enabled, or ``"disabled"`` when not.
    """

    _attr_icon = "mdi:battery-arrow-down"
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: HSEMDataUpdateCoordinator,
    ) -> None:
        """Initialise the effective discharge floor sensor.

        Args:
            config_entry: The HSEM config entry.
            coordinator: The shared :class:`HSEMDataUpdateCoordinator`.
        """
        HSEMCoordinatorEntity.__init__(self, coordinator)
        HSEMEntity.__init__(self, config_entry)

        self._config_entry = config_entry

        self._attr_unique_id = get_effective_discharge_floor_sensor_unique_id(
            config_entry.entry_id
        )
        self.entity_id = get_effective_discharge_floor_sensor_entity_id()
        self._name = get_effective_discharge_floor_sensor_name()

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

    @property  # type: ignore[misc]
    @override
    def state(self) -> str:
        """Return the effective floor SoC percentage, or ``"disabled"``."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None:
            return self._restored_state or STATE_UNAVAILABLE
        floor_pct = data.effective_discharge_floor_pct
        if floor_pct is None:
            return self._restored_state or "disabled"
        return f"{floor_pct:.1f}"

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
        """Return diagnostic attributes about the dynamic floor computation."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None:
            return {
                "enabled": False,
                "effective_floor_pct": None,
                "safety_margin": None,
                "bridge_duration_hours": None,
                "reserve_kwh": None,
                "next_refill_slot": None,
            }
        diag = data.effective_discharge_floor_diag or {}
        return {
            "enabled": data.effective_discharge_floor_pct is not None,
            "effective_floor_pct": data.effective_discharge_floor_pct,
            "safety_margin": diag.get("safety_margin"),
            "bridge_duration_hours": diag.get("bridge_duration_hours"),
            "reserve_kwh": diag.get("reserve_kwh"),
            "next_refill_slot": diag.get("next_refill_slot"),
            "refill_type": diag.get("refill_type"),
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
