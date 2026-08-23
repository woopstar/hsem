"""EV charger phase topology — single authority for per-phase fuse math.

The hard per-phase fuse model is expressed in three independent places: the
MILP constraint rows, the reconstruction of phase flows from a solved decision
vector, and the validation of the final published plan.  All three must agree
on how much of an EV command a single phase may carry, or the solver can
produce a plan that its own validator later erases.

Every consumer derives its EV term from :func:`ev_phase_share` (or from the
shared helpers below), never by re-deriving a fraction inline.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from custom_components.hsem.utils.units import GRID_PHASE_VOLTAGE

if TYPE_CHECKING:
    from custom_components.hsem.models.ev_config import EVConfig

#: Nominal number of mains phases used by the per-phase fuse model.
PHASE_COUNT = 3

#: EV charger phase topology identifiers.  ``single_phase`` is the safe
#: default: with an unknown or single-phase charger every hard per-phase row
#: must assume the whole EV command can land on that one phase.
EV_TOPOLOGY_SINGLE_PHASE = "single_phase"
EV_TOPOLOGY_THREE_PHASE_BALANCED = "three_phase_balanced"
EV_PHASE_TOPOLOGIES = (
    EV_TOPOLOGY_SINGLE_PHASE,
    EV_TOPOLOGY_THREE_PHASE_BALANCED,
)


def ev_phase_share(topology: str | None) -> float:
    """Return the fraction of one EV's AC draw a single phase may carry.

    This is the single authority for EV charger topology.  Every hard
    per-phase site — constraint construction, solved-vector reconstruction and
    published-plan validation — must derive its EV term from this helper, or
    the optimiser and its validators can disagree and silently erase a plan
    that was feasible when it was solved.

    Args:
        topology: One of :data:`EV_PHASE_TOPOLOGIES`.  Any unknown or missing
            value falls back to the conservative single-phase share.

    Returns:
        ``1 / PHASE_COUNT`` for a balanced three-phase charger, otherwise
        ``1.0``.
    """
    if topology == EV_TOPOLOGY_THREE_PHASE_BALANCED:
        return 1.0 / PHASE_COUNT
    return 1.0


def charger_power_to_current_a(power_w: float, topology: str | None) -> int:
    """Return the whole-amp ceiling equivalent to an AC charger command.

    HSEM plans in watts, but an external current controller that consumes the
    published charging ceiling is commanded in whole amps, so the conversion
    belongs in one place.  Rounding is always *down*: a partial amp the
    charger cannot be commanded to draw must never be published as available
    headroom.

    Args:
        power_w: Planned AC power for the charger, in watts.
        topology: The charger's phase topology.  A balanced three-phase
            charger spreads the command over ``PHASE_COUNT`` phases; anything
            else is treated as single-phase.

    Returns:
        Whole amps per phase, floored, and never negative.
    """
    if not math.isfinite(power_w) or power_w <= 0.0:
        return 0
    phases = PHASE_COUNT if topology == EV_TOPOLOGY_THREE_PHASE_BALANCED else 1
    return int(math.floor(power_w / (GRID_PHASE_VOLTAGE * phases)))


def normalize_ev_phase_topology(value: object) -> str:
    """Return a supported EV phase topology for any stored config value.

    Config entries written before this option existed carry no topology at
    all, and a stale or hand-edited entry may carry an unrecognised string.
    Both resolve to the conservative single-phase model rather than silently
    relaxing a hard fuse constraint.
    """
    if isinstance(value, str) and value in EV_PHASE_TOPOLOGIES:
        return value
    return EV_TOPOLOGY_SINGLE_PHASE


def ev_phase_share_for_slot(
    *,
    active_evs: list[EVConfig],
) -> tuple[float, float]:
    """Return ``(primary_share, second_share)`` for one planning slot.

    The published plan carries one power field per charger, so each field is
    weighted by that charger's own topology share.  A charger missing from
    ``active_evs`` keeps the conservative single-phase share, matching the
    behaviour of an unconfigured topology.
    """
    shares = {False: 1.0, True: 1.0}
    for ev in active_evs:
        shares[bool(ev.is_second)] = ev.phase_share
    return (shares[False], shares[True])


def executable_ev_phase_kwh(
    *,
    primary_power_w: float,
    second_power_w: float,
    active_evs: list[EVConfig],
    hours: float,
) -> float:
    """Return one phase's share of the executable EV command energy (kWh).

    Used identically by solved-decision-vector reconstruction and by
    published-plan validation so both sites weight each charger's power field
    with the same topology share.
    """
    primary_share, second_share = ev_phase_share_for_slot(active_evs=active_evs)
    return (
        (
            max(float(primary_power_w), 0.0) * primary_share
            + max(float(second_power_w), 0.0) * second_share
        )
        * hours
        / 1000.0
    )


def fixed_session_phase_ac_kwh(
    *,
    active_evs: list[EVConfig],
    session_slots_set: set[int],
    lp_t: int,
    hours: float,
    unmanaged_only: bool = False,
) -> float:
    """Return one phase's share of full-slot AC energy for measured sessions.

    Session variables are fixed to the observed charger power during the
    certainty window.  Each session is weighted by its charger's
    :attr:`~EVConfig.phase_share`, so a single-phase charger keeps the full
    worst-case envelope while a balanced three-phase charger contributes only
    the third it can physically place on any one phase.
    """
    if lp_t not in session_slots_set:
        return 0.0
    return sum(
        max(float(ev.session_charge_kw or 0.0), 0.0) * hours * ev.phase_share
        for ev in active_evs
        if not unmanaged_only or ev.fixed_session_only
    )
