"""Diagnostic sensor that exposes the HSEM system health / degraded-mode state.

The sensor reports one of three states that mirror :class:`DegradedMode`:

``ok``
    All required input entities are present and readable.  HSEM is operating
    normally; hardware writes are permitted.

``degraded``
    One or more *non-critical* entities are unavailable (e.g. electricity
    price feed).  Read-only planner calculations continue on best-effort
    values; hardware writes are still allowed because the battery state data
    is intact.

``error``
    One or more *critical* entities are missing (battery SoC, max charge /
    discharge power, rated capacity, or house consumption power).  Hardware
    writes are **blocked** to prevent acting on incomplete state.

The sensor is a *diagnostic* entity (``entity_category = EntityCategory.DIAGNOSTIC``)
so it appears in the *Diagnostic* section of the device page and is excluded
from the default Lovelace dashboard.

This sensor subscribes to :class:`~custom_components.hsem.coordinator.HSEMDataUpdateCoordinator`
and updates automatically after every coordinator cycle without any additional
polling or push from the working-mode sensor.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.coordinator import (
    CoordinatorData,
    HSEMDataUpdateCoordinator,
)
from custom_components.hsem.entity import HSEMCoordinatorEntity, HSEMEntity
from custom_components.hsem.utils.degraded_mode import (
    DegradedMode,
    hardware_writes_allowed,
)
from custom_components.hsem.utils.sensornames import (
    get_degraded_mode_sensor_entity_id,
    get_degraded_mode_sensor_name,
    get_degraded_mode_sensor_unique_id,
)


class HSEMDegradedModeSensor(
    HSEMCoordinatorEntity,
    SensorEntity,
    HSEMEntity,
    RestoreEntity,
):
    """Diagnostic sensor exposing the current HSEM system-health state.

    The state is one of ``"ok"``, ``"degraded"``, or ``"error"`` — matching
    the :attr:`DegradedMode.value` strings.

    The sensor subscribes to the shared coordinator and is updated
    automatically after every coordinator cycle.
    """

    _attr_icon = "mdi:shield-check"
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        config_entry,
        coordinator: HSEMDataUpdateCoordinator,
    ) -> None:
        """Initialise the sensor with an ``ok`` state.

        Args:
            config_entry: The HSEM config entry.
            coordinator: The shared :class:`HSEMDataUpdateCoordinator`.
        """
        HSEMCoordinatorEntity.__init__(self, coordinator)
        HSEMEntity.__init__(self, config_entry)

        self._config_entry = config_entry

        self._attr_unique_id = get_degraded_mode_sensor_unique_id()
        self.entity_id = get_degraded_mode_sensor_entity_id()
        self._name = get_degraded_mode_sensor_name()

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

    @property
    def state(self) -> str:
        """Return the current health state string."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None or data.live is None:
            # Fall back to restored state while waiting for first cycle.
            return self._restored_state or DegradedMode.OK.value
        return data.live.degraded_mode.value

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
        if data is None or data.live is None:
            return {
                "missing_entities": [],
                "hardware_writes_blocked": False,
                "read_only_mode": False,
            }
        live = data.live
        cfg = data.cfg
        return {
            "missing_entities": list(live.missing_entities_list),
            "hardware_writes_blocked": not hardware_writes_allowed(live.degraded_mode),
            "read_only_mode": bool(cfg.read_only) if cfg is not None else False,
        }

    # ------------------------------------------------------------------
    # HA lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Restore previous state and register coordinator listener."""
        await super().async_added_to_hass()
        restored = await self.async_get_last_state()
        if restored is not None and restored.state in {m.value for m in DegradedMode}:
            self._restored_state = restored.state
