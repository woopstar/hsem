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

The bridge's house load and PV come from the reference plan's slots too
(issue #1187).  The regenerated recommendations hold the per-slot house load
next to the *unscaled hourly* Solcast value, which the planner splits per slot
itself; subtracting one from the other overstated PV 4× at 15-minute slots and
ended the bridge at a solar surplus that was not there.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime

from custom_components.hsem.coordinator_helpers import (
    _SimpleSlot,
    _StaleUpdateCycle,
)
from custom_components.hsem.coordinator_state import CoordinatorSharedState
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.planner import run_planner
from custom_components.hsem.utils.datetime_utils import utc_key
from custom_components.hsem.utils.dynamic_floor import DynamicDischargeFloor
from custom_components.hsem.utils.logger import async_log
from custom_components.hsem.utils.misc import get_config_value, resolve_cycle_cost
from custom_components.hsem.utils.recommendations import Recommendations
from custom_components.hsem.utils.units import (
    slot_duration_hours,
    usable_kwh_from_rated,
)


def _house_net_consumption_kwh(
    rec: HourlyRecommendation, plan_slot: PlannedSlot | None
) -> float:
    """Return a bridge slot's house load minus PV, both in kWh per slot.

    The reference plan's slot carries both per slot, with the planner's PV
    correction, confidence decay and live injection applied, so the bridge
    sees the same forecast as the plan it constrains.  Planned EV load is
    left out: the floor reserves for the house only.

    A recommendation the plan does not cover still holds the Solcast value as
    the populator stored it, in kWh per hour, so it is scaled to the slot.

    Args:
        rec: This cycle's regenerated and populated recommendation slot.
        plan_slot: The reference plan's slot for the same interval, if any.

    Returns:
        Net house consumption of the slot in kWh; negative is PV surplus.
    """
    if plan_slot is not None:
        return plan_slot.avg_house_consumption_kwh - plan_slot.solcast_pv_estimate_kwh
    return (
        rec.avg_house_consumption_kwh
        - rec.solcast_pv_estimate_kwh * slot_duration_hours(rec.start, rec.end)
    )


def build_dynamic_floor_bridge_slots(
    hourly_recommendations: list[HourlyRecommendation],
    reference_plan: PlannerOutput | None,
) -> list[_SimpleSlot]:
    """Return bridge slots for the dynamic floor's refill scan.

    House load, PV, the charge decision (``batteries_charged_kwh`` and
    ``recommendation``) and the import price all come from the slot of the
    reference plan with the same ``(start, end)`` (issues #1140, #1156,
    #1187).  A slot the plan does not cover keeps the regenerated forecast,
    with its hourly PV scaled to the slot, has no charge, which is the
    pre-#1140 behaviour, and has no price.

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
                estimated_net_consumption_kwh=_house_net_consumption_kwh(
                    rec, plan_slot
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
) -> tuple[float, dict, list[tuple[str, float]]]:
    """Return the dynamic floor for this replan, its diagnostics and profile.

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
        ``(floor_pct, diagnostics, profile)`` from
        :meth:`~custom_components.hsem.utils.dynamic_floor.DynamicDischargeFloor.compute_floor_profile`.
        The profile is the floor at the start of every look-ahead slot as
        ``(slot start ISO-8601, floor SoC %)``, ready for
        ``PlannerInput.dynamic_floor_profile`` (issue #1188).
    """
    rated_kwh = (live.huawei_batteries_rated_capacity_wh or 0.0) / 1000.0
    min_soc_pct = live.huawei_batteries_end_of_discharge_soc_pct or 0.0
    max_soc_pct = live.huawei_batteries_charging_cutoff_capacity_pct or 100.0
    usable_kwh = usable_kwh_from_rated(rated_kwh, min_soc_pct, max_soc_pct)
    floor_pct, diag, profile = dynamic_floor.compute_floor_profile(
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
    return floor_pct, diag, [(start.isoformat(), pct) for start, pct in profile]


def floor_required_at_slot_end(
    profile: list[tuple[str, float]] | None, now: datetime, floor_now_pct: float
) -> float:
    """Return the floor the plan may reach by the end of the slot holding *now*.

    The plan follows the declining reserve (issue #1188): within a slot the
    battery may go down to the floor at the start of the next slot.  That is
    the floor the safety-margin learner must judge the live SoC against; the
    floor at the start of the current slot would report every planned slot of
    self-consumption as a shortfall.

    Args:
        profile: ``(slot start ISO-8601, floor SoC %)`` of the replan whose
            floor is in force, or ``None``.
        now: Timezone-aware current datetime.
        floor_now_pct: The floor in force, used when the profile has no slot
            starting after *now*.

    Returns:
        The floor at the start of the first profile slot that begins after
        *now*, or *floor_now_pct*.
    """
    for start_iso, floor_pct in profile or ():
        if utc_key(datetime.fromisoformat(start_iso)) > utc_key(now):
            return floor_pct
    return floor_now_pct


class CoordinatorDynamicFloorMixin(CoordinatorSharedState):
    """Dynamic discharge floor steps of the coordinator's planner phase.

    Moved out of ``coordinator_planner_phase.py`` to keep that module under
    the 30 KB file limit (issue #1186).  The methods run on the coordinator
    through the mixin chain, so ``self`` and every attribute are unchanged.
    """

    def _sync_dynamic_floor_enabled(self) -> bool:
        """Return whether the dynamic floor is enabled; clear its state if not.

        The floor is computed from a floor-free reference solve in the same
        replan (issue #1140), never from the plan it constrains; between
        replans the floor in force is kept.
        """
        enabled = bool(
            get_config_value(self._config_entry, "hsem_dynamic_discharge_floor")
        )
        if not enabled:
            self._effective_discharge_floor_pct = None
            self._effective_discharge_floor_diag = None
            self._effective_discharge_floor_profile = None
        return enabled

    async def _async_apply_dynamic_floor(
        self,
        planner_input: PlannerInput,
        live: LiveState,
        now: datetime,
        captured_generation: int,
    ) -> PlannerInput:
        """Solve the reference plan and return the input with this replan's floor.

        The reference solve uses *planner_input* unchanged apart from the
        missing floor.  In particular it keeps the house-battery target
        (issue #1109), so the scan reads a plan with the same features as the
        one that is published (issue #1186).

        Args:
            planner_input: This replan's floor-free planner input.
            live: Live state; supplies the battery's capacity and SoC limits.
            now: Timezone-aware current datetime.
            captured_generation: The update generation this cycle started in.

        Returns:
            *planner_input* with ``dynamic_discharge_floor_pct`` and
            ``dynamic_floor_profile`` set.

        Raises:
            _StaleUpdateCycle: A newer update cycle started during the solve.
        """
        reference_output = await self.hass.async_add_executor_job(
            run_planner, planner_input
        )
        if getattr(self, "_update_generation", 0) != captured_generation:
            raise _StaleUpdateCycle
        floor_pct, floor_diag, floor_profile = compute_dynamic_floor_from_plan(
            self._dynamic_floor,
            self._hourly_recommendations,
            reference_output,
            planner_input,
            live,
            now,
        )
        self._effective_discharge_floor_pct = floor_pct
        self._effective_discharge_floor_diag = floor_diag
        self._effective_discharge_floor_profile = floor_profile
        return replace(
            planner_input,
            dynamic_discharge_floor_pct=floor_pct,
            dynamic_floor_profile=floor_profile,
        )

    def _learn_dynamic_floor_margin(self, live: LiveState, now: datetime) -> None:
        """Feed the live SoC and the floor in force to the margin learner.

        The floor passed is the one the plan may reach by the end of the live
        slot: the plan follows the reserve down to the next slot's floor
        (issue #1188).
        """
        floor_in_force = self._effective_discharge_floor_pct
        if floor_in_force is None or live.huawei_batteries_soc_pct is None:
            return
        self._dynamic_floor.correct_margin(
            live.huawei_batteries_soc_pct,
            floor_required_at_slot_end(
                self._effective_discharge_floor_profile, now, floor_in_force
            ),
            now=now,
        )
