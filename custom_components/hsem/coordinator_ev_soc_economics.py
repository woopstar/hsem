"""EV SoC economics recomputation for the coordinator.

Extracted to its own module (mirroring ``coordinator_planner_phase.py`` and
friends) to satisfy the repository's 30 KB / 1000-line file limit. Wires the
pure ``planner/ev_soc_economics.py`` module into the coordinator: throttled
recomputation of the "cost to reach target SoC by deadline" table for both
EVs, run at most once per throttle window and always in a single executor
job. Each recompute is one extra ``run_planner()`` solve per (target SoC above
the car's SoC, deadline column) pair: up to 5 targets x 3 columns = 15 solves
per EV with the default two deadlines plus the target deadline.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from custom_components.hsem.coordinator_helpers import _StaleUpdateCycle
from custom_components.hsem.coordinator_staleness import cycle_is_stale
from custom_components.hsem.coordinator_state import CoordinatorSharedState
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.planner.ev_soc_economics import (
    EVSoCEconomicsResult,
    compute_ev_soc_economics,
    effective_target_deadline,
)
from custom_components.hsem.utils.logger import async_log
from custom_components.hsem.utils.pool_jobs import (
    EV_SOC_ECONOMICS_JOB_BUDGET_S,
    JOB_EV_SOC_ECONOMICS,
    PoolJobUnavailable,
)

#: Minimum seconds between EV SoC economics recomputes when no fresh plan
#: ran. On a fresh replan (the same events that update
#: ``sensor.hsem_ev_optimal_charging_plan``) the throttle is bypassed so the
#: two sensors always move together; the window only rate-limits refreshes
#: while the plan itself is being reused.
EV_SOC_ECONOMICS_RECOMPUTE_MIN_SECONDS = 1800.0


def _compute_both_ev_soc_economics(
    planner_input: PlannerInput,
    now: datetime,
    primary_deadlines: Sequence[tuple[int, int]],
    second_deadlines: Sequence[tuple[int, int]],
) -> tuple[EVSoCEconomicsResult, EVSoCEconomicsResult]:
    """Blocking helper — computes both EVs' tables in one executor job."""
    primary = compute_ev_soc_economics(
        planner_input,
        is_second=False,
        current_soc_pct=planner_input.ev_planned_load_current_soc_pct,
        capacity_kwh=planner_input.ev_planned_load_battery_capacity_kwh,
        max_charge_kw=planner_input.ev_planned_load_charger_power_kw,
        now=now,
        deadlines=primary_deadlines,
    )
    second = compute_ev_soc_economics(
        planner_input,
        is_second=True,
        current_soc_pct=planner_input.ev_second_planned_load_current_soc_pct,
        capacity_kwh=planner_input.ev_second_planned_load_battery_capacity_kwh,
        max_charge_kw=planner_input.ev_second_planned_load_charger_power_kw,
        now=now,
        deadlines=second_deadlines,
    )
    return primary, second


def _deadline_columns_changed(
    previous: EVSoCEconomicsResult | None,
    deadlines: Sequence[tuple[int, int]],
    target_deadline: datetime | None,
    now: datetime,
) -> bool:
    """Return whether a table's deadline columns no longer match the inputs.

    A changed configured list or target deadline (including one that has
    just passed) makes the published columns wrong, so the recompute must
    not wait out the throttle window (issue #1344). ``None`` (no table yet)
    is not a change: the first computation runs unthrottled anyway.
    """
    if previous is None:
        return False
    return previous.configured_deadlines != tuple(
        deadlines
    ) or previous.target_deadline != effective_target_deadline(target_deadline, now)


class CoordinatorEvSoCEconomicsMixin(CoordinatorSharedState):
    """Throttled EV SoC economics recomputation, mixed into the coordinator."""

    async def _maybe_compute_ev_soc_economics(
        self,
        now: datetime,
        captured_generation: int,
        *,
        force: bool = False,
    ) -> None:
        """Recompute both EVs' SoC economics tables, throttled.

        No-ops when ``self._last_planner_input`` is not yet set (e.g. the
        first cycle, before any plan has run) or when the throttle window
        has not elapsed. Pass ``force=True`` when this cycle produced a
        fresh plan (the same event that updates
        ``sensor.hsem_ev_optimal_charging_plan``) to bypass the throttle and
        keep the economics table in lock-step with the plan it is derived
        from. A change to either EV's deadline columns (configured list or
        target deadline) also bypasses the throttle. Both EVs are solved in a
        single executor job so a recompute never blocks the event loop.
        Re-checks the update-generation guard afterwards, exactly like the main
        ``run_planner()`` call in ``coordinator_planner_phase.py``, so a
        stale cycle's result is discarded rather than published.
        """
        planner_input = getattr(self, "_last_planner_input", None)
        if planner_input is None:
            return

        primary_deadlines = self._cfg.ev_planned_load_economics_deadlines
        second_deadlines = self._cfg.ev_second_planned_load_economics_deadlines
        force = (
            force
            or _deadline_columns_changed(
                getattr(self, "_ev_soc_economics", None),
                primary_deadlines,
                planner_input.ev_planned_load_deadline,
                now,
            )
            or _deadline_columns_changed(
                getattr(self, "_ev_second_soc_economics", None),
                second_deadlines,
                planner_input.ev_second_planned_load_deadline,
                now,
            )
        )

        last_computed = getattr(self, "_ev_soc_economics_last_computed", None)
        if (
            not force
            and last_computed is not None
            and (now - last_computed).total_seconds()
            < EV_SOC_ECONOMICS_RECOMPUTE_MIN_SECONDS
        ):
            return

        try:
            primary, second = await self._pool_job_guard().run(
                self.hass,
                JOB_EV_SOC_ECONOMICS,
                EV_SOC_ECONOMICS_JOB_BUDGET_S,
                _compute_both_ev_soc_economics,
                planner_input,
                now,
                primary_deadlines,
                second_deadlines,
            )
        except PoolJobUnavailable as err:
            # The table is advisory: keep the previous one rather than queue
            # another sweep behind one the pool has not returned.
            async_log("debug", "[ev_soc_economics] Skipped this cycle (%s).", err)
            return

        if cycle_is_stale(self, captured_generation):
            raise _StaleUpdateCycle

        self._ev_soc_economics = primary
        self._ev_second_soc_economics = second
        self._ev_soc_economics_last_computed = now
