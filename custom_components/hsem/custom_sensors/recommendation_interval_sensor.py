"""Diagnostic sensor that exposes the HSEM recommendation interval configuration.

Two related settings define the planning granularity:

- ``recommendation_interval_minutes``: The width of each planning slot (15 or 60 min).
- ``recommendation_interval_length``: The planning horizon in hours (e.g. 24 or 48 h).

The sensor state is the slot width in minutes; the planning horizon is exposed as
an attribute.  Both values are useful for understanding how far ahead HSEM is
planning and at what resolution.

The sensor is a *diagnostic* entity (``entity_category = EntityCategory.DIAGNOSTIC``)
so it appears in the *Diagnostic* section of the device page and is excluded from
the default Lovelace dashboard.

This sensor subscribes to :class:`~custom_components.hsem.coordinator.HSEMDataUpdateCoordinator`
and updates automatically after every coordinator cycle.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
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
    get_recommendation_interval_sensor_entity_id,
    get_recommendation_interval_sensor_name,
    get_recommendation_interval_sensor_unique_id,
)


class HSEMRecommendationIntervalSensor(
    HSEMCoordinatorEntity,
    SensorEntity,
    HSEMEntity,
    RestoreEntity,
):
    """Diagnostic sensor exposing the HSEM recommendation slot-width in minutes.

    State is the slot width in minutes (integer).  The planning horizon (hours)
    is available as the ``recommendation_interval_length_hours`` attribute.

    The sensor subscribes to the shared coordinator and is updated automatically
    after every coordinator cycle.
    """

    _attr_icon = "mdi:calendar-clock"
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_suggested_display_precision = 0

    def __init__(
        self,
        config_entry,
        coordinator: HSEMDataUpdateCoordinator,
    ) -> None:
        """Initialise the recommendation-interval sensor.

        Args:
            config_entry: The HSEM config entry.
            coordinator: The shared :class:`HSEMDataUpdateCoordinator`.
        """
        HSEMCoordinatorEntity.__init__(self, coordinator)
        HSEMEntity.__init__(self, config_entry)

        self._config_entry = config_entry

        self._attr_unique_id = get_recommendation_interval_sensor_unique_id()
        self.entity_id = get_recommendation_interval_sensor_entity_id()
        self._name = get_recommendation_interval_sensor_name()

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
        """Return the recommendation slot width in minutes."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None or data.cfg is None:
            if self._restored_state is not None:
                try:
                    return int(self._restored_state)
                except (ValueError, TypeError):
                    pass
            return None
        return data.cfg.recommendation_interval_minutes

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
        """Return the planning horizon (hours) as an attribute."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None or data.cfg is None:
            return {
                "recommendation_interval_length_hours": None,
                "total_planning_slots": None,
            }
        cfg = data.cfg
        slots = (
            cfg.recommendation_interval_length * 60
        ) // cfg.recommendation_interval_minutes
        return {
            "recommendation_interval_length_hours": cfg.recommendation_interval_length,
            "total_planning_slots": slots,
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
