"""Two-stage charge-past-target solve (issue #1015).

A charge-past-target EV may charge only from PV surplus the house battery and
the other consumers would not otherwise have used (``docs/planner-spec.md``,
"EV surplus-only for charge-past-target").  Two ways to break that were found:

* The battery-first row ``ec[t] + Σ ev_c[t]/η ≤ surplus[t]`` capped *all*
  battery charging at the surplus, because ``ec[t]`` mixes grid- and
  PV-sourced energy: with a past-target EV plugged in the battery could not
  grid-charge at all, even at night.
* Simply dropping ``ec[t]`` from that row lets the EV take surplus the battery
  would have stored while the battery refills from cheap grid — the EV then
  draws from grid in all but name.

The two-stage solve reserves what the plan without any past-target EV spent on
the battery and other EVs, and caps the EV at the PV left over.
"""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.models.ev_config import EVConfig
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.planner.milp import _past_target_reservation
from custom_components.hsem.planner.milp._past_target_reservation import (
    reserved_ac_kwh_per_future_slot,
    solve_milp_with_past_target_reservation,
)
from custom_components.hsem.planner.milp_optimizer import is_scipy_available, solve_milp
from custom_components.hsem.utils.prices import SlotPrice
from custom_components.hsem.utils.units import remaining_slot_fraction

_TZ = ZoneInfo("Europe/Copenhagen")
_SLOT_MINUTES = 15
_SLOT_HOURS = _SLOT_MINUTES / 60.0
_START = datetime(2026, 9, 14, 11, 0, tzinfo=_TZ)

#: Reporter's house battery.
_BATTERY: dict[str, Any] = {
    "usable_kwh": 14.25,
    "max_charge_per_slot": 1.225,
    "max_discharge_per_slot": 1.25,
}

pytestmark = pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available in this environment"
)


def _slot(
    index: int,
    *,
    pv_kwh: float,
    house_kwh: float,
    import_price: float,
    export_price: float,
    start: datetime = _START,
) -> PlannedSlot:
    """Build one 15-minute slot with a fixed forecast and price."""
    slot_start = start + timedelta(minutes=_SLOT_MINUTES * index)
    slot = PlannedSlot(
        start=slot_start,
        end=slot_start + timedelta(minutes=_SLOT_MINUTES),
        price=SlotPrice(import_price=import_price, export_price=export_price),
    )
    slot.avg_house_consumption_kwh = house_kwh
    slot.solcast_pv_estimate_kwh = pv_kwh
    slot.ev_planned_load_kwh = 0.0
    slot.ev_accounted_load_kwh = 0.0
    slot.ev_total_planned_load_kwh = 0.0
    slot.estimated_net_consumption_kwh = house_kwh - pv_kwh
    return slot


def _past_target_ev(*, future_value_per_kwh: float = 2.5) -> EVConfig:
    """An 86.5 kWh EV at 94 %, above its 80 % target, in charge-past-target mode."""
    return EVConfig(
        enabled=True,
        initial_soc_kwh=81.31,
        target_kwh=86.5,
        capacity_kwh=86.5,
        max_charge_per_slot=11.0 * _SLOT_HOURS * 0.92,
        charger_efficiency=0.92,
        charger_min_power_w=1380.0,
        charge_past_target=True,
        future_value_per_kwh=future_value_per_kwh,
    )


def _displacement_day() -> list[PlannedSlot]:
    """Sunny slots, then cheap grid, then an expensive evening deficit.

    Without an EV the battery stores every kWh of the sunny surplus (storing
    PV costs the 0.05 export price, grid costs 0.10) *and* tops up from cheap
    grid for the 3.00 evening.  A high-value past-target EV is the tempting
    alternative home for that surplus.
    """
    sunny = [
        _slot(i, pv_kwh=0.8, house_kwh=0.2, import_price=1.00, export_price=0.05)
        for i in range(4)
    ]
    cheap = [
        _slot(i, pv_kwh=0.0, house_kwh=0.1, import_price=0.10, export_price=0.02)
        for i in range(4, 8)
    ]
    evening = [
        _slot(i, pv_kwh=0.0, house_kwh=1.2, import_price=3.00, export_price=0.05)
        for i in range(8, 16)
    ]
    return sunny + cheap + evening


def _grid_import(slots: list[PlannedSlot]) -> float:
    return sum(slot.grid_import_kwh for slot in slots)


def _solve(
    slots: list[PlannedSlot],
    ev_configs: list[EVConfig],
    *,
    now: datetime = _START,
    current_kwh: float = 4.0,
    two_stage: bool = True,
) -> list[PlannedSlot]:
    solver = solve_milp_with_past_target_reservation if two_stage else solve_milp
    result = solver(
        slots, now, current_kwh=current_kwh, ev_configs=ev_configs, **_BATTERY
    )
    assert result is not None
    return result[0]


# ---------------------------------------------------------------------------
# Defect A — the battery is no longer blocked from grid-charging
# ---------------------------------------------------------------------------


def test_battery_grid_charges_at_night_with_past_target_ev_plugged_in() -> None:
    """A plugged-in past-target EV must not stop cheap-night battery charging."""
    night = [
        _slot(
            i,
            pv_kwh=0.0,
            house_kwh=0.10 if i < 8 else 1.20,
            import_price=0.20 if i < 8 else 3.00,
            export_price=0.05,
            start=datetime(2026, 9, 14, 1, 0, tzinfo=_TZ),
        )
        for i in range(16)
    ]
    now = night[0].start

    without_ev = _solve(night, [], now=now, current_kwh=2.0)
    with_ev = _solve(night, [_past_target_ev()], now=now, current_kwh=2.0)

    charged_without = sum(s.batteries_charged_kwh for s in without_ev[:8])
    charged_with = sum(s.batteries_charged_kwh for s in with_ev[:8])
    assert charged_without > 5.0
    assert charged_with == pytest.approx(charged_without, abs=0.01)
    assert sum(s.ev_total_planned_load_kwh for s in with_ev) == pytest.approx(
        0.0, abs=1e-6
    )


def test_battery_still_grid_charges_on_a_sunny_day() -> None:
    """The battery keeps both its PV share and its cheap-grid top-up."""
    day = _displacement_day()
    without_ev = _solve(day, [])
    with_ev = _solve(day, [_past_target_ev()])

    assert sum(s.batteries_charged_kwh for s in with_ev[4:8]) == pytest.approx(
        sum(s.batteries_charged_kwh for s in without_ev[4:8]), abs=0.01
    )
    assert sum(s.batteries_charged_kwh for s in with_ev[4:8]) > 1.0


# ---------------------------------------------------------------------------
# The EV must not displace the battery
# ---------------------------------------------------------------------------


def test_ev_does_not_take_surplus_the_battery_would_store() -> None:
    """The EV gets none of the surplus the battery stores without it."""
    day = _displacement_day()
    without_ev = _solve(day, [])
    with_ev = _solve(day, [_past_target_ev()])

    assert sum(s.ev_total_planned_load_kwh for s in with_ev[:4]) == pytest.approx(
        0.0, abs=1e-6
    )
    assert sum(s.batteries_charged_kwh for s in with_ev[:4]) == pytest.approx(
        sum(s.batteries_charged_kwh for s in without_ev[:4]), abs=0.01
    )


def test_ev_never_increases_grid_import() -> None:
    """Adding a past-target EV cannot raise the plan's grid import."""
    day = _displacement_day()
    assert _grid_import(_solve(day, [_past_target_ev()])) <= (
        _grid_import(_solve(day, [])) + 0.01
    )


def test_zero_reservation_would_leak_through_the_battery() -> None:
    """Guard: without the stage-1 reservation the EV draws grid via the battery.

    Pins that the scenario above really exercises the leak, so the two tests
    before this one cannot pass vacuously. An all-zero reservation is exactly
    "battery-first row with ``ec[t]`` dropped".
    """
    day = _displacement_day()
    naive = replace(_past_target_ev(), past_target_reserved_ac_kwh=(0.0,) * len(day))
    leaked = _solve(day, [naive], two_stage=False)

    assert sum(s.ev_total_planned_load_kwh for s in leaked[:4]) > 1.0
    assert _grid_import(leaked) > _grid_import(_solve(day, [])) + 1.0


# ---------------------------------------------------------------------------
# The EV still gets genuinely spare PV
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("minutes_elapsed", [0, 6, 12])
def test_ev_takes_surplus_the_battery_cannot(minutes_elapsed: int) -> None:
    """With the battery full, the EV absorbs the surplus — within what remains."""
    pv_kwh, house_kwh = 1.00, 0.10
    day = [
        _slot(i, pv_kwh=pv_kwh, house_kwh=house_kwh, import_price=1.5, export_price=0.1)
        for i in range(8)
    ]
    now = _START + timedelta(minutes=minutes_elapsed)
    out = _solve(
        day, [_past_target_ev(future_value_per_kwh=1.1)], now=now, current_kwh=14.25
    )

    remaining = remaining_slot_fraction(
        (_SLOT_MINUTES - minutes_elapsed) / 60.0, _SLOT_HOURS
    )
    assert out[1].ev_total_planned_load_kwh > 0.5
    assert out[0].ev_total_planned_load_kwh <= (pv_kwh - house_kwh) * remaining + 1e-6
    assert _grid_import(out) == pytest.approx(0.0, abs=1e-6)


def test_ev_takes_surplus_the_battery_declines_to_store() -> None:
    """A battery with headroom that stores nothing must not starve the EV.

    With nothing to spend stored energy on, the plan without an EV exports the
    whole surplus. The battery-first objective cap (issue #775) sizes itself
    from the battery's *initial* headroom, so it priced the EV at the
    battery's charge credit — below export — and the EV got nothing while
    every kWh was exported. In reservation mode the battery has already had
    its pick, so the cap must not apply.
    """
    day = [
        _slot(i, pv_kwh=1.0, house_kwh=0.1, import_price=0.5, export_price=0.1)
        for i in range(8)
    ]
    ev = _past_target_ev(future_value_per_kwh=1.1)
    out = _solve(day, [ev], current_kwh=5.0)

    headroom_ac_kwh = (ev.capacity_kwh - ev.initial_soc_kwh) / ev.charger_efficiency
    assert sum(s.ev_total_planned_load_kwh for s in out) == pytest.approx(
        headroom_ac_kwh, abs=0.05
    )
    assert _grid_import(out) == pytest.approx(0.0, abs=1e-6)


# ---------------------------------------------------------------------------
# Wrapper contract
# ---------------------------------------------------------------------------


def test_no_past_target_ev_is_a_single_solve(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only a charge-past-target EV pays for the second solve."""
    calls: list[list[EVConfig] | None] = []

    def counting(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs.get("ev_configs"))
        return solve_milp(*args, **kwargs)

    monkeypatch.setattr(_past_target_reservation, "solve_milp", counting)
    deadline_ev = replace(_past_target_ev(), charge_past_target=False)
    _solve(_displacement_day(), [deadline_ev])

    assert len(calls) == 1


def test_stage_two_preserves_ev_order_and_reserves_only_past_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Primary/second order survives, and only past-target EVs carry a reservation."""
    calls: list[list[EVConfig]] = []

    def capturing(*args: Any, **kwargs: Any) -> Any:
        calls.append(list(kwargs["ev_configs"]))
        return solve_milp(*args, **kwargs)

    monkeypatch.setattr(_past_target_reservation, "solve_milp", capturing)
    deadline_ev = replace(_past_target_ev(), charge_past_target=False)
    past_target_ev = replace(_past_target_ev(), is_second=True)
    _solve(_displacement_day(), [deadline_ev, past_target_ev])

    stage1, stage2 = calls
    assert stage1 == [deadline_ev]
    assert stage2[0] is deadline_ev
    assert stage2[1].is_second
    assert stage2[1].past_target_reserved_ac_kwh is not None
    assert deadline_ev.past_target_reserved_ac_kwh is None


def test_stage_one_failure_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """When stage 1 cannot solve, the past-target EV gets nothing, not everything."""

    def failing_stage_one(*args: Any, **kwargs: Any) -> Any:
        if not any(ev.charge_past_target for ev in kwargs["ev_configs"]):
            return None
        return solve_milp(*args, **kwargs)

    monkeypatch.setattr(_past_target_reservation, "solve_milp", failing_stage_one)
    day = [
        _slot(i, pv_kwh=1.0, house_kwh=0.1, import_price=1.5, export_price=0.1)
        for i in range(8)
    ]
    out = _solve(day, [_past_target_ev()], current_kwh=14.25)

    assert sum(s.ev_total_planned_load_kwh for s in out) == pytest.approx(0.0, abs=1e-6)


def test_mismatched_reservation_falls_back_to_battery_first_row() -> None:
    """A reservation of the wrong length is ignored, never misaligned."""
    day = _displacement_day()
    legacy = _solve(day, [_past_target_ev()], two_stage=False)
    mismatched = _solve(
        day,
        [replace(_past_target_ev(), past_target_reserved_ac_kwh=(0.0,) * 3)],
        two_stage=False,
    )

    assert _grid_import(mismatched) == pytest.approx(_grid_import(legacy), abs=1e-6)
    assert [s.batteries_charged_kwh for s in mismatched] == pytest.approx(
        [s.batteries_charged_kwh for s in legacy], abs=1e-6
    )


def test_reservation_aligns_with_future_slots_and_input_ev_load() -> None:
    """Past slots are skipped; battery AC and only *added* EV load are reserved."""
    inputs = [
        _slot(i, pv_kwh=1.0, house_kwh=0.1, import_price=1.0, export_price=0.1)
        for i in range(3)
    ]
    inputs[2].ev_total_planned_load_kwh = 0.4
    stage1 = [
        _slot(i, pv_kwh=1.0, house_kwh=0.1, import_price=1.0, export_price=0.1)
        for i in range(3)
    ]
    stage1[1].batteries_charged_kwh = 0.97
    stage1[2].batteries_charged_kwh = 0.0
    stage1[2].ev_total_planned_load_kwh = 1.0
    now = _START + timedelta(minutes=20)  # slot 0 has ended

    reserved = reserved_ac_kwh_per_future_slot(inputs, stage1, now, 97.0)

    assert reserved == pytest.approx((1.0, 0.6))


# ---------------------------------------------------------------------------
# Property check
# ---------------------------------------------------------------------------


def _random_day(rng: random.Random) -> list[PlannedSlot]:
    """A random 24-slot day with a strict import/export spread.

    The spread keeps the LP out of cost-equal ties, where PV/grid attribution
    within a slot is arbitrary and a grid-import comparison is meaningless.
    """
    day = []
    for i in range(24):
        import_price = rng.choice([rng.uniform(0.05, 0.3), rng.uniform(0.5, 3.5)])
        day.append(
            _slot(
                i,
                pv_kwh=max(0.0, rng.uniform(-0.3, 1.6)),
                house_kwh=rng.uniform(0.05, 1.2),
                import_price=import_price,
                export_price=min(import_price - 0.05, rng.uniform(-0.2, 0.4)),
            )
        )
    return day


@pytest.mark.parametrize("seed", range(12))
def test_random_days_never_raise_grid_import_or_exceed_remaining_surplus(
    seed: int,
) -> None:
    """Across random days the EV neither adds grid import nor outruns the surplus."""
    rng = random.Random(1015 + seed)
    day = _random_day(rng)
    minutes_elapsed = rng.choice([0, 3, 7, 11, 14])
    now = _START + timedelta(minutes=minutes_elapsed)
    current_kwh = rng.uniform(0.5, 14.0)
    ev = _past_target_ev(future_value_per_kwh=rng.uniform(0.0, 4.0))

    result = solve_milp_with_past_target_reservation(
        day, now, current_kwh=current_kwh, ev_configs=[ev], **_BATTERY
    )
    baseline = solve_milp(day, now, current_kwh=current_kwh, ev_configs=[], **_BATTERY)
    assert result is not None and baseline is not None
    with_ev, diag = result

    assert _grid_import(with_ev) <= _grid_import(baseline[0]) + 0.01
    assert not diag["has_violations"]
    for index, slot in enumerate(with_ev):
        fraction = (
            remaining_slot_fraction(
                (_SLOT_MINUTES - minutes_elapsed) / 60.0, _SLOT_HOURS
            )
            if index == 0
            else 1.0
        )
        surplus_kwh = max(
            slot.solcast_pv_estimate_kwh - slot.avg_house_consumption_kwh, 0.0
        )
        assert slot.ev_total_planned_load_kwh <= surplus_kwh * fraction + 1e-3
