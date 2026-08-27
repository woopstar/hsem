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
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeGuard

from custom_components.hsem.utils.misc import clamp_efficiency
from custom_components.hsem.utils.units import GRID_PHASE_VOLTAGE

if TYPE_CHECKING:
    from custom_components.hsem.models.ev_config import EVConfig

#: Nominal number of mains phases used by the per-phase fuse model.
PHASE_COUNT = 3

#: Signed live per-phase grid power in Watts, ``(phase_a, phase_b, phase_c)``.
PhasePowers = tuple[float, float, float]

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
    return int(math.floor(power_w / (GRID_PHASE_VOLTAGE * phases) + 1e-9))


def charger_current_to_power_w(
    current_a: int | float,
    topology: str | None,
) -> float:
    """Return AC charger power for a per-phase current command.

    Charger current controls use the same whole-amp value on every active
    phase. Unknown topology stays conservative and is treated as one phase.
    Invalid or non-positive currents produce a zero-power command.
    """
    if not math.isfinite(current_a) or current_a <= 0.0:
        return 0.0
    phases = PHASE_COUNT if topology == EV_TOPOLOGY_THREE_PHASE_BALANCED else 1
    return float(current_a) * GRID_PHASE_VOLTAGE * phases


def charger_max_power_to_current_a(
    power_w: float,
    topology: str | None,
) -> int:
    """Return the nearest whole-amp nameplate current.

    Configured charger power is an approximate nameplate. Snapping it to the
    nearest supported current preserves the physical rating: for example,
    11.0 kW for a balanced three-phase charger represents 16 A (11.04 kW),
    not a 15 A hard cap. Half-amp ties round upward deterministically.
    """
    step_power_w = charger_current_to_power_w(1, topology)
    if not math.isfinite(power_w) or power_w <= 0.0 or step_power_w <= 0.0:
        return 0
    return int(math.floor(power_w / step_power_w + 0.5))


def charger_min_power_to_current_a(
    power_w: float,
    topology: str | None,
) -> int:
    """Return the first whole-amp command at or above a power threshold.

    Configured minimum power is a physical start threshold, so it rounds up.
    A 3.6 kW balanced three-phase threshold therefore becomes 6 A / 4.14 kW;
    publishing 5 A would ask the charger to run below its configured minimum.
    """
    step_power_w = charger_current_to_power_w(1, topology)
    if not math.isfinite(power_w) or power_w <= 0.0 or step_power_w <= 0.0:
        return 0
    return int(math.ceil((power_w - 1e-9) / step_power_w))


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
    session_slots_by_ev: dict[int, set[int]],
    lp_t: int,
    hours: float,
    unmanaged_only: bool = False,
) -> float:
    """Return one phase's share of full-slot AC energy for measured sessions.

    Session variables are fixed to the observed charger power during each
    EV's own certainty window (bounded by control authority — issue #789),
    so membership is checked per EV rather than against one shared,
    site-wide slot set: an unmanaged second charger's window must not make a
    different, managed charger's flexible slots look session-fixed.  Each
    session is weighted by its charger's :attr:`~EVConfig.phase_share`, so a
    single-phase charger keeps the full worst-case envelope while a balanced
    three-phase charger contributes only the third it can physically place
    on any one phase.
    """
    return sum(
        max(float(ev.session_charge_kw or 0.0), 0.0) * hours * ev.phase_share
        for ev_idx, ev in enumerate(active_evs)
        if lp_t in session_slots_by_ev.get(ev_idx, set())
        and (not unmanaged_only or ev.fixed_session_only)
    )


# ---------------------------------------------------------------------------
# Live per-phase Huawei grid-charge safety limiter (issue #831)
# ---------------------------------------------------------------------------
#
# Runtime correction on top of the horizon MILP: the MILP's phase-fuse
# constraint (above) uses a forecast at solve time.  Immediately before each
# hardware write, this section re-checks the newest live phase-meter snapshot
# so an appliance change since the plan was solved cannot push a phase over
# the fuse rating.  Huawei-only — this repo has no secondary/PowMr inverter.


@dataclass(frozen=True)
class PhaseChargeLimits:
    """Safe Huawei grid-charge command derived from live per-phase power."""

    primary_charge_power_w: float
    """Safe grid-charge maximum-power command (W), floored to a 100 W step."""

    base_phase_power_w: PhasePowers
    """Live phase power with Huawei's own measured contribution removed."""

    predicted_phase_power_w: PhasePowers
    """``base_phase_power_w`` plus the commanded charge, evenly split."""


def phase_powers_valid(
    values: tuple[float | None, float | None, float | None],
) -> TypeGuard[PhasePowers]:
    """Return whether all three signed phase readings are finite numbers."""
    return all(value is not None and math.isfinite(value) for value in values)


def _floor_step(value: float, step: float) -> float:
    """Round a non-negative command down to a supported hardware step."""
    if value <= 1e-9 or step <= 1e-9:
        return 0.0
    return math.floor((value + 1e-9) / step) * step


def compute_phase_charge_limits(
    *,
    measured_phase_power_w: PhasePowers,
    fuse_amps: float,
    desired_charge_power_w: float,
    battery_actual_power_w: float,
    charge_efficiency_pct: float,
    discharge_efficiency_pct: float,
) -> PhaseChargeLimits:
    """Return the safe Huawei grid-charge command for the live phase snapshot.

    The battery's own currently measured contribution is removed from the
    meter snapshot before the new command is calculated.  This avoids a
    feedback loop where a running charge consumes its own apparent headroom:
    without the correction, a battery already charging at full power would
    make the meter appear to have no spare capacity, and the limiter would
    cut the command to zero even though the fuse has ample headroom.

    ``battery_actual_power_w`` follows the ``STORAGE_CHARGE_DISCHARGE_POWER``
    sign convention: positive is charging, negative is discharging.  The
    resulting command targets the rated fuse current; no intentional
    overload allowance is used.

    Args:
        measured_phase_power_w: Live per-phase grid power, signed (import
            positive), from the Huawei power meter.
        fuse_amps: Main fuse rating in amps. Must be a three-phase supply;
            callers gate on ``main_fuse_phases == 3`` before calling this.
        desired_charge_power_w: The plan's requested grid-charge power (W,
            battery-side/DC), non-negative.
        battery_actual_power_w: Live signed battery charge/discharge power
            (W), from ``STORAGE_CHARGE_DISCHARGE_POWER``.
        charge_efficiency_pct: Battery charge-side efficiency (0-100).
        discharge_efficiency_pct: Battery discharge-side efficiency (0-100).

    Returns:
        :class:`PhaseChargeLimits` with the safe command and the phase-power
        frames used to compute it, for diagnostics.
    """
    limit_w = max(fuse_amps, 0.0) * GRID_PHASE_VOLTAGE
    base = list(measured_phase_power_w)

    charge_eff = clamp_efficiency(charge_efficiency_pct)
    discharge_eff = clamp_efficiency(discharge_efficiency_pct)
    if battery_actual_power_w > 1e-9:
        actual_site_w = battery_actual_power_w / charge_eff
    elif battery_actual_power_w < -1e-9:
        actual_site_w = battery_actual_power_w * discharge_eff
    else:
        actual_site_w = 0.0
    for index in range(PHASE_COUNT):
        base[index] -= actual_site_w / PHASE_COUNT

    desired_dc_w = max(desired_charge_power_w, 0.0)
    ac_headroom_w = PHASE_COUNT * max(
        min(limit_w - phase_w for phase_w in base),
        0.0,
    )
    dc_limit_w = ac_headroom_w * charge_eff
    dc_target_w = _floor_step(min(desired_dc_w, dc_limit_w), 100.0)
    ac_target_w = dc_target_w / charge_eff if dc_target_w > 1e-9 else 0.0

    predicted = tuple(
        base[index] + ac_target_w / PHASE_COUNT for index in range(PHASE_COUNT)
    )
    return PhaseChargeLimits(
        primary_charge_power_w=dc_target_w,
        base_phase_power_w=tuple(base),  # type: ignore[arg-type]
        predicted_phase_power_w=predicted,  # type: ignore[arg-type]
    )
