"""EV charger command stability — amp deadband and slot-tail stop suppression.

The planner re-solves on every cycle and re-derives the *live* slot's charger
command from scratch:

    command_W = energy allocated to the remainder of this slot
                ÷ time remaining in this slot

Both terms move every solve.  The amp lattice is integer, the EV target-cap
pins total pre-deadline energy to the remaining need, and the live slot's amp
step shrinks continuously as the slot elapses — so the live slot's amps is a
*residual* on a lattice that is itself moving.  Competing integer splits are
routinely within a rounding error of each other on cost, which means a 0.3 %
SoC update can flip the published command by 2–3 A for a fraction of a cent.

Three corrections are applied here, all purely at the **command layer**:

1. **Phase-mode hysteresis** — while a managed switchable charger is actively
   charging below target, keep an executable command in its current phase mode
   instead of forcing a disruptive one-/three-phase reconnect. Target energy,
   deadline feasibility, fuse safety, and material economics always bypass it.
2. **Ceiling deadband** — hold the previous command unless the plan asks to
   *lower* it by at least ``command_deadband_a``, or holding would cost more
   than
   :data:`~custom_components.hsem.const.EV_COMMAND_DEADBAND_COST_BYPASS_FRACTION`
   of the live slot's own EV cost.  Deliberately asymmetric: the published
   value is a ceiling an external controller (or the charger's own surplus
   logic) ramps *within*, so only a downward move can force the charger to
   reduce.  Raising the ceiling is always published immediately.
3. **Slot-tail stop suppression** — in the last ``stub_floor_minutes`` of a
   slot, do not publish a zero command while the EV still has unmet need.  A
   few seconds of remaining slot cannot hold enough energy to clear the
   charger minimum, so the plan correctly allocates it nothing — but a 0 W
   command stops the session, and the restart handshake costs far more energy
   than the stub was ever worth.

Why post-plan and not inside the MILP: the planner spec requires
``winner.cost == final_output.cost`` (no post-selection mutation of the
*plan*).  A deadband is execution-layer smoothing that deliberately departs
from the freshly solved optimum, so it belongs after candidate selection,
alongside the force-charge-now override — not inside the solver, where it
would corrupt the plan's own cost identity.

Held commands are always re-clamped to the live fuse budget and re-quantised
to whole amps, so stability can never publish something the site cannot carry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from custom_components.hsem.const import EV_COMMAND_DEADBAND_COST_BYPASS_FRACTION
from custom_components.hsem.coordinator_helpers import (
    ev_is_managed,
    ev_site_power_budget_w,
    write_ev_slot_commands,
)
from custom_components.hsem.coordinator_state import CoordinatorSharedState
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import EVLiveState, LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.datetime_utils import slot_contains, utc_key
from custom_components.hsem.utils.ev_accounting import normalized_baseline_includes_ev
from custom_components.hsem.utils.logger import async_log
from custom_components.hsem.utils.misc import clamp_efficiency, get_config_value
from custom_components.hsem.utils.phase_power import (
    EV_TOPOLOGY_SINGLE_PHASE,
    EV_TOPOLOGY_THREE_PHASE_BALANCED,
    EV_TOPOLOGY_THREE_PHASE_SWITCHABLE,
    PHASE_COUNT,
    charger_current_to_power_w,
    charger_max_power_to_current_a,
    charger_power_to_current_a,
    ev_min_start_current_a,
    phase_powers_valid,
    switchable_command_phase_count,
    switchable_power_to_current_and_power_w,
)
from custom_components.hsem.utils.units import (
    GRID_PHASE_VOLTAGE,
    ev_ac_to_dc_kwh,
    slot_duration_hours,
)


@dataclass(frozen=True)
class _EvCommandSpec:
    """Everything the stability layer needs to decide one EV's command."""

    key: str
    label: str
    is_second: bool
    deadband_a: float
    stub_floor_minutes: float
    topology: str
    rated_current_a: int
    min_current_a: int
    managed: bool
    ev_live: EVLiveState
    capacity_kwh: float
    target_soc_pct: float
    deadline: datetime | None
    charger_efficiency: float = 1.0
    main_fuse_amps: float = 0.0
    main_fuse_phases: int = 3
    grid_phase_power_w: tuple[float | None, float | None, float | None] = (
        None,
        None,
        None,
    )
    #: Whether this EV may charge past its target SoC from PV surplus.
    allow_charge_past_target: bool = False


class CoordinatorEvCommandStabilityMixin(CoordinatorSharedState):
    """Damp integer-lattice churn in the published EV charger commands."""

    # ------------------------------------------------------------------
    # Spec resolution
    # ------------------------------------------------------------------

    def _resolve_ev_command_specs(
        self, cfg: SensorConfig, live: LiveState
    ) -> list[_EvCommandSpec]:
        """Return one spec per configured EV planned-load charger."""
        specs: list[_EvCommandSpec] = []
        for (
            key,
            label,
            is_second,
            ev_live,
            deadband_a,
            stub_floor_minutes,
            topology,
            charger_power_kw,
            min_power_w,
            capacity_kwh,
            charger_efficiency_pct,
            target_soc_pct,
            deadline,
            allow_charge_past_target,
        ) in (
            (
                "ev",
                "EV",
                False,
                live.ev,
                cfg.ev_planned_load_command_deadband_a,
                cfg.ev_planned_load_stub_floor_minutes,
                cfg.ev_planned_load_charger_phase_topology,
                cfg.ev_planned_load_charger_power_kw,
                cfg.ev_planned_load_charger_min_power_w,
                cfg.ev_planned_load_battery_capacity_kwh,
                cfg.ev_planned_load_charger_efficiency_pct,
                live.ev_planned_load_target_soc_pct,
                live.ev_planned_load_deadline,
                cfg.ev.allow_charge_past_target_soc,
            ),
            (
                "ev_second",
                "EV2",
                True,
                live.ev_second,
                cfg.ev_second_planned_load_command_deadband_a,
                cfg.ev_second_planned_load_stub_floor_minutes,
                cfg.ev_second_planned_load_charger_phase_topology,
                cfg.ev_second_planned_load_charger_power_kw,
                cfg.ev_second_planned_load_charger_min_power_w,
                cfg.ev_second_planned_load_battery_capacity_kwh,
                cfg.ev_second_planned_load_charger_efficiency_pct,
                live.ev_second_planned_load_target_soc_pct,
                live.ev_second_planned_load_deadline,
                cfg.ev_second.allow_charge_past_target_soc,
            ),
        ):
            specs.append(
                _EvCommandSpec(
                    key=key,
                    label=label,
                    is_second=is_second,
                    deadband_a=max(float(deadband_a or 0.0), 0.0),
                    stub_floor_minutes=max(float(stub_floor_minutes or 0.0), 0.0),
                    topology=topology,
                    # Configured charger power is an approximate
                    # nameplate: 11.0 kW three-phase *is* 16 A / 11.04 kW.
                    # Snapping through the canonical helper keeps this
                    # clamp from being tighter than the planner's own
                    # envelope and silently capping the charger a step low.
                    rated_current_a=charger_max_power_to_current_a(
                        max(float(charger_power_kw or 0.0), 0.0) * 1000.0,
                        topology,
                    ),
                    min_current_a=ev_min_start_current_a(
                        max(float(min_power_w or 0.0), 0.0), topology
                    ),
                    managed=ev_is_managed(cfg, live, is_second=is_second),
                    ev_live=ev_live,
                    capacity_kwh=max(float(capacity_kwh or 0.0), 0.0),
                    charger_efficiency=clamp_efficiency(charger_efficiency_pct),
                    main_fuse_amps=max(float(cfg.main_fuse_amps or 0.0), 0.0),
                    main_fuse_phases=max(int(cfg.main_fuse_phases or 0), 0),
                    grid_phase_power_w=live.grid_phase_power_w,
                    target_soc_pct=float(target_soc_pct or 0.0),
                    deadline=deadline,
                    allow_charge_past_target=bool(allow_charge_past_target),
                )
            )
        return specs

    # ------------------------------------------------------------------
    # Predicates
    # ------------------------------------------------------------------

    def _ev_is_past_target(self, spec: _EvCommandSpec) -> bool:
        """Return whether this EV is charging past its target SoC (issue #1015).

        Only then is its planned command a PV-surplus ceiling. Fails closed to
        ``False`` on missing telemetry — the planner refuses to plan an EV
        with an unavailable SoC anyway (issue #988), so its plan is zero.
        """
        if not spec.allow_charge_past_target:
            return False
        remaining_target_kwh = self._remaining_target_kwh(spec)
        return remaining_target_kwh is not None and remaining_target_kwh <= 1e-9

    def _ev_has_unmet_need(self, spec: _EvCommandSpec, now: datetime) -> bool:
        """Return whether this EV still needs energy before its deadline.

        Fails closed to ``False`` — suppressing a stop is only ever justified
        by a *proven* remaining need, never by missing telemetry.
        """
        if spec.capacity_kwh <= 1e-9 or spec.deadline is None:
            return False
        try:
            if utc_key(spec.deadline) <= utc_key(now):
                return False
        except TypeError, ValueError:
            return False
        remaining_target_kwh = self._remaining_target_kwh(spec)
        return remaining_target_kwh is not None and remaining_target_kwh > 1e-9

    def _remaining_target_kwh(self, spec: _EvCommandSpec) -> float | None:
        """Return proven DC-side energy still needed to reach the EV target."""
        if spec.capacity_kwh <= 1e-9:
            return None
        current_kwh = self._ev_effective_energy_kwh(spec.ev_live, spec.capacity_kwh)
        if current_kwh is None:
            return None
        target_kwh = (
            max(min(spec.target_soc_pct, 100.0), 0.0) / 100.0 * spec.capacity_kwh
        )
        return max(target_kwh - current_kwh, 0.0)

    @staticmethod
    def _holding_cost_exceeds_bypass(
        *,
        held_w: float,
        planned_w: float,
        remaining_hours: float,
        price_now: float,
        price_alt: float | None,
    ) -> bool:
        """Return whether holding ``held_w`` is materially worse than the plan.

        Holding shifts energy between the live slot and whichever slot the
        plan would otherwise use, so the honest cost of holding is the energy
        delta priced at the *difference* between the two slots' import prices.
        A negative result means holding is actually cheaper, which never
        bypasses the deadband.

        Returns ``False`` (hold) when there is no comparable alternative slot
        or no meaningful planned cost to measure the delta against.
        """
        if price_alt is None or remaining_hours <= 1e-9:
            return False
        held_kwh = held_w * remaining_hours / 1000.0
        planned_kwh = planned_w * remaining_hours / 1000.0
        if planned_kwh <= 1e-9:
            return False
        planned_cost_magnitude = abs(planned_kwh * price_now)
        extra_cost = (held_kwh - planned_kwh) * (price_now - price_alt)
        return (
            extra_cost
            > EV_COMMAND_DEADBAND_COST_BYPASS_FRACTION * planned_cost_magnitude
        )

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def _apply_ev_command_stability(
        self,
        now: datetime,
        live: LiveState,
        cfg: SensorConfig,
    ) -> None:
        """Damp the live slot's EV charger commands in place.

        Runs after every other post-plan override so it smooths the command
        that would actually have been published, and records what it published
        so the next cycle can hold against it.
        """
        slot = next(
            (
                item
                for item in self._hourly_recommendations
                if slot_contains(item.start, item.end, now)
            ),
            None,
        )
        if slot is None:
            return
        remaining_hours = slot_duration_hours(max(now, slot.start), slot.end)
        if remaining_hours <= 1e-9:
            return

        specs = self._resolve_ev_command_specs(cfg, live)
        old_planned_ev_kwh = max(float(slot.ev_planned_load_kwh), 0.0)
        budget_w = ev_site_power_budget_w(self._config_entry, live)
        published: dict[str, float] = {}

        for spec in specs:
            planned_w = self._planned_command_w(slot, spec.is_second)
            decided_w = self._decide_command_w(
                spec=spec,
                slot=slot,
                now=now,
                planned_w=planned_w,
                remaining_hours=remaining_hours,
            )
            # Safety clamps always win over stability: never publish above the
            # charger rating or the live fuse budget the other EV must share.
            headroom_w = max(budget_w - sum(published.values()), 0.0)
            decided_w = min(decided_w, headroom_w)
            decided_w = self._quantise_to_whole_amps(decided_w, spec)
            published[spec.key] = decided_w
            self._ev_last_command_w[spec.key] = decided_w
            if abs(decided_w - planned_w) > 1e-9:
                async_log(
                    "debug",
                    "[ev_stability] %s command held at %dW (plan asked %dW, "
                    "%.1f min left in slot)",
                    spec.label,
                    round(decided_w),
                    round(planned_w),
                    remaining_hours * 60.0,
                )

        write_ev_slot_commands(
            slot,
            primary_w=published.get("ev", 0.0),
            second_w=published.get("ev_second", 0.0),
            remaining_hours=remaining_hours,
            old_planned_ev_kwh=old_planned_ev_kwh,
            primary_base_load_includes_ev=normalized_baseline_includes_ev(
                raw_house_meter_includes_ev=bool(
                    get_config_value(
                        self._config_entry,
                        "hsem_house_power_includes_ev_charger_power",
                    )
                ),
                ev_power_entity=get_config_value(
                    self._config_entry, "hsem_ev_charger_power"
                ),
            ),
            second_base_load_includes_ev=normalized_baseline_includes_ev(
                raw_house_meter_includes_ev=bool(
                    get_config_value(
                        self._config_entry,
                        "hsem_house_power_includes_ev_charger_power",
                    )
                ),
                ev_power_entity=get_config_value(
                    self._config_entry, "hsem_ev_second_charger_power"
                ),
            ),
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _planned_command_w(slot: HourlyRecommendation, is_second: bool) -> float:
        """Return the plan's command for one charger, clamped non-negative."""
        value = (
            slot.ev_second_charger_calculated_power
            if is_second
            else slot.ev_charger_calculated_power
        )
        try:
            return max(float(value), 0.0)
        except TypeError, ValueError:
            return 0.0

    @staticmethod
    def _quantise_to_whole_amps(power_w: float, spec: _EvCommandSpec) -> float:
        """Round a command down to a whole-amp command the charger can run.

        A command below the charger's minimum operating power cannot start a
        session, so it collapses to zero rather than being published as an
        unrunnable trickle.  A switchable charger's round-trip is mode-aware
        (issue #1001): one-phase amps below its one-phase ceiling,
        three-phase amps above it.
        """
        if power_w <= 1e-9:
            return 0.0
        if (
            spec.topology == EV_TOPOLOGY_THREE_PHASE_SWITCHABLE
            and spec.rated_current_a > 0
        ):
            amps, executable_w = switchable_power_to_current_and_power_w(
                power_w, spec.rated_current_a
            )
            if amps > spec.rated_current_a:
                # Clamp to the nameplate, recovering the executable power of
                # the rated amps in the mode the original command selected.
                amps = spec.rated_current_a
                executable_w = charger_current_to_power_w(
                    amps,
                    (
                        EV_TOPOLOGY_THREE_PHASE_BALANCED
                        if power_w > GRID_PHASE_VOLTAGE * spec.rated_current_a
                        else EV_TOPOLOGY_SINGLE_PHASE
                    ),
                )
            if amps < spec.min_current_a:
                return 0.0
            return executable_w
        amps = min(
            charger_power_to_current_a(power_w, spec.topology),
            spec.rated_current_a,
        )
        if amps < spec.min_current_a:
            return 0.0
        return float(charger_current_to_power_w(amps, spec.topology))

    def _next_ev_slot_price(
        self, slot: HourlyRecommendation, is_second: bool
    ) -> float | None:
        """Return the import price of the next slot carrying this EV's load.

        That is the slot energy would move to (or come from) when the live
        command is held away from the plan, so its price is the correct
        counterfactual for the deadband's cost check.
        """
        for item in sorted(self._hourly_recommendations, key=lambda r: r.start):
            if utc_key(item.start) <= utc_key(slot.start):
                continue
            if self._planned_command_w(item, is_second) > 1e-9:
                return float(item.import_price)
        return None

    def _future_planned_ev_energy_kwh(
        self,
        *,
        spec: _EvCommandSpec,
        slot: HourlyRecommendation,
    ) -> float:
        """Return accepted-plan DC energy after this slot and before deadline."""
        if spec.deadline is None:
            return 0.0
        slot_end = utc_key(slot.end)
        deadline = utc_key(spec.deadline)
        total_kwh = 0.0
        for item in self._hourly_recommendations:
            start = max(utc_key(item.start), slot_end)
            end = min(utc_key(item.end), deadline)
            hours = slot_duration_hours(start, end)
            if hours <= 1e-9:
                continue
            power_w = self._planned_command_w(item, spec.is_second)
            total_kwh += ev_ac_to_dc_kwh(
                power_w * hours / 1000.0,
                spec.charger_efficiency,
            )
        return total_kwh

    @staticmethod
    def _one_phase_hold_is_phase_safe(spec: _EvCommandSpec, held_w: float) -> bool:
        """Return whether live per-phase telemetry proves a one-phase hold safe."""
        if spec.main_fuse_amps <= 1e-9:
            return True
        if spec.main_fuse_phases != PHASE_COUNT or not phase_powers_valid(
            spec.grid_phase_power_w
        ):
            return False
        additional_w = max(held_w - max(float(spec.ev_live.power_w or 0.0), 0.0), 0.0)
        phase_limit_w = spec.main_fuse_amps * GRID_PHASE_VOLTAGE
        return max(spec.grid_phase_power_w) + additional_w <= phase_limit_w + 1e-9

    def _phase_mode_hold_command_w(
        self,
        *,
        spec: _EvCommandSpec,
        slot: HourlyRecommendation,
        now: datetime,
        planned_w: float,
        previous_w: float,
        remaining_hours: float,
    ) -> float | None:
        """Return a safe same-mode command for a disruptive phase crossing."""
        if (
            spec.topology != EV_TOPOLOGY_THREE_PHASE_SWITCHABLE
            or spec.rated_current_a <= 0
            or not spec.ev_live.is_charging
            or not self._ev_has_unmet_need(spec, now)
        ):
            return None

        previous_phases = switchable_command_phase_count(
            previous_w, spec.rated_current_a
        )
        planned_phases = switchable_command_phase_count(planned_w, spec.rated_current_a)
        if previous_phases == planned_phases:
            return None

        if previous_phases == PHASE_COUNT:
            held_w = charger_current_to_power_w(
                spec.min_current_a, EV_TOPOLOGY_THREE_PHASE_BALANCED
            )
        else:
            held_w = previous_w
        held_w = self._quantise_to_whole_amps(min(held_w, previous_w), spec)
        if (
            held_w <= 1e-9
            or switchable_command_phase_count(held_w, spec.rated_current_a)
            != previous_phases
        ):
            return None

        if previous_phases == 1 and not self._one_phase_hold_is_phase_safe(
            spec, held_w
        ):
            return None

        remaining_target_kwh = self._remaining_target_kwh(spec)
        if remaining_target_kwh is None or spec.deadline is None:
            return None
        try:
            deadline_hours = max(
                (utc_key(spec.deadline) - utc_key(now)).total_seconds() / 3600.0,
                0.0,
            )
        except TypeError, ValueError:
            return None

        held_current_kwh = ev_ac_to_dc_kwh(
            held_w * min(remaining_hours, deadline_hours) / 1000.0,
            spec.charger_efficiency,
        )
        # A same-mode hold must never deliver more than the remaining target.
        if held_current_kwh > remaining_target_kwh + 1e-9:
            return None

        max_reachable_kwh = held_current_kwh + self._future_planned_ev_energy_kwh(
            spec=spec,
            slot=slot,
        )
        # Do not preserve one-phase mode when its lower current-slot delivery
        # cannot be recovered by the accepted plan's executable future commands.
        if max_reachable_kwh + 1e-9 < remaining_target_kwh:
            return None

        if self._holding_cost_exceeds_bypass(
            held_w=held_w,
            planned_w=planned_w,
            remaining_hours=remaining_hours,
            price_now=float(slot.import_price),
            price_alt=self._next_ev_slot_price(slot, spec.is_second),
        ):
            return None
        return held_w

    def _decide_command_w(
        self,
        *,
        spec: _EvCommandSpec,
        slot: HourlyRecommendation,
        now: datetime,
        planned_w: float,
        remaining_hours: float,
    ) -> float:
        """Return the command to publish for one EV before safety clamps."""
        previous_w = self._ev_last_command_w.get(spec.key, 0.0)

        # An unmanaged charger is never held — a disconnected car, a disabled
        # planned load, or smart charging switched off must follow the plan
        # (including straight to zero) immediately.
        if not spec.managed:
            return planned_w

        # A charge-past-target EV may only draw PV surplus, and its planned
        # command already *is* that surplus (issue #1015). Holding a higher,
        # stale ceiling would make up the difference from the grid: under
        # OCPP the profile is a hard current limit the vehicle draws up to,
        # and the cost bypass below cannot release it — it prices a hold as
        # energy shifted between slots, but past-target energy is not being
        # shifted, so on flat prices the bypass never fires. Follow the plan.
        if self._ev_is_past_target(spec):
            return planned_w

        if planned_w <= 1e-9:
            return self._stub_floor_command_w(
                spec=spec,
                slot=slot,
                now=now,
                previous_w=previous_w,
                remaining_hours=remaining_hours,
            )

        if previous_w <= 1e-9 or spec.deadband_a <= 0.0:
            return planned_w

        phase_crossing = (
            spec.topology == EV_TOPOLOGY_THREE_PHASE_SWITCHABLE
            and spec.rated_current_a > 0
            and switchable_command_phase_count(previous_w, spec.rated_current_a)
            != switchable_command_phase_count(planned_w, spec.rated_current_a)
        )
        if phase_crossing:
            phase_hold_w = self._phase_mode_hold_command_w(
                spec=spec,
                slot=slot,
                now=now,
                planned_w=planned_w,
                previous_w=previous_w,
                remaining_hours=remaining_hours,
            )
            # A crossing rejected by any hard guard must not fall through to
            # the ordinary amp deadband and be held there accidentally.
            return planned_w if phase_hold_w is None else phase_hold_w

        planned_a = charger_power_to_current_a(
            planned_w, spec.topology, rated_current_a=spec.rated_current_a or None
        )
        previous_a = charger_power_to_current_a(
            previous_w, spec.topology, rated_current_a=spec.rated_current_a or None
        )
        # HSEM publishes a *ceiling*, not a setpoint: a charger that follows
        # PV surplus itself only has to react when the ceiling drops below
        # what it is already drawing.  Raising the ceiling merely grants
        # headroom the charger may or may not take, so an increase is never
        # held back — only a reduction is subject to the deadband.
        if planned_a >= previous_a:
            return planned_w
        if previous_a - planned_a >= spec.deadband_a:
            return planned_w

        if self._holding_cost_exceeds_bypass(
            held_w=previous_w,
            planned_w=planned_w,
            remaining_hours=remaining_hours,
            price_now=float(slot.import_price),
            price_alt=self._next_ev_slot_price(slot, spec.is_second),
        ):
            return planned_w
        return previous_w

    def _stub_floor_command_w(
        self,
        *,
        spec: _EvCommandSpec,
        slot: HourlyRecommendation,
        now: datetime,
        previous_w: float,
        remaining_hours: float,
    ) -> float:
        """Return the command for a slot the plan wants to leave at zero.

        Suppresses the stop only in the configured tail of the slot, only
        while the charger is genuinely mid-session, and only while the EV
        still has unmet need before its deadline — so a completed session, a
        finished target, or an unplugged car all still stop immediately.
        """
        if spec.stub_floor_minutes <= 0.0 or previous_w <= 1e-9:
            return 0.0
        if remaining_hours * 60.0 >= spec.stub_floor_minutes:
            return 0.0
        if not spec.ev_live.is_charging:
            return 0.0
        if not self._ev_has_unmet_need(spec, now):
            return 0.0
        async_log(
            "debug",
            "[ev_stability] %s suppressing stop in slot tail (%.1f min left, "
            "holding %dW) — target not yet reached before %s",
            spec.label,
            remaining_hours * 60.0,
            round(previous_w),
            slot.end.isoformat(),
        )
        return previous_w


__all__ = ["CoordinatorEvCommandStabilityMixin"]
