"""Convert flexible EV allocations into executable whole-amp energy.

Extracted from ``_write_results.py`` so it stays under the 30 KB file limit.
Pure move: no behaviour change.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import TYPE_CHECKING

from custom_components.hsem.utils.phase_power import (
    charger_current_to_power_w,
    charger_max_power_to_current_a,
    charger_min_power_to_current_a,
    charger_power_to_current_a,
)
from custom_components.hsem.utils.units import ev_dc_to_ac_kwh

if TYPE_CHECKING:
    import numpy as np

    from custom_components.hsem.models.ev_config import EVConfig

#: A stranded residue at or below this threshold is the sub-milliwatt-hour
#: artefact of rounding the rated charger power to whole watts, not a
#: genuine shortfall.  Re-portioning that would open an extra slot and churn
#: an otherwise clean plan for no material gain.
_MATERIAL_RESIDUE_KWH = 0.001

#: Minimum kWh a slot's EV allocation must clear to be considered "occupied"
#: rather than solver noise, mirroring ``_write_milp_results_to_slots``'s
#: ``_min_action_kwh`` default.
_MIN_ACTION_KWH = 1e-4


def _redistribute_below_minimum_power(
    values: dict[int, float],
    *,
    minimum_dc: float,
    deadline_lp_limit: int | None,
    session_slots_set: set[int],
    room_dc: Callable[[int], float],
    donor_energy: float,
) -> tuple[dict[int, float], float, int | None]:
    """Open one further runnable slot to absorb a stranded EV residue.

    The caller's recipients pass can still leave a residue below the
    charger's minimum when every recipient is already at a hard ceiling
    (e.g. a fuse-limited evening).  Discarding it silently misses the
    deadline by that amount.  This opens one empty, runnable slot before the
    deadline at the charger minimum instead, borrowing the shortfall back
    from slots that can spare it above their own minimum.  Later slots are
    drained first so cheaper early charging is preserved.  Total energy is
    unchanged when a slot is opened.

    Args:
        values: Per-LP-slot DC allocation (kWh) for one EV, keyed by LP-slot
            index.  Mutated in place and also returned.
        minimum_dc: Charger minimum deliverable DC energy for a full slot
            (kWh) — below this the charger cannot start.
        deadline_lp_limit: The EV's deadline LP-slot index, or ``None`` for
            no deadline (or charge-past-target).  Only deadline-driven
            charging may open a slot; past-target charging is surplus-only
            with no target to protect.
        session_slots_set: LP-slot indices reserved for fixed session
            demand.  Never opened or drained.
        room_dc: Callable returning slot ``t``'s remaining headroom (kWh)
            under its own EV/grid-import/phase caps.
        donor_energy: The residue left unplaced after the recipients pass.

    Returns:
        ``(values, remaining_donor_energy, reportioned_lp_slot)``.
    """
    if donor_energy <= _MATERIAL_RESIDUE_KWH or deadline_lp_limit is None:
        return values, donor_energy, None
    for t in range(deadline_lp_limit + 1):
        if t in session_slots_set or values.get(t, 0.0) > _MIN_ACTION_KWH:
            continue
        if room_dc(t) < minimum_dc - 1e-9:
            continue
        spare = {
            src: max(v - minimum_dc, 0.0)
            for src, v in values.items()
            if src != t and src not in session_slots_set and v > _MIN_ACTION_KWH
        }
        if donor_energy + sum(spare.values()) < minimum_dc - 1e-9:
            continue
        needed = max(minimum_dc - donor_energy, 0.0)
        for src in sorted(spare, reverse=True):
            if needed <= 1e-12:
                break
            take = min(spare[src], needed)
            values[src] -= take
            needed -= take
        values[t] = minimum_dc
        return values, 0.0, t
    return values, donor_energy, None


def _quantize_ev_allocation_to_whole_amps(
    dc_by_slot: dict[int, float],
    *,
    target_dc_kwh: float,
    slot_hours: dict[int, float],
    charger_efficiency: float,
    charger_min_power_w: float,
    rated_ac_power_w: float,
    charger_phase_topology: str | None,
    slot_ceiling_dc: dict[int, float],
    candidate_dc: dict[int, float] | None = None,
) -> tuple[dict[int, float], float]:
    """Convert a flexible EV allocation into executable whole-amp energy.

    The continuous MILP result is only a budget.  This pass publishes energy
    that the integer-amp charger command can actually deliver, rounds every
    slot down inside its solved fuse/phase ceiling, and spends accumulated
    fractional residue on existing or explicitly eligible candidate slots.
    It never exceeds ``target_dc_kwh``; any irreducible current-step residue is
    returned for deadline/unmet diagnostics.
    """
    target_dc = max(float(target_dc_kwh), 0.0)
    efficiency = max(float(charger_efficiency), 1e-9)
    if target_dc <= 1e-12 or not slot_hours:
        return {}, target_dc

    rated_current_a = charger_max_power_to_current_a(
        rated_ac_power_w,
        charger_phase_topology,
    )
    configured_min_current_a = charger_min_power_to_current_a(
        charger_min_power_w,
        charger_phase_topology,
    )
    activation_min_current_a = max(configured_min_current_a, 1)
    if rated_current_a < activation_min_current_a:
        return {}, target_dc

    all_slots = set(dc_by_slot)
    all_slots.update(candidate_dc or {})
    step_dc: dict[int, float] = {}
    max_current: dict[int, int] = {}
    for slot_i in all_slots:
        hours = max(slot_hours.get(slot_i, 0.0), 0.0)
        if hours <= 1e-12:
            continue
        one_amp_w = charger_current_to_power_w(1, charger_phase_topology)
        step_dc[slot_i] = one_amp_w * hours * efficiency / 1000.0
        ceiling_dc = max(
            slot_ceiling_dc.get(slot_i, (candidate_dc or {}).get(slot_i, 0.0)),
            0.0,
        )
        ceiling_power_w = ceiling_dc / efficiency / hours * 1000.0
        max_current[slot_i] = min(
            rated_current_a,
            charger_power_to_current_a(
                ceiling_power_w,
                charger_phase_topology,
            ),
        )

    current_by_slot: dict[int, int] = {}
    for slot_i, dc_kwh in dc_by_slot.items():
        hours = max(slot_hours.get(slot_i, 0.0), 0.0)
        if hours <= 1e-12 or slot_i not in max_current:
            continue
        power_w = max(dc_kwh, 0.0) / efficiency / hours * 1000.0
        current_a = min(
            charger_power_to_current_a(power_w, charger_phase_topology),
            max_current[slot_i],
        )
        if current_a >= activation_min_current_a:
            current_by_slot[slot_i] = current_a

    def _delivered_dc() -> float:
        return sum(
            current_a * step_dc[slot_i] for slot_i, current_a in current_by_slot.items()
        )

    def _fill_active_slots() -> None:
        """Spend affordable full current steps without exceeding a slot cap."""
        residue = max(target_dc - _delivered_dc(), 0.0)
        for slot_i in sorted(current_by_slot, key=lambda key: (step_dc[key], key)):
            available_steps = max(
                max_current.get(slot_i, 0) - current_by_slot[slot_i],
                0,
            )
            affordable_steps = int(math.floor((residue + 1e-12) / step_dc[slot_i]))
            added_steps = min(available_steps, affordable_steps)
            if added_steps <= 0:
                continue
            current_by_slot[slot_i] += added_steps
            residue -= added_steps * step_dc[slot_i]

    _fill_active_slots()

    # A solver-selected fragment that rounded to zero gets first opportunity
    # to reopen.  Thereafter only caller-supplied candidates are eligible; the
    # caller filters those to actionable slots before the deadline.
    open_candidates = [
        slot_i for slot_i in sorted(dc_by_slot) if slot_i not in current_by_slot
    ]
    open_candidates.extend(
        slot_i
        for slot_i in sorted(candidate_dc or {})
        if slot_i not in current_by_slot and slot_i not in dc_by_slot
    )
    for slot_i in open_candidates:
        if max_current.get(slot_i, 0) < activation_min_current_a:
            continue
        minimum_dc = activation_min_current_a * step_dc[slot_i]
        residue = max(target_dc - _delivered_dc(), 0.0)
        withdrawals: dict[int, int] = {}
        removed_dc = 0.0
        if residue < minimum_dc - 1e-12:
            needed_dc = minimum_dc - residue
            for donor in sorted(current_by_slot, reverse=True):
                spare_steps = max(
                    current_by_slot[donor] - activation_min_current_a,
                    0,
                )
                while spare_steps > 0 and removed_dc < needed_dc - 1e-12:
                    withdrawals[donor] = withdrawals.get(donor, 0) + 1
                    removed_dc += step_dc[donor]
                    spare_steps -= 1
                if removed_dc >= needed_dc - 1e-12:
                    break
            # Opening a slot must increase executable energy, not merely churn
            # an equal-or-larger donor quantity into a different time window.
            if (
                residue + removed_dc < minimum_dc - 1e-12
                or minimum_dc <= removed_dc + 1e-12
            ):
                continue
        for donor, steps in withdrawals.items():
            current_by_slot[donor] -= steps
        current_by_slot[slot_i] = activation_min_current_a
        _fill_active_slots()

    executable = {
        slot_i: current_a * step_dc[slot_i]
        for slot_i, current_a in current_by_slot.items()
        if current_a >= activation_min_current_a
    }
    delivered_dc = sum(executable.values())
    return executable, max(target_dc - delivered_dc, 0.0)


def _quantize_one_ev_allocation(
    values: np.ndarray,  # type: ignore[type-arg]
    *,
    ev: EVConfig,
    ev_session_slots: set[int],
    m: int,
    full_slot_hours: float,
    hours_by_slot: dict[int, float],
    room_dc: Callable[[int], float],
    min_action_kwh: float,
) -> tuple[np.ndarray, float, float, list[int], float]:  # type: ignore[type-arg]
    """Quantize one EV's post-concentration allocation to whole amps.

    Floors the managed live-session slots (if any) and the flexible
    (non-session) allocation down to whole-amp-executable energy, pooling
    and re-spending the fractional residue on slots with headroom (issue
    #789).  ``values`` is mutated in place and also returned.

    Returns ``(values, session_quantization_residue_dc, unplaceable_quant_dc,
    reportioned_quant_slots, effective_min_power_w)``.
    """
    rated_ac_power_w = charger_current_to_power_w(
        charger_max_power_to_current_a(
            (
                ev_dc_to_ac_kwh(ev.max_charge_per_slot, ev.charger_efficiency)
                / full_slot_hours
            )
            * 1000.0,
            ev.charger_phase_topology,
        ),
        ev.charger_phase_topology,
    )
    effective_min_power_w = charger_current_to_power_w(
        charger_min_power_to_current_a(
            ev.charger_min_power_w, ev.charger_phase_topology
        ),
        ev.charger_phase_topology,
    )

    # A managed live session still receives an HSEM current ceiling.  Its
    # fixed measured LP energy must therefore be reduced to the same
    # whole-amp command that will be published; the fractional residue is
    # handed to the flexible slots below to recover.
    session_quantization_residue_dc = 0.0
    min_current_a = max(
        charger_min_power_to_current_a(
            effective_min_power_w, ev.charger_phase_topology
        ),
        1,
    )
    for t in range(m):
        if t not in ev_session_slots:
            continue
        measured_dc = float(values[t])
        if measured_dc <= min_action_kwh:
            continue
        slot_hours_t = max(hours_by_slot.get(t, full_slot_hours), 1e-9)
        measured_power_w = (
            ev_dc_to_ac_kwh(measured_dc, ev.charger_efficiency) / slot_hours_t * 1000.0
        )
        command_current_a = min(
            charger_power_to_current_a(measured_power_w, ev.charger_phase_topology),
            charger_max_power_to_current_a(rated_ac_power_w, ev.charger_phase_topology),
        )
        if command_current_a < min_current_a:
            command_current_a = 0
        executable_dc = (
            charger_current_to_power_w(command_current_a, ev.charger_phase_topology)
            * slot_hours_t
            * ev.charger_efficiency
            / 1000.0
        )
        session_quantization_residue_dc += max(measured_dc - executable_dc, 0.0)
        values[t] = executable_dc

    # Flexible (non-session) allocation: floor every slot down to what a
    # whole-amp command can deliver, then try to recover the pooled
    # fractional residue (including the session's own residue above) by
    # bumping other occupied slots up by whole amp-steps, or opening one
    # further deadline-eligible slot at the charger minimum.
    dc_by_slot = {
        t: float(values[t])
        for t in range(m)
        if t not in ev_session_slots and float(values[t]) > min_action_kwh
    }
    unplaceable_quant_dc = 0.0
    reportioned_quant_slots: list[int] = []
    if dc_by_slot or session_quantization_residue_dc > min_action_kwh:

        def _rated_dc_for_slot(t: int) -> float:
            """Rated energy ceiling for slot ``t``'s own available hours."""
            return (
                rated_ac_power_w
                * hours_by_slot.get(t, full_slot_hours)
                * ev.charger_efficiency
                / 1000.0
            )

        slot_ceiling_dc: dict[int, float] = {
            t: min(_rated_dc_for_slot(t), dc + max(room_dc(t), 0.0))
            for t, dc in dc_by_slot.items()
        }
        candidate_dc: dict[int, float] = {}
        if ev.deadline_slot is not None and not ev.charge_past_target:
            deadline_lp_limit = max(0, min(int(ev.deadline_slot), m - 1))
            for t in range(deadline_lp_limit + 1):
                if t in ev_session_slots or t in dc_by_slot:
                    continue
                ceiling = min(_rated_dc_for_slot(t), max(room_dc(t), 0.0))
                if ceiling > min_action_kwh:
                    candidate_dc[t] = ceiling
                    slot_ceiling_dc[t] = ceiling
        quantized_dc, unplaceable_quant_dc = _quantize_ev_allocation_to_whole_amps(
            dc_by_slot,
            target_dc_kwh=sum(dc_by_slot.values()) + session_quantization_residue_dc,
            slot_hours={
                t: hours_by_slot.get(t, full_slot_hours)
                for t in set(dc_by_slot) | set(candidate_dc)
            },
            charger_efficiency=ev.charger_efficiency,
            charger_min_power_w=effective_min_power_w,
            rated_ac_power_w=rated_ac_power_w,
            charger_phase_topology=ev.charger_phase_topology,
            slot_ceiling_dc=slot_ceiling_dc,
            candidate_dc=candidate_dc,
        )
        for t in dc_by_slot:
            values[t] = 0.0
        for t, dc in quantized_dc.items():
            values[t] = dc
        reportioned_quant_slots = sorted(set(quantized_dc) - set(dc_by_slot))

    return (
        values,
        session_quantization_residue_dc,
        unplaceable_quant_dc,
        reportioned_quant_slots,
        effective_min_power_w,
    )
