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

#: No real EVSE starts a charging session below this current, per IEC 61851
#: (also the practical floor for go-e and most other charger vendors).  A
#: configured ``charger_min_power_w`` is a single-phase watt figure by
#: convention (see ``EVConfig.charger_min_power_w``); dividing it across a
#: ``three_phase_balanced`` charger's phases can compute a lower current that
#: no EVSE will actually accept, so every site that turns a configured
#: minimum-power threshold into an executable amp floor must go through
#: :func:`ev_min_start_current_a`, never :func:`charger_min_power_to_current_a`
#: directly (issue #968).
EV_MIN_START_CURRENT_A = 6

#: Signed live per-phase grid power in Watts, ``(phase_a, phase_b, phase_c)``.
PhasePowers = tuple[float, float, float]

#: EV charger phase topology identifiers.  ``single_phase`` is the safe
#: default: with an unknown or single-phase charger every hard per-phase row
#: must assume the whole EV command can land on that one phase.
#: ``three_phase_switchable`` (issue #1001) models an auto-phase-switching
#: charger (go-e, Zaptec, Easee, ...): it starts a session at 6 A on a
#: single phase and switches to balanced three-phase once the command
#: exceeds what one phase can carry, so its minimum power is a single-phase
#: figure (230 V × 6 A = 1380 W) while its rated power is three-phase
#: (e.g. 230 V × 16 A × 3 = 11 kW).
EV_TOPOLOGY_SINGLE_PHASE = "single_phase"
EV_TOPOLOGY_THREE_PHASE_BALANCED = "three_phase_balanced"
EV_TOPOLOGY_THREE_PHASE_SWITCHABLE = "three_phase_switchable"
EV_PHASE_TOPOLOGIES = (
    EV_TOPOLOGY_SINGLE_PHASE,
    EV_TOPOLOGY_THREE_PHASE_BALANCED,
    EV_TOPOLOGY_THREE_PHASE_SWITCHABLE,
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
        ``1.0``.  A ``three_phase_switchable`` charger's share is
        mode-dependent (1.0 in one-phase mode, ``1 / PHASE_COUNT`` in
        three-phase mode), so this static helper conservatively returns
        ``1.0`` for it; power-aware sites must use
        :func:`ev_phase_share_for_power_w` instead.
    """
    if topology == EV_TOPOLOGY_THREE_PHASE_BALANCED:
        return 1.0 / PHASE_COUNT
    return 1.0


def ev_phase_share_for_power_w(
    topology: str | None,
    power_w: float,
    *,
    single_phase_max_power_w: float,
) -> float:
    """Return the per-phase share for a known AC command power.

    Extends :func:`ev_phase_share` with the mode a ``three_phase_switchable``
    charger must be in to deliver *power_w*: at or below
    ``single_phase_max_power_w`` the whole command can sit on one phase
    (share 1.0); above it the charger is physically in balanced three-phase
    mode (share ``1 / PHASE_COUNT``).  Other topologies are power-independent.
    """
    if topology == EV_TOPOLOGY_THREE_PHASE_SWITCHABLE:
        if math.isfinite(power_w) and power_w > single_phase_max_power_w + 1e-9:
            return 1.0 / PHASE_COUNT
        return 1.0
    return ev_phase_share(topology)


def ev_switchable_single_phase_max_power_w(
    *,
    max_charge_per_slot: float,
    charger_efficiency: float,
    slot_hours: float,
) -> float:
    """Return a switchable charger's one-phase-mode AC ceiling (W).

    Mirrors the amp lattice's rated-current derivation
    (``planner/milp/_ev_amp_lattice.py``): the rated whole-amp command comes
    from the exact per-slot AC envelope on a three-phase basis, and the
    one-phase mode tops out at that same per-phase current on a single
    phase.  Returns ``0.0`` when the envelope is not positive and finite.
    """
    try:
        bound_ac_power_w = (
            float(max_charge_per_slot)
            / max(float(charger_efficiency), 0.01)
            / max(float(slot_hours), 1e-9)
            * 1000.0
        )
    except TypeError, ValueError:
        return 0.0
    rated_current_a = charger_power_to_current_a(
        bound_ac_power_w, EV_TOPOLOGY_THREE_PHASE_BALANCED
    )
    return charger_current_to_power_w(rated_current_a, EV_TOPOLOGY_SINGLE_PHASE)


def charger_power_to_current_a(
    power_w: float,
    topology: str | None,
    *,
    rated_current_a: int | None = None,
) -> int:
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
        rated_current_a: Rated whole-amp command (per phase) of a
            ``three_phase_switchable`` charger.  Mode-aware conversion needs
            it to know where the one-phase range ends: at or below
            ``230 V × rated_current_a`` the command maps to one-phase amps,
            above it to three-phase amps.  Callers publishing a ceiling for
            a switchable charger must pass it; ``None`` falls back to the
            conservative single-phase division.

    Returns:
        Whole amps per phase, floored, and never negative.
    """
    if not math.isfinite(power_w) or power_w <= 0.0:
        return 0
    if topology == EV_TOPOLOGY_THREE_PHASE_SWITCHABLE and rated_current_a:
        single_phase_max_w = GRID_PHASE_VOLTAGE * rated_current_a
        if power_w <= single_phase_max_w + 1e-9:
            return int(math.floor(power_w / GRID_PHASE_VOLTAGE + 1e-9))
        amps = int(math.floor(power_w / (GRID_PHASE_VOLTAGE * PHASE_COUNT) + 1e-9))
        if amps < EV_MIN_START_CURRENT_A:
            # The unexecutable gap between the one-phase ceiling and the
            # three-phase minimum: publish the one-phase ceiling.
            return rated_current_a
        return amps
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

    A ``three_phase_switchable`` command is mode-dependent (the same amp
    value delivers ``a × 230 V`` in one-phase mode and ``a × 690 V`` in
    three-phase mode), so this static helper returns the **one-phase-mode**
    power for it — the basis every minimum-side conversion
    (:func:`charger_min_power_to_current_a`,
    :func:`ev_min_start_current_a`, activation quanta) must use.  Rated-side
    and mode-aware sites must pass an explicit topology
    (``EV_TOPOLOGY_THREE_PHASE_BALANCED``) or use
    :func:`charger_power_to_current_a` with ``rated_current_a``.
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

    A ``three_phase_switchable`` charger's nameplate is its three-phase
    rating, so the snap uses the three-phase step.
    """
    step_topology = (
        EV_TOPOLOGY_THREE_PHASE_BALANCED
        if topology == EV_TOPOLOGY_THREE_PHASE_SWITCHABLE
        else topology
    )
    step_power_w = charger_current_to_power_w(1, step_topology)
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


def ev_min_start_current_a(power_w: float, topology: str | None) -> int:
    """Return the executable minimum start current for a configured threshold.

    Wraps :func:`charger_min_power_to_current_a` with a hard floor at
    :data:`EV_MIN_START_CURRENT_A`: no real EVSE starts below 6 A, so a
    low, zero, or phase-naive configured ``charger_min_power_w`` can never
    compute an unusable sub-6A minimum, regardless of the charger's phase
    topology (issue #968). Every site that turns a configured minimum-power
    threshold into an executable amp floor must call this, not
    :func:`charger_min_power_to_current_a` directly.
    """
    return max(
        charger_min_power_to_current_a(power_w, topology),
        EV_MIN_START_CURRENT_A,
    )


def switchable_power_to_current_and_power_w(
    power_w: float,
    rated_current_a: int,
) -> tuple[int, float]:
    """Return ``(amps, executable_power_w)`` for a switchable charger command.

    Mode selection follows the physical switching rule: at or below the
    one-phase-mode ceiling (``230 V × rated_current_a``) the command is a
    one-phase amp value; above it, a three-phase amp value.  The returned
    power is the exact executable power of the floored whole-amp command in
    the selected mode, so amps→power round-trips stay consistent (used by
    the command-stability deadband, issue #1001).
    """
    if (
        not math.isfinite(power_w)
        or power_w <= 0.0
        or not isinstance(rated_current_a, int)
        or rated_current_a <= 0
    ):
        return (0, 0.0)
    single_phase_max_w = GRID_PHASE_VOLTAGE * rated_current_a
    if power_w <= single_phase_max_w + 1e-9:
        amps = int(math.floor(power_w / GRID_PHASE_VOLTAGE + 1e-9))
        return (amps, charger_current_to_power_w(amps, EV_TOPOLOGY_SINGLE_PHASE))
    amps = int(math.floor(power_w / (GRID_PHASE_VOLTAGE * PHASE_COUNT) + 1e-9))
    if amps < EV_MIN_START_CURRENT_A:
        # The command falls into the unexecutable gap between the one-phase
        # ceiling and the three-phase minimum (e.g. 3681–4139 W at 16 A):
        # the nearest executable command at or below it is the one-phase
        # ceiling itself.
        return (
            rated_current_a,
            charger_current_to_power_w(rated_current_a, EV_TOPOLOGY_SINGLE_PHASE),
        )
    return (amps, charger_current_to_power_w(amps, EV_TOPOLOGY_THREE_PHASE_BALANCED))


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
    with the same topology share.  A charger missing from ``active_evs``
    keeps the conservative single-phase share, matching the behaviour of an
    unconfigured topology.  A ``three_phase_switchable`` charger's share is
    derived from its published power: at or below its one-phase-mode ceiling
    the whole command may sit on one phase; above it the command is
    physically balanced three-phase (issue #1001).
    """
    shares = {False: 1.0, True: 1.0}
    powers = {False: primary_power_w, True: second_power_w}
    for ev in active_evs:
        is_second = bool(ev.is_second)
        if ev.charger_phase_topology == EV_TOPOLOGY_THREE_PHASE_SWITCHABLE:
            shares[is_second] = ev_phase_share_for_power_w(
                ev.charger_phase_topology,
                max(float(powers[is_second]), 0.0),
                single_phase_max_power_w=ev_switchable_single_phase_max_power_w(
                    max_charge_per_slot=ev.max_charge_per_slot,
                    charger_efficiency=ev.charger_efficiency,
                    slot_hours=hours,
                ),
            )
        else:
            shares[is_second] = ev.phase_share
    return (
        (
            max(float(primary_power_w), 0.0) * shares[False]
            + max(float(second_power_w), 0.0) * shares[True]
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
    session is weighted by its charger's topology share, so a
    single-phase charger keeps the full worst-case envelope while a balanced
    three-phase charger contributes only the third it can physically place
    on any one phase.  A ``three_phase_switchable`` charger's session power
    is measured, so its share is exact: the whole session at or below its
    one-phase-mode ceiling, one third when the measured power physically
    requires balanced three-phase mode (issue #1001).
    """
    return sum(
        max(float(ev.session_charge_kw or 0.0), 0.0)
        * hours
        * (
            ev_phase_share_for_power_w(
                ev.charger_phase_topology,
                max(float(ev.session_charge_kw or 0.0), 0.0) * 1000.0,
                single_phase_max_power_w=ev_switchable_single_phase_max_power_w(
                    max_charge_per_slot=ev.max_charge_per_slot,
                    charger_efficiency=ev.charger_efficiency,
                    slot_hours=hours,
                ),
            )
            if ev.charger_phase_topology == EV_TOPOLOGY_THREE_PHASE_SWITCHABLE
            else ev.phase_share
        )
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

    predicted_phase_power_w: PhasePowers
    """Live phase power, with Huawei's own contribution removed, plus the
    commanded charge, evenly split."""


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
        predicted_phase_power_w=predicted,  # type: ignore[arg-type]
    )
