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
    DataQuality,
    DischargeWindow,
    PlanExplanation,
    PlannedSlot,
    PlannerOutput,
    RejectedPlan,
)
from custom_components.hsem.planner.candidate_generator import generate_candidates
from custom_components.hsem.planner.candidate_selector import (
    replacement_price_from_next_discharge,
    select_best_candidate,
)
from custom_components.hsem.planner.charge_scheduler import (
    apply_arbitrage_grid_charge,
    apply_charge_schedules,
    apply_discharge_schedules,
    apply_excess_export,
    apply_opportunistic_charge,
    apply_optimization_strategy,
    calculate_required_battery_until_solar,
    concentrate_discharge_on_expensive_slots,
)
from custom_components.hsem.planner.cost_function import CostWeights, score_plan
from custom_components.hsem.planner.ev_planner import (
    EVChargingPlan,
    EVPlannerInput,
    apply_ev_planned_load_to_slots,
    build_ev_charging_plan,
)
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
from custom_components.hsem.planner.soc_simulation import simulate_soc
from custom_components.hsem.utils.datetime_utils import as_tz
from custom_components.hsem.utils.logger import log_planner
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

    log_planner(
        "info",
        "==== HSEM PLANNER RUN START ==== now=%s interval=%dmin horizon=%dh",
        inp.now_iso,
        inp.interval_minutes,
        inp.interval_length_hours,
    )

    # Battery state
    usable_kwh, current_kwh = usable_capacity(
        inp.battery_rated_capacity_kwh,
        inp.battery_soc_pct,
        inp.battery_end_of_discharge_soc_pct,
        inp.battery_max_soc_pct,
    )

    log_planner(
        "debug",
        "[engine] Battery inputs: rated=%.2f kWh  soc=%.1f%%  "
        "min_soc=%.1f%%  max_soc=%.1f%%  "
        "→ current_kwh=%.3f  usable_kwh=%.3f",
        inp.battery_rated_capacity_kwh,
        inp.battery_soc_pct,
        inp.battery_end_of_discharge_soc_pct,
        inp.battery_max_soc_pct,
        current_kwh,
        usable_kwh,
    )
    log_planner(
        "debug",
        "[engine] Battery power limits: max_charge=%dW  max_discharge=%s  "
        "charge_eff=%.1f%%  discharge_eff=%.1f%%",
        inp.battery_max_charge_power_w,
        (
            f"{inp.battery_max_discharge_power_w}W"
            if inp.battery_max_discharge_power_w is not None
            else "unlimited"
        ),
        inp.battery_charge_efficiency_pct,
        inp.battery_discharge_efficiency_pct,
    )
    log_planner(
        "debug",
        "[engine] Consumption weights: 1d=%d%%  3d=%d%%  7d=%d%%  14d=%d%%",
        inp.weight_1d,
        inp.weight_3d,
        inp.weight_7d,
        inp.weight_14d,
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

    log_planner("debug", "[engine] Generated %d planning slots", len(slots))

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

    # Surface any hours where input data was absent (generic — all series)
    for hour in sorted(tsi.missing_hours()):
        missing_inputs.append(f"hour_{hour:02d}")

    # -----------------------------------------------------------------------
    # Explicit future-day data-quality diagnostics (issue #370 + #324)
    # -----------------------------------------------------------------------
    # Detect missing future price and PV data separately so dashboards and
    # degraded-mode classification can distinguish them from general missing
    # hours.  We must NOT silently treat missing future data as real zero —
    # instead we surface the gap explicitly.
    #
    # For multi-day horizons (48 h or 72 h) we also check day+2 (day_offset=2)
    # and emit appropriate warnings.  Longer forecast data is inherently less
    # reliable, so confidence decay is applied to day-2+ PV estimates below.
    horizon_has_tomorrow = tsi.has_tomorrow_slots()
    horizon_days = tsi.horizon_days
    tomorrow_price_missing = sorted(tsi.missing_future_day_price_hours(1))
    tomorrow_pv_missing = sorted(tsi.missing_future_day_pv_hours(1))
    day2_price_missing = sorted(tsi.missing_future_day_price_hours(2))
    day2_pv_missing = sorted(tsi.missing_future_day_pv_hours(2))

    # Collect today's missing price/PV hours for complete diagnostics
    key_to_hour: dict = {m.key: m.hour for m in tsi.slots}
    today_price_missing = sorted(
        {
            key_to_hour[key]
            for key in tsi.missing_price_slots
            if key in key_to_hour and key.day_offset == 0
        }
    )
    today_pv_missing = sorted(
        {
            key_to_hour[key]
            for key in tsi.missing_pv_slots
            if key in key_to_hour and key.day_offset == 0
        }
    )

    data_quality = DataQuality(
        tomorrow_price_missing_hours=tomorrow_price_missing,
        tomorrow_pv_missing_hours=tomorrow_pv_missing,
        day2_price_missing_hours=day2_price_missing,
        day2_pv_missing_hours=day2_pv_missing,
        today_price_missing_hours=today_price_missing,
        today_pv_missing_hours=today_pv_missing,
        horizon_has_tomorrow=horizon_has_tomorrow,
        horizon_days=horizon_days,
    )

    # Surface tomorrow-specific missing data as explicit missing_inputs entries.
    # These labels are non-critical (do not match battery/house-load keywords)
    # so they trigger DegradedMode.Degraded — hardware writes are still allowed
    # but the plan is based on incomplete future data.
    if tomorrow_price_missing:
        hours_str = ",".join(f"{h:02d}" for h in tomorrow_price_missing)
        missing_inputs.append(f"tomorrow_price_missing_hours:{hours_str}")
        warnings.append(
            f"Tomorrow price data missing for {len(tomorrow_price_missing)} hour(s): "
            f"{hours_str}. Affected slots use 0.0 (import/export) as fallback — "
            "plan may not be optimal."
        )

    if tomorrow_pv_missing:
        hours_str = ",".join(f"{h:02d}" for h in tomorrow_pv_missing)
        missing_inputs.append(f"tomorrow_pv_missing_hours:{hours_str}")
        warnings.append(
            f"Tomorrow PV forecast missing for {len(tomorrow_pv_missing)} hour(s): "
            f"{hours_str}. Affected slots assume zero PV production — "
            "plan may over-charge from grid."
        )

    # Surface day+2 missing data (72-hour horizon) in the same non-critical style.
    if day2_price_missing:
        hours_str = ",".join(f"{h:02d}" for h in day2_price_missing)
        missing_inputs.append(f"day2_price_missing_hours:{hours_str}")
        warnings.append(
            f"Day+2 price data missing for {len(day2_price_missing)} hour(s): "
            f"{hours_str}. Affected slots use 0.0 as fallback."
        )

    if day2_pv_missing:
        hours_str = ",".join(f"{h:02d}" for h in day2_pv_missing)
        missing_inputs.append(f"day2_pv_missing_hours:{hours_str}")
        warnings.append(
            f"Day+2 PV forecast missing for {len(day2_pv_missing)} hour(s): "
            f"{hours_str}. Affected slots assume zero PV production."
        )

    # -----------------------------------------------------------------------
    # Confidence decay for multi-day horizons (issue #324)
    # -----------------------------------------------------------------------
    # Price and PV forecasts for day+1 and beyond are inherently less reliable
    # than today's data.  To avoid the planner over-committing to uncertain
    # future plans we apply a per-day confidence decay factor to PV estimates:
    #
    #   day 0 (today):         factor = 1.00  (no decay — current forecast)
    #   day 1 (tomorrow):      factor = 0.90  (10 % conservative discount)
    #   day 2 (day after):     factor = 0.80  (20 % conservative discount)
    #
    # Only PV is discounted — electricity prices are used as-is because they
    # are either known (spot market) or zero-fallback (missing).  Discounting
    # prices would distort the cost function in unpredictable ways.
    #
    # The decay is applied AFTER missing-data diagnostics so that the
    # DataQuality report reflects the *original* data gaps, not the decayed
    # values.
    _DECAY_BY_DAY: dict[int, float] = {0: 1.00, 1: 0.90, 2: 0.80}
    if horizon_days > 1:
        for slot in slots:
            # Identify the slot's day_offset via the TSI so we use the same
            # authoritative slot boundaries rather than ad-hoc date arithmetic.
            slot_idx = tsi.slot_index_for(slot.start)
            if slot_idx is None:
                continue
            day_offset = tsi.slots[slot_idx].key.day_offset
            if day_offset == 0:
                continue  # today — no decay
            decay = _DECAY_BY_DAY.get(day_offset, 0.80)
            slot.solcast_pv_estimate = round(slot.solcast_pv_estimate * decay, 3)
        if horizon_days >= 2:
            warnings.append(
                f"Multi-day horizon ({horizon_days} day(s)): confidence decay applied "
                f"to PV estimates — day+1 at 90 %, day+2+ at 80 %."
            )

    # -----------------------------------------------------------------------
    # EV planned load injection (issues #396, #404)
    # -----------------------------------------------------------------------
    # Build EV charging plans for the primary and secondary EV independently,
    # then split their per-slot loads into three semantic fields on each slot:
    #
    #   ev_planned_load_kwh      — extra EV AC load added to net consumption
    #                              (only when base_load_includes_ev=False)
    #   ev_accounted_load_kwh    — EV AC load already in house consumption
    #                              (only when base_load_includes_ev=True)
    #   ev_total_planned_load_kwh — sum of both; always reflects total EV demand
    #
    # The split is performed AFTER aggregating raw totals across both EVs so
    # that one EV can never overwrite the other's load.
    #
    # Design:
    #   1. Collect raw per-slot AC totals unconditionally for each EV.
    #   2. Collect injected (extra) per-slot AC totals only when
    #      base_load_includes_ev=False (via apply_ev_planned_load_to_slots).
    #   3. Write all three fields on the slot once both EVs are aggregated.
    #
    # This ensures:
    #   - Solar surplus is computed after EV demand is subtracted.
    #   - Battery solar-charge recommendations don't claim solar consumed by EVs.
    #   - No circular dependency: EV plans are built from raw inputs only.
    #   - No double-counting when base_load_includes_ev=True.
    #   - Diagnostics always show total planned EV load regardless of the
    #     base_load_includes_ev flag.
    ev_charging_plan: EVChargingPlan | None = None
    ev_second_charging_plan: EVChargingPlan | None = None

    # combined_ev_raw_load — sum of ALL EV AC loads per slot, regardless of
    # base_load_includes_ev.  Used to compute ev_total_planned_load_kwh and
    # ev_accounted_load_kwh.
    combined_ev_raw_load = [0.0] * len(slots)
    # combined_ev_injected_load — sum of EV AC loads that must be ADDED to
    # net consumption (base_load_includes_ev=False only).
    combined_ev_injected_load = [0.0] * len(slots)

    # Populate base net consumption BEFORE EV planning so the surplus signal
    # used for EV slot selection reflects the true house load after solar.
    # The house consumes solar first; only what remains (negative net = surplus)
    # is available to the EV charger at no extra grid cost.
    # After EV injection, populate_net_consumption is called again to incorporate
    # ev_planned_load_kwh into the final estimated_net_consumption values.
    populate_net_consumption(slots)

    # Net surplus per slot = max(-estimated_net_consumption, 0).
    # This is the energy available to the EV charger beyond house demand after
    # solar — the correct starting point for EV slot selection.
    # Using raw PV here would over-state available free energy because the
    # house has already consumed a portion of the solar output.
    slot_net_surplus = [max(-s.estimated_net_consumption, 0.0) for s in slots]
    _slot_starts = [s.start for s in slots]
    _slot_ends = [s.end for s in slots]
    _slot_prices = [s.price.import_price for s in slots]

    def _build_and_inject(
        enabled: bool,
        connected: bool,
        smart: bool,
        soc: float,
        target: float,
        cap_kwh: float,
        pwr_kw: float,
        eff: float,
        deadline,
        base_includes: bool,
        label: str,
    ) -> EVChargingPlan | None:
        """Build an EV plan and accumulate its loads.

        Accumulates into two separate slot arrays:
        - ``combined_ev_raw_load``: total AC load for ALL EVs (always).
        - ``combined_ev_injected_load``: extra load for net consumption math
          (only when ``base_includes=False``).
        """
        if not enabled:
            return None
        ev_inp = EVPlannerInput(
            enabled=enabled,
            ev_connected=connected,
            smart_charging_enabled=smart,
            current_soc_pct=soc,
            target_soc_pct=target,
            battery_capacity_kwh=cap_kwh,
            charger_power_kw=pwr_kw,
            charger_efficiency_pct=eff,
            deadline=deadline,
            base_load_includes_ev=base_includes,
            now=now,
        )
        plan = build_ev_charging_plan(
            ev_inp,
            slots_start=_slot_starts,
            slots_end=_slot_ends,
            slot_net_surplus_kwh=slot_net_surplus,
            slot_import_price=_slot_prices,
        )

        # Accumulate raw (unconditional) totals — always additive.
        # base_load_includes_ev=False passed here so apply never skips.
        raw_load_by_idx = [0.0] * len(slots)
        apply_ev_planned_load_to_slots(
            slot_starts=_slot_starts,
            slot_ev_planned_load_kwh=raw_load_by_idx,
            ev_plan=plan,
            base_load_includes_ev=False,  # always accumulate raw totals
        )
        for i in range(len(slots)):
            combined_ev_raw_load[i] += raw_load_by_idx[i]

        # Accumulate injected (net consumption) totals — skipped when base load
        # already includes EV to avoid double-counting.
        injected_by_idx = [0.0] * len(slots)
        apply_ev_planned_load_to_slots(
            slot_starts=_slot_starts,
            slot_ev_planned_load_kwh=injected_by_idx,
            ev_plan=plan,
            base_load_includes_ev=base_includes,  # respects the flag
        )
        for i in range(len(slots)):
            combined_ev_injected_load[i] += injected_by_idx[i]

        ev_extra_kwh = sum(injected_by_idx)
        ev_accounted_kwh = sum(raw_load_by_idx) - ev_extra_kwh
        ev_total_kwh = sum(raw_load_by_idx)
        if plan.state not in (
            "not_connected",
            "smart_charging_disabled",
            "fully_charged",
        ):
            warnings.append(
                f"EV planned load ({label}): state={plan.state}, "
                f"total_kwh_needed={plan.total_kwh_needed:.2f}, "
                f"charging_slots={len(plan.charging_slots)}, "
                f"ev_extra_load_kwh={ev_extra_kwh:.3f}, "
                f"ev_accounted_load_kwh={ev_accounted_kwh:.3f}, "
                f"ev_total_planned_load_kwh={ev_total_kwh:.3f}, "
                f"base_load_includes_ev={base_includes}."
            )
        return plan

    ev_charging_plan = _build_and_inject(
        enabled=inp.ev_planned_load_enabled,
        connected=inp.ev_planned_load_connected,
        smart=inp.ev_planned_load_smart_charging_enabled,
        soc=inp.ev_planned_load_current_soc_pct,
        target=inp.ev_planned_load_target_soc_pct,
        cap_kwh=inp.ev_planned_load_battery_capacity_kwh,
        pwr_kw=inp.ev_planned_load_charger_power_kw,
        eff=inp.ev_planned_load_charger_efficiency_pct,
        deadline=inp.ev_planned_load_deadline,
        base_includes=inp.ev_planned_load_base_load_includes_ev,
        label="primary",
    )
    ev_second_charging_plan = _build_and_inject(
        enabled=inp.ev_second_planned_load_enabled,
        connected=inp.ev_second_planned_load_connected,
        smart=inp.ev_second_planned_load_smart_charging_enabled,
        soc=inp.ev_second_planned_load_current_soc_pct,
        target=inp.ev_second_planned_load_target_soc_pct,
        cap_kwh=inp.ev_second_planned_load_battery_capacity_kwh,
        pwr_kw=inp.ev_second_planned_load_charger_power_kw,
        eff=inp.ev_second_planned_load_charger_efficiency_pct,
        deadline=inp.ev_second_planned_load_deadline,
        base_includes=inp.ev_second_planned_load_base_load_includes_ev,
        label="second",
    )

    # Write all three EV load fields into each slot.
    #
    # Semantics (per-slot):
    #   ev_planned_load_kwh      — extra load added to net consumption
    #   ev_accounted_load_kwh    — load already in avg_house_consumption
    #   ev_total_planned_load_kwh — raw total (injected + accounted)
    #
    # Re-run populate_net_consumption to incorporate ev_planned_load_kwh into
    # estimated_net_consumption.  The first run (above, before EV planning)
    # was used purely to derive the per-slot net surplus for EV slot selection.
    # This second run produces the final estimated_net_consumption values that
    # include any extra EV load (base_load_includes_ev=False case).
    for i, slot in enumerate(slots):
        slot.ev_planned_load_kwh = combined_ev_injected_load[i]
        slot.ev_accounted_load_kwh = round(
            combined_ev_raw_load[i] - combined_ev_injected_load[i], 3
        )
        slot.ev_total_planned_load_kwh = round(combined_ev_raw_load[i], 3)

    populate_net_consumption(slots)  # second pass: incorporates ev_planned_load_kwh
    populate_estimated_cost(slots)

    # Depreciation threshold diagnostic.
    # calculate_recommended_threshold returns the minimum economically
    # justified price difference (depreciation + conversion loss).
    recommended_threshold = calculate_recommended_threshold(
        purchase_price=inp.battery_purchase_price,
        expected_cycles=inp.battery_expected_cycles,
        usable_capacity=usable_kwh,
    )
    if recommended_threshold > 0:
        warnings.append(
            f"Recommended price threshold: {recommended_threshold:.4f} "
            f"(depreciation + conversion loss)."
        )

    log_planner(
        "debug",
        "[engine] Recommended price threshold: %.4f  "
        "(purchase=%.0f  cycles=%d  usable=%.2f kWh)",
        recommended_threshold,
        inp.battery_purchase_price,
        inp.battery_expected_cycles,
        usable_kwh,
    )

    # Log per-slot populated data for full transparency
    log_planner("debug", "[engine] ---- Slot population summary ----")
    for slot in slots:
        log_planner(
            "debug",
            "[slot] %s→%s  import_price=%.4f  export_price=%.4f  "
            "pv=%.3f  cons=%.3f  net=%.3f  est_cost=%.4f",
            slot.start.strftime("%d %H:%M"),
            slot.end.strftime("%H:%M"),
            slot.price.import_price,
            slot.price.export_price,
            slot.solcast_pv_estimate,
            slot.avg_house_consumption,
            slot.estimated_net_consumption,
            slot.estimated_cost,
        )

    # Mark past slots
    mark_time_passed(slots, now)

    # Discharge schedule detection
    apply_discharge_schedules(slots, inp.battery_schedules, now)

    # Charge scheduling
    charge_eff_dec = inp.battery_charge_efficiency_pct / 100.0
    discharge_eff_dec = inp.battery_discharge_efficiency_pct / 100.0
    # Roundtrip loss derived from separate charge/discharge efficiencies.
    roundtrip_loss_pct = (1.0 - charge_eff_dec * discharge_eff_dec) * 100.0
    max_charge_per_hour = (inp.battery_max_charge_power_w / 1000) * charge_eff_dec
    max_charge_per_interval = max_charge_per_hour / (60 / inp.interval_minutes)

    apply_charge_schedules(
        slots,
        inp.battery_schedules,
        now,
        max_charge_per_interval,
        cycle_cost_per_kwh=inp.battery_cycle_cost_per_kwh,
        recommended_threshold=recommended_threshold,
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

    # Arbitrage grid charge: charge in cheap future slots whenever an
    # expensive future import slot can be offset, even without a configured
    # discharge schedule.  Runs after scheduled and opportunistic passes so
    # that it never overwrites their assignments, and before the seasonal
    # fallback so that newly chosen cheap slots are not turned into
    # BatteriesDischarge by the fallback.
    apply_arbitrage_grid_charge(
        slots,
        inp.battery_schedules,
        now,
        current_kwh,
        usable_kwh,
        max_charge_per_interval,
        conversion_loss_pct=roundtrip_loss_pct,
        cycle_cost_per_kwh=inp.battery_cycle_cost_per_kwh,
        recommended_threshold=recommended_threshold,
    )

    # Derive per-slot power limits
    max_charge_per_slot = (inp.battery_max_charge_power_w / 1000 * charge_eff_dec) / (
        60 / inp.interval_minutes
    )
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

    log_planner(
        "debug",
        "[engine] max_charge_per_slot=%.3f kWh  max_discharge_per_slot=%s kWh  "
        "max_soc_capacity=%.3f kWh",
        max_charge_per_slot,
        f"{max_discharge_per_slot:.3f}" if max_discharge_per_slot is not None else "∞",
        max_soc_capacity_kwh,
    )

    # Log scheduled baseline recommendations before candidate generation
    log_planner(
        "debug", "[engine] ---- Baseline slot recommendations (pre-candidate) ----"
    )
    for slot in slots:
        log_planner(
            "debug",
            "[baseline] %s→%s  rec=%s  charged=%.3f kWh  ev_load=%.3f kWh",
            slot.start.strftime("%d %H:%M"),
            slot.end.strftime("%H:%M"),
            slot.recommendation or "None",
            slot.batteries_charged,
            slot.ev_planned_load_kwh,
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
        charge_efficiency_pct=inp.battery_charge_efficiency_pct,
        discharge_efficiency_pct=inp.battery_discharge_efficiency_pct,
    )
    slot_duration_hours = inp.interval_minutes / 60.0

    # Replacement price for the terminal-SoC opportunity cost term (issue #413).
    # We use the most expensive import prices from the next active discharge
    # schedule window.  The energy stored at end-of-horizon avoids importing at
    # those peak prices, so those are the appropriate replacement cost.
    # top_n is derived from battery capacity / discharge rate so it reflects
    # how many slots the battery can actually serve (fixes hardcoded 4-slot bug).
    import math

    top_n: int = 4  # safe fallback
    if max_discharge_per_slot is not None and max_discharge_per_slot > 1e-9:
        top_n = math.ceil(usable_kwh / max_discharge_per_slot)
    replacement_price_per_kwh: float | None = replacement_price_from_next_discharge(
        slots, now, top_n=top_n
    )
    log_planner(
        "debug",
        "[engine] terminal-SoC replacement price: %s  (from next discharge window)",
        (
            f"{replacement_price_per_kwh:.4f}"
            if replacement_price_per_kwh is not None
            else "(none — no future discharge slots)"
        ),
    )

    # Concentrate battery discharge on the most expensive slots — avoid
    # draining the battery on moderate-price slots when there are high-price
    # slots later in the horizon that would benefit more from the stored energy.
    concentrate_discharge_on_expensive_slots(
        slots,
        now,
        current_kwh,
        usable_kwh,
        max_discharge_per_slot,
        discharge_efficiency_pct=inp.battery_discharge_efficiency_pct,
    )

    candidates = generate_candidates(
        slots,
        inp,
        now,
        max_charge_per_slot,
        current_kwh=current_kwh,
        usable_kwh=usable_kwh,
        max_discharge_per_slot=max_discharge_per_slot,
    )
    log_planner(
        "debug",
        "[engine] ---- Candidate selection: %d candidates generated ----",
        len(candidates),
    )
    for cand in candidates:
        log_planner("debug", "[candidate] name=%s", cand.name)

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
        charge_efficiency_pct=inp.battery_charge_efficiency_pct,
        discharge_efficiency_pct=inp.battery_discharge_efficiency_pct,
        replacement_price_per_kwh=replacement_price_per_kwh,
    )

    winner_score = getattr(getattr(winner, "_cost", None), "score", None)
    winner_total_cost = getattr(getattr(winner, "_cost", None), "total_cost", None)
    log_planner(
        "info",
        "[engine] WINNER candidate: %s  score=%.4f  total_cost=%.4f",
        winner.name,
        winner_score if winner_score is not None else float("nan"),
        winner_total_cost if winner_total_cost is not None else float("nan"),
    )
    for rp in candidate_rejected:
        log_planner(
            "debug",
            "[rejected] %s  score=%.4f  reason=%s",
            rp.name,
            rp.estimated_cost,
            rp.reason,
        )

    # Use the winning candidate's slots as the final plan
    slots = winner.slots

    # Fill any remaining None recommendations on the winner's slots.
    # apply_optimization_strategy only modifies slots where recommendation is
    # None, so it will not disturb the intentional charge/discharge assignments
    # made by the winning strategy.  This guarantees that every slot has a
    # valid recommendation (BatteriesWaitMode, BatteriesChargeSolar, etc.)
    # regardless of which candidate was selected.
    #
    # Important: the fill pass may set batteries_charged on newly-assigned
    # BatteriesChargeSolar slots.  Those changes are not reflected in the
    # SoC fields that were written by the candidate's simulate_soc call.
    # We therefore re-run simulate_soc on the final slots so that
    # grid_import_kwh, grid_export_kwh, batteries_discharged, and
    # estimated_battery_soc are all consistent with the final recommendations.
    # Then we re-run score_plan to satisfy the spec invariant:
    #   output.plan_cost == score_plan(output.slots)
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

    # Re-run SoC simulation on the final (fill-completed) slots so that all
    # energy-flow fields are consistent with the final recommendations.
    simulate_soc(
        slots,
        now,
        current_kwh,
        usable_kwh,
        max_soc_capacity_kwh,
        max_charge_per_slot,
        max_discharge_per_slot,
        rated_kwh=inp.battery_rated_capacity_kwh,
        end_of_discharge_soc_pct=inp.battery_end_of_discharge_soc_pct,
        charge_efficiency_pct=inp.battery_charge_efficiency_pct,
        discharge_efficiency_pct=inp.battery_discharge_efficiency_pct,
    )

    # -----------------------------------------------------------------------
    # EV smart-charging recommendation labelling
    # -----------------------------------------------------------------------
    # Slots with EV planned load should be marked EVSmartCharging so that
    # dashboards and the working-mode sensor reflect that the slot is
    # primarily serving EV demand.
    #
    # Priority order (highest wins):
    #   batteries_charge_grid     — keep (grid charge overrides EV label)
    #   force_batteries_discharge — keep (forced export overrides EV label)
    #   force_export              — keep
    #   batteries_discharge_mode  — keep (scheduled discharge overrides EV label)
    #   time_passed               — keep
    #   ev_smart_charging         ← applied when ev_planned_load_kwh > 0
    #   batteries_charge_solar    — overridden by EV label
    #   batteries_wait_mode       — overridden by EV label
    # Recommendations that must never be overridden by the EV label.
    #
    # batteries_charge_grid     — grid charge takes absolute priority; overriding
    #                             it with ev_smart_charging would hide active grid
    #                             charging and break hardware-write logic.
    # force_batteries_discharge — forced export is a revenue action; EV label
    #                             must not obscure it.
    # force_export              — same reasoning as forced discharge.
    # time_passed               — past slots must not be relabelled.
    # missing_input_entities    — degraded-mode slots must not be relabelled.
    #
    # batteries_discharge_mode is intentionally NOT in this set.
    # When an EV is scheduled to charge in a slot that would otherwise be a
    # scheduled discharge window, the EV label takes precedence so dashboards
    # and the working-mode sensor correctly reflect EV activity rather than
    # showing a discharge recommendation during an active EV charge session.
    _EV_LABEL_KEEP = frozenset(
        {
            Recommendations.BatteriesChargeGrid.value,
            Recommendations.ForceBatteriesDischarge.value,
            Recommendations.ForceExport.value,
            Recommendations.TimePassed.value,
            Recommendations.MissingInputEntities.value,
        }
    )
    for slot in slots:
        # Use ev_total_planned_load_kwh so that EVSmartCharging is applied even
        # when base_load_includes_ev=True (where ev_planned_load_kwh stays 0
        # but EV charging is still planned and must be visible in the UI).
        if (
            abs(slot.ev_total_planned_load_kwh) > 1e-9
            and slot.recommendation not in _EV_LABEL_KEEP
        ):
            slot.recommendation = Recommendations.EVSmartCharging.value

    # Log final per-slot decisions with full energy-flow detail
    log_planner("info", "[engine] ---- Final slot decisions (post-simulation) ----")
    for slot in slots:
        is_current = as_tz(slot.start, now.tzinfo) <= now < as_tz(slot.end, now.tzinfo)
        log_planner(
            "info",
            "[final] %s%s→%s  rec=%-30s  soc=%5.1f%%  "
            "charged=%.3f  discharged=%.3f  "
            "pv=%.3f  cons=%.3f  net=%.3f  "
            "grid_in=%.3f  grid_out=%.3f  "
            "import_price=%.4f  export_price=%.4f  ev=%.3f",
            "▶ " if is_current else "  ",
            slot.start.strftime("%d %H:%M"),
            slot.end.strftime("%H:%M"),
            slot.recommendation if slot.recommendation is not None else "(none!)",
            slot.estimated_battery_soc,
            slot.batteries_charged,
            slot.batteries_discharged,
            slot.solcast_pv_estimate,
            slot.avg_house_consumption,
            slot.estimated_net_consumption,
            slot.grid_import_kwh,
            slot.grid_export_kwh,
            slot.price.import_price,
            slot.price.export_price,
            slot.ev_total_planned_load_kwh,
        )

    # Current recommendation
    current_recommendation: str | None = None
    for slot in slots:
        if as_tz(slot.start, now.tzinfo) <= now < as_tz(slot.end, now.tzinfo):
            current_recommendation = slot.recommendation
            break

    # Final SoC
    future_slots = [s for s in slots if as_tz(s.end, now.tzinfo) > now]
    battery_soc_at_end = future_slots[-1].estimated_battery_soc if future_slots else 0.0

    # Derive contiguous charge/discharge windows
    charge_windows, discharge_windows = _derive_windows(slots)

    # Build human-readable plan explanation
    explanation = _build_explanation(inp, slots, battery_soc_at_end, now)

    # Score the final (fill-completed, re-simulated) slots.
    # The spec invariant requires: output.plan_cost == score_plan(output.slots).
    # Because we re-ran simulate_soc above, the slot fields are now fully
    # consistent with the final recommendations and this score is authoritative.
    # The terminal-SoC opportunity cost (issue #413) uses the same
    # ``current_kwh`` initial value and average-future-import replacement
    # price that the selector used, so the final score is identical to the
    # winning candidate's score for the same slots.
    plan_cost = score_plan(
        slots,
        cost_weights,
        slot_duration_hours=slot_duration_hours,
        now=now,
        initial_battery_kwh=current_kwh,
        replacement_price_per_kwh=replacement_price_per_kwh,
    )

    # Merge candidate-rejected alternatives into the explanation's rejected list
    # (the explanation already contains schedule-based rejected alternatives built
    # by _build_explanation; we append the candidate-selection rejections after).
    for rp in candidate_rejected:
        explanation.rejected_plans.append(rp)

    log_planner(
        "info",
        "[engine] ==== PLANNER COMPLETE ==== "
        "winner=%s  score=%.4f  total_cost=%.4f  "
        "import=%.4f  export_rev=%.4f  "
        "conv_loss=%.4f  cycle=%.4f  soc_pen=%.4f  term_soc=%.4f  "
        "battery_soc_end=%.1f%%  required_cap=%.3f kWh  "
        "current_rec=%s",
        winner.name,
        plan_cost.score,
        plan_cost.total_cost,
        plan_cost.import_cost,
        plan_cost.export_revenue,
        plan_cost.conversion_loss_cost,
        plan_cost.cycle_cost,
        plan_cost.soc_penalty,
        plan_cost.terminal_soc_value,
        battery_soc_at_end,
        required_capacity,
        current_recommendation if current_recommendation is not None else "(none)",
    )

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
        data_quality=data_quality,
        explanation=explanation,
        plan_cost=plan_cost,
        candidates=candidates,
        ev_charging_plan=ev_charging_plan,
        ev_second_charging_plan=ev_second_charging_plan,
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
