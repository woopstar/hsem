"""Charge-past-target surplus bound is pro-rated for the live slot (issue #1012).

The surplus-only and battery-first rows bound a charge-past-target EV with the
slot's PV surplus.  The forecast surplus is a *full-width* slot energy, but the
published charger command is derived from the energy allocated to the minutes
that remain in the live slot.  Bounding a partly elapsed slot with a full
slot's surplus therefore let the command reach
``full_slot_surplus / remaining_hours`` — a multiple of the surplus actually
arriving — with the difference drawn from the house battery or the grid.

The spec guarantee under test (``docs/planner-spec.md``, "EV surplus-only for
charge-past-target"): past-target charging never draws from the battery or
grid.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.models.ev_config import EVConfig
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.planner.milp_optimizer import is_scipy_available, solve_milp
from custom_components.hsem.utils.prices import SlotPrice
from custom_components.hsem.utils.units import remaining_slot_fraction

_TZ = ZoneInfo("Europe/Copenhagen")
_SLOT_MINUTES = 15
_SLOT_HOURS = _SLOT_MINUTES / 60.0
_FIRST_SLOT_START = datetime(2024, 6, 15, 13, 0, tzinfo=_TZ)

#: Reporter's charger: 11 kW, 92 % efficient, 6 A single-phase minimum.
_CHARGER_KW = 11.0
_CHARGER_EFF = 0.92
_CHARGER_MIN_W = 1380.0

_pytestmark_scipy = pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available in this environment"
)


def _make_slot(index: int, *, pv_kwh: float, house_kwh: float) -> PlannedSlot:
    """Build one 15-minute slot with a fixed PV/house forecast."""
    start = _FIRST_SLOT_START + timedelta(minutes=_SLOT_MINUTES * index)
    slot = PlannedSlot(
        start=start,
        end=start + timedelta(minutes=_SLOT_MINUTES),
        price=SlotPrice(import_price=1.50, export_price=0.10),
    )
    slot.avg_house_consumption_kwh = house_kwh
    slot.solcast_pv_estimate_kwh = pv_kwh
    slot.ev_planned_load_kwh = 0.0
    slot.ev_accounted_load_kwh = 0.0
    slot.ev_total_planned_load_kwh = 0.0
    slot.estimated_net_consumption_kwh = house_kwh - pv_kwh
    return slot


def _past_target_ev() -> EVConfig:
    """An 86.5 kWh EV above its target SoC, in charge-past-target mode."""
    return EVConfig(
        enabled=True,
        initial_soc_kwh=81.31,  # 94 % — above the 80 % target
        target_kwh=86.5,  # charge-past-target lifts the target to capacity
        capacity_kwh=86.5,
        max_charge_per_slot=_CHARGER_KW * _SLOT_HOURS * _CHARGER_EFF,
        charger_efficiency=_CHARGER_EFF,
        charger_min_power_w=_CHARGER_MIN_W,
        charge_past_target=True,
        future_value_per_kwh=1.10,
    )


def _solve_live_slot(
    *,
    minutes_elapsed: int,
    pv_kwh: float,
    house_kwh: float,
    battery_current_kwh: float = 14.25,
) -> PlannedSlot:
    """Solve with the live slot *minutes_elapsed* in, return that slot.

    The house battery defaults to full so battery-first (issue #775) leaves the
    surplus to the EV — otherwise the battery absorbs it and the EV's own bound
    is never the binding constraint.
    """
    now = _FIRST_SLOT_START + timedelta(minutes=minutes_elapsed)
    slots = [_make_slot(i, pv_kwh=pv_kwh, house_kwh=house_kwh) for i in range(8)]
    result = solve_milp(
        slots,
        now,
        current_kwh=battery_current_kwh,
        usable_kwh=14.25,
        max_charge_per_slot=1.225,
        max_discharge_per_slot=1.25,
        ev_configs=[_past_target_ev()],
    )
    assert result is not None
    out_slots, _diag = result
    return out_slots[0]


# ---------------------------------------------------------------------------
# The canonical helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("available_hours", "slot_hours", "expected"),
    [
        (0.25, 0.25, 1.0),  # full-width slot
        (0.125, 0.25, 0.5),  # half elapsed
        (1.0 / 60.0, 0.25, 4.0 / 60.0),  # one minute of a 15-minute slot
        (0.0, 0.25, 0.0),  # slot exhausted
        (-1.0, 0.25, 0.0),  # clamped below
        (0.5, 0.25, 1.0),  # clamped above
        (0.25, 0.0, 1.0),  # degenerate slot width, clamped above
    ],
)
def test_remaining_slot_fraction(
    available_hours: float, slot_hours: float, expected: float
) -> None:
    """The fraction is the remaining share of the slot, clamped to [0, 1]."""
    assert remaining_slot_fraction(available_hours, slot_hours) == pytest.approx(
        expected
    )


# ---------------------------------------------------------------------------
# Sub-minimum surplus must never start the charger (issue #1012)
# ---------------------------------------------------------------------------


@_pytestmark_scipy
@pytest.mark.parametrize("minutes_elapsed", [0, 3, 6, 9, 12, 13, 14])
def test_sub_minimum_surplus_never_starts_charger(minutes_elapsed: int) -> None:
    """A surplus too small to run the charger stays too small in the slot tail.

    600 W of genuine surplus cannot run a 1380 W charger.  Before the fix the
    full-slot surplus bound divided by the shrinking remaining time lifted the
    command over the charger minimum from ~6 minutes left onward, starting a
    session that the surplus could never sustain.
    """
    # 0.30 kWh PV − 0.15 kWh house over a 15-minute slot == 600 W surplus.
    slot = _solve_live_slot(
        minutes_elapsed=minutes_elapsed, pv_kwh=0.30, house_kwh=0.15
    )

    assert slot.ev_charger_calculated_power == pytest.approx(0.0), (
        f"charger commanded {slot.ev_charger_calculated_power} W with only 600 W "
        f"of surplus and a {_CHARGER_MIN_W} W minimum, {15 - minutes_elapsed} min "
        "left in the slot"
    )
    assert slot.ev_total_planned_load_kwh == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# The command never exceeds the genuine surplus power (issue #1012)
# ---------------------------------------------------------------------------


@_pytestmark_scipy
@pytest.mark.parametrize("minutes_elapsed", [0, 3, 6, 9, 12, 13, 14])
def test_command_never_exceeds_genuine_surplus_power(minutes_elapsed: int) -> None:
    """Past-target charging tracks surplus *power*, not compressed slot energy.

    The surplus arrives at a fixed rate, so the admissible command is the same
    whether the slot has just started or is nearly over.
    """
    # 0.90 kWh PV − 0.15 kWh house over a 15-minute slot == 3000 W surplus.
    surplus_w = (0.90 - 0.15) / _SLOT_HOURS * 1000.0
    slot = _solve_live_slot(
        minutes_elapsed=minutes_elapsed, pv_kwh=0.90, house_kwh=0.15
    )

    assert slot.ev_charger_calculated_power <= surplus_w + 1e-6, (
        f"charger commanded {slot.ev_charger_calculated_power} W against "
        f"{surplus_w:.0f} W of genuine surplus, {15 - minutes_elapsed} min left"
    )


@_pytestmark_scipy
def test_genuine_surplus_still_charges_late_in_the_slot() -> None:
    """Pro-rating must not suppress charging the surplus genuinely supports.

    The fix tightens a bound; this guards against it becoming a blanket
    "never charge in the slot tail" rule.
    """
    slot = _solve_live_slot(minutes_elapsed=12, pv_kwh=0.90, house_kwh=0.15)

    assert slot.ev_charger_calculated_power >= _CHARGER_MIN_W, (
        "3 kW of genuine surplus should still run the charger with 3 minutes left"
    )


# ---------------------------------------------------------------------------
# Full-width future slots are unaffected
# ---------------------------------------------------------------------------


@_pytestmark_scipy
def test_full_width_slot_bound_is_unchanged() -> None:
    """A slot that has not started is scaled by exactly 1.0.

    Solving at the slot boundary and one slot earlier must place the same
    command on the live slot — the fix only ever bites on elapsed time.
    """
    at_boundary = _solve_live_slot(minutes_elapsed=0, pv_kwh=0.90, house_kwh=0.15)

    now = _FIRST_SLOT_START
    slots = [_make_slot(i, pv_kwh=0.90, house_kwh=0.15) for i in range(8)]
    result = solve_milp(
        slots,
        now,
        current_kwh=14.25,
        usable_kwh=14.25,
        max_charge_per_slot=1.225,
        max_discharge_per_slot=1.25,
        ev_configs=[_past_target_ev()],
    )
    assert result is not None
    out_slots, _diag = result

    assert out_slots[0].ev_charger_calculated_power == pytest.approx(
        at_boundary.ev_charger_calculated_power
    )
    assert out_slots[0].ev_total_planned_load_kwh == pytest.approx(
        at_boundary.ev_total_planned_load_kwh
    )


# ---------------------------------------------------------------------------
# The plan's own accounting stays honest
# ---------------------------------------------------------------------------


@_pytestmark_scipy
@pytest.mark.parametrize("minutes_elapsed", [0, 6, 12, 14])
def test_past_target_load_never_exceeds_remaining_surplus_energy(
    minutes_elapsed: int,
) -> None:
    """Planned EV load stays within the surplus the slot has left to deliver.

    Before the fix the plan booked up to a full slot's surplus into the live
    slot's remaining minutes while still reporting ``grid_import_kwh == 0``,
    so the import it implied was invisible in the published plan.
    """
    pv_kwh, house_kwh = 0.90, 0.15
    slot = _solve_live_slot(
        minutes_elapsed=minutes_elapsed, pv_kwh=pv_kwh, house_kwh=house_kwh
    )

    remaining_fraction = remaining_slot_fraction(
        (_SLOT_MINUTES - minutes_elapsed) / 60.0, _SLOT_HOURS
    )
    remaining_surplus_kwh = (pv_kwh - house_kwh) * remaining_fraction

    assert slot.ev_total_planned_load_kwh <= remaining_surplus_kwh + 1e-6, (
        f"planned {slot.ev_total_planned_load_kwh:.3f} kWh of EV load against "
        f"{remaining_surplus_kwh:.3f} kWh of remaining surplus"
    )
    assert slot.grid_import_kwh == pytest.approx(0.0, abs=1e-6)


# ---------------------------------------------------------------------------
# The battery side of the shared budget (issue #775 row)
# ---------------------------------------------------------------------------


def _solve_battery_first_live_slot(
    *,
    minutes_elapsed: int,
    pv_kwh: float,
    house_kwh: float,
) -> PlannedSlot:
    """Solve with the battery competing for the surplus, return the live slot.

    Differs from :func:`_solve_live_slot` in two ways, both required to make
    the battery-first row the binding constraint on ``ec[t]``:

    * the battery starts nearly empty, so it has headroom to absorb surplus;
    * the sunny slots are followed by an evening deficit, so storing the
      surplus is worth more than exporting it — without a later deficit the
      battery has no reason to charge and the row never binds.
    """
    now = _FIRST_SLOT_START + timedelta(minutes=minutes_elapsed)
    slots = [_make_slot(i, pv_kwh=pv_kwh, house_kwh=house_kwh) for i in range(8)]
    slots += [_make_slot(i, pv_kwh=0.0, house_kwh=1.20) for i in range(8, 16)]
    result = solve_milp(
        slots,
        now,
        current_kwh=2.0,
        usable_kwh=14.25,
        max_charge_per_slot=1.225,
        max_discharge_per_slot=1.25,
        ev_configs=[_past_target_ev()],
    )
    assert result is not None
    out_slots, _diag = result
    return out_slots[0]


@_pytestmark_scipy
@pytest.mark.parametrize("minutes_elapsed", [0, 6, 12, 14])
def test_battery_charge_never_exceeds_remaining_surplus(
    minutes_elapsed: int,
) -> None:
    """The battery half of the shared budget is pro-rated too (issue #1012).

    The battery-first row bounds ``ec[t] + Σ ev_c[t]/η`` with one shared
    surplus budget, so an un-pro-rated budget over-allocated the *battery* in
    the live slot exactly as it over-allocated the EV: before the fix the
    solver booked a full slot's surplus into the final minute, and the
    difference could only come from the grid.
    """
    pv_kwh, house_kwh = 1.00, 0.10
    slot = _solve_battery_first_live_slot(
        minutes_elapsed=minutes_elapsed, pv_kwh=pv_kwh, house_kwh=house_kwh
    )

    remaining_fraction = remaining_slot_fraction(
        (_SLOT_MINUTES - minutes_elapsed) / 60.0, _SLOT_HOURS
    )
    remaining_surplus_kwh = (pv_kwh - house_kwh) * remaining_fraction

    assert slot.batteries_charged_kwh <= remaining_surplus_kwh + 1e-6, (
        f"battery charged {slot.batteries_charged_kwh:.3f} kWh against "
        f"{remaining_surplus_kwh:.3f} kWh of remaining surplus, "
        f"{_SLOT_MINUTES - minutes_elapsed} min left"
    )
    assert slot.grid_import_kwh == pytest.approx(0.0, abs=1e-6)


@_pytestmark_scipy
@pytest.mark.parametrize("minutes_elapsed", [0, 6, 12, 14])
def test_battery_still_absorbs_the_remaining_surplus(minutes_elapsed: int) -> None:
    """Pro-rating must not starve the battery of surplus it can still take.

    The opposite failure direction to
    :func:`test_battery_charge_never_exceeds_remaining_surplus`: the shared
    budget is scaled, but the battery must still be free to absorb what the
    slot genuinely has left, and battery-first (issue #775) means it takes
    that surplus ahead of the past-target EV.
    """
    pv_kwh, house_kwh = 1.00, 0.10
    slot = _solve_battery_first_live_slot(
        minutes_elapsed=minutes_elapsed, pv_kwh=pv_kwh, house_kwh=house_kwh
    )

    remaining_fraction = remaining_slot_fraction(
        (_SLOT_MINUTES - minutes_elapsed) / 60.0, _SLOT_HOURS
    )
    remaining_surplus_kwh = (pv_kwh - house_kwh) * remaining_fraction

    assert slot.batteries_charged_kwh >= 0.9 * remaining_surplus_kwh, (
        f"battery absorbed only {slot.batteries_charged_kwh:.3f} kWh of "
        f"{remaining_surplus_kwh:.3f} kWh available"
    )
    assert slot.ev_total_planned_load_kwh == pytest.approx(0.0, abs=1e-6)
