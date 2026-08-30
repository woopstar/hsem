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

Two corrections are applied here, both purely at the **command layer**:

1. **Ceiling deadband** — hold the previous command unless the plan asks to
   *lower* it by at least ``command_deadband_a``, or holding would cost more
   than
   :data:`~custom_components.hsem.const.EV_COMMAND_DEADBAND_COST_BYPASS_FRACTION`
   of the live slot's own EV cost.  Deliberately asymmetric: the published
   value is a ceiling an external controller (or the charger's own surplus
   logic) ramps *within*, so only a downward move can force the charger to
   reduce.  Raising the ceiling is always published immediately.
2. **Slot-tail stop suppression** — in the last ``stub_floor_minutes`` of a
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
    ev_site_power_budget_w,
    write_ev_slot_commands,
)
from custom_components.hsem.coordinator_state import CoordinatorSharedState
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import EVLiveState, LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.datetime_utils import slot_contains, utc_key
from custom_components.hsem.utils.logger import async_log
from custom_components.hsem.utils.misc import get_config_value
from custom_components.hsem.utils.phase_power import (
    charger_current_to_power_w,
    charger_max_power_to_current_a,
    charger_min_power_to_current_a,
    charger_power_to_current_a,
)
from custom_components.hsem.utils.units import slot_duration_hours


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
            enabled,
            ev_live,
            connected,
            smart_charging,
            deadband_a,
            stub_floor_minutes,
            topology,
            charger_power_kw,
            min_power_w,
            capacity_kwh,
            target_soc_pct,
            deadline,
        ) in (
            (
                "ev",
                "EV",
                False,
                cfg.ev_planned_load_enabled,
                live.ev,
                live.ev_planned_load_connected,
                live.ev_planned_load_smart_charging_enabled,
                cfg.ev_planned_load_command_deadband_a,
                cfg.ev_planned_load_stub_floor_minutes,
                cfg.ev_planned_load_charger_phase_topology,
                cfg.ev_planned_load_charger_power_kw,
                cfg.ev_planned_load_charger_min_power_w,
                cfg.ev_planned_load_battery_capacity_kwh,
                live.ev_planned_load_target_soc_pct,
                live.ev_planned_load_deadline,
            ),
            (
                "ev_second",
                "EV2",
                True,
                cfg.ev_second_planned_load_enabled,
                live.ev_second,
                live.ev_second_planned_load_connected,
                live.ev_second_planned_load_smart_charging_enabled,
                cfg.ev_second_planned_load_command_deadband_a,
                cfg.ev_second_planned_load_stub_floor_minutes,
                cfg.ev_second_planned_load_charger_phase_topology,
                cfg.ev_second_planned_load_charger_power_kw,
                cfg.ev_second_planned_load_charger_min_power_w,
                cfg.ev_second_planned_load_battery_capacity_kwh,
                live.ev_second_planned_load_target_soc_pct,
                live.ev_second_planned_load_deadline,
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
                    min_current_a=charger_min_power_to_current_a(
                        max(float(min_power_w or 0.0), 0.0), topology
                    ),
                    managed=bool(enabled) and bool(connected) and bool(smart_charging),
                    ev_live=ev_live,
                    capacity_kwh=max(float(capacity_kwh or 0.0), 0.0),
                    target_soc_pct=float(target_soc_pct or 0.0),
                    deadline=deadline,
                )
            )
        return specs

    # ------------------------------------------------------------------
    # Predicates
    # ------------------------------------------------------------------

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
        current_kwh = self._ev_effective_energy_kwh(spec.ev_live, spec.capacity_kwh)
        if current_kwh is None:
            return False
        target_kwh = (
            max(min(spec.target_soc_pct, 100.0), 0.0) / 100.0 * spec.capacity_kwh
        )
        return current_kwh + 1e-9 < target_kwh

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
        planned_cost = planned_kwh * price_now
        if planned_cost <= 1e-9:
            return False
        extra_cost = (held_kwh - planned_kwh) * (price_now - price_alt)
        return extra_cost > EV_COMMAND_DEADBAND_COST_BYPASS_FRACTION * planned_cost

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
        old_total_ev_kwh = max(float(slot.ev_total_planned_load_kwh), 0.0)
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
            old_total_ev_kwh=old_total_ev_kwh,
            base_load_includes_ev=bool(
                get_config_value(
                    self._config_entry, "hsem_house_power_includes_ev_charger_power"
                )
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
        unrunnable trickle.
        """
        if power_w <= 1e-9:
            return 0.0
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

        planned_a = charger_power_to_current_a(planned_w, spec.topology)
        previous_a = charger_power_to_current_a(previous_w, spec.topology)
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
