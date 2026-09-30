"""Post-plan self-consistency gate for label/energy contracts (issue #1035).

A slot's recommendation and its energy fields must agree.  That rule was
violated three times in one week — issue #989 (charge label, zero energy),
issue #1026 (discharge label, zero energy) and issue #1032 (wait label,
non-zero energy), the last of which reached users in a shipped v6.3.2 — and
each violation had to be found by a human reading logs.

This module turns the rule into a mechanical check.  The contract itself lives
in :data:`~custom_components.hsem.utils.recommendations.LABEL_ENERGY_CONTRACTS`
so that adding a :class:`Recommendations` member forces a decision about its
energy contract.

A slot's flows must also balance (issue #1158).  The seasonal fill once booked a
solar charge on MILP slots where the LP exported the same PV surplus, so the
published slot both stored and exported it.  Every label contract held, and the
plan was wrong anyway.  :func:`energy_balance_deficit` catches that class of
bug: energy a slot uses that no source supplies.

Two deliberate non-behaviours
-----------------------------
The check **never raises** and **never auto-corrects**.  A violation is a bug in
HSEM, not a reason to stop controlling someone's battery, so a false positive
must not be able to break an automation or block a hardware write.
Auto-correction is worse still: it is exactly the fix that issue #1033
rejected, because zeroing a slot's energy after the fact leaves every
downstream slot with more battery energy than the plan assumed and hides the
wrong decision that produced the slot.
"""

from __future__ import annotations

from datetime import datetime

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.planner.ev_load_accounting import split_house_and_ev_load
from custom_components.hsem.utils.recommendations import (
    LABEL_ENERGY_CONTRACTS,
    MATERIAL_ENERGY_KWH,
    EnergyExpectation,
    LabelEnergyContract,
)

__all__ = [
    "ENERGY_BALANCE_TOLERANCE_KWH",
    "check_plan_self_consistency",
    "consistency_warning",
    "energy_balance_deficit",
]

#: Largest energy deficit (kWh) a slot may carry before the gate reports it.
#: Covers the 3-decimal rounding of the slot's energy fields.
ENERGY_BALANCE_TOLERANCE_KWH = 5e-3

_BY_VALUE: dict[str, LabelEnergyContract] = {
    member.value: contract for member, contract in LABEL_ENERGY_CONTRACTS.items()
}


def _field_violation(
    expectation: EnergyExpectation,
    value: float,
    field_name: str,
) -> str | None:
    """Return a description of how ``value`` breaks ``expectation``, or ``None``.

    Args:
        expectation: What the slot's label promises about this field.
        value: The slot's energy value in kWh.
        field_name: Name of the field, used in the returned description.

    Returns:
        A short human-readable description of the violation, or ``None`` when
        the value satisfies the expectation.
    """
    if expectation is EnergyExpectation.MATERIAL and value <= MATERIAL_ENERGY_KWH:
        return f"{field_name}={value:.3f} (expected material)"
    if expectation is EnergyExpectation.ZERO and value > MATERIAL_ENERGY_KWH:
        return f"{field_name}={value:.3f} (expected zero)"
    return None


def energy_balance_deficit(
    slot: PlannedSlot, charge_eff: float, discharge_eff: float
) -> float:
    """Return how much more energy a slot's flows use than they supply (kWh).

    ::

        supply = pv + grid_import + discharged × η_dis
        demand = house + ev + grid_export + charged / η_chg

    House and EV load are split as ``simulate_soc`` splits them.  A positive
    result is energy from nowhere, such as PV counted as both stored and
    exported (issue #1158).  A negative result is legitimate: PV curtailed
    at a negative export price leaves supply unused, and curtailment is not
    a slot field.

    Args:
        slot: A published slot.
        charge_eff: Charge efficiency fraction (0-1].
        discharge_eff: Discharge efficiency fraction (0-1].

    Returns:
        ``demand − supply`` in kWh.
    """
    house, ev = split_house_and_ev_load(slot)
    supply = (
        slot.solcast_pv_estimate_kwh
        + slot.grid_import_kwh
        + slot.batteries_discharged_kwh * discharge_eff
    )
    demand = house + ev + slot.grid_export_kwh + slot.batteries_charged_kwh / charge_eff
    return demand - supply


def check_plan_self_consistency(
    slots: list[PlannedSlot],
    *,
    now: datetime | None = None,
    charge_eff: float | None = None,
    discharge_eff: float | None = None,
) -> list[str]:
    """Check every slot's label against its energy fields.

    Intended to run on the **winning** candidate in the planner output path,
    after every relabelling pass has run.  ``simulate_soc`` runs per-candidate,
    so checking a single candidate's trace proves nothing about what was
    actually published.

    A label with no entry in
    :data:`~custom_components.hsem.utils.recommendations.LABEL_ENERGY_CONTRACTS`
    is skipped rather than reported, so an unmapped member cannot spam a user's
    warnings; ``tests/planner/test_plan_consistency.py`` is what fails in that
    case.

    When both efficiencies are given, every slot that ends after *now* is
    also checked for an :func:`energy_balance_deficit` above
    :data:`ENERGY_BALANCE_TOLERANCE_KWH` (issue #1158).  Past slots are
    skipped: they keep their planned values for the plan-vs-actual tracker.

    Args:
        slots: The selected plan's slots, in chronological order.
        now: Current time; slots ending at or before it are not
            balance-checked.  ``None`` checks every slot.
        charge_eff: Charge efficiency fraction; ``None`` skips the balance
            check.
        discharge_eff: Discharge efficiency fraction; ``None`` skips the
            balance check.

    Returns:
        One human-readable string per violating slot, in slot order.  An empty
        list means the plan is self-consistent.  Never raises, and never
        mutates ``slots``.
    """
    violations: list[str] = []
    for slot in slots:
        recommendation = slot.recommendation
        contract = _BY_VALUE.get(recommendation) if recommendation else None
        problems = (
            [
                problem
                for problem in (
                    _field_violation(
                        contract.charge,
                        slot.batteries_charged_kwh,
                        "batteries_charged_kwh",
                    ),
                    _field_violation(
                        contract.discharge,
                        slot.batteries_discharged_kwh,
                        "batteries_discharged_kwh",
                    ),
                )
                if problem is not None
            ]
            if contract is not None
            else []
        )
        if (
            charge_eff is not None
            and discharge_eff is not None
            and (now is None or slot.end > now)
        ):
            deficit = energy_balance_deficit(slot, charge_eff, discharge_eff)
            if deficit > ENERGY_BALANCE_TOLERANCE_KWH:
                problems.append(f"energy balance short by {deficit:.3f} kWh")
        if problems:
            violations.append(
                f"{slot.start.isoformat()} {recommendation}: {', '.join(problems)}"
            )
    return violations


def consistency_warning(violations: list[str]) -> str:
    """Render violations as a single warning line for ``PlannerOutput.warnings``.

    Args:
        violations: The list returned by :func:`check_plan_self_consistency`.

    Returns:
        A one-line summary naming the first few offending slots.  Callers must
        only use this when ``violations`` is non-empty.
    """
    shown = violations[:3]
    suffix = (
        f" (+{len(violations) - len(shown)} more)"
        if len(violations) > len(shown)
        else ""
    )
    return (
        f"Plan self-consistency: {len(violations)} slot(s) carry energy that "
        f"contradicts their recommendation or their energy balance — this is "
        f"an HSEM bug, please report "
        f"it with a diagnostics dump. {'; '.join(shown)}{suffix}"
    )
