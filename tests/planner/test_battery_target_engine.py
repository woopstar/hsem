"""House-battery target through the full planner engine (issue #1109).

End-to-end checks on ``run_planner``: the feature is inert when disabled, the
reporter's "63 % at 10:00" day exports now and still reaches the target, the
MILP plan beats ``passive`` on score, and the money figure never carries the
selector-only target penalty.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.models.price_point import PricePoint
from custom_components.hsem.models.solcast_slot import SolcastSlot
from custom_components.hsem.planner import candidate_generator, run_planner
from custom_components.hsem.planner.candidate_generator import CandidatePlan
from custom_components.hsem.planner.cost_function import PlanCostBreakdown
from custom_components.hsem.planner.milp_optimizer import is_scipy_available
from tests.planner.fixtures import make_summer_day_input

pytestmark = pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available in this environment"
)

_NOW_ISO = "2024-07-15T10:00:00+02:00"


def _inp(
    *,
    enabled: bool,
    target_pct: float = 100.0,
    target_time: str = "17:00:00",
    sunny: bool = True,
) -> PlannerInput:
    """10:00 on a summer day, battery at 63 %, export pays 1.40 right now.

    Import is a flat 0.60 and export 0.58 after the first hour, so storing PV
    for the evening is not worth it: the normal plan exports the surplus and
    leaves the battery at 63 %.
    """
    base = make_summer_day_input(now_iso=_NOW_ISO, battery_soc_pct=63.0)
    prices = [
        PricePoint(
            hour=hour, import_price=0.60, export_price=1.40 if hour == 10 else 0.58
        )
        for hour in range(24)
    ]
    changes: dict[str, object] = {
        "price_points": prices,
        "excess_export_enabled": False,
        "battery_target_soc_enabled": enabled,
        "battery_target_soc_pct": target_pct,
        "battery_target_soc_time": target_time,
    }
    if not sunny:
        changes["solcast_slots"] = [
            SolcastSlot(hour=hour, pv_estimate=0.0) for hour in range(24)
        ]
    return replace(base, **changes)  # type: ignore[arg-type]  # field overrides


def _candidate(output: PlannerOutput, name: str) -> CandidatePlan:
    candidate: CandidatePlan = next(c for c in output.candidates if c.name == name)
    return candidate


def _cost(output: PlannerOutput, name: str) -> PlanCostBreakdown:
    """Return the selector's cost breakdown for candidate *name*."""
    cost = _candidate(output, name)._cost
    assert cost is not None
    return cost


def _slot_at(output: PlannerOutput, hour: int) -> PlannedSlot:
    return next(s for s in output.slots if s.start.day == 15 and s.start.hour == hour)


def test_disabled_target_leaves_the_plan_bit_for_bit_unchanged() -> None:
    """Target settings must not leak into the plan while the feature is off."""
    default = run_planner(_inp(enabled=False))
    configured = run_planner(
        _inp(enabled=False, target_pct=55.0, target_time="12:00:00")
    )

    assert configured.slots == default.slots
    assert configured.plan_cost == default.plan_cost
    assert configured.winner_name == default.winner_name
    assert configured.battery_target is None
    assert default.plan_cost is not None
    assert default.plan_cost.battery_target_penalty == pytest.approx(0.0)
    for name in ("passive", "milp"):
        assert _cost(configured, name) == _cost(default, name)
    milp_diagnostics = _candidate(configured, "milp").diagnostics
    assert milp_diagnostics is not None
    assert "battery_target" not in milp_diagnostics


def test_target_the_normal_plan_already_meets_changes_nothing() -> None:
    """Stage 2 is skipped; the plan equals the disabled plan."""
    disabled = run_planner(_inp(enabled=False))
    met = run_planner(_inp(enabled=True, target_pct=63.0))

    assert met.slots == disabled.slots
    assert met.battery_target is not None
    assert met.battery_target["stage2_ran"] is False
    assert met.battery_target["stage2_status"] == "target_met"
    assert met.battery_target["shortfall_kwh"] == pytest.approx(0.0)
    assert met.plan_cost is not None and disabled.plan_cost is not None
    assert met.plan_cost.total_cost == pytest.approx(disabled.plan_cost.total_cost)
    assert met.plan_cost.battery_target_penalty == pytest.approx(0.0)


def test_reporters_deadline_day_exports_now_and_selects_the_milp_plan() -> None:
    """63 % at 10:00, 1.40 export now, plenty of PV later: export now, fill later."""
    normal = run_planner(_inp(enabled=False))
    output = run_planner(_inp(enabled=True))

    # The normal plan leaves the battery where it is.
    assert _slot_at(normal, 16).estimated_battery_soc_pct == pytest.approx(63.0)

    report = output.battery_target
    assert report is not None
    assert report["target_time"] == "2024-07-15T17:00:00+02:00"
    assert report["target_kwh"] == pytest.approx(9.0)
    assert report["stage1_projected_kwh"] == pytest.approx(5.3)
    assert report["stage2_ran"] is True
    assert report["stage2_status"] == "solved"
    assert report["projected_kwh"] == pytest.approx(9.0, abs=1e-2)
    assert report["shortfall_kwh"] == pytest.approx(0.0, abs=1e-2)
    assert report["selected_projected_kwh"] == pytest.approx(9.0, abs=1e-2)
    assert report["max_import_delta_kwh"] == pytest.approx(0.0, abs=2e-3)

    # The well-paid 10:00 surplus is still exported in full …
    now_slot, normal_now_slot = _slot_at(output, 10), _slot_at(normal, 10)
    assert now_slot.batteries_charged_kwh == pytest.approx(0.0, abs=1e-3)
    assert now_slot.grid_export_kwh == pytest.approx(normal_now_slot.grid_export_kwh)
    # … and the battery is full by 17:00.
    assert _slot_at(output, 16).estimated_battery_soc_pct == pytest.approx(
        100.0, abs=0.2
    )
    # No grid energy is bought for it.
    assert sum(s.grid_import_kwh for s in output.slots) == pytest.approx(
        sum(s.grid_import_kwh for s in normal.slots), abs=2e-3
    )

    # The MILP plan wins, and beats `passive` (which fills as soon as it can).
    assert output.winner_name == "milp"
    milp, passive = _cost(output, "milp"), _cost(output, "passive")
    assert passive.battery_target_penalty == pytest.approx(0.0)
    assert milp.score < passive.score - 1.0

    # winner.cost == final_output.cost, and the penalty never enters money.
    assert output.plan_cost is not None
    assert output.plan_cost.total_cost == pytest.approx(milp.total_cost)
    assert output.plan_cost.score == pytest.approx(milp.score)
    assert output.plan_cost.total_cost == pytest.approx(
        output.plan_cost.import_cost
        - output.plan_cost.export_revenue
        + output.plan_cost.cycle_cost
    )


def test_unmet_target_is_scored_but_is_not_money() -> None:
    """No PV at all: the shortfall is priced in ``score`` only, no grid bought."""
    normal = run_planner(_inp(enabled=False, sunny=False))
    output = run_planner(_inp(enabled=True, sunny=False))

    report = output.battery_target
    assert report is not None
    assert report["stage2_ran"] is True
    assert report["shortfall_kwh"] > 3.0
    assert report["projected_kwh"] == pytest.approx(
        report["stage1_projected_kwh"], abs=1e-2
    )
    assert sum(s.grid_import_kwh for s in output.slots) == pytest.approx(
        sum(s.grid_import_kwh for s in normal.slots), abs=2e-3
    )

    cost = output.plan_cost
    assert cost is not None and normal.plan_cost is not None
    assert cost.battery_target_penalty == pytest.approx(
        report["penalty_per_kwh"] * report["selected_shortfall_kwh"], abs=1e-2
    )
    assert cost.battery_target_penalty > 1.0
    assert cost.total_cost == pytest.approx(normal.plan_cost.total_cost, abs=1e-2)
    assert cost.score == pytest.approx(
        normal.plan_cost.score + cost.battery_target_penalty, abs=1e-2
    )
    # Every scored candidate carries the same penalty term.
    for name in ("no_action", "passive", "milp"):
        assert _cost(output, name).battery_target_penalty > 1.0


def test_without_a_milp_plan_the_selected_plan_is_still_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The passive fallback reports its own projection against the target."""
    monkeypatch.setattr(candidate_generator, "is_scipy_available", lambda: False)

    output = run_planner(_inp(enabled=True))

    assert output.winner_name == "passive"
    report = output.battery_target
    assert report is not None
    assert report["stage2_ran"] is False
    assert report["stage2_status"] == "milp_unavailable"
    assert report["selected_projected_kwh"] == pytest.approx(9.0, abs=1e-2)
    assert report["selected_shortfall_kwh"] == pytest.approx(0.0, abs=1e-2)
