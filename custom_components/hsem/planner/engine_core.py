"""Core planning flow for the HSEM planner.

Orchestrates the planning pipeline and returns a :class:`PlannerOutput`.

**No Home Assistant types are imported here.**  Makes the engine
directly testable with plain ``pytest`` without a running HA instance.
"""

from __future__ import annotations

from datetime import datetime

from custom_components.hsem.models.ev_config import EVConfig
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.planner.candidate_generator import (
    CANDIDATE_MILP,
    generate_candidates,
)
from custom_components.hsem.planner.candidate_selector import (
    replacement_price_from_next_discharge,
    select_best_candidate,
)
from custom_components.hsem.planner.charging.arbitrage_charge import (
    apply_arbitrage_grid_charge,
)
from custom_components.hsem.planner.charging.opportunistic_charge import (
    apply_opportunistic_charge,
)
from custom_components.hsem.planner.charging.pre_charge import apply_charge_schedules
from custom_components.hsem.planner.cost_function import CostWeights, score_plan
from custom_components.hsem.planner.discharge_scheduler import (
    apply_discharge_schedules,
    apply_excess_export,
    apply_optimization_strategy,
    calculate_required_battery_until_solar,
)
from custom_components.hsem.planner.engine_ev import (
    _build_and_inject_for_ev,
    _compute_ev_charger_power,
)
from custom_components.hsem.planner.engine_ev_milp import (
    _build_ev_configs_for_milp,
)
from custom_components.hsem.planner.engine_explanation import (
    _build_explanation,
    _derive_windows,
)
from custom_components.hsem.planner.engine_population import (
    _inject_live_data_into_current_slot,
    _parse_now,
    _populate_slots,
)
from custom_components.hsem.planner.ev_planner import (
    EVChargingPlan,
    rebuild_ev_plan_from_slots,
)
from custom_components.hsem.planner.slot_population import (
    build_slots,
    build_time_series_index,
    mark_time_passed,
    populate_battery_capacity,
    populate_estimated_cost,
    populate_net_consumption,
    usable_capacity,
)
from custom_components.hsem.utils.datetime_utils import as_tz
from custom_components.hsem.utils.logger import log_planner
from custom_components.hsem.utils.misc import (
    calculate_recommended_threshold,
    clamp_efficiency,
    resolve_cycle_cost,
)
from custom_components.hsem.utils.recommendations import Recommendations
from custom_components.hsem.utils.units import (
    hours_ahead,
    max_energy_per_slot_kwh,
    roundtrip_loss_pct,
)


def _schedule_slots(
    slots: list,
    inp: PlannerInput,
    now: datetime,
    current_kwh: float,
    usable_kwh: float,
    rt: float,
    effective_cycle_cost: float,
    warnings: list[str],
) -> tuple[float, float | None, float, float, list[str]]:
    """All charge/discharge scheduling passes.

    Args:
        effective_cycle_cost: Resolved per-kWh cycle cost used by all charge
            passes.  Computed as ``max(auto_calc, user_configured_margin)`` by
            the caller so heuristic and MILP paths use the same value.
    """
    mark_time_passed(slots, now)
    apply_discharge_schedules(slots, inp.battery_schedules, now)
    log_planner(
        "debug",
        "[core] _schedule_slots  pass=discharge_schedules  slots=%d",
        len(slots),
    )
    cd = clamp_efficiency(inp.battery_charge_efficiency_pct)
    rlp = roundtrip_loss_pct(
        inp.battery_charge_efficiency_pct,
        inp.battery_discharge_efficiency_pct,
    )
    mcphi = max_energy_per_slot_kwh(
        inp.battery_max_charge_power_w,
        inp.interval_minutes,
        efficiency_fraction=cd,
    )
    apply_charge_schedules(
        slots,
        inp.battery_schedules,
        now,
        mcphi,
        current_kwh=current_kwh,
        usable_kwh=usable_kwh,
        cycle_cost_per_kwh=effective_cycle_cost,
        recommended_threshold=rt,
    )
    apply_opportunistic_charge(
        slots,
        now,
        current_kwh,
        usable_kwh,
        mcphi,
        rt,
        cycle_cost_per_kwh=effective_cycle_cost,
    )
    apply_arbitrage_grid_charge(
        slots,
        inp.battery_schedules,
        now,
        current_kwh,
        usable_kwh,
        mcphi,
        conversion_loss_pct=rlp,
        cycle_cost_per_kwh=effective_cycle_cost,
        recommended_threshold=rt,
    )
    mcps = mcphi  # same formula — max charge energy per slot
    mdps: float | None = None
    if inp.battery_max_discharge_power_w is not None:
        mdps = max_energy_per_slot_kwh(
            inp.battery_max_discharge_power_w,
            inp.interval_minutes,
        )
    max_soc_kwh = usable_kwh
    populate_battery_capacity(slots, now, current_kwh, usable_kwh)
    rc = calculate_required_battery_until_solar(
        slots, now, usable_kwh, inp.excess_export_discharge_buffer_pct
    )
    log_planner(
        "debug",
        "[core] _schedule_slots  pass=after_scheduling  mcps=%.3f  mdps=%s  "
        "max_soc=%.3f  rc=%.3f",
        mcps,
        f"{mdps:.3f}" if mdps is not None else "∞",
        max_soc_kwh,
        rc,
    )
    if inp.excess_export_enabled:
        apply_excess_export(
            slots,
            now,
            current_kwh,
            rc,
            inp.excess_export_price_threshold,
            warnings,
            export_min_price=inp.export_min_price,
            recommended_threshold=rt,
        )
        log_planner(
            "debug",
            "[core] _schedule_slots  pass=excess_export  enabled=True",
        )
    else:
        log_planner(
            "debug",
            "[core] _schedule_slots  pass=excess_export  enabled=False  "
            "→ MILP no_export constraint active (battery will not export to grid)",
        )
    apply_optimization_strategy(
        slots,
        now,
        current_kwh,
        usable_kwh,
        rc,
        inp.months_winter,
        export_min_price=inp.export_min_price,
    )
    log_planner(
        "debug",
        "[core] _schedule_slots DONE  mcps=%.3f  mdps=%s  max_soc=%.3f  rc=%.3f  "
        "warnings=%d",
        mcps,
        f"{mdps:.3f}" if mdps is not None else "∞",
        max_soc_kwh,
        rc,
        len(warnings),
    )
    return mcps, mdps, max_soc_kwh, rc, warnings


def _select_candidate(
    slots: list,
    inp: PlannerInput,
    now: datetime,
    current_kwh: float,
    usable_kwh: float,
    mcps: float,
    mdps: float | None,
    max_soc_kwh: float,
    rppk: float | None,
    cw: CostWeights,
    sdh: float,
    rc: float,
    ev_configs: list[EVConfig] | None = None,
) -> tuple:
    """Generate and select best candidate plan."""
    candidates = generate_candidates(
        slots,
        inp,
        now,
        mcps,
        current_kwh=current_kwh,
        usable_kwh=usable_kwh,
        max_discharge_per_slot=mdps,
        replacement_price_per_kwh=rppk,
        ev_configs=ev_configs,
    )
    winner, rejected, hyst = select_best_candidate(
        candidates,
        now=now,
        current_kwh=current_kwh,
        usable_kwh=usable_kwh,
        max_soc_capacity_kwh=max_soc_kwh,
        max_charge_per_slot=mcps,
        max_discharge_per_slot=mdps,
        rated_kwh=inp.battery_rated_capacity_kwh,
        end_of_discharge_soc_pct=inp.battery_end_of_discharge_soc_pct,
        cost_weights=cw,
        slot_duration_hours=sdh,
        charge_efficiency_pct=inp.battery_charge_efficiency_pct,
        discharge_efficiency_pct=inp.battery_discharge_efficiency_pct,
        replacement_price_per_kwh=rppk,
        required_capacity=rc,
        months_winter=inp.months_winter,
        export_min_price=inp.export_min_price,
        hysteresis_enabled=inp.planner_hysteresis_enabled,
        hysteresis_absolute=inp.planner_hysteresis_absolute,
        hysteresis_percentage=inp.planner_hysteresis_percentage,
        previous_winner_name=inp.previous_winner_name,
        previous_winner_score=inp.previous_winner_score,
    )
    log_planner(
        "debug",
        "[core] _select_candidate DONE  candidates=%d  winner=%s  rejected=%d  hyst=%s",
        len(candidates),
        winner.name,
        len(rejected),
        f"applied={hyst.applied}" if hyst.applied else "inactive",
    )
    return candidates, winner, rejected, hyst


def run_planner(inp: PlannerInput) -> PlannerOutput:
    """Execute the HSEM planner and return a :class:`PlannerOutput`."""
    warnings: list[str] = []
    missing_inputs: list[str] = []
    now = _parse_now(inp.now_iso)
    log_planner(
        "debug",
        "==== HSEM PLANNER RUN START ==== now=%s interval=%dmin horizon=%dh",
        inp.now_iso,
        inp.interval_minutes,
        inp.interval_length_hours,
    )
    # Dynamic discharge floor (issue #600): when enabled and higher than the
    # configured minimum, use it as the effective discharge floor.  This
    # reduces usable capacity and current capacity above the floor, which
    # naturally limits export and preserves reserve energy.
    _effective_eod_soc = inp.battery_end_of_discharge_soc_pct
    if (
        inp.dynamic_discharge_floor_pct is not None
        and inp.dynamic_discharge_floor_pct > _effective_eod_soc
    ):
        _effective_eod_soc = inp.dynamic_discharge_floor_pct
        log_planner(
            "debug",
            "[core] Dynamic discharge floor active: %.1f%% (configured min: %.1f%%)",
            _effective_eod_soc,
            inp.battery_end_of_discharge_soc_pct,
        )
    usable_kwh, current_kwh = usable_capacity(
        inp.battery_rated_capacity_kwh,
        inp.battery_soc_pct,
        _effective_eod_soc,
        inp.battery_max_soc_pct,
    )
    if inp.battery_rated_capacity_kwh <= 0:
        warnings.append(
            "battery_rated_capacity_kwh is zero or negative; battery simulation disabled."
        )
        usable_kwh = 0.0
        current_kwh = 0.0
    ws = inp.weight_1d + inp.weight_3d + inp.weight_7d + inp.weight_14d
    if ws != 100:
        warnings.append(
            f"Consumption weights sum to {ws}, not 100. Results may not be meaningful."
        )
    tsi = build_time_series_index(inp, now)
    slots = build_slots(inp, now)
    if not slots:
        log_planner(
            "warning",
            "[core] run_planner ABORTED — no slots generated",
        )
        warnings.append(
            "No slots generated; check interval_minutes and interval_length_hours."
        )
        return PlannerOutput(missing_inputs=missing_inputs, warnings=warnings)
    # Step 1 — populate time-series data
    data_quality, warnings, missing_inputs = _populate_slots(
        slots, inp, tsi, warnings, missing_inputs
    )
    log_planner(
        "debug",
        "[core] run_planner  step=1_populate_slots COMPLETE  "
        "data_quality=horizon_has_tomorrow=%s,horizon_days=%d  "
        "warnings=%d  missing=%d",
        data_quality.horizon_has_tomorrow,
        data_quality.horizon_days,
        len(warnings),
        len(missing_inputs),
    )
    # Step 1b — inject live solar and consumption into the current slot
    _inject_live_data_into_current_slot(slots, inp, now)

    # Step 2 — EV planned load injection
    ev_cp: EVChargingPlan | None = None
    ev2_cp: EVChargingPlan | None = None
    combined_ev_raw = [0.0] * len(slots)
    combined_ev_inj = [0.0] * len(slots)
    populate_net_consumption(slots)
    sns = [max(-s.estimated_net_consumption_kwh, 0.0) for s in slots]
    ss = [s.start for s in slots]
    se = [s.end for s in slots]
    sp = [s.price.import_price for s in slots]
    if inp.ev_planned_load_enabled:
        ev_cp = _build_and_inject_for_ev(
            enabled=True,
            connected=inp.ev_planned_load_connected,
            smart=inp.ev_planned_load_smart_charging_enabled,
            soc=inp.ev_planned_load_current_soc_pct,
            target=inp.ev_planned_load_target_soc_pct,
            cap_kwh=inp.ev_planned_load_battery_capacity_kwh,
            pwr_kw=inp.ev_planned_load_charger_power_kw,
            eff=inp.ev_planned_load_charger_efficiency_pct,
            min_pwr_w=inp.ev_planned_load_charger_min_power_w,
            deadline=inp.ev_planned_load_deadline,
            base_includes=inp.ev_planned_load_base_load_includes_ev,
            allow_past_target=inp.ev_planned_allow_charge_past_target_soc,
            label="primary",
            now=now,
            slots=slots,
            slot_starts=ss,
            slot_ends=se,
            slot_prices=sp,
            slot_net_surplus=sns,
            combined_ev_raw_load=combined_ev_raw,
            combined_ev_injected_load=combined_ev_inj,
            warnings=warnings,
        )
    if inp.ev_second_planned_load_enabled:
        ev2_cp = _build_and_inject_for_ev(
            enabled=True,
            connected=inp.ev_second_planned_load_connected,
            smart=inp.ev_second_planned_load_smart_charging_enabled,
            soc=inp.ev_second_planned_load_current_soc_pct,
            target=inp.ev_second_planned_load_target_soc_pct,
            cap_kwh=inp.ev_second_planned_load_battery_capacity_kwh,
            pwr_kw=inp.ev_second_planned_load_charger_power_kw,
            eff=inp.ev_second_planned_load_charger_efficiency_pct,
            min_pwr_w=inp.ev_second_planned_load_charger_min_power_w,
            deadline=inp.ev_second_planned_load_deadline,
            base_includes=inp.ev_second_planned_load_base_load_includes_ev,
            allow_past_target=inp.ev_second_allow_charge_past_target_soc,
            label="second",
            now=now,
            slots=slots,
            slot_starts=ss,
            slot_ends=se,
            slot_prices=sp,
            slot_net_surplus=sns,
            combined_ev_raw_load=combined_ev_raw,
            combined_ev_injected_load=combined_ev_inj,
            warnings=warnings,
        )
    for i, s in enumerate(slots):
        s.ev_planned_load_kwh = combined_ev_inj[i]
        s.ev_accounted_load_kwh = round(combined_ev_raw[i] - combined_ev_inj[i], 3)
        s.ev_total_planned_load_kwh = round(combined_ev_raw[i], 3)

    # Compute per-slot EV charger target power (W) from the planner's
    # per-slot energy targets.  The EVChargingSlot.estimated_charged_kwh is
    # battery-side (DC) kWh delivered to the EV.  The AC power the charger
    # must draw is larger by 1/eff to account for charger/cable losses.
    _compute_ev_charger_power(slots, ss, ev_cp, inp.interval_minutes, now)
    _compute_ev_charger_power(slots, ss, ev2_cp, inp.interval_minutes, now, second=True)
    populate_net_consumption(slots)
    populate_estimated_cost(slots, export_min_price=inp.export_min_price)
    rt = calculate_recommended_threshold(
        purchase_price=inp.battery_purchase_price,
        expected_cycles=inp.battery_expected_cycles,
        usable_capacity=usable_kwh,
        capacity_loss_pct=inp.battery_capacity_loss_pct,
    )
    if rt > 0:
        warnings.append(
            f"Recommended price threshold: {rt:.4f} (depreciation + conversion loss)."
        )

    # Resolve the effective cycle cost once so the MILP, cost function, and
    # heuristic charge passes all use the same value.
    # Uses resolve_cycle_cost() — the single source of truth.
    effective_cycle_cost = resolve_cycle_cost(
        purchase_price=inp.battery_purchase_price,
        usable_kwh=usable_kwh,
        expected_cycles=inp.battery_expected_cycles,
        capacity_loss_pct=inp.battery_capacity_loss_pct,
        user_margin=inp.battery_cycle_cost_per_kwh,
    )

    # Step 3 — charge/discharge scheduling
    log_planner(
        "debug",
        "[core] run_planner  step=3_schedule_slots START  "
        "current=%.3f  usable=%.3f  rt=%.4f  cycle_cost=%.6f",
        current_kwh,
        usable_kwh,
        rt,
        effective_cycle_cost,
    )
    mcps, mdps, max_soc_kwh, rc, warnings = _schedule_slots(
        slots,
        inp,
        now,
        current_kwh,
        usable_kwh,
        rt,
        effective_cycle_cost,
        warnings,
    )
    log_planner(
        "debug",
        "[core] run_planner  step=3_schedule_slots COMPLETE",
    )
    # Step 4 — candidate plan generation and selection
    cw = CostWeights(
        min_soc_pct=_effective_eod_soc,
        max_soc_pct=inp.battery_max_soc_pct,
        cycle_cost_per_kwh=effective_cycle_cost,
        battery_purchase_price=inp.battery_purchase_price,
        battery_rated_capacity_kwh=inp.battery_rated_capacity_kwh,
        battery_expected_cycles=inp.battery_expected_cycles,
        battery_capacity_loss_pct=inp.battery_capacity_loss_pct,
        charge_efficiency_pct=inp.battery_charge_efficiency_pct,
        discharge_efficiency_pct=inp.battery_discharge_efficiency_pct,
        export_min_price=inp.export_min_price,
        time_discount_rate=inp.time_discount_rate,
    )
    sdh = inp.interval_minutes / 60.0
    import math

    top_n = 4
    if mdps is not None and mdps > 1e-9:
        top_n = math.ceil(usable_kwh / mdps)
    rppk = replacement_price_from_next_discharge(
        slots, now, top_n=top_n, interval_minutes=inp.interval_minutes
    )
    log_planner(
        "debug",
        "[core] run_planner  step=4_candidate_selection START  top_n=%d  rppk=%s",
        top_n,
        f"{rppk:.6f}" if rppk is not None else "None",
    )
    # Note: concentrate_discharge_on_expensive_slots() is now applied per-candidate
    # in the selector before scoring, so we don't run it on the baseline here.

    # Build EV configs for MILP co-optimisation (when EVs are active)
    ev_configs = _build_ev_configs_for_milp(inp, slots, now)
    candidates, winner, candidate_rejected, hysteresis_result = _select_candidate(
        slots,
        inp,
        now,
        current_kwh,
        usable_kwh,
        mcps,
        mdps,
        max_soc_kwh,
        rppk,
        cw,
        sdh,
        rc,
        ev_configs=ev_configs,
    )
    # Surface MILP penalty violations in warnings if the winner used penalties
    if (
        winner.name == CANDIDATE_MILP
        and winner.diagnostics is not None
        and winner.diagnostics.get("has_violations", False)
    ):
        diag = winner.diagnostics
        total = diag.get("total_violation_kwh", 0.0)
        fuse_total = diag.get("total_fuse_violation_kwh", 0.0)
        parts: list[str] = []
        if total > 1e-9:
            parts.append(f"SoC penalty={total:.4f} kWh")
        if fuse_total > 1e-9:
            parts.append(f"fuse excess={fuse_total:.4f} kWh")
        if parts:
            warnings.append(
                f"MILP: Penalty violations detected ({', '.join(parts)}). "
                f"The plan may have been forced due to out-of-bounds initial SoC "
                f"or main fuse limit."
            )
    # Step 5 — finalize plan from winner
    # Note: apply_optimization_strategy() and simulate_soc() are now applied
    # in the selector before scoring, so the winner's slots are already fully
    # populated. We do NOT re-run simulate_soc() here to avoid double-simulation
    # drift and to ensure the final score matches the selector's score.
    slots = winner.slots

    # Post-hoc main fuse check — runs regardless of which candidate won.
    # If any slot exceeds the fuse rating, throttle EV charger power and
    # battery charge energy to bring total grid import within the limit.
    if inp.main_fuse_amps is not None and inp.main_fuse_amps > 0:
        slot_hours = inp.interval_minutes / 60.0
        max_per_slot_kwh = (
            inp.main_fuse_amps
            * 230.0
            * float(inp.main_fuse_phases)
            / 1000.0
            * slot_hours
        )

        for s in slots:
            if s.grid_import_kwh <= max_per_slot_kwh + 1e-9:
                continue

            excess_kwh = s.grid_import_kwh - max_per_slot_kwh
            excess_power_w = round((excess_kwh / slot_hours) * 1000.0)

            # Step 1 — throttle EV charger power first.
            for attr in (
                "ev_charger_calculated_power",
                "ev_second_charger_calculated_power",
            ):
                ev_w = round(getattr(s, attr))
                if ev_w > 0 and excess_power_w > 0:
                    cut = min(ev_w, excess_power_w)
                    setattr(s, attr, ev_w - cut)
                    excess_power_w -= cut

            # Step 2 — throttle battery charging with remaining excess.
            if excess_power_w > 0 and s.batteries_charged_kwh > 1e-9:
                chg_eff = clamp_efficiency(inp.battery_charge_efficiency_pct)
                excess_ac_kwh = (excess_power_w / 1000.0) * slot_hours
                dc_cut = excess_ac_kwh * chg_eff
                s.batteries_charged_kwh = round(
                    max(0.0, s.batteries_charged_kwh - dc_cut), 3
                )

                # If we zeroed battery charging, clear the recommendation
                # so the applier does not enable TOU charge for this slot.
                if s.batteries_charged_kwh < 1e-6:
                    s.recommendation = None

                excess_power_w = 0

            if excess_power_w > 0:
                log_planner(
                    "warning",
                    "[core] Main fuse violation in slot %s: "
                    "grid_import=%.3f kWh  limit=%.3f kWh  "
                    "unresolved_excess=%d W",
                    s.start.isoformat(),
                    s.grid_import_kwh,
                    max_per_slot_kwh,
                    excess_power_w,
                )
                warnings.append(
                    f"Main fuse ({inp.main_fuse_amps:.0f} A) exceeded in slot "
                    f"{s.start.isoformat()}: "
                    f"{excess_kwh:.3f} kWh above limit "
                    f"(EV/battery throttling insufficient)."
                )

    # Re-apply per-EV minimum-power floor after MILP and fuse throttling.
    #
    # Both _compute_ev_charger_power() (for non-MILP candidates) and the
    # MILP's own EV power computation already set ev_charger_calculated_power
    # and ev_second_charger_calculated_power correctly per-EV, and the
    # main-fuse throttling block above correctly adjusts them per-field.
    #
    # This block checks whether any power field fell below its OWN charger's
    # minimum operating power due to fuse throttling, and zeroes it if so.
    # The energy contribution is reverse-engineered from the power field and
    # subtracted from the combined slot energy totals so net consumption and
    # cost remain consistent.
    #
    # IMPORTANT: This block MUST NOT recompute per-EV power from the combined
    # ev_planned_load_kwh / ev_accounted_load_kwh totals.  Those fields are
    # the SUM across both EVs; deriving a per-EV power from them would
    # corrupt the per-EV output field with the combined total.
    _slot_hours = inp.interval_minutes / 60.0
    _ev_power_checks: list[tuple[str, float, bool]] = []
    if inp.ev_planned_load_enabled:
        _ev_power_checks.append(
            (
                "ev_charger_calculated_power",
                inp.ev_planned_load_charger_min_power_w,
                inp.ev_planned_load_base_load_includes_ev,
            )
        )
    if inp.ev_second_planned_load_enabled:
        _ev_power_checks.append(
            (
                "ev_second_charger_calculated_power",
                inp.ev_second_planned_load_charger_min_power_w,
                inp.ev_second_planned_load_base_load_includes_ev,
            )
        )

    for s in slots:
        for attr, min_pwr_w, base_includes in _ev_power_checks:
            ev_w = round(getattr(s, attr))
            if ev_w <= 0:
                continue
            if min_pwr_w > 1e-9 and ev_w < min_pwr_w:
                # Below this EV's own minimum — charger won't start.
                # Reverse-engineer the energy contribution from the
                # power field to subtract from combined slot totals.
                s_end_tz = as_tz(s.end, now.tzinfo)
                if as_tz(s.start, now.tzinfo) <= now < s_end_tz:
                    remaining_h = max(
                        hours_ahead(now, s_end_tz),
                        1.0 / 3600.0,
                    )
                    ev_energy = round((ev_w / 1000.0) * remaining_h, 3)
                else:
                    ev_energy = round((ev_w / 1000.0) * _slot_hours, 3)

                log_planner(
                    "debug",
                    "[core] EV power below %s minimum (%d < %d), "
                    "zeroing field and subtracting %.3f kWh",
                    attr,
                    ev_w,
                    min_pwr_w,
                    ev_energy,
                )

                # Zero this EV's power field only (not the other EV's).
                setattr(s, attr, 0)

                # Remove this EV's energy contribution from the combined
                # slot energy fields.  The energy bucket depends on whether
                # base load already includes EV consumption.
                if base_includes:
                    s.ev_accounted_load_kwh = round(
                        max(0.0, s.ev_accounted_load_kwh - ev_energy), 3
                    )
                else:
                    s.ev_planned_load_kwh = round(
                        max(0.0, s.ev_planned_load_kwh - ev_energy), 3
                    )
                s.ev_total_planned_load_kwh = round(
                    s.ev_planned_load_kwh + s.ev_accounted_load_kwh, 3
                )

                # Recompute net consumption and cost with the reduced EV load.
                s.estimated_net_consumption_kwh = (
                    s.avg_house_consumption_kwh
                    + s.ev_planned_load_kwh
                    - s.solcast_pv_estimate_kwh
                )
                net = s.estimated_net_consumption_kwh
                if net > 0:
                    s.estimated_cost_currency = round(net * s.price.import_price, 4)
                else:
                    s.estimated_cost_currency = round(net * s.price.export_price, 4)

    # Spec (planner-spec.md, Layer 2): slots with ev_total_planned_load_kwh > 0
    # are relabelled ev_smart_charging UNLESS the recommendation is one of the
    # protected set below.  batteries_charge_solar and batteries_wait_mode are
    # intentionally NOT protected — they are overridden so dashboards reflect
    # the EV activity rather than a solar-charge label during an EV session.
    _EV_KEEP = frozenset(
        {
            Recommendations.BatteriesChargeGrid.value,
            Recommendations.ForceBatteriesDischarge.value,
            Recommendations.ForceExport.value,
            Recommendations.TimePassed.value,
            Recommendations.MissingInputEntities.value,
        }
    )
    for s in slots:
        if abs(s.ev_total_planned_load_kwh) > 1e-9 and s.recommendation not in _EV_KEEP:
            s.recommendation = Recommendations.EVSmartCharging.value
    cur_rec: str | None = None
    for s in slots:
        if as_tz(s.start, now.tzinfo) <= now < as_tz(s.end, now.tzinfo):
            cur_rec = s.recommendation
            break
    fut = [s for s in slots if as_tz(s.end, now.tzinfo) > now]
    bsoc_end = fut[-1].estimated_battery_soc_pct if fut else 0.0
    cw_out, dw_out = _derive_windows(slots)
    expl = _build_explanation(inp, slots, bsoc_end, now)
    expl.winner_name = winner.name
    expl.hysteresis_active = hysteresis_result.applied
    expl.hysteresis_reason = hysteresis_result.reason
    expl.previous_plan_name = hysteresis_result.previous_plan_name
    pc = score_plan(
        slots,
        cw,
        slot_duration_hours=sdh,
        now=now,
        initial_battery_kwh=current_kwh,
        replacement_price_per_kwh=rppk,
    )
    for rp in candidate_rejected:
        expl.rejected_plans.append(rp)

    log_planner(
        "debug",
        "[core] run_planner DONE  winner=%s  cost=%.4f  score=%.4f  "
        "cur_rec=%s  bsoc_end=%.1f%%  rc=%.3f  warnings=%d  missing=%d  "
        "cw=%d  dw=%d",
        winner.name,
        pc.total_cost,
        pc.score,
        cur_rec if cur_rec is not None else "(none)",
        bsoc_end,
        rc,
        len(warnings),
        len(missing_inputs),
        len(cw_out),
        len(dw_out),
    )

    # When the MILP wins, rebuild the EV charging plans from the MILP's
    # slot decisions so the sensor reflects what the system *actually*
    # plans to do, not the EV planner's pre-MILP estimate.
    if winner.name == CANDIDATE_MILP:
        if ev_cp is not None:
            ev_cp = rebuild_ev_plan_from_slots(
                ev_cp,
                slots,
                now,
                charger_efficiency_pct=inp.ev_planned_load_charger_efficiency_pct,
                is_second=False,
            )
        if ev2_cp is not None:
            ev2_cp = rebuild_ev_plan_from_slots(
                ev2_cp,
                slots,
                now,
                charger_efficiency_pct=inp.ev_second_planned_load_charger_efficiency_pct,
                is_second=True,
            )

    return PlannerOutput(
        slots=slots,
        charge_windows=cw_out,
        discharge_windows=dw_out,
        current_recommendation=cur_rec,
        battery_soc_at_end=bsoc_end,
        required_capacity_kwh=rc,
        missing_inputs=missing_inputs,
        warnings=warnings,
        time_series_index=tsi,
        data_quality=data_quality,
        explanation=expl,
        plan_cost=pc,
        candidates=candidates,
        winner_name=winner.name,
        ev_charging_plan=ev_cp,
        ev_second_charging_plan=ev2_cp,
    )
