"""Sensor exposing the HSEM EV SoC economics cost/feasibility table.

State
-----
The sensor state reflects the current EV SoC economics computation state:

- ``not_connected``           — No vehicle plugged in.
- ``smart_charging_disabled`` — Smart charging feature disabled or EV disabled.
- ``ready``                   — Cost table computed and available.
- ``unavailable``             — EV integration disabled or configuration invalid.

Attributes
----------
All fields from
:class:`~custom_components.hsem.planner.ev_soc_economics.EVSoCEconomicsResult`
are exposed as sensor attributes — including the flat ``points`` list with
the real-money cost of charging to each target SoC by each deadline — so
dashboards and automations can access them without parsing nested dicts.

No auto-recommended target is exposed: the raw cost/delta numbers are
surfaced and the user decides.

The sensor is a *diagnostic* entity (``EntityCategory.DIAGNOSTIC``) so it
appears in the *Diagnostic* section of the device page.
"""

from __future__ import annotations

from typing import Any, override

from homeassistant.components.sensor import SensorEntity
from homeassistant.components.sensor.const import SensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.coordinator import (
    CoordinatorData,
    HSEMDataUpdateCoordinator,
)
from custom_components.hsem.entity import HSEMCoordinatorEntity, HSEMEntity
from custom_components.hsem.utils.sensornames.ev import (
    get_ev_soc_economics_sensor_entity_id,
    get_ev_soc_economics_sensor_name,
    get_ev_soc_economics_sensor_unique_id,
)

_VALID_STATES = {
    "not_connected",
    "ready",
    "smart_charging_disabled",
    STATE_UNAVAILABLE,
}


class HSEMEVSoCEconomicsSensor(
    HSEMCoordinatorEntity,
    RestoreEntity,
    SensorEntity,
    HSEMEntity,
):
    """Sensor exposing the HSEM EV SoC economics state and cost table.

    State: one of the ``_VALID_STATES`` strings.
    Attributes: all fields from :class:`~EVSoCEconomicsResult`.
    """

    _attr_icon = "mdi:cash-multiple"
    _attr_has_entity_name = True
    _attr_translation_key = "ev_soc_economics"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = sorted(_VALID_STATES)
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: HSEMDataUpdateCoordinator,
    ) -> None:
        """Initialise the EV SoC economics sensor.

        Args:
            config_entry: The HSEM config entry.
            coordinator: The shared :class:`HSEMDataUpdateCoordinator`.
        """
        HSEMCoordinatorEntity.__init__(self, coordinator)
        HSEMEntity.__init__(self, config_entry)

        self._config_entry = config_entry
        self._attr_unique_id = get_ev_soc_economics_sensor_unique_id(
            config_entry.entry_id
        )
        self.entity_id = get_ev_soc_economics_sensor_entity_id()
        self._name = get_ev_soc_economics_sensor_name()

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
        """Return the current EV SoC economics computation state."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None:
            return self._restored_state or STATE_UNAVAILABLE
        result = data.ev_soc_economics
        if result is None:
            return STATE_UNAVAILABLE
        state = result.state
        return state if state in _VALID_STATES else STATE_UNAVAILABLE

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return EV SoC economics attributes."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None or data.ev_soc_economics is None:
            return {}
        return data.ev_soc_economics.as_attributes()

    @property
    @override
    def available(self) -> bool:
        """Return True when the coordinator has data."""
        return self.coordinator.data is not None

    @property
    @override
    def should_poll(self) -> bool:
        """Return False — this sensor is coordinator-driven."""
        return False

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
