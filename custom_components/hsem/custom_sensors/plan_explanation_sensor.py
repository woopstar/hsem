"""Diagnostic sensor that exposes the HSEM planner's plan explanation.

State
-----
The sensor state is the winning candidate name from the planner
(e.g. ``"milp"``, ``"passive"``, ``"no_action"``), matching the
``name`` field in ``rejected_plans`` for direct comparison::

    {{ states('sensor.hsem_plan_explanation_sensor') }}
    {{ state_attr('sensor.hsem_plan_explanation_sensor', 'score') }}
    {{ state_attr('sensor.hsem_plan_explanation_sensor', 'rejected_plans') }}

The ``selected_strategy`` attribute gives a human-readable description
(e.g. ``"charge_grid_discharge_peak"``, ``"winter_wait"``).

Attributes
----------
All fields from :class:`~custom_components.hsem.models.planner_outputs.PlanExplanation`
are exposed as individual state attributes so users can reference them directly
via ``state_attr()`` without parsing a nested dict.  The ``rejected_plans`` list
includes per-candidate cost breakdowns (``import_cost``, ``export_revenue``,
``conversion_loss``, ``cycle_cost``, ``score``) for comparison against the winner.

Additional diagnostic attributes are merged from the coordinator snapshot:

- ``planning_horizon_hours`` — how many hours ahead the planner evaluates.
- ``planning_interval_minutes`` — width of each planning slot in minutes.
- ``forecast_mode`` — ``"winter"`` or ``"summer"`` based on the configured
  month ranges.
- ``current_slot_start`` / ``current_slot_end`` / ``current_slot_recommendation``
  — the active time-slot boundaries and its recommendation.
- ``last_apply_status`` — outcome of the most recent hardware-write cycle.
- ``data_quality_complete`` — ``True`` when all price and PV data is available.

The sensor is a *diagnostic* entity (``EntityCategory.DIAGNOSTIC``) so it
appears in the *Diagnostic* section of the device page and is excluded from
the default Lovelace dashboard.
"""

from __future__ import annotations

from typing import Any, override

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.coordinator import (
    CoordinatorData,
    HSEMDataUpdateCoordinator,
)
from custom_components.hsem.entity import HSEMCoordinatorEntity, HSEMEntity
from custom_components.hsem.models.plan_explanation import PlanExplanation
from custom_components.hsem.utils.datetime_utils import now as hsem_now
from custom_components.hsem.utils.sensornames import (
    get_plan_explanation_sensor_entity_id,
    get_plan_explanation_sensor_name,
    get_plan_explanation_sensor_unique_id,
)

_UNKNOWN_STRATEGY = STATE_UNKNOWN


def _determine_forecast_mode(
    cfg: Any,
    now_tz_aware: Any,
) -> str:
    """Determine the active forecast season (``"winter"`` or ``"summer"``).

    Uses the configured ``months_winter`` list from *cfg* and the current
    month from *now_tz_aware*.
    """
    current_month = now_tz_aware.month
    return "winter" if current_month in (cfg.months_winter or []) else "summer"


class HSEMPlanExplanationSensor(
    HSEMCoordinatorEntity,
    RestoreEntity,
    SensorEntity,
    HSEMEntity,
):
    """Diagnostic sensor exposing the winning plan and candidate scorecard.

    State: winning candidate name (e.g. ``"milp"``, ``"passive"``).
    Attributes: full :class:`PlanExplanation` fields plus per-candidate
    cost breakdowns in ``rejected_plans``.
    """

    _attr_icon = "mdi:chart-gantt"
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
            coordinator: The shared :class:`HSEMDataUpdateCoordinator`.
        """
        HSEMCoordinatorEntity.__init__(self, coordinator)
        HSEMEntity.__init__(self, config_entry)

        self._config_entry = config_entry
        self._attr_unique_id = get_plan_explanation_sensor_unique_id(
            config_entry.entry_id
        )
        self.entity_id = get_plan_explanation_sensor_entity_id()
        self._name = get_plan_explanation_sensor_name()

        # Restored state used before the first coordinator cycle completes.
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
        """Return the winning candidate name (matches rejected_plans).

        Falls back to selected_strategy or restored state.
        """
        data: CoordinatorData | None = self.coordinator.data
        if data is None:
            return self._restored_state or _UNKNOWN_STRATEGY
        winner = data.plan_explanation.winner_name
        if winner:
            return winner
        return data.plan_explanation.selected_strategy or _UNKNOWN_STRATEGY

    @property
    @override
    def should_poll(self) -> bool:
        """No polling — driven by the coordinator."""
        return False

    @property
    @override
    def available(self) -> bool:
        """True once the coordinator has completed at least one successful cycle."""
        return (
            self.coordinator.last_update_success and self.coordinator.data is not None
        ) or self._restored_state is not None

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the full plan explanation merged with diagnostic context.

        Each field on :class:`PlanExplanation` is exposed individually so
        users can template against ``state_attr('sensor.hsem_plan_explanation_sensor',
        'score')`` etc. without needing to unpack a nested dict.

        Context attributes from the coordinator snapshot include:
        *planning_horizon_hours*, *planning_interval_minutes*, *forecast_mode*,
        *current_slot_start*, *current_slot_end*, *current_slot_recommendation*,
        *last_apply_status*, and *data_quality_complete*.
        """
        data: CoordinatorData | None = self.coordinator.data
        explanation: PlanExplanation = (
            data.plan_explanation if data is not None else PlanExplanation()
        )
        d = explanation.as_dict()

        if data is not None and data.cfg is not None and data.live is not None:
            cfg = data.cfg
            now = hsem_now()

            # Planner horizon and slot configuration
            d["planning_horizon_hours"] = cfg.recommendation_interval_length
            d["planning_interval_minutes"] = cfg.recommendation_interval_minutes

            # Forecast mode (winter / summer)
            d["forecast_mode"] = _determine_forecast_mode(cfg, now)

            # Current slot info
            rec = data.hourly_recommendation
            if rec is not None:
                d["current_slot_start"] = rec.start.isoformat()
                d["current_slot_end"] = rec.end.isoformat()
                d["current_slot_recommendation"] = str(rec.recommendation)
            else:
                d["current_slot_start"] = None
                d["current_slot_end"] = None
                d["current_slot_recommendation"] = None

            # Safety / apply status
            apply_summary = data.apply_summary
            d["last_apply_status"] = (
                apply_summary.overall_status.value if apply_summary else None
            )

            # Data quality summary
            d["data_quality_complete"] = data.data_quality.is_complete

        return d

    # ------------------------------------------------------------------
    # HA lifecycle
    # ------------------------------------------------------------------

    @override
    async def async_added_to_hass(self) -> None:
        """Restore previous state and register coordinator listener."""
        await super().async_added_to_hass()
        restored = await self.async_get_last_state()
        if restored is not None and restored.state not in (
            None,
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
        ):
            self._restored_state = restored.state
