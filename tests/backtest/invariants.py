"""Spec invariants checked against a replayed planning cycle (issue #1037).

Every check here restates one bullet from ``docs/planner-spec.md``.  They are
written against a ``(PlannerInput, PlannerOutput)`` pair rather than against a
fixture, so the same code can be pointed at any recorded production cycle.

Checks return violations instead of asserting, for two reasons: one replay
should report *all* of its problems rather than stopping at the first, and the
same functions are useful from an analysis script where an assertion would be
the wrong control flow.

Scope: these are self-consistency invariants — they prove a plan does not
contradict itself or the spec.  They deliberately say nothing about whether the
plan is *economically good*; that needs realized actuals and is Stage 2 (see
``docs/backtest-harness.md``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.planner.plan_consistency import (
    check_plan_self_consistency,
)
from custom_components.hsem.utils.recommendations import (
    SENTINEL_RECS,
    Recommendations,
)

__all__ = ["InvariantViolation", "check_invariants", "format_violations"]

#: Tolerance for kWh/percent comparisons.  Wide enough for float accumulation
#: across 192 slots, tight enough that a real accounting error still shows.
_EPS = 1e-6

#: Energy below this is noise, not a direction.  Matches the planner's own
#: materiality threshold for label/energy contracts.
_MATERIAL_KWH = 1e-3

#: Values of the inert planner-state sentinels (``time_passed``,
#: ``missing_input_entities``).  Those slots are never simulated, so their
#: energy and SoC fields stay at their defaults and must be skipped.
_SENTINEL_VALUES: frozenset[str] = frozenset(m.value for m in SENTINEL_RECS)

_KNOWN_RECOMMENDATIONS: frozenset[str] = frozenset(m.value for m in Recommendations)


@dataclass(frozen=True)
class InvariantViolation:
    """One failed spec invariant.

    Attributes:
        invariant: Short machine-readable name of the invariant.
        detail: Human-readable description of how it was broken, naming the
            offending slot or value.
    """

    invariant: str
    detail: str

    def __str__(self) -> str:
        """Return ``"<invariant>: <detail>"``."""
        return f"{self.invariant}: {self.detail}"


def _actionable(slots: list[PlannedSlot]) -> list[PlannedSlot]:
    """Return only the slots the planner actually simulated.

    Args:
        slots: All slots of a plan.

    Returns:
        The slots whose recommendation is not an inert state sentinel.
    """
    return [s for s in slots if s.recommendation not in _SENTINEL_VALUES]


def _effective_soc_bounds(inp: PlannerInput) -> tuple[float, float]:
    """Return the ``(floor, ceiling)`` SoC percentages the plan must respect.

    Mirrors the engine's own resolution: the dynamic discharge floor (issue
    #600) replaces the hardware end-of-discharge floor only when it is higher,
    and neither may exceed the configured maximum SoC.

    Args:
        inp: The replayed planner input.

    Returns:
        The effective floor and ceiling, both in percent.
    """
    hardware = min(max(inp.battery_end_of_discharge_soc_pct, 0.0), 100.0)
    ceiling = min(max(inp.battery_max_soc_pct, hardware), 100.0)
    dynamic = inp.dynamic_discharge_floor_pct
    floor = hardware if dynamic is None else max(dynamic, hardware)
    return min(floor, ceiling), ceiling


def _check_slot_grid(inp: PlannerInput, out: PlannerOutput) -> list[InvariantViolation]:
    """Check slot count and contiguity against the configured horizon."""
    violations: list[InvariantViolation] = []
    expected = (inp.interval_length_hours * 60) // inp.interval_minutes
    if len(out.slots) != expected:
        violations.append(
            InvariantViolation(
                "slot_count",
                f"{len(out.slots)} slots for a {inp.interval_length_hours} h "
                f"horizon at {inp.interval_minutes} min (expected {expected})",
            )
        )
    for prev, nxt in zip(out.slots, out.slots[1:], strict=False):
        if prev.end != nxt.start:
            violations.append(
                InvariantViolation(
                    "slots_contiguous",
                    f"{prev.end.isoformat()} does not meet {nxt.start.isoformat()}",
                )
            )
    return violations


def _check_energy_balance(
    _inp: PlannerInput, out: PlannerOutput
) -> list[InvariantViolation]:
    """Check the per-slot net-consumption identity from the spec."""
    violations: list[InvariantViolation] = []
    for slot in out.slots:
        expected = (
            slot.avg_house_consumption_kwh
            + slot.ev_planned_load_kwh
            - slot.solcast_pv_estimate_kwh
        )
        if abs(slot.estimated_net_consumption_kwh - expected) > _EPS:
            violations.append(
                InvariantViolation(
                    "energy_balance",
                    f"{slot.start.isoformat()} net="
                    f"{slot.estimated_net_consumption_kwh:.6f} != house+ev-pv="
                    f"{expected:.6f}",
                )
            )
    return violations


def _check_soc_bounds(
    inp: PlannerInput, out: PlannerOutput
) -> list[InvariantViolation]:
    """Check that simulated SoC never leaves the configured bounds."""
    floor, ceiling = _effective_soc_bounds(inp)
    violations: list[InvariantViolation] = []
    for slot in _actionable(out.slots):
        soc = slot.estimated_battery_soc_pct
        if soc < floor - _EPS or soc > ceiling + _EPS:
            violations.append(
                InvariantViolation(
                    "soc_bounds",
                    f"{slot.start.isoformat()} soc={soc:.3f}% outside "
                    f"[{floor:.3f}, {ceiling:.3f}]",
                )
            )
    return violations


def _check_flow_signs(
    _inp: PlannerInput, out: PlannerOutput
) -> list[InvariantViolation]:
    """Check that every energy flow is non-negative and single-directional."""
    violations: list[InvariantViolation] = []
    for slot in out.slots:
        flows = {
            "batteries_charged_kwh": slot.batteries_charged_kwh,
            "batteries_discharged_kwh": slot.batteries_discharged_kwh,
            "grid_import_kwh": slot.grid_import_kwh,
            "grid_export_kwh": slot.grid_export_kwh,
        }
        for name, value in flows.items():
            if value < -_EPS:
                violations.append(
                    InvariantViolation(
                        "non_negative_flows",
                        f"{slot.start.isoformat()} {name}={value:.6f}",
                    )
                )
        if (
            slot.grid_import_kwh > _MATERIAL_KWH
            and slot.grid_export_kwh > _MATERIAL_KWH
        ):
            violations.append(
                InvariantViolation(
                    "grid_direction_exclusive",
                    f"{slot.start.isoformat()} imports "
                    f"{slot.grid_import_kwh:.3f} and exports "
                    f"{slot.grid_export_kwh:.3f} kWh in the same slot",
                )
            )
        if (
            slot.batteries_charged_kwh > _MATERIAL_KWH
            and slot.batteries_discharged_kwh > _MATERIAL_KWH
        ):
            violations.append(
                InvariantViolation(
                    "battery_direction_exclusive",
                    f"{slot.start.isoformat()} charges "
                    f"{slot.batteries_charged_kwh:.3f} and discharges "
                    f"{slot.batteries_discharged_kwh:.3f} kWh in the same slot",
                )
            )
    return violations


def _check_export_attribution(
    _inp: PlannerInput, out: PlannerOutput
) -> list[InvariantViolation]:
    """Check that battery-origin plus direct-PV export equals total export."""
    violations: list[InvariantViolation] = []
    for slot in out.slots:
        attributed = slot.primary_battery_export_kwh + slot.pv_export_kwh
        if abs(attributed - slot.grid_export_kwh) > _EPS:
            violations.append(
                InvariantViolation(
                    "export_attribution",
                    f"{slot.start.isoformat()} battery+pv export="
                    f"{attributed:.6f} != grid_export="
                    f"{slot.grid_export_kwh:.6f}",
                )
            )
    return violations


def _check_recommendations(
    _inp: PlannerInput, out: PlannerOutput
) -> list[InvariantViolation]:
    """Check that every slot carries a known recommendation value."""
    violations: list[InvariantViolation] = []
    for slot in out.slots:
        if slot.recommendation not in _KNOWN_RECOMMENDATIONS:
            violations.append(
                InvariantViolation(
                    "known_recommendation",
                    f"{slot.start.isoformat()} recommendation={slot.recommendation!r}",
                )
            )
    return violations


def _check_plan_self_consistency(
    _inp: PlannerInput, out: PlannerOutput
) -> list[InvariantViolation]:
    """Check each slot's label against its energy fields (issue #1035).

    Delegates to the planner's own contract table so the harness cannot drift
    from the gate that ships to users.
    """
    return [
        InvariantViolation("plan_self_consistency", detail)
        for detail in check_plan_self_consistency(out.slots)
    ]


def _check_no_post_selection_mutation(
    _inp: PlannerInput, out: PlannerOutput
) -> list[InvariantViolation]:
    """Check that the published plan is exactly the candidate that won.

    Covers three spec bullets at once: ``winner.cost == final_output.cost``,
    ``final output slots equal selected candidate slots``, and ``no
    post-selection mutation happens without re-score``.

    The engine currently publishes the winning candidate's own slot list, so
    the slot half holds by object identity and cannot fail today.  It is kept
    because that is an implementation detail, not a guarantee: the moment any
    pass copies or rewrites the published slots — the fix issue #1033
    explicitly rejected — this is what notices.
    """
    if not out.candidates:
        return []
    winners = [c for c in out.candidates if c.name == out.winner_name]
    if not winners:
        return [
            InvariantViolation(
                "winner_present",
                f"winner {out.winner_name!r} is not among the "
                f"{len(out.candidates)} evaluated candidates",
            )
        ]
    winner = winners[0]
    violations: list[InvariantViolation] = []
    if out.plan_cost is None:
        violations.append(
            InvariantViolation(
                "winner_cost_identity",
                f"plan produced {len(out.slots)} slots but carries no plan_cost",
            )
        )
    elif winner._cost is None:
        violations.append(
            InvariantViolation(
                "winner_cost_identity",
                f"winner {winner.name!r} carries no scored cost",
            )
        )
    else:
        for term in ("total_cost", "score"):
            published = getattr(out.plan_cost, term)
            scored = getattr(winner._cost, term)
            if abs(published - scored) > _EPS:
                violations.append(
                    InvariantViolation(
                        "winner_cost_identity",
                        f"published {term}={published:.6f} != winner "
                        f"{winner.name!r} {term}={scored:.6f}",
                    )
                )
    if winner.slots != out.slots:
        violations.append(
            InvariantViolation(
                "winner_slots_identity",
                f"published slots differ from winner {winner.name!r} slots",
            )
        )
    return violations


def _check_winner_beats_no_action(
    _inp: PlannerInput, out: PlannerOutput
) -> list[InvariantViolation]:
    """Check the winner scores no worse than the no-action baseline.

    The selector minimises ``score``, not ``total_cost`` — a plan may spend
    more money in-horizon and still win by leaving the battery fuller.
    """
    if out.plan_cost is None:
        return []
    baselines = [
        c
        for c in out.candidates
        if c.name in ("no_action", "baseline") and c._cost is not None
    ]
    if not baselines:
        return []
    best_baseline = min(c._cost.score for c in baselines)
    if out.plan_cost.score > best_baseline + _EPS:
        return [
            InvariantViolation(
                "winner_not_worse_than_no_action",
                f"winner {out.winner_name!r} score="
                f"{out.plan_cost.score:.6f} > no-action score="
                f"{best_baseline:.6f}",
            )
        ]
    return []


def _check_terminal_soc(
    _inp: PlannerInput, out: PlannerOutput
) -> list[InvariantViolation]:
    """Check the reported terminal SoC matches the simulated trajectory."""
    actionable = _actionable(out.slots)
    if not actionable:
        return []
    final = actionable[-1].estimated_battery_soc_pct
    if abs(out.battery_soc_at_end - final) > _EPS:
        return [
            InvariantViolation(
                "terminal_soc_reported",
                f"battery_soc_at_end={out.battery_soc_at_end:.3f}% != final "
                f"slot soc={final:.3f}%",
            )
        ]
    return []


def _check_missing_data_reported(
    _inp: PlannerInput, out: PlannerOutput
) -> list[InvariantViolation]:
    """Check that an all-zero price day is reported, not silently planned.

    The spec forbids missing price data becoming real zero silently.  A whole
    calendar day of exactly-zero import prices is the signature of that
    failure, so it must be matched by a ``DataQuality`` report for that day.
    """
    by_day: dict[int, list[PlannedSlot]] = {}
    if not out.slots:
        return []
    first_day = out.slots[0].start.date()
    for slot in out.slots:
        offset = (slot.start.date() - first_day).days
        by_day.setdefault(offset, []).append(slot)

    reported: dict[int, list[int]] = {
        0: out.data_quality.today_price_missing_hours,
        1: out.data_quality.tomorrow_price_missing_hours,
        2: out.data_quality.day2_price_missing_hours,
    }
    violations: list[InvariantViolation] = []
    for offset, slots in sorted(by_day.items()):
        if any(abs(s.price.import_price) > _EPS for s in slots):
            continue
        if reported.get(offset):
            continue
        violations.append(
            InvariantViolation(
                "missing_price_reported",
                f"day_offset={offset} has zero import prices on all "
                f"{len(slots)} slots but DataQuality reports no gap",
            )
        )
    return violations


#: Every invariant checker, in report order.
_CHECKS: tuple[
    Callable[[PlannerInput, PlannerOutput], list[InvariantViolation]], ...
] = (
    _check_slot_grid,
    _check_energy_balance,
    _check_soc_bounds,
    _check_flow_signs,
    _check_export_attribution,
    _check_recommendations,
    _check_plan_self_consistency,
    _check_no_post_selection_mutation,
    _check_winner_beats_no_action,
    _check_terminal_soc,
    _check_missing_data_reported,
)


def check_invariants(inp: PlannerInput, out: PlannerOutput) -> list[InvariantViolation]:
    """Check one planning cycle against the ``docs/planner-spec.md`` invariants.

    Args:
        inp: The input the plan was produced from.
        out: The plan the engine produced for it.

    Returns:
        Every violation found, in check order.  An empty list means the cycle
        satisfies every invariant this harness knows how to check.
    """
    violations: list[InvariantViolation] = []
    for check in _CHECKS:
        violations.extend(check(inp, out))
    return violations


def format_violations(violations: list[InvariantViolation]) -> str:
    """Render violations as a readable multi-line block.

    Args:
        violations: The list returned by :func:`check_invariants`.

    Returns:
        One line per violation, or a short "none" note when the list is empty.
    """
    if not violations:
        return "no invariant violations"
    return "\n".join(f"  - {v}" for v in violations)
