"""Two-stage house-battery target solve (issues #1109, #1203).

The target is an opt-in preference agreed with the reporter: build a reserve
towards a target SoC by a daily deadline using **only PV the normal plan
would otherwise export**.  It must never increase grid import, never reduce
or replace grid import the normal plan already needs, never weaken existing
discharge, never cancel a sale of battery energy (issue #1203), and it is a
*deadline*, not "charge as soon as possible".

Every scenario solves the normal plan (stage 1) and the target plan side by
side and compares them slot for slot.
"""

from __future__ import annotations

import random
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.models.ev_config import EVConfig
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.planner.battery_target import (
    BatteryTargetSpec,
    next_target_slot_index,
    target_penalty_per_kwh,
)
from custom_components.hsem.planner.cost_function import CostWeights, score_plan
from custom_components.hsem.planner.milp import _battery_target, _incumbent
from custom_components.hsem.planner.milp._battery_target import (
    IMPORT_ZERO_TOLERANCE_KWH,
    build_target_rows,
    solve_milp_with_battery_target,
)
from custom_components.hsem.planner.milp._objective import ev_deadline_penalty_per_kwh
from custom_components.hsem.planner.milp._past_target_reservation import (
    solve_milp_with_past_target_reservation,
)
from custom_components.hsem.planner.milp_optimizer import is_scipy_available
from custom_components.hsem.utils.datetime_utils import future_slot_indices
from custom_components.hsem.utils.prices import SlotPrice

_TZ = ZoneInfo("Europe/Copenhagen")
_MIDNIGHT = datetime(2026, 9, 14, 0, 0, tzinfo=_TZ)
_USABLE_KWH = 10.0
_CHARGE_EFF = 0.97
_DISCHARGE_EFF = 0.97

#: Published flows are rounded to 3 decimals, so two solves that agree on the
#: LP value can differ by one rounding step each.
_PUBLISHED_TOL = 2e-3

_BATTERY: dict[str, Any] = {
    "usable_kwh": _USABLE_KWH,
    "max_charge_per_slot": 2.5,
    "max_discharge_per_slot": 2.5,
    "cycle_cost_per_kwh": 0.02,
    "charge_efficiency_pct": 97.0,
    "discharge_efficiency_pct": 97.0,
    "no_export": True,
}

pytestmark = pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available in this environment"
)


def _day(
    pv: list[float],
    house: list[float],
    import_price: list[float],
    export_price: list[float],
    *,
    start: datetime = _MIDNIGHT,
) -> list[PlannedSlot]:
    """Build hourly slots from per-hour forecast and price lists."""
    slots: list[PlannedSlot] = []
    for i, (pv_kwh, house_kwh, imp, exp) in enumerate(
        zip(pv, house, import_price, export_price, strict=True)
    ):
        slot_start = start + timedelta(hours=i)
        slot = PlannedSlot(
            start=slot_start,
            end=slot_start + timedelta(hours=1),
            price=SlotPrice(import_price=imp, export_price=exp),
        )
        slot.avg_house_consumption_kwh = house_kwh
        slot.solcast_pv_estimate_kwh = pv_kwh
        slot.ev_planned_load_kwh = 0.0
        slot.ev_accounted_load_kwh = 0.0
        slot.ev_total_planned_load_kwh = 0.0
        slot.estimated_net_consumption_kwh = house_kwh - pv_kwh
        slots.append(slot)
    return slots


def _spec(
    slots: list[PlannedSlot],
    now: datetime,
    *,
    target_kwh: float = _USABLE_KWH,
    at: time = time(17, 0),
    ev_configs: list[EVConfig] | None = None,
) -> BatteryTargetSpec:
    """Resolve the next occurrence of *at* into a spec, like the engine does."""
    resolved = next_target_slot_index(slots, now, at)
    assert resolved is not None
    occurrence, slot_index = resolved
    future_idx = future_slot_indices((s.end for s in slots), now)
    return BatteryTargetSpec(
        target_time=occurrence,
        slot_end=slots[slot_index].end,
        target_pct=100.0,
        target_kwh=target_kwh,
        penalty_per_kwh=target_penalty_per_kwh(
            slots,
            future_idx,
            future_idx.index(slot_index),
            charge_efficiency_pct=_BATTERY["charge_efficiency_pct"],
            cycle_cost_per_kwh=_BATTERY["cycle_cost_per_kwh"],
            ev_configs=ev_configs,
        ),
    )


def _solve(
    slots: list[PlannedSlot],
    now: datetime,
    spec: BatteryTargetSpec | None,
    *,
    current_kwh: float,
    ev_configs: list[EVConfig] | None = None,
    **overrides: Any,
) -> tuple[list[PlannedSlot], dict[str, Any]]:
    result = solve_milp_with_battery_target(
        slots,
        now,
        battery_target=spec,
        current_kwh=current_kwh,
        ev_configs=ev_configs,
        **{**_BATTERY, **overrides},
    )
    assert result is not None
    return result


def _soc(slots: list[PlannedSlot], current_kwh: float) -> list[float]:
    """Return the end-of-slot inventory (model kWh) for every slot."""
    trajectory: list[float] = []
    soc = current_kwh
    for slot in slots:
        soc += slot.batteries_charged_kwh - slot.batteries_discharged_kwh
        trajectory.append(soc)
    return trajectory


def _target_index(slots: list[PlannedSlot], spec: BatteryTargetSpec) -> int:
    return next(i for i, s in enumerate(slots) if s.end == spec.slot_end)


def _assert_import_rule(
    stage1: list[PlannedSlot], stage2: list[PlannedSlot], target_index: int
) -> None:
    """Import is pinned for every slot ``t ≤ T`` and never higher after it."""
    for i, (one, two) in enumerate(zip(stage1, stage2, strict=True)):
        if i <= target_index:
            assert two.grid_import_kwh == pytest.approx(
                one.grid_import_kwh, abs=_PUBLISHED_TOL
            ), f"slot {i}: import changed inside the build window"
        else:
            assert two.grid_import_kwh <= one.grid_import_kwh + _PUBLISHED_TOL, (
                f"slot {i}: import increased after the target"
            )


def _assert_battery_export_rule(
    stage1: list[PlannedSlot], stage2: list[PlannedSlot], target_index: int
) -> None:
    """Battery energy stage 1 sells in ``t ≤ T`` is still sold (issue #1203)."""
    for i in range(target_index + 1):
        assert stage2[i].grid_export_kwh >= (
            stage1[i].primary_battery_export_kwh - _PUBLISHED_TOL
        ), f"slot {i}: battery export of the normal plan was cancelled"


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def _deadline_day() -> tuple[list[PlannedSlot], datetime]:
    """10:00, battery at 63 %, an attractive export price right now.

    3 kWh of PV per hour until 17:00 against a 0.5 kWh house load.  Export
    pays 1.40 in the first hour and 0.30 afterwards; grid costs 0.25 all
    evening, so the normal plan has no reason to fill the battery.
    """
    start = _MIDNIGHT.replace(hour=10)
    n = 24
    pv = [3.0] * 7 + [0.0] * (n - 7)
    house = [0.5] * n
    import_price = [0.25] * n
    export_price = [1.40] + [0.30] * (n - 1)
    return _day(pv, house, import_price, export_price, start=start), start


def _grid_charge_day() -> tuple[list[PlannedSlot], datetime]:
    """Reporter's "62 %" shape: the normal plan needs grid charging.

    Cheap night grid (0.10) before an expensive 06:00-10:00 morning (1.00)
    makes the normal plan grid-charge overnight.  A modest midday PV surplus
    is exported at 0.40 because the 0.30 evening import is not worth storing
    for.  PV alone can never reach 100 %.
    """
    n = 24
    pv = [0.0] * 11 + [1.5] * 4 + [0.0] * (n - 15)
    house = [0.2] * 6 + [1.5] * 4 + [0.5] * (n - 10)
    import_price = [0.10] * 6 + [1.00] * 4 + [0.30] * (n - 10)
    export_price = [0.02] * 11 + [0.40] * 4 + [0.02] * (n - 15)
    return _day(pv, house, import_price, export_price), _MIDNIGHT


# ---------------------------------------------------------------------------
# Disabled / skipped
# ---------------------------------------------------------------------------


def test_disabled_target_is_identical_to_the_normal_plan() -> None:
    """Without a target the wrapper is exactly the existing solve."""
    slots, now = _deadline_day()
    normal = solve_milp_with_past_target_reservation(
        slots, now, current_kwh=6.3, **_BATTERY
    )
    assert normal is not None

    plan, diagnostics = _solve(slots, now, None, current_kwh=6.3)

    assert plan == normal[0]
    assert diagnostics == normal[1]
    assert "battery_target" not in diagnostics


def test_stage2_is_skipped_when_the_normal_plan_meets_the_target() -> None:
    """A target the normal plan already reaches returns stage 1 unchanged."""
    slots, now = _deadline_day()
    normal, _ = _solve(slots, now, None, current_kwh=6.3)
    reached = _soc(normal, 6.3)[6]

    spec = _spec(slots, now, target_kwh=reached - 0.5)
    plan, diagnostics = _solve(slots, now, spec, current_kwh=6.3)

    assert plan == normal
    report = diagnostics["battery_target"]
    assert report["stage2_ran"] is False
    assert report["stage2_status"] == "target_met"
    assert report["shortfall_kwh"] == pytest.approx(0.0)
    assert report["stage1_projected_kwh"] == pytest.approx(reached, abs=1e-3)


def test_target_slot_outside_the_solve_returns_stage1() -> None:
    """A spec whose slot is not a future slot leaves the normal plan alone."""
    slots, now = _deadline_day()
    normal, _ = _solve(slots, now, None, current_kwh=6.3)
    spec = BatteryTargetSpec(
        target_time=now - timedelta(hours=3),
        slot_end=now - timedelta(hours=3),
        target_pct=100.0,
        target_kwh=_USABLE_KWH,
        penalty_per_kwh=1.5,
    )

    plan, diagnostics = _solve(slots, now, spec, current_kwh=6.3)

    assert plan == normal
    assert diagnostics["battery_target"]["stage2_status"] == "no_occurrence"
    assert diagnostics["battery_target"]["stage2_ran"] is False


# ---------------------------------------------------------------------------
# Deadline, not ASAP (reporter's 63 %-at-10:00 scenario)
# ---------------------------------------------------------------------------


def test_deadline_exports_now_and_still_reaches_the_target() -> None:
    """High export price now + enough surplus later: export now, fill later."""
    slots, now = _deadline_day()
    normal, _ = _solve(slots, now, None, current_kwh=6.3)
    spec = _spec(slots, now)

    plan, diagnostics = _solve(slots, now, spec, current_kwh=6.3)
    target_index = _target_index(slots, spec)

    # The normal plan stops short of the target …
    assert _soc(normal, 6.3)[target_index] < _USABLE_KWH - 1.0
    # … the target plan reaches it by 17:00 …
    assert _soc(plan, 6.3)[target_index] == pytest.approx(_USABLE_KWH, abs=1e-2)
    # … while the 1.40 hour still exports its whole PV surplus.
    assert plan[0].batteries_charged_kwh == pytest.approx(0.0, abs=1e-3)
    assert plan[0].grid_export_kwh == pytest.approx(2.5, abs=1e-3)
    _assert_import_rule(normal, plan, target_index)

    report = diagnostics["battery_target"]
    assert report["stage2_ran"] is True
    assert report["stage2_status"] == "solved"
    assert report["shortfall_kwh"] == pytest.approx(0.0, abs=1e-2)
    assert report["projected_kwh"] > report["stage1_projected_kwh"]
    assert report["max_import_delta_kwh"] == pytest.approx(0.0, abs=_PUBLISHED_TOL)
    assert report["max_import_increase_after_kwh"] == pytest.approx(0.0, abs=1e-3)


def test_surplus_above_the_target_is_still_exported() -> None:
    """Nothing is stored beyond the target: the rest of the PV is exported."""
    slots, now = _deadline_day()
    spec = _spec(slots, now, target_kwh=8.0)

    plan, _ = _solve(slots, now, spec, current_kwh=6.3)
    target_index = _target_index(slots, spec)

    assert _soc(plan, 6.3)[target_index] == pytest.approx(8.0, abs=1e-2)
    stored_ac = sum(s.batteries_charged_kwh for s in plan[:7]) / _CHARGE_EFF
    exported = sum(s.grid_export_kwh for s in plan[:7])
    assert exported == pytest.approx(7 * 2.5 - stored_ac, abs=1e-2)


def test_pv_is_kept_when_later_surplus_is_insufficient() -> None:
    """Too little surplus later: the current, well-paid surplus is stored too."""
    start = _MIDNIGHT.replace(hour=10)
    n = 24
    pv = [3.0, 1.5, 0.5, 0.5, 0.5, 0.5, 0.5] + [0.0] * (n - 7)
    house = [0.5] * n
    slots = _day(pv, house, [0.25] * n, [1.40] + [0.30] * (n - 1), start=start)
    normal, _ = _solve(slots, start, None, current_kwh=6.3)
    spec = _spec(slots, start)

    plan, diagnostics = _solve(slots, start, spec, current_kwh=6.3)
    target_index = _target_index(slots, spec)

    assert normal[0].batteries_charged_kwh == pytest.approx(0.0, abs=1e-3)
    # Only 1 kWh of surplus follows the first hour, so the first hour's
    # surplus is stored despite its 1.40 export price.
    assert plan[0].batteries_charged_kwh == pytest.approx(2.5 * _CHARGE_EFF, abs=1e-2)
    assert plan[1].batteries_charged_kwh == pytest.approx(1.0 * _CHARGE_EFF, abs=1e-2)
    assert sum(s.grid_export_kwh for s in plan[:7]) == pytest.approx(0.0, abs=1e-2)
    assert diagnostics["battery_target"]["shortfall_kwh"] > 0.1
    _assert_import_rule(normal, plan, target_index)


def test_no_surplus_keeps_the_normal_plan_and_reports_the_shortfall() -> None:
    """No PV surplus: SoC stays where the normal plan left it, no grid bought."""
    start = _MIDNIGHT.replace(hour=10)
    n = 24
    slots = _day(
        [0.2] * 7 + [0.0] * (n - 7), [0.5] * n, [0.25] * n, [0.30] * n, start=start
    )
    normal, _ = _solve(slots, start, None, current_kwh=6.3)
    spec = _spec(slots, start)

    plan, diagnostics = _solve(slots, start, spec, current_kwh=6.3)
    target_index = _target_index(slots, spec)

    assert _soc(plan, 6.3)[target_index] == pytest.approx(
        _soc(normal, 6.3)[target_index], abs=1e-2
    )
    assert sum(s.grid_import_kwh for s in plan) == pytest.approx(
        sum(s.grid_import_kwh for s in normal), abs=1e-2
    )
    report = diagnostics["battery_target"]
    assert report["stage2_ran"] is True
    assert report["shortfall_kwh"] == pytest.approx(
        _USABLE_KWH - _soc(normal, 6.3)[target_index], abs=1e-2
    )
    _assert_import_rule(normal, plan, target_index)


# ---------------------------------------------------------------------------
# Grid import: never increased, never reduced or replaced
# ---------------------------------------------------------------------------


def test_existing_grid_charging_is_kept_and_no_grid_is_bought_for_the_gap() -> None:
    """Reporter's 62 % example: grid covers the normal plan, PV the extra."""
    slots, now = _grid_charge_day()
    normal, _ = _solve(slots, now, None, current_kwh=0.5)
    spec = _spec(slots, now)

    plan, diagnostics = _solve(slots, now, spec, current_kwh=0.5)
    target_index = _target_index(slots, spec)

    night_charge = sum(s.batteries_charged_kwh for s in normal[:6])
    assert night_charge > 3.0, "the normal plan must grid-charge overnight"
    # The overnight grid charging is exactly what it was.
    for one, two in zip(normal[:6], plan[:6], strict=True):
        assert two.batteries_charged_kwh == pytest.approx(
            one.batteries_charged_kwh, abs=_PUBLISHED_TOL
        )
        assert two.grid_import_kwh == pytest.approx(
            one.grid_import_kwh, abs=_PUBLISHED_TOL
        )
    _assert_import_rule(normal, plan, target_index)
    # Otherwise-exported PV lifts the battery above the normal plan …
    assert _soc(plan, 0.5)[target_index] > _soc(normal, 0.5)[target_index] + 2.0
    assert sum(s.grid_export_kwh for s in plan) < sum(s.grid_export_kwh for s in normal)
    # … but the remaining gap to 100 % is not bought from the grid.
    assert diagnostics["battery_target"]["shortfall_kwh"] > 1.0
    assert sum(s.grid_import_kwh for s in plan) <= (
        sum(s.grid_import_kwh for s in normal) + _PUBLISHED_TOL
    )


def test_morning_discharge_window_is_unchanged_on_a_low_pv_day() -> None:
    """Battery coverage of the 06:00-10:00 load is not traded for the target."""
    n = 24
    pv = [0.0] * 11 + [0.8] * 4 + [0.0] * (n - 15)
    house = [0.2] * 6 + [1.5] * 4 + [0.5] * (n - 10)
    import_price = [0.40] * 6 + [1.00] * 4 + [0.30] * (n - 10)
    export_price = [0.02] * 11 + [0.40] * 4 + [0.02] * (n - 15)
    slots = _day(pv, house, import_price, export_price)
    normal, _ = _solve(slots, _MIDNIGHT, None, current_kwh=7.0)
    spec = _spec(slots, _MIDNIGHT)

    plan, _ = _solve(slots, _MIDNIGHT, spec, current_kwh=7.0)
    target_index = _target_index(slots, spec)

    assert sum(s.batteries_discharged_kwh for s in normal[6:10]) > 4.0
    for i in range(target_index + 1):
        assert plan[i].batteries_discharged_kwh >= (
            normal[i].batteries_discharged_kwh - _PUBLISHED_TOL
        ), f"slot {i}: battery covers less house load than the normal plan"
    _assert_import_rule(normal, plan, target_index)
    assert _soc(plan, 7.0)[target_index] > _soc(normal, 7.0)[target_index] + 0.5


@pytest.mark.parametrize("seed", range(20))
def test_import_is_pinned_in_the_window_and_never_higher_after(seed: int) -> None:
    """Property: random days never move grid import the wrong way."""
    rng = random.Random(seed)
    n = 30
    start = _MIDNIGHT.replace(hour=rng.randint(0, 12))
    pv = [
        round(rng.uniform(0.0, 4.0), 2) if 8 <= (start.hour + i) % 24 <= 17 else 0.0
        for i in range(n)
    ]
    house = [round(rng.uniform(0.1, 1.8), 2) for _ in range(n)]
    import_price = [round(rng.uniform(0.05, 1.2), 3) for _ in range(n)]
    export_price = [round(p * rng.uniform(0.2, 0.95), 3) for p in import_price]
    slots = _day(pv, house, import_price, export_price, start=start)
    current_kwh = round(rng.uniform(0.0, _USABLE_KWH), 2)
    no_export = rng.random() < 0.5
    normal, normal_diag = _solve(
        slots, start, None, current_kwh=current_kwh, no_export=no_export
    )
    spec = _spec(slots, start, target_kwh=round(rng.uniform(5.0, _USABLE_KWH), 2))

    plan, diagnostics = _solve(
        slots, start, spec, current_kwh=current_kwh, no_export=no_export
    )
    target_index = _target_index(slots, spec)

    _assert_import_rule(normal, plan, target_index)
    _assert_battery_export_rule(normal, plan, target_index)
    report = diagnostics["battery_target"]
    if report["stage2_ran"]:
        # The solver's own import column obeys the pin exactly.
        tolerance = IMPORT_ZERO_TOLERANCE_KWH + 1e-7
        assert report["stage2_status"] in ("solved", "no_gain")
        for t, (one, two) in enumerate(
            zip(
                normal_diag["lp_grid_import_kwh"],
                diagnostics["lp_grid_import_kwh"],
                strict=True,
            )
        ):
            if t <= target_index:
                assert two == pytest.approx(one, abs=tolerance)
            else:
                assert two <= one + tolerance
        assert _soc(plan, current_kwh)[target_index] >= (
            _soc(normal, current_kwh)[target_index] - _PUBLISHED_TOL
        )
    # SoC bounds hold either way.
    for value in _soc(plan, current_kwh):
        assert -_PUBLISHED_TOL <= value <= _USABLE_KWH + _PUBLISHED_TOL


# ---------------------------------------------------------------------------
# Time-limited / tied solutions cannot swap grid charging for PV
# ---------------------------------------------------------------------------


def test_stage2_model_pins_grid_import_as_hard_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any incumbent HiGHS returns is bound to the stage-1 import, optimal or not."""
    slots, now = _grid_charge_day()
    captured: list[dict[str, Any]] = []
    real = _incumbent.solve_and_validate

    def _capture(linprog: Any, **kwargs: Any) -> Any:
        captured.append(kwargs)
        return real(linprog, **kwargs)

    monkeypatch.setattr(_incumbent, "solve_and_validate", _capture)
    spec = _spec(slots, now)

    _, diagnostics = _solve(slots, now, spec, current_kwh=0.5)

    assert diagnostics["battery_target"]["stage2_ran"] is True
    stage1_model, stage2_model = captured
    assert "battery_target_penalty" not in stage1_model["variable_blocks"]
    offset, width = stage2_model["variable_blocks"]["grid_import"]
    stage1_import = _solve(slots, now, None, current_kwh=0.5)[1]["lp_grid_import_kwh"]
    target_index = _target_index(slots, spec)
    bounds = stage2_model["bounds"][offset : offset + width]
    grid_charged = 0
    for t, (lower, upper) in enumerate(bounds):
        imported = stage1_import[t] > IMPORT_ZERO_TOLERANCE_KWH
        assert upper == pytest.approx(stage1_import[t] if imported else 0.0, abs=1e-12)
        if t <= target_index and imported:
            assert lower == pytest.approx(upper, abs=1e-12)
            grid_charged += 1
        else:
            assert lower == pytest.approx(0.0)
    assert grid_charged >= 6
    assert stage2_model["variable_blocks"]["battery_target_penalty"][1] == 1


def test_tied_grid_and_pv_cost_does_not_replace_grid_charging() -> None:
    """With grid and PV equally cheap, the planned grid charging still stays."""
    n = 24
    pv = [0.0] * 11 + [2.0] * 4 + [0.0] * (n - 15)
    house = [0.2] * 6 + [1.5] * 4 + [0.5] * (n - 10)
    # Storing a kWh of 0.10 night grid costs exactly what storing a kWh of
    # 0.10-export PV does: the swap would be free without the pin.
    import_price = [0.10] * 6 + [1.00] * 4 + [0.05] * (n - 10)
    export_price = [0.0] * 11 + [0.10] * 4 + [0.0] * (n - 15)
    slots = _day(pv, house, import_price, export_price)
    normal, _ = _solve(slots, _MIDNIGHT, None, current_kwh=0.5)
    spec = _spec(slots, _MIDNIGHT)

    plan, _ = _solve(slots, _MIDNIGHT, spec, current_kwh=0.5)

    assert sum(s.grid_import_kwh for s in normal[:6]) > 3.0
    for one, two in zip(normal[:6], plan[:6], strict=True):
        assert two.grid_import_kwh == pytest.approx(
            one.grid_import_kwh, abs=_PUBLISHED_TOL
        )
    _assert_import_rule(normal, plan, _target_index(slots, spec))


def test_rows_leave_export_slots_free_and_cap_after_the_window() -> None:
    """Floor is 0 where stage 1 imported nothing and after the window."""
    spec = BatteryTargetSpec(
        target_time=_MIDNIGHT,
        slot_end=_MIDNIGHT,
        target_pct=100.0,
        target_kwh=9.0,
        penalty_per_kwh=1.2,
    )

    rows = build_target_rows(spec, 1, [0.8, 5e-7, 0.5, -1e-9], [0.0, 1.7, 2.0, 0.0])

    assert rows.grid_import_floor == pytest.approx((0.8, 0.0, 0.0, 0.0))
    assert rows.grid_import_cap == pytest.approx((0.8, 0.0, 0.5, 0.0))
    # Battery export is kept inside the window only (issue #1203).
    assert rows.grid_export_floor == pytest.approx((0.0, 1.7, 0.0, 0.0))
    assert rows.target_index == 1
    assert rows.target_kwh == pytest.approx(9.0)
    assert rows.penalty_per_kwh == pytest.approx(1.2)


# ---------------------------------------------------------------------------
# Battery export of the normal plan is not cancelled (issue #1203)
# ---------------------------------------------------------------------------


def _evening_sale_day(*, pv_at_19: float = 0.0) -> tuple[list[PlannedSlot], datetime]:
    """18:00, battery at 70 %, export pays 0.90 at 19:00-21:00, target by 22:00.

    Import is a flat 0.30 and the house draws 0.5 kWh an hour, so the normal
    plan sells the battery into the evening price.  There is no PV unless
    *pv_at_19* adds a surplus to the first sale hour.
    """
    start = _MIDNIGHT.replace(hour=18)
    n = 24
    pv = [0.0, pv_at_19] + [0.0] * (n - 2)
    house = [0.5] * n
    import_price = [0.30] * n
    export_price = [0.05, 0.90, 0.90] + [0.05] * (n - 3)
    return _day(pv, house, import_price, export_price, start=start), start


_EVENING_SALE: dict[str, Any] = {"current_kwh": 7.0, "no_export": False}


def test_battery_sale_before_the_deadline_is_not_cancelled_without_pv() -> None:
    """No PV in the build window: the target cannot stop a sale of battery energy.

    Before #1203 stage 2 kept the energy stage 1 sold at 19:00-21:00: import
    was unchanged, the target row was served and the penalty outbid the
    export price.  The feature was agreed to cost forgone PV export only.
    """
    slots, now = _evening_sale_day()
    normal, _ = _solve(slots, now, None, **_EVENING_SALE)
    spec = _spec(slots, now, at=time(22, 0))
    target_index = _target_index(slots, spec)

    plan, diagnostics = _solve(slots, now, spec, **_EVENING_SALE)

    sold = sum(s.primary_battery_export_kwh for s in normal[: target_index + 1])
    assert sold > 3.0, "the scenario must sell battery energy before the deadline"
    report = diagnostics["battery_target"]
    assert report["stage2_ran"] is True
    assert report["stage2_status"] == "no_gain"
    assert report["projected_kwh"] == pytest.approx(report["stage1_projected_kwh"])
    assert plan == normal


def test_only_the_pv_share_of_a_mixed_export_slot_is_held_back() -> None:
    """PV surplus and battery export in one slot: the battery's sale stays."""
    slots, now = _evening_sale_day(pv_at_19=2.0)
    normal, _ = _solve(slots, now, None, **_EVENING_SALE)
    spec = _spec(slots, now, at=time(22, 0))
    target_index = _target_index(slots, spec)

    plan, diagnostics = _solve(slots, now, spec, **_EVENING_SALE)

    # The 19:00 slot exports its 1.5 kWh PV surplus and battery energy on top.
    assert normal[1].primary_battery_export_kwh > 1.0
    assert normal[1].grid_export_kwh > normal[1].primary_battery_export_kwh + 1.0
    report = diagnostics["battery_target"]
    assert report["stage2_status"] == "solved"
    _assert_import_rule(normal, plan, target_index)
    _assert_battery_export_rule(normal, plan, target_index)
    # What the target gains is the PV that was exported, and no more.  The
    # 1.5 kWh of PV now covers part of the sale, so the battery discharges
    # 1.5 / η_dis less: no round trip through the battery is needed.
    gained = report["projected_kwh"] - report["stage1_projected_kwh"]
    pv_exported = sum(
        s.grid_export_kwh - s.primary_battery_export_kwh
        for s in normal[: target_index + 1]
    )
    assert pv_exported == pytest.approx(1.5, abs=_PUBLISHED_TOL)
    assert gained == pytest.approx(pv_exported / _DISCHARGE_EFF, abs=_PUBLISHED_TOL)
    assert plan[1].grid_export_kwh == pytest.approx(
        normal[1].primary_battery_export_kwh, abs=_PUBLISHED_TOL
    )


def test_stage2_model_keeps_battery_export_as_hard_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The export floor is a column bound, so any incumbent obeys it."""
    slots, now = _evening_sale_day(pv_at_19=2.0)
    captured: list[dict[str, Any]] = []
    real = _incumbent.solve_and_validate

    def _capture(linprog: Any, **kwargs: Any) -> Any:
        captured.append(kwargs)
        return real(linprog, **kwargs)

    monkeypatch.setattr(_incumbent, "solve_and_validate", _capture)
    spec = _spec(slots, now, at=time(22, 0))

    _, diagnostics = _solve(slots, now, spec, **_EVENING_SALE)

    assert diagnostics["battery_target"]["stage2_ran"] is True
    stage1_model, stage2_model = captured
    offset, width = stage2_model["variable_blocks"]["grid_export"]
    sold = _solve(slots, now, None, **_EVENING_SALE)[1]["lp_battery_export_ac_kwh"]
    target_index = _target_index(slots, spec)
    kept = 0
    for t, (lower, upper) in enumerate(stage2_model["bounds"][offset : offset + width]):
        if t <= target_index and sold[t] > IMPORT_ZERO_TOLERANCE_KWH:
            assert lower == pytest.approx(sold[t], abs=1e-12)
            assert lower <= upper
            kept += 1
        else:
            assert lower == pytest.approx(0.0)
    assert kept >= 2
    # Stage 1 has no export floor at all.
    offset, width = stage1_model["variable_blocks"]["grid_export"]
    assert all(
        lower == pytest.approx(0.0)
        for lower, _upper in stage1_model["bounds"][offset : offset + width]
    )


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


def test_stage2_failure_falls_back_to_the_normal_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed stage-2 solve never costs the planner its plan."""
    slots, now = _deadline_day()
    normal, _ = _solve(slots, now, None, current_kwh=6.3)
    warnings: list[str] = []
    monkeypatch.setattr(_battery_target, "solve_milp", lambda *_a, **_k: None)
    monkeypatch.setattr(
        _battery_target,
        "log_planner",
        lambda level, message, *_a: warnings.append(f"{level}:{message}"),
    )

    plan, diagnostics = _solve(slots, now, _spec(slots, now), current_kwh=6.3)

    assert plan == normal
    report = diagnostics["battery_target"]
    assert report["stage2_ran"] is True
    assert report["stage2_status"] == "failed"
    assert report["projected_kwh"] == report["stage1_projected_kwh"]
    assert any(w.startswith("warning:") and "stage-2" in w for w in warnings)


def test_stage1_failure_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no normal plan there is nothing to build the target on."""
    slots, now = _deadline_day()
    monkeypatch.setattr(
        _battery_target,
        "solve_milp_with_past_target_reservation",
        lambda *_a, **_k: None,
    )

    result = solve_milp_with_battery_target(
        slots, now, battery_target=_spec(slots, now), current_kwh=6.3, **_BATTERY
    )

    assert result is None


@pytest.mark.parametrize("missing", ["lp_grid_import_kwh", "lp_battery_export_ac_kwh"])
def test_missing_stage1_flows_keep_the_normal_plan(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    """Without the stage-1 LP flows there is nothing to pin against."""
    slots, now = _deadline_day()
    real = _battery_target.solve_milp_with_past_target_reservation

    def _without_lp_import(*args: Any, **kwargs: Any) -> Any:
        result = real(*args, **kwargs)
        assert result is not None
        result[1].pop(missing)
        return result

    monkeypatch.setattr(
        _battery_target, "solve_milp_with_past_target_reservation", _without_lp_import
    )
    normal, _ = _solve(slots, now, None, current_kwh=6.3)

    plan, diagnostics = _solve(slots, now, _spec(slots, now), current_kwh=6.3)

    assert plan == normal
    assert diagnostics["battery_target"]["stage2_status"] == "stage1_import_unavailable"
    assert diagnostics["battery_target"]["stage2_ran"] is False


# ---------------------------------------------------------------------------
# EV interaction
# ---------------------------------------------------------------------------


def _deadline_ev(deadline_slot: int) -> EVConfig:
    """A 60 kWh EV 6 kWh short of its target, due at *deadline_slot*."""
    return EVConfig(
        enabled=True,
        initial_soc_kwh=42.0,
        target_kwh=48.0,
        capacity_kwh=60.0,
        max_charge_per_slot=3.0,
        charger_efficiency=0.92,
        charger_min_power_w=0.0,
        deadline_slot=deadline_slot,
    )


def _past_target_ev() -> EVConfig:
    """A 60 kWh EV above its target, charging past it from surplus only."""
    return EVConfig(
        enabled=True,
        initial_soc_kwh=50.0,
        target_kwh=60.0,
        capacity_kwh=60.0,
        max_charge_per_slot=3.0,
        charger_efficiency=0.92,
        charger_min_power_w=0.0,
        charge_past_target=True,
        future_value_per_kwh=0.5,
    )


def test_deadline_ev_keeps_priority_over_the_battery_target() -> None:
    """The target penalty is below the EV's, so the EV still gets its energy."""
    slots, now = _deadline_day()
    ev = _deadline_ev(deadline_slot=6)
    spec = _spec(slots, now, ev_configs=[ev])
    ev_penalty = ev_deadline_penalty_per_kwh(ev, 0.25, len(slots))
    assert ev_penalty is not None
    assert spec.penalty_per_kwh < ev_penalty

    normal, _ = _solve(slots, now, None, current_kwh=2.0, ev_configs=[ev])
    plan, diagnostics = _solve(slots, now, spec, current_kwh=2.0, ev_configs=[ev])

    ev_normal = sum(s.ev_total_planned_load_kwh for s in normal[:7])
    ev_plan = sum(s.ev_total_planned_load_kwh for s in plan[:7])
    # The whole-amp lattice may deliver slightly more than the bare need.
    assert ev_normal >= 6.0 / 0.92 - 0.05
    assert ev_plan >= 6.0 / 0.92 - 0.05
    assert diagnostics["battery_target"]["stage2_ran"] is True
    _assert_import_rule(normal, plan, _target_index(slots, spec))


def test_house_battery_target_goes_before_a_charge_past_target_ev() -> None:
    """Option A: the EV only gets the PV the battery target leaves unused."""
    slots, now = _deadline_day()
    ev = _past_target_ev()
    spec = _spec(slots, now, ev_configs=[ev])

    normal, _ = _solve(slots, now, None, current_kwh=2.0, ev_configs=[ev])
    plan, diagnostics = _solve(slots, now, spec, current_kwh=2.0, ev_configs=[ev])
    without_ev, _ = _solve(slots, now, spec, current_kwh=2.0)
    target_index = _target_index(slots, spec)

    assert diagnostics["battery_target"]["stage2_ran"] is True
    # The battery reaches what it reaches with no EV at all …
    assert _soc(plan, 2.0)[target_index] == pytest.approx(
        _soc(without_ev, 2.0)[target_index], abs=1e-2
    )
    assert _soc(plan, 2.0)[target_index] > _soc(normal, 2.0)[target_index] + 1.0
    # … and the EV gives up exactly the surplus the battery now takes.
    ev_normal = sum(s.ev_total_planned_load_kwh for s in normal[:7])
    ev_plan = sum(s.ev_total_planned_load_kwh for s in plan[:7])
    assert ev_normal > 1.0
    assert ev_plan < ev_normal - 1.0
    for slot in plan[:7]:
        battery_ac = slot.batteries_charged_kwh / _CHARGE_EFF
        assert slot.ev_total_planned_load_kwh + battery_ac <= 2.5 + 1e-2
    _assert_import_rule(normal, plan, target_index)


# ---------------------------------------------------------------------------
# Preference cost (issue #1185)
# ---------------------------------------------------------------------------

_PREFERENCE_KEYS = (
    "stage1_cost",
    "stage2_cost",
    "preference_cost",
    "preference_cost_per_kwh",
    "terminal_soc_value_delta",
)

_WEIGHTS = CostWeights(
    cycle_cost_per_kwh=_BATTERY["cycle_cost_per_kwh"],
    charge_efficiency_pct=_BATTERY["charge_efficiency_pct"],
    discharge_efficiency_pct=_BATTERY["discharge_efficiency_pct"],
)


def _export_only_day() -> tuple[list[PlannedSlot], datetime]:
    """Three hours of 2 kWh PV at a 0.50 export price, and nothing else.

    No house load and a 0.20 import price, so the normal plan exports all
    6 kWh and never touches the battery.  Nothing after the target can use
    stored energy, so the target's cost is exactly the export it gives up
    plus the cycling.
    """
    pv = [0.0] * 12 + [2.0, 2.0, 2.0] + [0.0] * 9
    slots = _day(pv, [0.0] * 24, [0.20] * 24, [0.50] * 24)
    return slots, _MIDNIGHT + timedelta(hours=12)


class TestPreferenceCost:
    """What the target costs: stage 2 against stage 1, in money."""

    def test_equals_forgone_export_plus_cycle_cost(self) -> None:
        """Hand-computed: 2 kWh stored instead of exported.

        Storing 2 kWh takes 2 / 0.97 = 2.062 kWh of PV that is not exported
        at 0.50 (1.031), and cycles 2 kWh at 0.02 (0.040).
        """
        slots, now = _export_only_day()
        spec = _spec(slots, now, target_kwh=6.0, at=time(15, 0))

        stage2, diagnostics = _solve(
            slots, now, spec, current_kwh=4.0, cost_weights=_WEIGHTS
        )
        report = diagnostics["battery_target"]

        assert report["stage2_status"] == "solved"
        assert report["stage1_projected_kwh"] == pytest.approx(4.0)
        assert report["projected_kwh"] == pytest.approx(6.0, abs=_PUBLISHED_TOL)
        forgone_export = 2.0 / _CHARGE_EFF * 0.50
        cycle_cost = 2.0 * _BATTERY["cycle_cost_per_kwh"]
        assert report["stage1_cost"] == pytest.approx(-6.0 * 0.50, abs=2e-3)
        assert report["preference_cost"] == pytest.approx(
            forgone_export + cycle_cost, abs=2e-3
        )
        assert report["stage2_cost"] == pytest.approx(
            report["stage1_cost"] + report["preference_cost"], abs=1e-4
        )
        assert report["preference_cost_per_kwh"] == pytest.approx(
            (forgone_export + cycle_cost) / 2.0, abs=2e-3
        )
        # No end value in this solve, so the stored 2 kWh carry no credit.
        assert report["terminal_soc_value_delta"] == pytest.approx(0.0)
        # stage2_cost is the money figure of the plan that is returned.
        published = score_plan(stage2, _WEIGHTS, slot_duration_hours=1.0, now=now)
        assert report["stage2_cost"] == pytest.approx(published.total_cost, abs=1e-4)

    def test_terminal_soc_difference_is_reported_alongside(self) -> None:
        """With an end value V, the 2 kWh still stored are credited at V."""
        slots, now = _export_only_day()
        spec = _spec(slots, now, target_kwh=6.0, at=time(15, 0))

        _stage2, diagnostics = _solve(
            slots,
            now,
            spec,
            current_kwh=4.0,
            cost_weights=_WEIGHTS,
            replacement_price_per_kwh=0.10,
        )
        report = diagnostics["battery_target"]

        assert report["stage2_status"] == "solved"
        assert report["terminal_soc_value_delta"] == pytest.approx(
            -2.0 * 0.10, abs=2e-3
        )
        # The money figure is not reduced by it.
        assert report["preference_cost"] == pytest.approx(
            2.0 / _CHARGE_EFF * 0.50 + 2.0 * 0.02, abs=2e-3
        )

    def test_keys_are_none_when_stage_2_does_not_run(self) -> None:
        slots, now = _export_only_day()
        spec = _spec(slots, now, target_kwh=4.0, at=time(15, 0))

        _plan, diagnostics = _solve(
            slots, now, spec, current_kwh=4.0, cost_weights=_WEIGHTS
        )
        report = diagnostics["battery_target"]

        assert report["stage2_status"] == "target_met"
        assert [report[key] for key in _PREFERENCE_KEYS] == [None] * 5

    def test_keys_are_none_without_cost_weights(self) -> None:
        """A caller that passes no weights gets the plan, not the figure."""
        slots, now = _export_only_day()
        spec = _spec(slots, now, target_kwh=6.0, at=time(15, 0))

        _plan, diagnostics = _solve(slots, now, spec, current_kwh=4.0)
        report = diagnostics["battery_target"]

        assert report["stage2_status"] == "solved"
        assert [report[key] for key in _PREFERENCE_KEYS] == [None] * 5

    def test_keys_are_none_when_stage_2_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        slots, now = _export_only_day()
        spec = _spec(slots, now, target_kwh=6.0, at=time(15, 0))
        monkeypatch.setattr(_battery_target, "_solve_stage2", lambda *_a, **_k: None)

        _plan, diagnostics = _solve(
            slots, now, spec, current_kwh=4.0, cost_weights=_WEIGHTS
        )
        report = diagnostics["battery_target"]

        assert report["stage2_status"] == "failed"
        assert [report[key] for key in _PREFERENCE_KEYS] == [None] * 5

    def test_keys_are_none_when_nothing_was_gained(self) -> None:
        """No PV and no export to give up: the normal plan is kept, at no cost."""
        slots = _day([0.0] * 24, [0.0] * 24, [0.20] * 24, [0.50] * 24)
        now = _MIDNIGHT + timedelta(hours=12)
        spec = _spec(slots, now, target_kwh=6.0, at=time(15, 0))

        _plan, diagnostics = _solve(
            slots, now, spec, current_kwh=4.0, cost_weights=_WEIGHTS
        )
        report = diagnostics["battery_target"]

        assert report["stage2_ran"] is True
        assert report["stage2_status"] == "no_gain"
        assert report["projected_kwh"] == pytest.approx(report["stage1_projected_kwh"])
        assert [report[key] for key in _PREFERENCE_KEYS] == [None] * 5

    def test_the_figure_does_not_change_the_plan(self) -> None:
        """Passing the weights reports the cost; the slots are the same."""
        slots, now = _export_only_day()
        spec = _spec(slots, now, target_kwh=6.0, at=time(15, 0))

        with_weights, _ = _solve(
            slots, now, spec, current_kwh=4.0, cost_weights=_WEIGHTS
        )
        without_weights, _ = _solve(slots, now, spec, current_kwh=4.0)

        assert with_weights == without_weights
