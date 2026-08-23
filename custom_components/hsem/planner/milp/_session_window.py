"""Resolve live-session EV certainty windows, bounded by control authority.

Extracted from ``milp_optimizer.py`` so it stays under the 30 KB file limit.
Pure move: no behaviour change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

import numpy as np

from custom_components.hsem.utils.units import slot_duration_hours

if TYPE_CHECKING:
    from custom_components.hsem.models.ev_config import EVConfig
    from custom_components.hsem.models.planned_slot import PlannedSlot

#: Bounded forecast certainty window for an unmanaged (uncontrollable) live
#: session — HSEM cannot start/stop this charger, so the whole window is
#: treated as certain demand.
_SESSION_HOURS = 2.0


@dataclass(frozen=True)
class SessionWindows:
    """Per-EV live-session certainty windows for one MILP solve."""

    slot_hours: float
    available_slot_hours: np.ndarray  # type: ignore[type-arg]
    session_ev_indices: list[int]
    session_dc_by_ev: dict[int, dict[int, float]]
    session_slots_set: set[int]
    session_slots_by_ev: dict[int, set[int]]
    has_session_demand: bool


def resolve_session_windows(
    *,
    slots: list[PlannedSlot],
    future_idx: list[int],
    now: datetime,
    active_evs: list[EVConfig],
    m: int,
) -> SessionWindows:
    """Return each active EV's live-session certainty window.

    A live session's certainty window depends on whether HSEM can actually
    stop it (issue #615, #789):

    - Unmanaged (``fixed_session_only``): HSEM cannot command this charger
      at all, so the whole bounded two-hour forecast window is certain,
      uncontrollable demand — unchanged from the original fix.
    - Managed: HSEM can start/stop it through the bridge each cycle, so only
      the already-running remainder of the CURRENT slot is certain.
      Reserving further slots would lock in energy the planner has no
      reason to commit to and cannot cancel.  The pinned amount is also
      capped at the EV's own remaining target so a session that already
      satisfies its target does not force additional certain charging.
    """
    slot_hours = (
        slot_duration_hours(slots[future_idx[0]].start, slots[future_idx[0]].end)
        if future_idx
        else 0.0
    )
    available_slot_hours = np.asarray(
        [
            (
                slot_duration_hours(max(now, slots[slot_i].start), slots[slot_i].end)
                if slots[slot_i].start <= now < slots[slot_i].end
                else slot_duration_hours(slots[slot_i].start, slots[slot_i].end)
            )
            for slot_i in future_idx
        ],
        dtype=float,
    )
    session_ev_indices: list[int] = []
    session_dc_by_ev: dict[int, dict[int, float]] = {}
    if active_evs and slot_hours > 0:
        for ev_idx, ev in enumerate(active_evs):
            if ev.session_charge_kw is None or ev.session_charge_kw <= 1e-9:
                continue
            session_ev_indices.append(ev_idx)
            session_power_kw = max(float(ev.session_charge_kw), 0.0)
            fixed_dc: dict[int, float] = {}
            if ev.fixed_session_only:
                hours_remaining = _SESSION_HOURS
                for t, available_hours in enumerate(available_slot_hours):
                    if hours_remaining <= 1e-9:
                        break
                    duration_scale = min(
                        max(float(available_hours) / max(slot_hours, 1e-9), 0.0),
                        1.0,
                    )
                    fixed_dc[t] = min(
                        session_power_kw
                        * float(available_hours)
                        * ev.charger_efficiency,
                        ev.max_charge_per_slot * duration_scale,
                    )
                    # Commands are slot-constant. Include the whole slot that
                    # crosses the two-hour boundary so its energy still maps
                    # back to the observed power instead of a diluted command.
                    hours_remaining -= float(available_hours)
            elif m:
                remaining_target_dc = max(
                    min(ev.target_kwh, ev.capacity_kwh) - ev.initial_soc_kwh,
                    0.0,
                )
                duration_scale = min(
                    max(float(available_slot_hours[0]) / max(slot_hours, 1e-9), 0.0),
                    1.0,
                )
                fixed_dc[0] = min(
                    session_power_kw
                    * float(available_slot_hours[0])
                    * ev.charger_efficiency,
                    ev.max_charge_per_slot * duration_scale,
                    remaining_target_dc,
                )
            fixed_slots = {t: dc for t, dc in fixed_dc.items() if dc > 1e-9}
            if fixed_slots:
                session_dc_by_ev[ev_idx] = fixed_slots

    session_slots_set: set[int] = (
        set().union(*(set(v) for v in session_dc_by_ev.values()))
        if session_dc_by_ev
        else set()
    )
    session_slots_by_ev: dict[int, set[int]] = {
        ev_idx: set(fixed) for ev_idx, fixed in session_dc_by_ev.items()
    }
    return SessionWindows(
        slot_hours=slot_hours,
        available_slot_hours=available_slot_hours,
        session_ev_indices=session_ev_indices,
        session_dc_by_ev=session_dc_by_ev,
        session_slots_set=session_slots_set,
        session_slots_by_ev=session_slots_by_ev,
        has_session_demand=bool(session_dc_by_ev),
    )
