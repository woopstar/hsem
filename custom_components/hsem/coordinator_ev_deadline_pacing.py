"""EV deadline-pacing replan trigger (issue #845).

Extracted from ``coordinator_planner_phase.py`` to satisfy the repository's
30 KB / 1000-line file limit.

Full replans already run every ``hsem_update_interval`` and each re-derives
EV SoC from live state, re-applying the deadline safety margin and
escalation logic (``EVConfig.deadline_escalated`` in
``planner/milp/_ev_constraints.py`` / ``_objective.py``). This module adds
one lightweight trigger so escalation doesn't have to wait for some
unrelated event to force the next replan: as soon as max-power charging for
the remaining time can no longer reach ``target + margin``, a fresh plan is
requested promptly.
"""

from __future__ import annotations

from datetime import datetime

from custom_components.hsem.coordinator_cycle import (
    EV_DEADLINE_PACING_REPLAN_MIN_SECONDS,
)
from custom_components.hsem.coordinator_state import CoordinatorSharedState
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.utils.datetime_utils import utc_key
from custom_components.hsem.utils.logger import async_log


class CoordinatorEvDeadlinePacingMixin(CoordinatorSharedState):
    """EV deadline-pacing replan trigger (issue #845)."""

    def _ev_deadline_pacing_requires_replan(
        self,
        live: LiveState,
        now: datetime,
    ) -> bool:
        """Return whether an EV can no longer reach its margined target at max power.

        Gated by a minimum cadence so it doesn't fire on every tick.
        """
        last_plan_at = self._last_plan_slot_start
        elapsed_seconds: float | None = None
        if last_plan_at is not None:
            try:
                elapsed_seconds = (utc_key(now) - utc_key(last_plan_at)).total_seconds()
            except TypeError, ValueError:
                elapsed_seconds = None

        if last_plan_at is not None and (
            elapsed_seconds is None
            or elapsed_seconds < EV_DEADLINE_PACING_REPLAN_MIN_SECONDS
        ):
            return False

        for (
            label,
            ev_live,
            connected,
            smart_charging,
            target_soc_pct,
            deadline,
            capacity_kwh,
            charger_power_kw,
            charger_efficiency_pct,
            margin_pct,
        ) in (
            (
                "EV",
                live.ev,
                live.ev_planned_load_connected,
                live.ev_planned_load_smart_charging_enabled,
                live.ev_planned_load_target_soc_pct,
                live.ev_planned_load_deadline,
                float(self._cfg.ev_planned_load_battery_capacity_kwh),
                float(self._cfg.ev_planned_load_charger_power_kw),
                float(self._cfg.ev_planned_load_charger_efficiency_pct),
                float(self._cfg.ev_planned_load_deadline_safety_margin_pct),
            ),
            (
                "EV2",
                live.ev_second,
                live.ev_second_planned_load_connected,
                live.ev_second_planned_load_smart_charging_enabled,
                live.ev_second_planned_load_target_soc_pct,
                live.ev_second_planned_load_deadline,
                float(self._cfg.ev_second_planned_load_battery_capacity_kwh),
                float(self._cfg.ev_second_planned_load_charger_power_kw),
                float(self._cfg.ev_second_planned_load_charger_efficiency_pct),
                float(self._cfg.ev_second_planned_load_deadline_safety_margin_pct),
            ),
        ):
            if not connected or not smart_charging or deadline is None:
                continue
            if capacity_kwh <= 1e-9 or charger_power_kw <= 1e-9:
                continue
            current_kwh = self._ev_effective_energy_kwh(ev_live, capacity_kwh)
            if current_kwh is None:
                continue
            target_pct = max(min(float(target_soc_pct), 100.0), 0.0)
            target_kwh = target_pct / 100.0 * capacity_kwh
            if current_kwh + 1e-9 >= target_kwh:
                continue
            try:
                remaining_hours = (deadline - now).total_seconds() / 3600.0
            except TypeError:
                continue
            if remaining_hours <= 0:
                continue
            margin_kwh = (target_kwh - current_kwh) * margin_pct / 100.0
            effective_target_kwh = min(target_kwh + margin_kwh, capacity_kwh)
            efficiency = max(charger_efficiency_pct, 1.0) / 100.0
            max_reachable_kwh = (
                current_kwh + charger_power_kw * efficiency * remaining_hours
            )
            if max_reachable_kwh < effective_target_kwh - 1e-9:
                async_log(
                    "debug",
                    "[replan] %s can no longer reach its margined deadline "
                    "target at max power (%.3f reachable vs %.3f needed by "
                    "%s) — re-planning.",
                    label,
                    max_reachable_kwh,
                    effective_target_kwh,
                    deadline.isoformat(),
                )
                return True
        return False
