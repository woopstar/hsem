"""Diagnostic sensor that exposes the HSEM planner's plan explanation.

State
-----
The sensor state is the ``selected_strategy`` string produced by the planner
(e.g. ``"charge_grid_discharge_peak"``, ``"winter_wait"``,
``"opportunistic_charge"``).  This makes it trivial to use in HA automations,
conditional cards, and template sensors::

    {{ states('sensor.hsem_plan_explanation_sensor') }}
    {{ state_attr('sensor.hsem_plan_explanation_sensor', 'score') }}

Attributes
----------
All fields from :class:`~custom_components.hsem.models.planner_outputs.PlanExplanation`
are exposed as individual state attributes so users can reference them directly
via ``state_attr()`` without parsing a nested dict.  The ``rejected_plans`` list
is included as-is (a list of dicts).

The sensor is a *diagnostic* entity (``EntityCategory.DIAGNOSTIC``) so it
appears in the *Diagnostic* section of the device page and is excluded from
the default Lovelace dashboard.
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
from custom_components.hsem.models.planner_outputs import PlanExplanation
from custom_components.hsem.utils.sensornames import (
    get_plan_explanation_sensor_entity_id,
    get_plan_explanation_sensor_name,
    get_plan_explanation_sensor_unique_id,
)

_UNKNOWN_STRATEGY = "unknown"


class HSEMPlanExplanationSensor(
    HSEMCoordinatorEntity,
    SensorEntity,
    HSEMEntity,
    RestoreEntity,
):
    """Diagnostic sensor exposing the active HSEM planner strategy.

    State: ``selected_strategy`` string from :class:`PlanExplanation`.
    Attributes: all other :class:`PlanExplanation` fields as flat key-value
    pairs, plus ``rejected_plans`` as a list of dicts.
    """

    _attr_icon = "mdi:chart-gantt"
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        config_entry,
        coordinator: HSEMDataUpdateCoordinator,
    ) -> None:
        """Initialise the sensor.

        Args:
            config_entry: The HSEM config entry.
            coordinator: The shared :class:`HSEMDataUpdateCoordinator`.
        """
        HSEMCoordinatorEntity.__init__(self, coordinator)
        HSEMEntity.__init__(self, config_entry)

        self._config_entry = config_entry
        self._attr_unique_id = get_plan_explanation_sensor_unique_id()
        self.entity_id = get_plan_explanation_sensor_entity_id()
        self._name = get_plan_explanation_sensor_name()

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
        """Return the currently active plan strategy.

        Returns ``"unknown"`` while waiting for the first coordinator cycle.
        Falls back to the last restored state on HA restart.
        """
        data: CoordinatorData | None = self.coordinator.data
        if data is None:
            return self._restored_state or _UNKNOWN_STRATEGY
        return data.plan_explanation.selected_strategy or _UNKNOWN_STRATEGY

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
        """Return the full plan explanation as flat key-value attributes.

        Each field on :class:`PlanExplanation` is exposed individually so
        users can template against ``state_attr('sensor.hsem_plan_explanation_sensor',
        'score')`` etc. without needing to unpack a nested dict.
        """
        data: CoordinatorData | None = self.coordinator.data
        explanation: PlanExplanation = (
            data.plan_explanation if data is not None else PlanExplanation()
        )
        d = explanation.as_dict()
        # Hoist all keys to the top level (as_dict already returns a flat dict
        # except for rejected_plans which is a list of dicts — leave that as-is).
        return d

    # ------------------------------------------------------------------
    # HA lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Restore previous state and register coordinator listener."""
        await super().async_added_to_hass()
        restored = await self.async_get_last_state()
        if restored is not None and restored.state not in (
            None,
            "unavailable",
            "unknown",
        ):
            self._restored_state = restored.state
