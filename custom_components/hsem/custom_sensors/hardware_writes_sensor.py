"""Diagnostic sensor that exposes whether HSEM hardware writes are currently allowed.

The state is ``"allowed"`` when HSEM can write commands to the inverter and battery,
or ``"blocked"`` when writes are gated by the degraded-mode error state (critical
entities missing).

Note: read-only mode is a separate, intentional configuration toggle exposed by
:class:`~custom_components.hsem.custom_sensors.read_only_sensor.HSEMReadOnlySensor`.
This sensor reflects only the *safety gate* — whether the planner considers the
available data complete enough to safely issue hardware commands.

The sensor is a *diagnostic* entity (``entity_category = EntityCategory.DIAGNOSTIC``)
so it appears in the *Diagnostic* section of the device page and is excluded from
the default Lovelace dashboard.

This sensor subscribes to :class:`~custom_components.hsem.coordinator.HSEMDataUpdateCoordinator`
and updates automatically after every coordinator cycle.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.coordinator import (
    CoordinatorData,
    HSEMDataUpdateCoordinator,
)
from custom_components.hsem.entity import HSEMCoordinatorEntity, HSEMEntity
from custom_components.hsem.utils.degraded_mode import hardware_writes_allowed
from custom_components.hsem.utils.sensornames import (
    get_hardware_writes_sensor_entity_id,
    get_hardware_writes_sensor_name,
    get_hardware_writes_sensor_unique_id,
)

_ALLOWED = "allowed"
_BLOCKED = "blocked"
_VALID_STATES = {_ALLOWED, _BLOCKED}


class HSEMHardwareWritesSensor(
    HSEMCoordinatorEntity,
    SensorEntity,
    HSEMEntity,
    RestoreEntity,
):
    """Diagnostic sensor exposing whether HSEM hardware writes are allowed.

    State is ``"allowed"`` when writes are safe (data quality ok or only
    non-critical entities missing), ``"blocked"`` when critical data is absent.

    The sensor subscribes to the shared coordinator and is updated automatically
    after every coordinator cycle.
    """

    _attr_icon = "mdi:transmission-tower"
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: HSEMDataUpdateCoordinator,
    ) -> None:
        """Initialise the hardware-writes sensor.

        Args:
            config_entry: The HSEM config entry.
            coordinator: The shared :class:`HSEMDataUpdateCoordinator`.
        """
        HSEMCoordinatorEntity.__init__(self, coordinator)
        HSEMEntity.__init__(self, config_entry)

        self._config_entry = config_entry

        self._attr_unique_id = get_hardware_writes_sensor_unique_id()
        self.entity_id = get_hardware_writes_sensor_entity_id()
        self._name = get_hardware_writes_sensor_name()

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
        """Return ``'allowed'`` or ``'blocked'`` based on degraded-mode state."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None or data.live is None:
            return self._restored_state or _ALLOWED
        return (
            _ALLOWED if hardware_writes_allowed(data.live.degraded_mode) else _BLOCKED
        )

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
        """Return details about why writes are blocked (if applicable).

        Includes the degraded-mode reason, missing entity list, read-only
        toggle status, and the current planning horizon.
        """
        data: CoordinatorData | None = self.coordinator.data
        if data is None or data.live is None:
            return {
                "degraded_mode": None,
                "missing_entities_list": [],
                "read_only_active": False,
                "planning_horizon_hours": None,
                "planning_interval_minutes": None,
            }
        live = data.live
        cfg = data.cfg
        attrs = {
            "degraded_mode": live.degraded_mode.value,
            "missing_entities_list": list(live.missing_entities_list),
            "read_only_active": bool(cfg.read_only) if cfg is not None else False,
        }
        if cfg is not None:
            attrs["planning_horizon_hours"] = cfg.recommendation_interval_length
            attrs["planning_interval_minutes"] = cfg.recommendation_interval_minutes
        return attrs

    # ------------------------------------------------------------------
    # HA lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Restore previous state and register coordinator listener."""
        await super().async_added_to_hass()
        restored = await self.async_get_last_state()
        if restored is not None and restored.state in _VALID_STATES:
            self._restored_state = restored.state
