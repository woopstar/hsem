"""Core planning flow for the HSEM planner.

Orchestrates the planning pipeline and returns a :class:`PlannerOutput`.

**No Home Assistant types are imported here.**  Makes the engine
directly testable with plain ``pytest`` without a running HA instance.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from custom_components.hsem.models.data_quality import DataQuality
from custom_components.hsem.models.ev_config import EVConfig
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.models.time_series import TimeSeriesIndex
from custom_components.hsem.planner.candidate_generator import (
    CANDIDATE_MILP,
    generate_candidates,
)
from custom_components.hsem.planner.candidate_selector import (
    replacement_price_from_next_discharge,
    select_best_candidate,
)
from custom_components.hsem.planner.charge_scheduler import (
    apply_arbitrage_grid_charge,
    apply_charge_schedules,
    apply_opportunistic_charge,
)
from custom_components.hsem.planner.cost_function import CostWeights, score_plan
from custom_components.hsem.planner.discharge_scheduler import (
    apply_discharge_schedules,
    apply_excess_export,
    apply_optimization_strategy,
    calculate_required_battery_until_solar,
    concentrate_discharge_on_expensive_slots,
)
from custom_components.hsem.planner.engine_explanation import (
    _build_explanation,
    _derive_windows,
)
from custom_components.hsem.planner.ev_planner import (
    EVChargingPlan,
    EVPlannerInput,
    apply_ev_planned_load_to_slots,
    build_ev_charging_plan,
    rebuild_ev_plan_from_slots,
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
from custom_components.hsem.utils.misc import (
    calculate_recommended_threshold,
    clamp_efficiency,
)
from custom_components.hsem.utils.recommendations import Recommendations


def _parse_now(now_iso: str) -> datetime:
    """Parse a timezone-aware ISO-8601 string."""
    dt = datetime.fromisoformat(now_iso)
    if dt.tzinfo is None:
        raise ValueError(f"now_iso must be timezone-aware, got: {now_iso!r}")
    return dt


def _populate_slots(
    slots: list,
    inp: PlannerInput,
    tsi: TimeSeriesIndex,
    warnings: list[str],
    missing_inputs: list[str],
) -> tuple[DataQuality, list[str], list[str]]:
    """Populate price/PV/consumption data, data-quality diagnostics, confidence decay."""
    log_planner(
        "debug",
        "[core] _populate_slots  slots=%d  interval=%dmin  horizon=%dh",
        len(slots),
        inp.interval_minutes,
        inp.interval_length_hours,
    )
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
    for hour in sorted(tsi.missing_hours()):
        missing_inputs.append(f"hour_{hour:02d}")
    horizon_has_tomorrow = tsi.has_tomorrow_slots()
    horizon_days = tsi.horizon_days
    dq = DataQuality(
        tomorrow_price_missing_hours=sorted(tsi.missing_future_day_price_hours(1)),
        tomorrow_pv_missing_hours=sorted(tsi.missing_future_day_pv_hours(1)),
        day2_price_missing_hours=sorted(tsi.missing_future_day_price_hours(2)),
        day2_pv_missing_hours=sorted(tsi.missing_future_day_pv_hours(2)),
        today_price_missing_hours=sorted(
            {
                m.hour
                for k in tsi.missing_price_slots
                if k.day_offset == 0
                for m in tsi.slots
                if m.key == k
            }
        ),
        today_pv_missing_hours=sorted(
            {
                m.hour
                for k in tsi.missing_pv_slots
                if k.day_offset == 0
                for m in tsi.slots
                if m.key == k
            }
        ),
        horizon_has_tomorrow=horizon_has_tomorrow,
        horizon_days=horizon_days,
    )
    for tm_label, tm_list in [
        ("tomorrow_price_missing_hours", dq.tomorrow_price_missing_hours),
        ("tomorrow_pv_missing_hours", dq.tomorrow_pv_missing_hours),
        ("day2_price_missing_hours", dq.day2_price_missing_hours),
        ("day2_pv_missing_hours", dq.day2_pv_missing_hours),
    ]:
        if tm_list:
            hs = ",".join(f"{h:02d}" for h in tm_list)
            missing_inputs.append(f"{tm_label}:{hs}")
            labels = {
                "tomorrow_price_missing_hours": "price",
                "tomorrow_pv_missing_hours": "PV",
                "day2_price_missing_hours": "price",
                "day2_pv_missing_hours": "PV",
            }
            warnings.append(
                f"{'Day+2' if tm_label.startswith('day2') else 'Tomorrow'} {labels[tm_label]} data missing for {len(tm_list)} hour(s): {hs}."
            )
    _DECAY_BY_DAY: dict[int, float] = {0: 1.00, 1: 0.90, 2: 0.80}
    if horizon_days > 1:
        for slot in slots:
            si = tsi.slot_index_for(slot.start)
            if si is None:
                continue
            do = tsi.slots[si].key.day_offset
            if do > 0:
                slot.solcast_pv_estimate_kwh = round(
                    slot.solcast_pv_estimate_kwh * _DECAY_BY_DAY.get(do, 0.80), 3
                )
        if horizon_days >= 2:
            warnings.append(
                f"Multi-day horizon ({horizon_days} day(s)): confidence decay applied to PV estimates."
            )
    log_planner(
        "debug",
        "[core] _populate_slots DONE  warnings=%d  missing=%d  horizon_days=%d  "
        "has_tomorrow=%s",
        len(warnings),
        len(missing_inputs),
        horizon_days,
        horizon_has_tomorrow,
    )

    return dq, warnings, missing_inputs


def _schedule_slots(
    slots: list,
    inp: PlannerInput,
    now: datetime,
    current_kwh: float,
    usable_kwh: float,
    rt: float,
    warnings: list[str],
) -> tuple[float, float | None, float, float, list[str]]:
    """All charge/discharge scheduling passes."""
    mark_time_passed(slots, now)
    apply_discharge_schedules(slots, inp.battery_schedules, now)
    log_planner(
        "debug",
        "[core] _schedule_slots  pass=discharge_schedules  slots=%d",
        len(slots),
    )
    cd = inp.battery_charge_efficiency_pct / 100.0
    dd = inp.battery_discharge_efficiency_pct / 100.0
    rlp = (1.0 - cd * dd) * 100.0
    mcphi = (inp.battery_max_charge_power_w / 1000 * cd) / (60 / inp.interval_minutes)
    # `rt` is depreciation-only — conversion losses are priced per-slot
    # by the MILP objective, the cost function, and the arbitrage-grid-charge
    # pass (`conversion_loss_pct`).  No need to subtract a fixed add-on.
    apply_charge_schedules(
        slots,
        inp.battery_schedules,
        now,
        mcphi,
        current_kwh=current_kwh,
        usable_kwh=usable_kwh,
        cycle_cost_per_kwh=inp.battery_cycle_cost_per_kwh,
        recommended_threshold=rt,
    )
    apply_opportunistic_charge(
        slots,
        now,
        current_kwh,
        usable_kwh,
        mcphi,
        rt,
        cycle_cost_per_kwh=inp.battery_cycle_cost_per_kwh,
    )
    apply_arbitrage_grid_charge(
        slots,
        inp.battery_schedules,
        now,
        current_kwh,
        usable_kwh,
        mcphi,
        conversion_loss_pct=rlp,
        cycle_cost_per_kwh=inp.battery_cycle_cost_per_kwh,
        recommended_threshold=rt,
    )
    mcps = (inp.battery_max_charge_power_w / 1000 * cd) / (60 / inp.interval_minutes)
    mdps: float | None = None
    if inp.battery_max_discharge_power_w is not None:
        mdps = (inp.battery_max_discharge_power_w / 1000) / (60 / inp.interval_minutes)
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


def _compute_ev_charger_power(
    slots: list,
    slot_starts: list[datetime],
    ev_plan: EVChargingPlan | None,
    interval_minutes: int,
    now: datetime,
    *,
    second: bool = False,
) -> None:
    """Compute per-slot EV charger target power (W) and write to slots.

    ``EVChargingSlot.ac_load_kwh`` is the AC-side energy the charger draws
    from the grid/PV.  The target power is::

        AC power (W) = (ac_load_kwh / slot_hours) × 1000

    For the **current** (partially elapsed) slot the divisor is the
    remaining slot duration, not the full slot width, because the EV
    planner already scales ``ac_load_kwh`` to the remaining minutes.
    Using the full slot width would understate the required charge power.

    When the plan is ``None`` or empty the field stays at the default 0.0.

    Args:
        slots: Mutable planner slot list to update in place.
        slot_starts: Slot start datetimes (same length as *slots*).
        ev_plan: EV charging plan (may be ``None``).
        interval_minutes: Slot width in minutes.
        now: Current time (timezone-aware), used to detect the current slot.
        second: If ``True``, write to ``ev_second_charger_calculated_power``;
            otherwise write to ``ev_charger_calculated_power``.
    """
    if ev_plan is None or not ev_plan.charging_slots:
        return

    # Build a lookup from UTC key → slot index.
    from custom_components.hsem.utils.datetime_utils import utc_key

    slot_map = {utc_key(s): i for i, s in enumerate(slot_starts)}
    full_hours = interval_minutes / 60.0

    for ev_slot in ev_plan.charging_slots:
        idx = slot_map.get(utc_key(ev_slot.start))
        if idx is None:
            continue
        if ev_slot.ac_load_kwh < 1e-9:
            continue

        # For the current (partially elapsed) slot, the EV planner has
        # already scaled ``ac_load_kwh`` to the *remaining* minutes.
        # Divide by remaining hours to get the correct target power.
        # For future slots the full slot width is used.
        slot_end = slots[idx].end
        if slots[idx].start <= now < slot_end:
            remaining_min = max((slot_end - now).total_seconds() / 60.0, 0.0167)
            slot_hours = remaining_min / 60.0
        else:
            slot_hours = full_hours

        ac_power_w = round((ev_slot.ac_load_kwh / slot_hours) * 1000)

        # Cap at the charger's rated AC power — the EV planner may allocate
        # a full slot's worth of energy to a slot with only a few minutes
        # remaining.  The charger physically cannot exceed its nameplate.
        if ev_plan.charger_power_kw > 1e-9:
            max_ac_power_w = round(ev_plan.charger_power_kw * 1000)
            ac_power_w = min(ac_power_w, max_ac_power_w)

        # Floor at the charger's minimum operating power — if the target
        # power is below the minimum the charger needs to start, it will
        # never deliver any energy.  Zero out the field so the applier
        # does not attempt to throttle the charger below its minimum.
        if (
            ev_plan.charger_min_power_w > 1e-9
            and ac_power_w < ev_plan.charger_min_power_w
        ):
            ac_power_w = 0

        attr = (
            "ev_second_charger_calculated_power"
            if second
            else "ev_charger_calculated_power"
        )
        setattr(slots[idx], attr, ac_power_w)


def _build_and_inject_for_ev(
    enabled: bool,
    connected: bool,
    smart: bool,
    soc: float,
    target: float,
    cap_kwh: float,
    pwr_kw: float,
    eff: float,
    min_pwr_w: float,
    deadline: datetime | None,
    base_includes: bool,
    allow_past_target: bool,
    label: str,
    now: datetime,
    slots: list,
    slot_starts: list,
    slot_ends: list,
    slot_prices: list,
    slot_net_surplus: list[float],
    combined_ev_raw_load: list[float],
    combined_ev_injected_load: list[float],
    warnings: list[str],
    predicted_battery_kwh: list[float],
    usable_battery_kwh: float,
    live_net_consumption_w: float,
) -> EVChargingPlan | None:
    """Build an EV charging plan and accumulate its loads."""
    if not enabled:
        return None
    log_planner(
        "debug",
        "[core] _build_and_inject_for_ev  label=%s  connected=%s  smart=%s  "
        "soc=%.1f%%  target=%.1f%%  cap=%.2f  pwr=%.2f  eff=%.1f%%  min_pwr=%.0fW",
        label,
        connected,
        smart,
        soc,
        target,
        cap_kwh,
        pwr_kw,
        eff,
        min_pwr_w,
    )
    ev_inp = EVPlannerInput(
        enabled=enabled,
        ev_connected=connected,
        smart_charging_enabled=smart,
        current_soc_pct=soc,
        target_soc_pct=target,
        battery_capacity_kwh=cap_kwh,
        charger_power_kw=pwr_kw,
        charger_efficiency_pct=eff,
        charger_min_power_w=min_pwr_w,
        deadline=deadline,
        base_load_includes_ev=base_includes,
        allow_charge_past_target_soc=allow_past_target,
        slot_predicted_battery_kwh=predicted_battery_kwh,
        usable_battery_kwh=usable_battery_kwh,
        live_net_consumption_w=live_net_consumption_w,
        now=now,
    )
    plan = build_ev_charging_plan(
        ev_inp,
        slots_start=slot_starts,
        slots_end=slot_ends,
        slot_net_surplus_kwh=slot_net_surplus,
        slot_import_price=slot_prices,
    )
    raw = [0.0] * len(slots)
    apply_ev_planned_load_to_slots(
        slot_starts=slot_starts,
        slot_ev_planned_load_kwh=raw,
        ev_plan=plan,
        base_load_includes_ev=False,
    )
    for i in range(len(slots)):
        combined_ev_raw_load[i] += raw[i]
    inj = [0.0] * len(slots)
    apply_ev_planned_load_to_slots(
        slot_starts=slot_starts,
        slot_ev_planned_load_kwh=inj,
        ev_plan=plan,
        base_load_includes_ev=base_includes,
    )
    for i in range(len(slots)):
        combined_ev_injected_load[i] += inj[i]
    if plan.state not in ("not_connected", "smart_charging_disabled", "fully_charged"):
        warnings.append(
            f"EV planned load ({label}): state={plan.state}, total_kwh_needed={plan.total_kwh_needed:.2f}, charging_slots={len(plan.charging_slots)}, base_load_includes_ev={base_includes}."
        )
    log_planner(
        "debug",
        "[core] _build_and_inject_for_ev DONE  label=%s  state=%s  slots=%d  "
        "total_kwh=%.3f",
        label,
        plan.state,
        len(plan.charging_slots),
        plan.total_kwh_needed,
    )
    return plan


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


def _build_ev_configs_for_milp(
    inp: PlannerInput,
    slots: list,
    now: datetime,
) -> list[EVConfig] | None:
    """Build EVConfig list for the MILP from PlannerInput EV fields.

    Maps the user-configured deadline (clamped by the one-midnight-crossing
    horizon cap) to an LP slot index.  Returns ``None`` when no EVs are
    active or neither EV has sufficient config to be optimised.
    """

    def _effective_deadline_dt(user_deadline: datetime | None) -> datetime:
        """Replicate ev_planner._effective_deadline without HA import."""
        horizon_cap = (now + timedelta(days=2)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        if user_deadline is None:
            return horizon_cap
        return min(user_deadline, horizon_cap)

    configs: list[EVConfig] = []
    future_slots = [i for i, s in enumerate(slots) if as_tz(s.end, now.tzinfo) > now]
    if not future_slots:
        return None

    # Build config for each EV slot pair (primary, secondary)
    ev_sources: list[
        tuple[
            bool,
            bool,
            bool,
            float,
            float,
            float,
            float,
            float,
            float,
            datetime | None,
            bool,
            bool,
        ]
    ] = [
        (
            inp.ev_planned_load_enabled,
            inp.ev_planned_load_connected,
            inp.ev_planned_load_smart_charging_enabled,
            inp.ev_planned_load_current_soc_pct,
            inp.ev_planned_load_target_soc_pct,
            inp.ev_planned_load_battery_capacity_kwh,
            inp.ev_planned_load_charger_power_kw,
            inp.ev_planned_load_charger_efficiency_pct,
            inp.ev_planned_load_charger_min_power_w,
            inp.ev_planned_load_deadline,
            inp.ev_planned_load_base_load_includes_ev,
            inp.ev_planned_allow_charge_past_target_soc,
        ),
        (
            inp.ev_second_planned_load_enabled,
            inp.ev_second_planned_load_connected,
            inp.ev_second_planned_load_smart_charging_enabled,
            inp.ev_second_planned_load_current_soc_pct,
            inp.ev_second_planned_load_target_soc_pct,
            inp.ev_second_planned_load_battery_capacity_kwh,
            inp.ev_second_planned_load_charger_power_kw,
            inp.ev_second_planned_load_charger_efficiency_pct,
            inp.ev_second_planned_load_charger_min_power_w,
            inp.ev_second_planned_load_deadline,
            inp.ev_second_planned_load_base_load_includes_ev,
            inp.ev_second_allow_charge_past_target_soc,
        ),
    ]
    for (
        enabled,
        connected,
        smart,
        soc_pct,
        target_pct,
        cap,
        pwr,
        eff_pct,
        min_pwr_w,
        deadline,
        base_includes,
        allow_past_target,
    ) in ev_sources:
        if not enabled:
            continue
        if not connected or not smart:
            continue
        if cap <= 1e-9 or pwr <= 1e-9:
            continue
        initial_kwh = (soc_pct / 100.0) * cap
        target_kwh = (target_pct / 100.0) * cap

        # When the EV is already at or above its target SoC, normally we
        # skip it — there is no energy deficit to meet.  But when
        # allow_charge_past_target_soc is enabled and the EV is not yet
        # at 100 %, the MILP should still include the EV so it can
        # allocate surplus PV that would otherwise be curtailed or
        # exported at low/negative prices.  In this mode the deadline
        # constraint is suppressed (deadline_slot=None) so the MILP
        # never imports from grid to meet a target that is already
        # satisfied — it only charges from free/cheap surplus.
        at_or_above_target = target_kwh <= initial_kwh + 1e-9
        deadline_slot: int | None = None
        charge_past_target = False
        if at_or_above_target:
            if not allow_past_target or soc_pct >= 100:
                continue  # fully charged or past-target not allowed
            # Charge-past-target mode: allow up to 100 %, no deadline pressure.
            target_kwh = cap
            charge_past_target = True
        else:
            # Normal mode: map deadline to LP slot index.
            eff_deadline = _effective_deadline_dt(deadline)
            for lp_t, slot_i in enumerate(future_slots):
                s = slots[slot_i]
                if as_tz(s.end, now.tzinfo) <= eff_deadline:
                    deadline_slot = lp_t
                else:
                    break
            if deadline_slot is None:
                # No slot before deadline
                continue
            charge_past_target = False

        eff = max(eff_pct, 1.0) / 100.0
        slot_hours = inp.interval_minutes / 60.0
        max_dc = pwr * slot_hours * eff  # DC-side kWh per slot

        configs.append(
            EVConfig(
                enabled=True,
                initial_soc_kwh=round(initial_kwh, 3),
                target_kwh=round(target_kwh, 3),
                capacity_kwh=round(cap, 3),
                max_charge_per_slot=round(max_dc, 4),
                charger_efficiency=round(eff, 4),
                charger_min_power_w=round(min_pwr_w, 1),
                deadline_slot=deadline_slot,
                base_load_includes_ev=base_includes,
                charge_past_target=charge_past_target,
            )
        )

    return configs if configs else None


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
    # Step 2 — EV planned load injection
    ev_cp: EVChargingPlan | None = None
    ev2_cp: EVChargingPlan | None = None
    combined_ev_raw = [0.0] * len(slots)
    combined_ev_inj = [0.0] * len(slots)
    populate_net_consumption(slots)
    sns = [max(-s.estimated_net_consumption_kwh, 0.0) for s in slots]
    # Predicted battery energy (kWh above floor) at the START of each slot,
    # assuming no EV charging and no grid charging.  Used to gate Pass 3
    # in the EV planner: the EV only charges past target when the battery
    # is already full and the surplus would otherwise be stranded.
    predicted_battery_kwh: list[float] = []
    cumulative = current_kwh
    for s in slots:
        predicted_battery_kwh.append(cumulative)
        net = s.estimated_net_consumption_kwh
        # net positive → battery must cover deficit
        # net negative → surplus charges battery (up to usable_kwh)
        cumulative = max(0.0, min(usable_kwh, cumulative - net))
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
            predicted_battery_kwh=predicted_battery_kwh,
            usable_battery_kwh=usable_kwh,
            live_net_consumption_w=inp.live_net_consumption_w,
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
            predicted_battery_kwh=predicted_battery_kwh,
            usable_battery_kwh=usable_kwh,
            live_net_consumption_w=inp.live_net_consumption_w,
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
    # Step 3 — charge/discharge scheduling
    log_planner(
        "debug",
        "[core] run_planner  step=3_schedule_slots START  "
        "current=%.3f  usable=%.3f  rt=%.4f",
        current_kwh,
        usable_kwh,
        rt,
    )
    mcps, mdps, max_soc_kwh, rc, warnings = _schedule_slots(
        slots, inp, now, current_kwh, usable_kwh, rt, warnings
    )
    log_planner(
        "debug",
        "[core] run_planner  step=3_schedule_slots COMPLETE",
    )
    # Step 4 — candidate plan generation and selection
    cw = CostWeights(
        min_soc_pct=inp.battery_end_of_discharge_soc_pct,
        max_soc_pct=inp.battery_max_soc_pct,
        battery_purchase_price=inp.battery_purchase_price,
        battery_rated_capacity_kwh=inp.battery_rated_capacity_kwh,
        battery_expected_cycles=inp.battery_expected_cycles,
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
    concentrate_discharge_on_expensive_slots(
        slots,
        now,
        current_kwh,
        usable_kwh,
        mdps,
        discharge_efficiency_pct=inp.battery_discharge_efficiency_pct,
    )
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
    slots = winner.slots
    apply_optimization_strategy(
        slots,
        now,
        current_kwh,
        usable_kwh,
        rc,
        inp.months_winter,
        export_min_price=inp.export_min_price,
    )
    simulate_soc(
        slots,
        now,
        current_kwh,
        usable_kwh,
        max_soc_kwh,
        mcps,
        mdps,
        rated_kwh=inp.battery_rated_capacity_kwh,
        end_of_discharge_soc_pct=inp.battery_end_of_discharge_soc_pct,
        charge_efficiency_pct=inp.battery_charge_efficiency_pct,
        discharge_efficiency_pct=inp.battery_discharge_efficiency_pct,
    )

    # Post-hoc main fuse check — runs regardless of which candidate won.
    # If any slot exceeds the fuse rating, throttle EV charger power and
    # battery charge energy to bring total grid import within the limit.
    if inp.main_fuse_amps is not None and inp.main_fuse_amps > 0:
        slot_hours = inp.interval_minutes / 60.0
        max_per_slot_kwh = inp.main_fuse_amps * 230.0 * 3.0 / 1000.0 * slot_hours

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

    # Recompute ev_charger_calculated_power from the actual per-slot EV
    # load after the MILP (or baseline) has finalized the slots.  This
    # ensures the power field is always consistent with the load, even
    # when the MILP didn't write it (e.g. baseline winner) or when the
    # EV planner's Pass 3 allocated full max_charge_per_slot to a slot
    # with only a small surplus.
    #
    # Also apply the charger_min_power_w floor: if the computed AC power
    # is below the charger's minimum operating power, zero out the power
    # and the EV load — the charger physically cannot start, so the
    # energy will never be delivered.
    _slot_hours = inp.interval_minutes / 60.0
    _min_pwr_w = max(
        inp.ev_planned_load_charger_min_power_w,
        inp.ev_second_planned_load_charger_min_power_w,
    )
    for s in slots:
        total_ev_ac = s.ev_planned_load_kwh + s.ev_accounted_load_kwh
        if total_ev_ac < 1e-9:
            continue
        # For the current (partially elapsed) slot use remaining time.
        s_end_tz = as_tz(s.end, now.tzinfo)
        if as_tz(s.start, now.tzinfo) <= now < s_end_tz:
            remaining_h = max((s_end_tz - now).total_seconds() / 3600.0, 1.0 / 3600.0)
            ac_power_w = round((total_ev_ac / remaining_h) * 1000.0)
        else:
            ac_power_w = round((total_ev_ac / _slot_hours) * 1000.0)

        if _min_pwr_w > 1e-9 and ac_power_w < _min_pwr_w:
            # Below minimum — charger won't start.  Zero out power,
            # load, and recommendation so net consumption and cost
            # reflect reality.
            s.ev_charger_calculated_power = 0
            s.ev_second_charger_calculated_power = 0
            s.ev_planned_load_kwh = 0.0
            s.ev_accounted_load_kwh = 0.0
            s.ev_total_planned_load_kwh = 0.0
            s.estimated_net_consumption_kwh = (
                s.avg_house_consumption_kwh - s.solcast_pv_estimate_kwh
            )
            net = s.estimated_net_consumption_kwh
            if net > 0:
                s.estimated_cost_currency = round(net * s.price.import_price, 4)
            else:
                s.estimated_cost_currency = round(net * s.price.export_price, 4)
        else:
            s.ev_charger_calculated_power = ac_power_w

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
            )
        if ev2_cp is not None:
            ev2_cp = rebuild_ev_plan_from_slots(
                ev2_cp,
                slots,
                now,
                charger_efficiency_pct=inp.ev_second_planned_load_charger_efficiency_pct,
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
