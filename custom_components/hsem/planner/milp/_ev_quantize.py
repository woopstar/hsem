"""Re-portion a stranded EV charging residue into a further runnable slot.

Extracted from ``_write_results.py`` so it stays under the 30 KB file limit.
Kept as a standalone pure function for direct/compatibility callers: managed
EV writeback (issue #797) links its amp command to the solve itself and
never calls this, but ``_redistribute_below_minimum_power`` remains a real,
tested code path.
"""

from __future__ import annotations

from collections.abc import Callable

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
