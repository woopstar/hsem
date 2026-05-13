"""Pure-Python HSEM planner engine.

Single responsibility: orchestrate the planning pipeline and return a
:class:`PlannerOutput`.

The heavy lifting is fully delegated:

- :mod:`slot_population` — slot generation, time-series population, battery
  capacity simulation, and the spike-aware consumption weighting algorithm
- :mod:`charge_scheduler` — discharge window detection, charge scheduling,
  excess export, and seasonal optimization

**No Home Assistant types are imported here.**  This makes the engine
directly testable with plain ``pytest`` without a running HA instance.

Design notes
------------
- The engine is intentionally *synchronous*.  The sensor's async wrappers
  exist only because HA's event loop requires them; the underlying
  calculations are CPU-bound and need no I/O.
- ``warnings`` emitted during planning are collected and returned in
  :attr:`PlannerOutput.warnings` so tests can assert on diagnostic
  messages without capturing log output.
"""

from __future__ import annotations

from datetime import datetime

from custom_components.hsem.models.planner_inputs import PlannerInput
from custom_components.hsem.models.planner_outputs import (
    ChargeWindow,
    DischargeWindow,
    PlanExplanation,
    PlannedSlot,
    PlannerOutput,
    RejectedPlan,
)
from custom_components.hsem.planner.candidate_generator import generate_candidates
from custom_components.hsem.planner.candidate_selector import select_best_candidate
from custom_components.hsem.planner.charge_scheduler import (
    apply_charge_schedules,
    apply_discharge_schedules,
    apply_excess_export,
    apply_opportunistic_charge,
    apply_optimization_strategy,
    calculate_required_battery_until_solar,
)
from custom_components.hsem.planner.cost_function import CostWeights, score_plan
from custom_components.hsem.planner.slot_population import (
    build_slots,
    build_time_series_index,
    mark_time_passed,
    populate_battery_capacity,
    populate_consumption,
    populate_estimated_cost,
    populate_net_consumption,
    populate_prices,
    populate_solcast,
    usable_capacity,
)
from custom_components.hsem.utils.misc import calculate_recommended_threshold
from custom_components.hsem.utils.recommendations import Recommendations

# Sets used by _derive_windows — kept here as they reference Recommendations
_CHARGE_RECOMMENDATIONS = frozenset(
    {
        Recommendations.BatteriesChargeGrid.value,
        Recommendations.BatteriesChargeSolar.value,
    }
)
_DISCHARGE_RECOMMENDATIONS = frozenset(
    {
        Recommendations.BatteriesDischargeMode.value,
        Recommendations.ForceBatteriesDischarge.value,
    }
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_planner(inp: PlannerInput) -> PlannerOutput:
    """Execute the HSEM planner and return a :class:`PlannerOutput`.

    This function is the pure-Python equivalent of
    ``HSEMWorkingModeSensor._async_run_update_cycle`` stripped of all
    Home Assistant I/O.  It is safe to call from any synchronous context.

    Args:
        inp: Fully populated :class:`PlannerInput`.

    Returns:
        A :class:`PlannerOutput` containing per-slot decisions, charge /
        discharge windows, and diagnostic information.

    Raises:
        ValueError: If ``inp.now_iso`` is not a timezone-aware ISO-8601
            string or if ``interval_minutes`` is ≤ 0.
    """
    warnings: list[str] = []
    missing_inputs: list[str] = []

    # Parse now
    now = _parse_now(inp.now_iso)

    # Battery state
    usable_kwh, current_kwh = usable_capacity(
        inp.battery_rated_capacity_kwh,
        inp.battery_soc_pct,
        inp.battery_end_of_discharge_soc_pct,
        inp.battery_max_soc_pct,
    )

    if inp.battery_rated_capacity_kwh <= 0:
        warnings.append(
            "battery_rated_capacity_kwh is zero or negative; battery simulation disabled."
        )
        usable_kwh = 0.0
        current_kwh = 0.0

    # Validate weights
    weight_sum = inp.weight_1d + inp.weight_3d + inp.weight_7d + inp.weight_14d
    if weight_sum != 100:
        warnings.append(
            f"Consumption weights sum to {weight_sum}, not 100. "
            "Results may not be meaningful."
        )

    # Build shared time-series index — single source of truth for all slot boundaries
    tsi = build_time_series_index(inp, now)

    # Generate slots (boundaries come from TSI for DST-safe consistency)
    slots = build_slots(inp, now)
    if not slots:
        warnings.append(
            "No slots generated; check interval_minutes and interval_length_hours."
        )
        return PlannerOutput(missing_inputs=missing_inputs, warnings=warnings)

    # Populate time-series — all series aligned to the shared TSI axis
    populate_prices(slots, inp.price_points, tsi)
    populate_solcast(slots, inp.solcast_slots, inp.interval_minutes, tsi)
    populate_consumption(
        slots,
        inp.consumption_averages,
        inp.weight_1d,
        inp.weight_3d,
        inp.weight_7d,
        inp.weight_14d,
        inp.interval_minutes,
        tsi,
    )

    # Surface any hours where input data was absent
    for hour in sorted(tsi.missing_hours()):
        missing_inputs.append(f"hour_{hour:02d}")
    populate_net_consumption(slots)
    populate_estimated_cost(slots)

    # Depreciation threshold diagnostic and C13 auto-fill.
    # calculate_recommended_threshold returns the minimum economically
    # justified price difference (depreciation + conversion loss).
    # When a battery schedule has min_price_difference == 0 (user left it
    # at the default), we automatically fill it with the depreciation
    # threshold so the planner never charges from the grid unless it is
    # actually profitable.
    recommended_threshold = calculate_recommended_threshold(
        inp.battery_purchase_price,
        inp.battery_expected_cycles,
        usable_kwh,
        inp.battery_conversion_loss_pct,
    )
    if recommended_threshold > 0:
        warnings.append(
            f"Recommended price threshold: {recommended_threshold:.4f} "
            f"(depreciation + conversion loss)."
        )
        for sched in inp.battery_schedules:
            if sched.enabled and abs(sched.min_price_difference) < 1e-9:
                sched.min_price_difference = recommended_threshold

    # Mark past slots
    mark_time_passed(slots, now)

    # Discharge schedule detection
    apply_discharge_schedules(slots, inp.battery_schedules, now)

    # Charge scheduling
    conversion_loss_factor = 1 - (inp.battery_conversion_loss_pct / 100)
    max_charge_per_hour = (
        inp.battery_max_charge_power_w / 1000
    ) * conversion_loss_factor
    max_charge_per_interval = max_charge_per_hour / (60 / inp.interval_minutes)

    apply_charge_schedules(
        slots,
        inp.battery_schedules,
        now,
        max_charge_per_interval,
        cycle_cost_per_kwh=inp.battery_cycle_cost_per_kwh,
    )

    # Opportunistic grid charge (A2/H28/H29): charge from grid when prices
    # are negative or below the depreciation + cycle cost threshold,
    # independent of any configured discharge schedule.
    apply_opportunistic_charge(
        slots,
        now,
        current_kwh,
        usable_kwh,
        max_charge_per_interval,
        recommended_threshold,
        cycle_cost_per_kwh=inp.battery_cycle_cost_per_kwh,
    )

    # Derive per-slot power limits
    max_charge_per_slot = (
        inp.battery_max_charge_power_w
        / 1000
        * (1 - inp.battery_conversion_loss_pct / 100)
    ) / (60 / inp.interval_minutes)
    max_discharge_per_slot: float | None = None
    if inp.battery_max_discharge_power_w is not None:
        max_discharge_per_slot = (inp.battery_max_discharge_power_w / 1000) / (
            60 / inp.interval_minutes
        )
    # Absolute ceiling from max_soc_pct expressed in usable kWh
    max_soc_capacity_kwh = usable_kwh  # usable_kwh already respects max_soc_pct

    # Battery SoC forward simulation (first pass — used for required-capacity calc)
    populate_battery_capacity(slots, now, current_kwh, usable_kwh)

    # Required capacity until solar surplus
    required_capacity = calculate_required_battery_until_solar(
        slots, now, usable_kwh, inp.excess_export_discharge_buffer_pct
    )

    # Excess export
    if inp.excess_export_enabled:
        apply_excess_export(
            slots,
            now,
            current_kwh,
            required_capacity,
            inp.excess_export_price_threshold,
            warnings,
        )

    # Seasonal optimization (A3: export_min_price guards ForceExport)
    apply_optimization_strategy(
        slots,
        now,
        current_kwh,
        usable_kwh,
        required_capacity,
        inp.months_winter,
        warnings,
        export_min_price=inp.export_min_price,
    )

    # --- Candidate plan generation and selection -------------------------
    # Generate multiple independent strategies from the fully-scheduled
    # baseline slots (pre-SoC-simulation).  The selector runs simulate_soc
    # on each candidate and returns the lowest-cost valid plan.
    cost_weights = CostWeights(
        min_soc_pct=inp.battery_end_of_discharge_soc_pct,
        max_soc_pct=inp.battery_max_soc_pct,
        battery_purchase_price=inp.battery_purchase_price,
        battery_rated_capacity_kwh=inp.battery_rated_capacity_kwh,
        battery_expected_cycles=inp.battery_expected_cycles,
        conversion_loss_pct=inp.battery_conversion_loss_pct,
    )
    slot_duration_hours = inp.interval_minutes / 60.0

    candidates = generate_candidates(
        slots,
        inp,
        now,
        max_charge_per_slot,
    )
    winner, candidate_rejected = select_best_candidate(
        candidates,
        now=now,
        current_kwh=current_kwh,
        usable_kwh=usable_kwh,
        max_soc_capacity_kwh=max_soc_capacity_kwh,
        max_charge_per_slot=max_charge_per_slot,
        max_discharge_per_slot=max_discharge_per_slot,
        rated_kwh=inp.battery_rated_capacity_kwh,
        end_of_discharge_soc_pct=inp.battery_end_of_discharge_soc_pct,
        cost_weights=cost_weights,
        slot_duration_hours=slot_duration_hours,
    )
    # Use the winning candidate's slots as the final plan
    slots = winner.slots

    # Fill any remaining None recommendations on the winner's slots.
    # apply_optimization_strategy only modifies slots where recommendation is
    # None, so it will not disturb the intentional charge/discharge assignments
    # made by the winning strategy.  This guarantees that every slot has a
    # valid recommendation (BatteriesWaitMode, BatteriesChargeSolar, etc.)
    # regardless of which candidate was selected.
    apply_optimization_strategy(
        slots,
        now,
        current_kwh,
        usable_kwh,
        required_capacity,
        inp.months_winter,
        warnings=[],  # suppress duplicate warnings from this fill-only pass
        export_min_price=inp.export_min_price,
    )

    # Current recommendation
    current_recommendation: str | None = None
    for slot in slots:
        if slot.start.astimezone(now.tzinfo) <= now < slot.end.astimezone(now.tzinfo):
            current_recommendation = slot.recommendation
            break

    # Final SoC
    future_slots = [s for s in slots if s.end.astimezone(now.tzinfo) > now]
    battery_soc_at_end = future_slots[-1].estimated_battery_soc if future_slots else 0.0

    # Derive contiguous charge/discharge windows
    charge_windows, discharge_windows = _derive_windows(slots)

    # Build human-readable plan explanation
    explanation = _build_explanation(inp, slots, battery_soc_at_end, now)

    # Score the selected (winning) plan with the full cost function.
    # Re-use cost_weights built during candidate selection above.
    plan_cost = score_plan(
        slots,
        cost_weights,
        slot_duration_hours=slot_duration_hours,
    )

    # Merge candidate-rejected alternatives into the explanation's rejected list
    # (the explanation already contains schedule-based rejected alternatives built
    # by _build_explanation; we append the candidate-selection rejections after).
    for rp in candidate_rejected:
        explanation.rejected_plans.append(rp)

    return PlannerOutput(
        slots=slots,
        charge_windows=charge_windows,
        discharge_windows=discharge_windows,
        current_recommendation=current_recommendation,
        battery_soc_at_end=battery_soc_at_end,
        required_capacity_kwh=required_capacity,
        missing_inputs=missing_inputs,
        warnings=warnings,
        time_series_index=tsi,
        explanation=explanation,
        plan_cost=plan_cost,
        candidates=candidates,
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _parse_now(now_iso: str) -> datetime:
    """Parse a timezone-aware ISO-8601 string into a ``datetime``.

    Args:
        now_iso: ISO-8601 string with timezone offset.

    Raises:
        ValueError: If the string cannot be parsed or is not timezone-aware.
    """
    dt = datetime.fromisoformat(now_iso)
    if dt.tzinfo is None:
        raise ValueError(f"now_iso must be timezone-aware, got: {now_iso!r}")
    return dt


def _derive_windows(
    slots: list[PlannedSlot],
) -> tuple[list[ChargeWindow], list[DischargeWindow]]:
    """Derive contiguous charge and discharge windows from the slot list.

    Args:
        slots: Ordered list of planned slots.

    Returns:
        Tuple of ``(charge_windows, discharge_windows)``.
    """
    charge_windows: list[ChargeWindow] = []
    discharge_windows: list[DischargeWindow] = []

    def _flush_charge(group: list[PlannedSlot]) -> None:
        if not group:
            return
        total_e = round(sum(s.batteries_charged for s in group), 3)
        prices = [s.price.import_price for s in group]
        avg_p = round(sum(prices) / len(prices), 4)
        charge_windows.append(
            ChargeWindow(
                start=group[0].start,
                end=group[-1].end,
                total_energy_kwh=total_e,
                avg_import_price=avg_p,
                recommendation=group[0].recommendation or "",
            )
        )

    def _flush_discharge(group: list[PlannedSlot]) -> None:
        if not group:
            return
        prices = [s.price.import_price for s in group]
        avg_p = round(sum(prices) / len(prices), 4)
        discharge_windows.append(
            DischargeWindow(
                start=group[0].start,
                end=group[-1].end,
                avg_import_price=avg_p,
                recommendation=group[0].recommendation or "",
            )
        )

    current_charge_group: list[PlannedSlot] = []
    current_discharge_group: list[PlannedSlot] = []

    for slot in slots:
        rec = slot.recommendation or ""
        if rec in _CHARGE_RECOMMENDATIONS:
            if current_discharge_group:
                _flush_discharge(current_discharge_group)
                current_discharge_group = []
            current_charge_group.append(slot)
        elif rec in _DISCHARGE_RECOMMENDATIONS:
            if current_charge_group:
                _flush_charge(current_charge_group)
                current_charge_group = []
            current_discharge_group.append(slot)
        else:
            if current_charge_group:
                _flush_charge(current_charge_group)
                current_charge_group = []
            if current_discharge_group:
                _flush_discharge(current_discharge_group)
                current_discharge_group = []

    if current_charge_group:
        _flush_charge(current_charge_group)
    if current_discharge_group:
        _flush_discharge(current_discharge_group)

    return charge_windows, discharge_windows


def _build_explanation(
    inp: PlannerInput,
    slots: list[PlannedSlot],
    battery_soc_at_end: float,
    now: datetime,
) -> PlanExplanation:
    """Build a human-readable explanation of the chosen plan.

    Derives key metrics from the finalised slot list, identifies which strategy
    was selected, lists active constraints, and constructs rejected-plan entries
    describing what alternatives would have looked like.

    Args:
        inp: The planner inputs used in this run.
        slots: The fully-populated slot list after all scheduling passes.
        battery_soc_at_end: Estimated battery SoC (%) at the end of horizon.
        now: Timezone-aware current datetime.

    Returns:
        A populated :class:`PlanExplanation` instance.
    """
    future_slots = [s for s in slots if s.end.astimezone(now.tzinfo) > now]

    # --- Price metrics ---------------------------------------------------
    import_prices = [s.price.import_price for s in future_slots]
    export_prices = [s.price.export_price for s in future_slots]
    peak_import = max(import_prices) if import_prices else 0.0
    off_peak_import = min(import_prices) if import_prices else 0.0
    price_spread = round(peak_import - off_peak_import, 4)

    # --- Forecast metrics ------------------------------------------------
    forecast_pv = round(sum(s.solcast_pv_estimate for s in future_slots), 3)
    forecast_net = round(sum(s.estimated_net_consumption for s in future_slots), 3)

    # --- Cost of the selected plan ---------------------------------------
    selected_cost = round(sum(s.estimated_cost for s in future_slots), 4)

    # --- Do-nothing baseline cost (battery fully idle, pay import for all load) ---
    # Computed here so strategy detection can use it in summaries.
    do_nothing_cost = round(
        sum(
            max(s.estimated_net_consumption, 0.0) * s.price.import_price
            for s in future_slots
        ),
        4,
    )

    # --- Strategy detection ----------------------------------------------
    has_grid_charge = any(
        s.recommendation == Recommendations.BatteriesChargeGrid.value
        for s in future_slots
    )
    has_solar_charge = any(
        s.recommendation == Recommendations.BatteriesChargeSolar.value
        for s in future_slots
    )
    has_discharge = any(
        s.recommendation
        in {
            Recommendations.BatteriesDischargeMode.value,
            Recommendations.ForceBatteriesDischarge.value,
        }
        for s in future_slots
    )
    # has_force_export: excess capacity sent to grid (ForceBatteriesDischarge)
    has_force_export = any(
        s.recommendation == Recommendations.ForceBatteriesDischarge.value
        for s in future_slots
    )
    # has_force_export_pv: export price > import price slots (ForceExport)
    has_force_export_pv = any(
        s.recommendation == Recommendations.ForceExport.value for s in future_slots
    )

    current_month = now.month
    is_winter = current_month in inp.months_winter

    if has_grid_charge and has_discharge:
        selected_strategy = "charge_grid_discharge_peak"
        summary = (
            f"Battery will be charged from the grid during cheap hours "
            f"(min {off_peak_import:.3f}) and discharged during peak hours "
            f"(max {peak_import:.3f})."
        )
    elif has_grid_charge and not has_discharge:
        selected_strategy = "opportunistic_charge"
        summary = (
            f"Opportunistic grid charging during very cheap or negative-price "
            f"hours (min {off_peak_import:.3f}); no scheduled discharge window."
        )
    elif has_solar_charge and has_discharge:
        selected_strategy = "charge_solar_discharge_peak"
        summary = (
            f"Battery will be charged from solar surplus "
            f"({forecast_pv:.1f} kWh forecast) and discharged during "
            f"peak hours (max {peak_import:.3f})."
        )
    elif has_force_export:
        selected_strategy = "force_export"
        summary = (
            f"Surplus battery capacity will be exported to the grid at "
            f"high export prices (max {max(export_prices):.3f})."
        )
    elif has_force_export_pv:
        selected_strategy = "force_export_pv"
        summary = "Export price exceeds import price; PV surplus exported."
    elif has_discharge and not has_grid_charge and not has_solar_charge:
        selected_strategy = "discharge_only"
        summary = (
            f"Battery will be discharged during scheduled windows; "
            f"no cheap charging slots available (spread {price_spread:.3f})."
        )
    elif is_winter:
        selected_strategy = "winter_wait"
        summary = (
            f"Winter month ({current_month}): battery is held in reserve; "
            f"no arbitrage or solar charging warranted."
        )
    else:
        selected_strategy = "solar_charge_only"
        summary = (
            f"Summer month ({current_month}): charging from solar surplus only "
            f"({forecast_pv:.1f} kWh forecast)."
        )

    # --- Active constraints ----------------------------------------------
    constraints: list[str] = []
    if is_winter:
        constraints.append("winter_month")
    else:
        constraints.append("summer_month")
    if abs(price_spread) < 1e-9:
        constraints.append("no_price_spread")
    if inp.excess_export_enabled:
        constraints.append("excess_export_enabled")
    if inp.battery_rated_capacity_kwh <= 0:
        constraints.append("battery_disabled")
    if inp.battery_soc_pct >= inp.battery_max_soc_pct:
        constraints.append("battery_full")
    if inp.battery_soc_pct <= inp.battery_end_of_discharge_soc_pct:
        constraints.append("battery_empty")
    if battery_soc_at_end <= inp.battery_end_of_discharge_soc_pct:
        constraints.append("battery_low_at_end")

    # --- Rejected plans -------------------------------------------------
    rejected: list[RejectedPlan] = []

    # The "savings" the selected plan achieves vs doing nothing.
    # Positive = selected plan is cheaper (saves money vs idle battery).
    # Negative = selected plan costs more (e.g. pre-charging costs exceed discharge savings).
    savings = round(do_nothing_cost - selected_cost, 4)

    # Alternative: do-nothing (battery fully idle for the whole horizon).
    # Always include this as a rejected alternative so the user can see
    # the baseline comparison even when the savings are marginal or negative.
    if selected_strategy != "discharge_only":
        if savings > 1e-4:
            do_nothing_reason = (
                f"Battery idle would cost {do_nothing_cost:.4f}; "
                f"selected plan saves {savings:.4f} over the horizon."
            )
        elif savings < -1e-4:
            do_nothing_reason = (
                f"Battery idle would cost {do_nothing_cost:.4f}. "
                f"Selected plan costs {abs(savings):.4f} more due to "
                "charging overhead; discharge savings expected to materialise "
                "within the current scheduling window."
            )
        else:
            do_nothing_reason = (
                f"Battery idle cost ({do_nothing_cost:.4f}) and selected plan "
                f"cost ({selected_cost:.4f}) are approximately equal; "
                "strategy chosen for schedule adherence."
            )
        rejected.append(
            RejectedPlan(
                name="do_nothing",
                reason=do_nothing_reason,
                estimated_cost=do_nothing_cost,
            )
        )

    # Alternative: charge-only (no discharge), relevant when discharge was chosen
    if has_discharge and not has_grid_charge:
        rejected.append(
            RejectedPlan(
                name="charge_only_solar",
                reason=(
                    "Charging from solar without discharging would leave "
                    f"{forecast_pv:.1f} kWh of PV unused during peak demand."
                ),
                estimated_cost=do_nothing_cost,
            )
        )

    # Alternative: grid charge skipped when price spread too small
    if not has_grid_charge and price_spread > 0:
        min_diff_values = [
            s.min_price_difference
            for s in inp.battery_schedules
            if s.enabled and abs(s.min_price_difference) > 1e-9
        ]
        min_diff = min(min_diff_values) if min_diff_values else 0.0
        if price_spread < min_diff:
            rejected.append(
                RejectedPlan(
                    name="grid_charge_rejected_spread",
                    reason=(
                        f"Price spread {price_spread:.4f} is below the minimum "
                        f"required difference {min_diff:.4f}; grid charging "
                        "not profitable."
                    ),
                    estimated_cost=do_nothing_cost,
                )
            )

    # Score: estimated savings vs doing nothing.
    # Positive = the plan saves money compared to leaving the battery idle.
    # Negative = the plan costs more than idle (pre-charge overhead dominates).
    score = savings

    return PlanExplanation(
        selected_strategy=selected_strategy,
        summary=summary,
        score=score,
        estimated_total_cost=selected_cost,
        price_spread=price_spread,
        peak_import_price=round(peak_import, 4),
        off_peak_import_price=round(off_peak_import, 4),
        forecast_pv_kwh=forecast_pv,
        forecast_net_consumption_kwh=forecast_net,
        battery_soc_pct=round(inp.battery_soc_pct, 1),
        battery_soc_at_end_pct=round(battery_soc_at_end, 1),
        constraints=constraints,
        rejected_plans=rejected,
    )
