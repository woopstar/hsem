"""Dynamic discharge floor from a floor-free reference plan (issue #1140).

The floor's bridge scan in
:meth:`~custom_components.hsem.utils.dynamic_floor.DynamicDischargeFloor.compute_floor`
needs to know whether the plan grid-charges before the next PV surplus.  The
coordinator's ``_hourly_recommendations`` are regenerated empty at the start of
every cycle, so they cannot tell it.

The charge decisions therefore come from a **reference plan**: the same
planner input solved once *without* the dynamic floor, in the same replan.
Taking them from the previous committed plan instead made the floor depend on
the plan it constrains: a partial night charge lowered the floor, the next
plan then charged less, the floor rose again, and the floor and the night
charge flipped on every replan.  A reference plan has no such feedback — the
floor is a deterministic function of this replan's inputs.

The reference plan's slot prices, with the cycle cost and charge power of its
input, also tell the scan where the grid could refill the battery at an
affordable price even though the plan does not charge there (issue #1156).
"""

from __future__ import annotations

import math
from datetime import datetime

from custom_components.hsem.coordinator_helpers import _SimpleSlot
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.utils.datetime_utils import utc_key
from custom_components.hsem.utils.dynamic_floor import DynamicDischargeFloor
from custom_components.hsem.utils.logger import async_log
from custom_components.hsem.utils.misc import resolve_cycle_cost
from custom_components.hsem.utils.recommendations import Recommendations
from custom_components.hsem.utils.units import usable_kwh_from_rated


def build_dynamic_floor_bridge_slots(
    hourly_recommendations: list[HourlyRecommendation],
    reference_plan: PlannerOutput | None,
) -> list[_SimpleSlot]:
    """Return bridge slots for the dynamic floor's refill scan.

    Consumption and PV come from this cycle's freshly populated forecast
    (house load only, exactly as before issue #1140). The charge decision —
    ``batteries_charged_kwh`` and ``recommendation`` — and the import price
    come from the slot of the reference plan with the same ``(start, end)``.
    A slot the plan does not cover keeps the regenerated values (no charge),
    which is the pre-#1140 behaviour, and has no price (issue #1156).

    Args:
        hourly_recommendations: This cycle's regenerated and populated slots.
        reference_plan: The floor-free reference solve of this replan, or
            ``None`` when there is none.

    Returns:
        One bridge slot per recommendation slot, in the same order.
    """
    planned: dict[tuple[datetime, datetime], PlannedSlot] = {}
    if reference_plan is None:
        async_log(
            "debug",
            "[dynamic_floor] No reference plan — refill scan cannot see "
            "planned grid charges this cycle.",
        )
    else:
        planned = {
            (utc_key(slot.start), utc_key(slot.end)): slot
            for slot in reference_plan.slots
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
                import_price=(
                    plan_slot.price.import_price if plan_slot is not None else math.nan
                ),
            )
        )

    if reference_plan is not None:
        async_log(
            "debug",
            "[dynamic_floor] Refill scan reads the reference plan: %d planned "
            "grid-charge slot(s).",
            grid_charge_slots,
        )
    return bridge_slots


def compute_dynamic_floor_from_plan(
    dynamic_floor: DynamicDischargeFloor,
    hourly_recommendations: list[HourlyRecommendation],
    reference_plan: PlannerOutput,
    reference_input: PlannerInput,
    live: LiveState,
    now: datetime,
) -> tuple[float, dict]:
    """Return the dynamic floor for this replan and its diagnostics.

    Args:
        dynamic_floor: The coordinator's self-learning floor instance.
        hourly_recommendations: This cycle's regenerated and populated slots.
        reference_plan: The same planner input solved without the dynamic
            floor (issue #1140).
        reference_input: The input of that reference solve.  Supplies the
            cycle cost (via :func:`resolve_cycle_cost`, as the planner
            resolves it) and the battery charge power that size an affordable
            grid refill (issue #1156).
        live: Live state; supplies the battery's rated capacity and SoC limits.
        now: Timezone-aware current datetime.

    Returns:
        ``(floor_pct, diagnostics)`` from
        :meth:`~custom_components.hsem.utils.dynamic_floor.DynamicDischargeFloor.compute_floor`.
    """
    rated_kwh = (live.huawei_batteries_rated_capacity_wh or 0.0) / 1000.0
    min_soc_pct = live.huawei_batteries_end_of_discharge_soc_pct or 0.0
    max_soc_pct = live.huawei_batteries_charging_cutoff_capacity_pct or 100.0
    usable_kwh = usable_kwh_from_rated(rated_kwh, min_soc_pct, max_soc_pct)
    return dynamic_floor.compute_floor(
        now=now,
        slots=build_dynamic_floor_bridge_slots(hourly_recommendations, reference_plan),
        usable_kwh=usable_kwh,
        configured_min_soc_pct=min_soc_pct,
        cycle_cost_per_kwh=resolve_cycle_cost(
            purchase_price=reference_input.battery_purchase_price,
            usable_kwh=usable_kwh,
            expected_cycles=reference_input.battery_expected_cycles,
            capacity_loss_pct=reference_input.battery_capacity_loss_pct,
            user_margin=reference_input.battery_cycle_cost_per_kwh,
        ),
        max_grid_charge_kw=reference_input.battery_max_charge_power_w / 1000.0,
    )
