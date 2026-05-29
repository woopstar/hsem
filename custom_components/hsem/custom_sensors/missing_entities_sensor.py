"""Diagnostic sensor that exposes the count of missing HSEM input entities.

The state is an integer — the number of input entities that were absent or
unreadable during the last coordinator cycle.  When the state is ``0`` all
required entities are present.

This sensor makes the ``missing_entities_list`` automatable: users can trigger
notifications or log entries as soon as any sensor disappears, without having to
inspect the working-mode sensor attributes.

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
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.coordinator import (
    CoordinatorData,
    HSEMDataUpdateCoordinator,
)
from custom_components.hsem.entity import HSEMCoordinatorEntity, HSEMEntity
from custom_components.hsem.utils.sensornames import (
    get_missing_entities_sensor_entity_id,
    get_missing_entities_sensor_name,
    get_missing_entities_sensor_unique_id,
)


class HSEMMissingEntitiesSensor(
    HSEMCoordinatorEntity,
    SensorEntity,
    HSEMEntity,
    RestoreEntity,
):
    """Diagnostic sensor exposing the count of missing HSEM input entities.

    State is an integer count of missing/unreadable entities.  ``0`` means all
    required entities are present and readable.

    The sensor subscribes to the shared coordinator and is updated automatically
    after every coordinator cycle.
    """

    _attr_icon = "mdi:alert-circle-outline"
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = None

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: HSEMDataUpdateCoordinator,
    ) -> None:
        """Initialise the missing-entities sensor.

        Args:
            config_entry: The HSEM config entry.
            coordinator: The shared :class:`HSEMDataUpdateCoordinator`.
        """
        HSEMCoordinatorEntity.__init__(self, coordinator)
        HSEMEntity.__init__(self, config_entry)

        self._config_entry = config_entry

        self._attr_unique_id = get_missing_entities_sensor_unique_id()
        self.entity_id = get_missing_entities_sensor_entity_id()
        self._name = get_missing_entities_sensor_name()

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
    def state(self) -> int:
        """Return the number of missing input entities."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None or data.live is None:
            if self._restored_state is not None:
                try:
                    return int(self._restored_state)
                except (ValueError, TypeError):
                    pass
            return 0
        return len(data.live.missing_entities_list)

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
        """Return the full list of missing entity labels as an attribute."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None or data.live is None:
            return {"missing_entities_list": []}
        return {
            "missing_entities_list": list(data.live.missing_entities_list),
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
