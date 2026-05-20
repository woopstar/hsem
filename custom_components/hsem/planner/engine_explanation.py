"""Explanation and formatting logic for the HSEM planner.

Single responsibility: build human-readable plan explanations and derive
contiguous charge/discharge windows from the finalised slot list.

**No Home Assistant types are imported here.**  This makes the module
directly testable with plain ``pytest`` without a running HA instance.
"""

from __future__ import annotations

from datetime import datetime

from custom_components.hsem.models.planner_inputs import PlannerInput
from custom_components.hsem.models.planner_outputs import (
    ChargeWindow,
    DischargeWindow,
    PlanExplanation,
    PlannedSlot,
    RejectedPlan,
)
from custom_components.hsem.utils.datetime_utils import as_tz
from custom_components.hsem.utils.misc import calculate_recommended_threshold
from custom_components.hsem.utils.recommendations import Recommendations

# Sets used by _derive_windows — kept here as they reference Recommendations
_CHARGE_RECOMMENDATIONS = frozenset(
    {
        Recommendations.BatteriesChargeGrid.value,
        Recommendations.BatteriesChargeSolar.value,
        Recommendations.EVSmartCharging.value,
    }
)
_DISCHARGE_RECOMMENDATIONS = frozenset(
    {
        Recommendations.BatteriesDischargeMode.value,
        Recommendations.ForceBatteriesDischarge.value,
    }
)


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
        total_e = round(sum(s.batteries_charged_kwh for s in group), 3)
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
    future_slots = [s for s in slots if as_tz(s.end, now.tzinfo) > now]

    # --- Price metrics ---------------------------------------------------
    import_prices = [s.price.import_price for s in future_slots]
    export_prices = [s.price.export_price for s in future_slots]
    peak_import = max(import_prices) if import_prices else 0.0
    off_peak_import = min(import_prices) if import_prices else 0.0
    price_spread = round(peak_import - off_peak_import, 4)

    # --- Depreciation threshold for spread rejection diagnostics ---------
    eod_soc = inp.battery_end_of_discharge_soc_pct / 100.0
    usable_kwh = inp.battery_rated_capacity_kwh * (1.0 - eod_soc)
    recommended_threshold = calculate_recommended_threshold(
        purchase_price=inp.battery_purchase_price,
        expected_cycles=inp.battery_expected_cycles,
        usable_capacity=usable_kwh,
    )

    # --- Forecast metrics ------------------------------------------------
    forecast_pv = round(sum(s.solcast_pv_estimate_kwh for s in future_slots), 3)
    forecast_net = round(sum(s.estimated_net_consumption_kwh for s in future_slots), 3)

    # --- Cost of the selected plan ---------------------------------------
    selected_cost = round(sum(s.estimated_cost_currency for s in future_slots), 4)

    # --- Do-nothing baseline cost (battery fully idle, pay import for all load) ---
    # Computed here so strategy detection can use it in summaries.
    do_nothing_cost = round(
        sum(
            max(s.estimated_net_consumption_kwh, 0.0) * s.price.import_price
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
    if not has_grid_charge and price_spread > 0 and recommended_threshold > 1e-9:
        if price_spread < recommended_threshold:
            rejected.append(
                RejectedPlan(
                    name="grid_charge_rejected_spread",
                    reason=(
                        f"Price spread {price_spread:.4f} is below the minimum "
                        f"required difference {recommended_threshold:.4f}; grid charging "
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
