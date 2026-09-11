"""EV SoC economics recomputation for the coordinator.

Extracted to its own module (mirroring ``coordinator_planner_phase.py`` and
friends) to satisfy the repository's 30 KB / 1000-line file limit. Wires the
pure ``planner/ev_soc_economics.py`` module into the coordinator: throttled
recomputation of the "cost to reach target SoC by deadline" table for both
EVs, run at most once per throttle window and always in a single executor
job (each recompute is up to ~8 extra ``run_planner()`` solves per EV).
"""

from __future__ import annotations

from datetime import datetime

from custom_components.hsem.coordinator_helpers import _StaleUpdateCycle
from custom_components.hsem.coordinator_state import CoordinatorSharedState
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.planner.ev_soc_economics import (
    EVSoCEconomicsResult,
    compute_ev_soc_economics,
)

#: Minimum seconds between EV SoC economics recomputes, independent of the
#: normal replan cadence — this table is a "what if" diagnostic, not part of
#: the live plan, and does not need to track every replan.
EV_SOC_ECONOMICS_RECOMPUTE_MIN_SECONDS = 1800.0


def _compute_both_ev_soc_economics(
    planner_input: PlannerInput,
    now: datetime,
) -> tuple[EVSoCEconomicsResult, EVSoCEconomicsResult]:
    """Blocking helper — computes both EVs' tables in one executor job."""
    primary = compute_ev_soc_economics(
        planner_input,
        is_second=False,
        current_soc_pct=planner_input.ev_planned_load_current_soc_pct,
        capacity_kwh=planner_input.ev_planned_load_battery_capacity_kwh,
        max_charge_kw=planner_input.ev_planned_load_charger_power_kw,
        now=now,
    )
    second = compute_ev_soc_economics(
        planner_input,
        is_second=True,
        current_soc_pct=planner_input.ev_second_planned_load_current_soc_pct,
        capacity_kwh=planner_input.ev_second_planned_load_battery_capacity_kwh,
        max_charge_kw=planner_input.ev_second_planned_load_charger_power_kw,
        now=now,
    )
    return primary, second


class CoordinatorEvSoCEconomicsMixin(CoordinatorSharedState):
    """Throttled EV SoC economics recomputation, mixed into the coordinator."""

    async def _maybe_compute_ev_soc_economics(
        self,
        now: datetime,
        captured_generation: int,
    ) -> None:
        """Recompute both EVs' SoC economics tables, throttled.

        No-ops when ``self._last_planner_input`` is not yet set (e.g. the
        first cycle, before any plan has run) or when the throttle window
        has not elapsed. Both EVs are solved in a single executor job so a
        recompute never blocks the event loop. Re-checks the
        update-generation guard afterwards, exactly like the main
        ``run_planner()`` call in ``coordinator_planner_phase.py``, so a
        stale cycle's result is discarded rather than published.
        """
        planner_input = getattr(self, "_last_planner_input", None)
        if planner_input is None:
            return

        last_computed = getattr(self, "_ev_soc_economics_last_computed", None)
        if (
            last_computed is not None
            and (now - last_computed).total_seconds()
            < EV_SOC_ECONOMICS_RECOMPUTE_MIN_SECONDS
        ):
            return

        primary, second = await self.hass.async_add_executor_job(
            _compute_both_ev_soc_economics, planner_input, now
        )

        if getattr(self, "_update_generation", 0) != captured_generation:
            raise _StaleUpdateCycle

        self._ev_soc_economics = primary
        self._ev_second_soc_economics = second
        self._ev_soc_economics_last_computed = now
