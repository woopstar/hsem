"""EV config builder for MILP co-optimisation.

Builds the :class:`~custom_components.hsem.models.ev_config.EVConfig` list that
the MILP solver consumes for EV-aware battery scheduling.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from custom_components.hsem.models.ev_config import EVConfig
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.planner.candidate_selector import (
    ev_future_charge_value_per_kwh,
)
from custom_components.hsem.utils.datetime_utils import as_tz
from custom_components.hsem.utils.logger import log_planner
from custom_components.hsem.utils.misc import clamp_efficiency
from custom_components.hsem.utils.phase_power import (
    normalize_ev_phase_topology,
)


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

    # Avoided-future-import-cost valuation for charge-past-target EVs
    # (issue #630). Computed once — depends only on the slot price
    # forecast, not per-EV state. Per-EV confidence factors are applied
    # below when building each EVConfig.
    ev_avg_future_import_price = ev_future_charge_value_per_kwh(
        slots, now, confidence_factor=1.0
    )

    # Build config for each EV slot pair (primary, secondary).
    # Each source tuple carries an is_second flag so the downstream
    # write-out loop can route EV power to the correct output field by
    # identity rather than by list position (issue #646).
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
            str,
            datetime | None,
            bool,
            bool,
            float,
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
            normalize_ev_phase_topology(inp.ev_planned_load_charger_phase_topology),
            inp.ev_planned_load_deadline,
            inp.ev_planned_load_base_load_includes_ev,
            inp.ev_planned_allow_charge_past_target_soc,
            inp.ev_past_target_confidence_factor,
            False,  # is_second = False (primary EV)
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
            normalize_ev_phase_topology(
                inp.ev_second_planned_load_charger_phase_topology
            ),
            inp.ev_second_planned_load_deadline,
            inp.ev_second_planned_load_base_load_includes_ev,
            inp.ev_second_allow_charge_past_target_soc,
            inp.ev_second_past_target_confidence_factor,
            True,  # is_second = True (second EV)
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
        phase_topology,
        deadline,
        base_includes,
        allow_past_target,
        past_target_confidence_factor,
        is_second,
    ) in ev_sources:
        label = "second" if is_second else "primary"
        session_charge_kw = (
            inp.ev_second_session_charge_kw if is_second else inp.ev_session_charge_kw
        )
        has_live_session = session_charge_kw is not None and session_charge_kw > 1e-9

        if not enabled and not has_live_session:
            log_planner(
                "debug",
                "[milp_ev] %s EV excluded: not enabled",
                label,
            )
            continue
        if (not connected or not smart) and not has_live_session:
            log_planner(
                "debug",
                "[milp_ev] %s EV excluded: connected=%s smart=%s",
                label,
                connected,
                smart,
            )
            continue
        eff = clamp_efficiency(eff_pct)
        session_power_kw = max(float(session_charge_kw or 0.0), 0.0)
        configured_max_power_w = max(float(pwr), 0.0) * 1000.0
        effective_capacity = float(cap)
        if has_live_session and effective_capacity <= 1e-9:
            effective_capacity = max(session_power_kw * 2.0 * eff, 0.001)
        if effective_capacity <= 1e-9:
            log_planner(
                "debug",
                "[milp_ev] %s EV excluded: capacity=%.2f (zero/missing)",
                label,
                effective_capacity,
            )
            continue
        initial_kwh = (soc_pct / 100.0) * effective_capacity
        target_kwh = (target_pct / 100.0) * effective_capacity
        fixed_session_only = has_live_session and (
            not enabled
            or not connected
            or not smart
            or configured_max_power_w <= 1e-9
        )

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
        if fixed_session_only:
            initial_kwh = 0.0
            target_kwh = 0.0
        elif at_or_above_target:
            if has_live_session:
                target_kwh = initial_kwh
                fixed_session_only = True
            elif not allow_past_target or soc_pct >= 100:
                log_planner(
                    "debug",
                    "[milp_ev] %s EV excluded: fully_charged (soc=%.1f%% target=%.1f%% "
                    "allow_past_target=%s)",
                    label,
                    soc_pct,
                    target_pct,
                    allow_past_target,
                )
                continue  # fully charged or past-target not allowed
            # Charge-past-target mode: allow up to 100 %, no deadline pressure.
            target_kwh = effective_capacity
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
                log_planner(
                    "debug",
                    "[milp_ev] %s EV excluded: no slot before deadline=%s",
                    label,
                    eff_deadline.isoformat(),
                )
                continue
            charge_past_target = False

        # Configured power is the hard actuator nameplate for every managed
        # session. Only an unmanaged session may expand its *accounting*
        # envelope to the measured physical draw; it emits no HSEM command.
        # Without this distinction, a managed 6 kW measurement could turn a
        # configured 3 kW charger ceiling into a 6 kW published command.
        effective_power_w = (
            max(configured_max_power_w, session_power_kw * 1000.0)
            if fixed_session_only
            else configured_max_power_w
        )
        effective_power_kw = effective_power_w / 1000.0
        if effective_power_kw <= 1e-9:
            log_planner(
                "debug",
                "[milp_ev] %s EV excluded: power=%.2f (zero/missing)",
                label,
                effective_power_kw,
            )
            continue

        slot_hours = inp.interval_minutes / 60.0
        max_dc = effective_power_kw * slot_hours * eff  # DC-side kWh per slot

        future_value_per_kwh: float | None = None
        if charge_past_target and ev_avg_future_import_price is not None:
            future_value_per_kwh = (
                past_target_confidence_factor * ev_avg_future_import_price
            )

        configs.append(
            EVConfig(
                enabled=True,
                initial_soc_kwh=round(initial_kwh, 3),
                target_kwh=round(target_kwh, 3),
                capacity_kwh=round(effective_capacity, 3),
                max_charge_per_slot=round(max_dc, 4),
                charger_efficiency=round(eff, 4),
                charger_min_power_w=round(min_pwr_w, 1),
                charger_phase_topology=phase_topology,
                deadline_slot=deadline_slot,
                base_load_includes_ev=base_includes,
                charge_past_target=charge_past_target,
                future_value_per_kwh=future_value_per_kwh,
                is_second=is_second,
                session_charge_kw=session_charge_kw,
                fixed_session_only=fixed_session_only,
            )
        )

        if charge_past_target:
            log_planner(
                "debug",
                "[milp_ev] %s EV included  mode=charge_past_target  "
                "soc=%.1f%%  target=100%%  future_value=%.4f/kWh",
                label,
                soc_pct,
                future_value_per_kwh if future_value_per_kwh else 0.0,
            )
        else:
            log_planner(
                "debug",
                "[milp_ev] %s EV included  mode=normal  "
                "soc=%.1f%%  target=%.1f%%  initial=%.3fkWh  target=%.3fkWh  "
                "deadline_slot=%s  max_dc_per_slot=%.4fkWh",
                label,
                soc_pct,
                target_pct,
                initial_kwh,
                target_kwh,
                deadline_slot if deadline_slot is not None else "none",
                max_dc,
            )

    if not configs:
        log_planner(
            "debug",
            "[milp_ev] no EV configs built — MILP will run without EV co-optimisation",
        )

    return configs if configs else None
