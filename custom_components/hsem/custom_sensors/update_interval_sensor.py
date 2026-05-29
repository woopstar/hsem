"""Diagnostic sensor that exposes the HSEM coordinator update interval in minutes.

This sensor makes the current polling cadence visible on the HSEM device page
without having to open the integration options flow.  It is also useful for
debugging timing issues — e.g. verifying that HSEM switches to a 1-minute
interval when input entities are missing.

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
from homeassistant.const import (
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
    UnitOfTime,
)
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.coordinator import (
    CoordinatorData,
    HSEMDataUpdateCoordinator,
)
from custom_components.hsem.entity import HSEMCoordinatorEntity, HSEMEntity
from custom_components.hsem.utils.sensornames import (
    get_update_interval_sensor_entity_id,
    get_update_interval_sensor_name,
    get_update_interval_sensor_unique_id,
)


class HSEMUpdateIntervalSensor(
    HSEMCoordinatorEntity,
    RestoreEntity,
    SensorEntity,
    HSEMEntity,
):
    """Diagnostic sensor exposing the current HSEM update interval in minutes.

    State is the integer number of minutes between coordinator cycles.  The value
    is normally the user-configured ``update_interval``; HSEM temporarily switches
    to 1 minute when input entities are missing so the state may differ.

    The sensor subscribes to the shared coordinator and is updated automatically
    after every coordinator cycle.
    """

    _attr_icon = "mdi:timer-outline"
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_suggested_display_precision = 0

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: HSEMDataUpdateCoordinator,
    ) -> None:
        """Initialise the update-interval sensor.

        Args:
            config_entry: The HSEM config entry.
            coordinator: The shared :class:`HSEMDataUpdateCoordinator`.
        """
        HSEMCoordinatorEntity.__init__(self, coordinator)
        HSEMEntity.__init__(self, config_entry)

        self._config_entry = config_entry

        self._attr_unique_id = get_update_interval_sensor_unique_id()
        self.entity_id = get_update_interval_sensor_entity_id()
        self._name = get_update_interval_sensor_name()

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
    def native_value(self) -> int | None:
        """Return the update interval in minutes."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None or data.cfg is None:
            if self._restored_state is not None:
                try:
                    return int(self._restored_state)
                except (ValueError, TypeError):
                    pass
            return None
        return data.cfg.update_interval

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
        """Return additional configuration context."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None or data.cfg is None:
            return {
                "recommendation_interval_minutes": None,
                "recommendation_interval_length_hours": None,
            }
        return {
            "recommendation_interval_minutes": data.cfg.recommendation_interval_minutes,
            "recommendation_interval_length_hours": data.cfg.recommendation_interval_length,
        }

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
