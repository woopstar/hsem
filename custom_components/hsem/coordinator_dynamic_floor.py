"""Bridge-slot construction for the dynamic discharge floor (issue #1140).

The coordinator regenerates ``_hourly_recommendations`` at the start of every
cycle, so at the point the floor is computed every slot carries
``batteries_charged_kwh = 0.0`` and ``recommendation = None``. The planned
grid charge only exists in the last committed plan
(``_last_planner_output``). This module overlays that plan's charge decisions
onto the freshly populated forecast so the bridge scan in
:meth:`~custom_components.hsem.utils.dynamic_floor.DynamicDischargeFloor.compute_floor`
can see a planned grid-charge refill.
"""

from __future__ import annotations

from datetime import datetime

from custom_components.hsem.coordinator_helpers import _SimpleSlot
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.utils.datetime_utils import utc_key
from custom_components.hsem.utils.logger import async_log
from custom_components.hsem.utils.recommendations import Recommendations


def build_dynamic_floor_bridge_slots(
    hourly_recommendations: list[HourlyRecommendation],
    committed_plan: PlannerOutput | None,
) -> list[_SimpleSlot]:
    """Return bridge slots for the dynamic floor's refill scan.

    Consumption and PV come from this cycle's freshly populated forecast
    (house load only, exactly as before issue #1140). The charge decision —
    ``batteries_charged_kwh`` and ``recommendation`` — comes from the slot of
    the last committed plan with the same ``(start, end)``. A slot the plan
    does not cover (no plan yet, a plan that no longer reaches that far, or a
    changed slot interval) keeps the regenerated values, which is the
    pre-#1140 behaviour.

    Args:
        hourly_recommendations: This cycle's regenerated and populated slots.
        committed_plan: The last committed planner output, or ``None`` before
            the first plan has been committed.

    Returns:
        One bridge slot per recommendation slot, in the same order.
    """
    planned: dict[tuple[datetime, datetime], PlannedSlot] = {}
    if committed_plan is None:
        async_log(
            "debug",
            "[dynamic_floor] No committed plan yet — refill scan cannot see "
            "planned grid charges this cycle.",
        )
    else:
        planned = {
            (utc_key(slot.start), utc_key(slot.end)): slot
            for slot in committed_plan.slots
        }

    bridge_slots: list[_SimpleSlot] = []
    grid_charge_slots = 0
    for rec in hourly_recommendations:
        plan_slot = planned.get((utc_key(rec.start), utc_key(rec.end)))
        source = plan_slot if plan_slot is not None else rec
        if (
            source.recommendation == Recommendations.BatteriesChargeGrid.value
            and source.batteries_charged_kwh > 1e-9
        ):
            grid_charge_slots += 1
        bridge_slots.append(
            _SimpleSlot(
                start=rec.start,
                end=rec.end,
                estimated_net_consumption_kwh=(
                    rec.avg_house_consumption_kwh - rec.solcast_pv_estimate_kwh
                ),
                batteries_charged_kwh=source.batteries_charged_kwh,
                recommendation=source.recommendation,
            )
        )

    if committed_plan is not None:
        async_log(
            "debug",
            "[dynamic_floor] Refill scan reads the committed plan: %d planned "
            "grid-charge slot(s).",
            grid_charge_slots,
        )
    return bridge_slots
