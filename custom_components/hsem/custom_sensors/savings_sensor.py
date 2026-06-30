"""Sensor entity that exposes HSEM savings tracking data.

State
-----
The sensor state is today's actual savings (currency), rounded to 3 decimal
places.  Returns ``None`` when the tracker is not yet initialised.

Attributes
----------
All savings metrics are exposed as flat state attributes:

- ``today_actual`` / ``today_missed`` / ``today_baseline`` — today's values.
- ``last_7_days_actual`` / ``last_7_days_missed`` — rolling 7-day sums.
- ``last_30_days_actual`` / ``last_30_days_missed`` — rolling 30-day sums.
- ``total_actual`` / ``total_missed`` / ``total_baseline`` — cumulative totals.
- ``daily`` — list of daily snapshots (up to 90 days).
- ``max_history_days`` / ``history_total_days`` — history metadata.

The sensor is a *diagnostic* entity (``EntityCategory.DIAGNOSTIC``) with
``device_class: monetary`` and ``state_class: total``.
"""

from __future__ import annotations

from typing import Any, cast, override

from homeassistant.components.sensor import SensorEntity
from homeassistant.components.sensor.const import SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
)
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.coordinator import HSEMDataUpdateCoordinator
from custom_components.hsem.entity import HSEMCoordinatorEntity, HSEMEntity
from custom_components.hsem.models.savings_tracker import SavingsTracker
from custom_components.hsem.utils.sensornames.diagnostics import (
    get_savings_tracker_sensor_entity_id,
    get_savings_tracker_sensor_name,
    get_savings_tracker_sensor_unique_id,
)


class HSEMSavingsSensor(
    HSEMCoordinatorEntity,
    RestoreEntity,
    SensorEntity,
    HSEMEntity,
):
    """Sensor exposing HSEM savings tracking metrics.

    State: today's actual savings (currency).
    Attributes: full savings breakdown with daily history.
    """

    _attr_icon = "mdi:cash-plus"
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: HSEMDataUpdateCoordinator,
    ) -> None:
        """Initialise the sensor.

        Args:
            config_entry: The HSEM config entry.
            coordinator: The shared HSEM coordinator.
        """
        HSEMCoordinatorEntity.__init__(self, coordinator)
        HSEMEntity.__init__(self, config_entry)
        self._attr_unique_id = get_savings_tracker_sensor_unique_id(
            config_entry.entry_id
        )
        self._attr_name = get_savings_tracker_sensor_name()
        self.entity_id = get_savings_tracker_sensor_entity_id()

        self._restored_state: str | None = None

    # ------------------------------------------------------------------
    # HA entity properties
    # ------------------------------------------------------------------

    @property
    @override
    def native_value(self) -> float | None:
        """Return today's actual savings as the sensor state."""
        tracker = self._get_tracker()
        if tracker is None:
            if self._restored_state is not None:
                try:
                    return float(self._restored_state)
                except ValueError, TypeError:
                    pass
            return None
        return cast(float, round(tracker.today_actual, 3))

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return savings metrics as sensor attributes."""
        tracker = self._get_tracker()
        if tracker is None:
            return None
        return cast(dict[str, Any], tracker.as_dict())

    @property
    @override
    def should_poll(self) -> bool:
        """No polling — driven by the coordinator."""
        return False

    @property
    @override
    def available(self) -> bool:
        """True once the coordinator has completed at least one successful cycle."""
        return self.coordinator.last_update_success or self._restored_state is not None

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

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    def _get_tracker(self) -> SavingsTracker | None:
        """Return the savings tracker from the coordinator."""
        return getattr(self.coordinator, "_savings_tracker", None)
