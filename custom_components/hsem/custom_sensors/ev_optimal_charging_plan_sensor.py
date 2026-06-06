"""Sensor exposing the HSEM EV optimal charging plan.

State
-----
The sensor state reflects the current EV charging plan state:

- ``not_connected``       — No vehicle plugged in.
- ``smart_charging_disabled`` — Smart charging feature disabled or EV disabled.
- ``fully_charged``       — EV already at or above target SoC.
- ``charging``            — EV is actively charging in the current slot.
- ``waiting``             — EV is connected but not currently scheduled to charge.
- ``unavailable``         — EV integration disabled or configuration invalid.

Attributes
----------
All fields from :class:`~custom_components.hsem.planner.ev_planner.EVChargingPlan`
are exposed as sensor attributes so dashboards and automations can access them
without parsing nested dicts.

The sensor is a *diagnostic* entity (``EntityCategory.DIAGNOSTIC``) so it
appears in the *Diagnostic* section of the device page.
"""

from __future__ import annotations

from typing import Any, override

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.coordinator import (
    CoordinatorData,
    HSEMDataUpdateCoordinator,
)
from custom_components.hsem.entity import HSEMCoordinatorEntity, HSEMEntity
from custom_components.hsem.utils.sensornames.ev import (
    get_ev_optimal_charging_plan_sensor_entity_id,
    get_ev_optimal_charging_plan_sensor_name,
    get_ev_optimal_charging_plan_sensor_unique_id,
)

_VALID_STATES = {
    "charging",
    "fully_charged",
    "not_connected",
    "smart_charging_disabled",
    STATE_UNAVAILABLE,
    "waiting",
}


class HSEMEVOptimalChargingPlanSensor(
    HSEMCoordinatorEntity,
    RestoreEntity,
    SensorEntity,
    HSEMEntity,
):
    """Sensor exposing the HSEM EV optimal charging plan state and attributes.

    State: one of the ``_VALID_STATES`` strings.
    Attributes: all fields from :class:`~EVChargingPlan`.
    """

    _attr_icon = "mdi:ev-station"
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: HSEMDataUpdateCoordinator,
    ) -> None:
        """Initialise the EV optimal charging plan sensor.

        Args:
            config_entry: The HSEM config entry.
            coordinator: The shared :class:`HSEMDataUpdateCoordinator`.
        """
        HSEMCoordinatorEntity.__init__(self, coordinator)
        HSEMEntity.__init__(self, config_entry)

        self._config_entry = config_entry
        self._attr_unique_id = get_ev_optimal_charging_plan_sensor_unique_id(
            config_entry.entry_id
        )
        self.entity_id = get_ev_optimal_charging_plan_sensor_entity_id()
        self._name = get_ev_optimal_charging_plan_sensor_name()

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

    @property  # type: ignore[misc]  # HA stub declares state as @final
    @override
    def state(self) -> str:
        """Return the current EV charging plan state."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None:
            return self._restored_state or STATE_UNAVAILABLE
        plan = data.ev_charging_plan
        if plan is None:
            return STATE_UNAVAILABLE
        state = plan.state
        return state if state in _VALID_STATES else STATE_UNAVAILABLE

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return EV charging plan attributes."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None or data.ev_charging_plan is None:
            return {}
        return data.ev_charging_plan.as_attributes()

    @property
    @override
    def available(self) -> bool:
        """Return True when the coordinator has data."""
        return self.coordinator.data is not None

    # ------------------------------------------------------------------
    # State restore
    # ------------------------------------------------------------------

    @override
    async def async_added_to_hass(self) -> None:
        """Restore previous state on startup."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in _VALID_STATES:
            self._restored_state = last.state
