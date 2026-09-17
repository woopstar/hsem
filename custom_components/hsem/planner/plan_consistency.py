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

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.utils.recommendations import (
    LABEL_ENERGY_CONTRACTS,
    MATERIAL_ENERGY_KWH,
    EnergyExpectation,
    LabelEnergyContract,
)

__all__ = ["check_plan_self_consistency", "consistency_warning"]

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


def check_plan_self_consistency(slots: list[PlannedSlot]) -> list[str]:
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

    Args:
        slots: The selected plan's slots, in chronological order.

    Returns:
        One human-readable string per violating slot, in slot order.  An empty
        list means the plan is self-consistent.  Never raises, and never
        mutates ``slots``.
    """
    violations: list[str] = []
    for slot in slots:
        recommendation = slot.recommendation
        if recommendation is None:
            continue
        contract = _BY_VALUE.get(recommendation)
        if contract is None:
            continue
        problems = [
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
        f"contradicts their recommendation — this is an HSEM bug, please report "
        f"it with a diagnostics dump. {'; '.join(shown)}{suffix}"
    )
