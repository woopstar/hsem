"""Diagnostic sensor that exposes daily plan-vs-actual tracking metrics.

Shows cumulative actual vs planned energy/cost values since midnight,
with a 90-day rolling history persisted to a JSON file.

State
-----
The sensor state is the net cost actual (import cost − export revenue)
for today, rounded to 3 decimal places.

Attributes
----------
- ``today`` — today's actual vs plan vs diff record.
- ``yesterday`` — yesterday's record from history (or ``None``).
- ``history`` — last 30 days of records from the JSON file.
- ``history_file`` — path to the JSON history file.
- ``history_days`` — configured maximum history window (default 90).
- ``history_total_days`` — number of days currently stored.

The sensor is a *diagnostic* entity (``EntityCategory.DIAGNOSTIC``).
"""

from __future__ import annotations

from typing import Any, cast, override

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.coordinator import (
    HSEMDataUpdateCoordinator,
)
from custom_components.hsem.entity import HSEMCoordinatorEntity, HSEMEntity
from custom_components.hsem.models.daily_plan_vs_actual_tracker import (
    DailyPlanVsActualTracker,
)
from custom_components.hsem.utils.sensornames import (
    get_daily_plan_vs_actual_sensor_entity_id,
    get_daily_plan_vs_actual_sensor_name,
    get_daily_plan_vs_actual_sensor_unique_id,
)


class HSEMDailyPlanVsActualSensor(
    HSEMCoordinatorEntity,
    RestoreEntity,
    SensorEntity,
    HSEMEntity,
):
    """Diagnostic sensor exposing daily plan-vs-actual metrics.

    State: net cost actual today (currency).
    Attributes: today, yesterday, history, and metadata.
    """

    _attr_icon = "mdi:scale-balance"
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

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
        self._attr_unique_id = get_daily_plan_vs_actual_sensor_unique_id(
            config_entry.entry_id
        )
        self._attr_name = get_daily_plan_vs_actual_sensor_name()
        self.entity_id = get_daily_plan_vs_actual_sensor_entity_id()

    # ------------------------------------------------------------------
    # HA entity properties
    # ------------------------------------------------------------------

    @property
    @override
    def native_value(self) -> str | float | None:
        """Return the sensor state.

        State is the net cost actual today.  Returns ``None`` when
        the tracker is not yet initialised.
        """
        tracker = self._get_tracker()
        if tracker is None:
            return None
        today_record = tracker.get_today_record()
        return cast(float, round(today_record.net_cost_actual, 3))

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return diagnostic attributes for the sensor."""
        tracker = self._get_tracker()
        if tracker is None:
            return None
        return cast(dict[str, Any], tracker.as_sensor_attributes())

    @property
    @override
    def should_poll(self) -> bool:
        """No polling — driven by the coordinator."""
        return False

    @property
    @override
    def available(self) -> bool:
        """True once the coordinator has completed at least one successful cycle."""
        return self.coordinator.last_update_success

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    def _get_tracker(self) -> DailyPlanVsActualTracker | None:
        """Return the daily plan-vs-actual tracker from the coordinator."""
        return getattr(self.coordinator, "_daily_tracker", None)
