"""EV charging ceiling: amp conversion and stranded-residue re-portioning.

Ported from Ambilights/hsem-ambilights#27 (commit 372d70f7).  HSEM already
decides how much to charge in every future slot; these tests pin the
behaviour that makes the published ceiling trustworthy: the amp conversion
always rounds down, and a residue too small for the charger to run must be
re-portioned into a further slot rather than discarded.

The upstream fork's ``_redistribute_below_minimum_power`` concentrates the
*entire* EV allocation from scratch (a self-contained pure function).  This
repository's ``_write_milp_results_to_slots`` already performs that
concentration inline with a different (donor-pool) algorithm predating this
port, so only the new re-portioning tail — opening one further empty,
runnable slot for a residue concentration could not place — was extracted
into a standalone function of the same name for testability.  These tests
exercise that tail directly, with ``values`` representing the per-slot EV
allocation *after* the (unchanged) concentration pass has already run.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.planner.engine_ev_milp import _build_ev_configs_for_milp
from custom_components.hsem.planner.milp._write_results import (
    _redistribute_below_minimum_power,
)
from custom_components.hsem.planner.milp_optimizer import is_scipy_available, solve_milp
from custom_components.hsem.utils.phase_power import (
    EV_TOPOLOGY_SINGLE_PHASE,
    EV_TOPOLOGY_THREE_PHASE_BALANCED,
    charger_power_to_current_a,
)
from tests.planner.test_session_ev import _NOW, _build_slots

# 6 A three-phase minimum on a 15-minute slot at 90% charger efficiency.
_MIN_W = 3600.0
_EFF = 0.9
_QUARTER = 0.25
_MIN_DC = _MIN_W * _QUARTER * _EFF / 1000.0  # 0.81 kWh


def _repair(values, donor_energy, room):
    """Run the re-portioning tail with everything else held fixed."""
    return _redistribute_below_minimum_power(
        dict(values),
        minimum_dc=_MIN_DC,
        deadline_lp_limit=7,
        session_slots_set=set(),
        room_dc=lambda t: room.get(t, 0.0),
        donor_energy=donor_energy,
    )


# ---------------------------------------------------------------------------
# Amp conversion
# ---------------------------------------------------------------------------


def test_current_conversion_splits_a_three_phase_command() -> None:
    """A balanced charger's command is per-phase, so 11.04 kW is 16 A."""
    assert charger_power_to_current_a(11040.0, EV_TOPOLOGY_THREE_PHASE_BALANCED) == 16


def test_current_conversion_keeps_single_phase_whole() -> None:
    """A single-phase charger puts the whole command on one phase."""
    assert charger_power_to_current_a(3680.0, EV_TOPOLOGY_SINGLE_PHASE) == 16


def test_current_conversion_always_rounds_down() -> None:
    """A partial amp cannot be commanded and must never be published."""
    # 15.9 A three-phase — publishing 16 would exceed what HSEM planned.
    assert charger_power_to_current_a(10970.0, EV_TOPOLOGY_THREE_PHASE_BALANCED) == 15


@pytest.mark.parametrize("power_w", [0.0, -1.0, float("nan"), float("inf")])
def test_current_conversion_rejects_unusable_power(power_w: float) -> None:
    """Zero, negative and non-finite commands publish no headroom."""
    assert charger_power_to_current_a(power_w, EV_TOPOLOGY_THREE_PHASE_BALANCED) == 0


# ---------------------------------------------------------------------------
# Nameplate cap (issue #789)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not is_scipy_available(),
    reason="scipy not available in this environment",
)
def test_managed_session_cannot_expand_the_configured_nameplate() -> None:
    """A measured 6 kW draw cannot turn a configured 3 kW cap into a command.

    A managed (smart-controlled) live session is capped at the configured
    charger power even if its power sensor currently reports a higher draw;
    only an unmanaged accounting-only session (``fixed_session_only``) may
    retain the larger physical observation.
    """
    slots = _build_slots(
        4,
        start_hour=14,
        import_price=0.20,
        consumption_kwh=0.0,
        interval_minutes=60,
    )
    inp = PlannerInput(
        now_iso=_NOW.isoformat(),
        interval_minutes=60,
        ev_planned_load_enabled=True,
        ev_planned_load_connected=True,
        ev_planned_load_smart_charging_enabled=True,
        ev_planned_load_current_soc_pct=0.0,
        ev_planned_load_target_soc_pct=80.0,
        ev_planned_load_battery_capacity_kwh=10.0,
        ev_planned_load_charger_power_kw=3.0,
        ev_planned_load_charger_efficiency_pct=100.0,
        ev_planned_load_charger_phase_topology=EV_TOPOLOGY_SINGLE_PHASE,
        ev_planned_load_deadline=_NOW + timedelta(hours=4),
        ev_session_charge_kw=6.0,
    )

    configs = _build_ev_configs_for_milp(inp, slots, _NOW)

    assert configs is not None
    assert len(configs) == 1
    ev = configs[0]
    assert ev.fixed_session_only is False
    assert ev.max_charge_per_slot == pytest.approx(3.0)

    result = solve_milp(
        slots,
        _NOW,
        current_kwh=0.0,
        usable_kwh=10.0,
        max_charge_per_slot=1.0,
        max_discharge_per_slot=0.0,
        ev_configs=configs,
    )

    assert result is not None
    planned, _diagnostics = result
    assert planned[0].ev_charger_calculated_power == pytest.approx(3_000.0)
    assert all(slot.ev_charger_calculated_power <= 3_000.0 for slot in planned)


def test_unmanaged_session_may_expand_the_accounting_envelope() -> None:
    """An unmanaged (not smart-controlled) session keeps its measured draw.

    HSEM emits no command for a fixed-session-only EV, so the larger
    measured session power is retained purely for accounting purposes.
    """
    slots = _build_slots(
        4,
        start_hour=14,
        import_price=0.20,
        consumption_kwh=0.0,
        interval_minutes=60,
    )
    inp = PlannerInput(
        now_iso=_NOW.isoformat(),
        interval_minutes=60,
        ev_planned_load_enabled=True,
        ev_planned_load_connected=True,
        ev_planned_load_smart_charging_enabled=False,
        ev_planned_load_current_soc_pct=0.0,
        ev_planned_load_target_soc_pct=80.0,
        ev_planned_load_battery_capacity_kwh=10.0,
        ev_planned_load_charger_power_kw=3.0,
        ev_planned_load_charger_efficiency_pct=100.0,
        ev_planned_load_charger_phase_topology=EV_TOPOLOGY_SINGLE_PHASE,
        ev_planned_load_deadline=_NOW + timedelta(hours=4),
        ev_session_charge_kw=6.0,
    )

    configs = _build_ev_configs_for_milp(inp, slots, _NOW)

    assert configs is not None
    assert len(configs) == 1
    ev = configs[0]
    assert ev.fixed_session_only is True
    assert ev.max_charge_per_slot == pytest.approx(6.0)


def test_zero_configured_power_with_live_session_is_fixed_session_only() -> None:
    """A live session with no configured charger power is never managed.

    ``configured_max_power_w <= 1e-9`` forces ``fixed_session_only`` even
    when the EV is otherwise enabled/connected/smart, because there is no
    actuator nameplate to command.
    """
    slots = _build_slots(
        4,
        start_hour=14,
        import_price=0.20,
        consumption_kwh=0.0,
        interval_minutes=60,
    )
    inp = PlannerInput(
        now_iso=_NOW.isoformat(),
        interval_minutes=60,
        ev_planned_load_enabled=True,
        ev_planned_load_connected=True,
        ev_planned_load_smart_charging_enabled=True,
        ev_planned_load_current_soc_pct=0.0,
        ev_planned_load_target_soc_pct=80.0,
        ev_planned_load_battery_capacity_kwh=10.0,
        ev_planned_load_charger_power_kw=0.0,
        ev_planned_load_charger_efficiency_pct=100.0,
        ev_planned_load_charger_phase_topology=EV_TOPOLOGY_SINGLE_PHASE,
        ev_planned_load_deadline=_NOW + timedelta(hours=4),
        ev_session_charge_kw=6.0,
    )

    configs = _build_ev_configs_for_milp(inp, slots, _NOW)

    assert configs is not None
    assert len(configs) == 1
    ev = configs[0]
    assert ev.fixed_session_only is True
    assert ev.max_charge_per_slot == pytest.approx(6.0)


# ---------------------------------------------------------------------------
# Re-portioning
# ---------------------------------------------------------------------------


def test_residue_is_reportioned_instead_of_discarded() -> None:
    """The live 2026-08-23 case: a fuse-locked plan strands 0.2655 kWh.

    Three slots are already at their fuse ceiling after concentration (no
    ``room_dc`` headroom left), so the fragment concentration could not
    place must open a further, previously untouched slot at the charger
    minimum and borrow the shortfall back.
    """
    values = {0: 1.601, 1: 2.064, 2: 2.370}
    placed, deficit, opened = _repair(values, 0.2655, room={4: 5.0})

    assert opened == 4
    assert deficit == pytest.approx(0.0, abs=1e-9)
    assert sum(placed.values()) == pytest.approx(
        1.601 + 2.064 + 2.370 + 0.2655, abs=1e-9
    )
    assert len(placed) == 4
    for dc in placed.values():
        assert dc >= _MIN_DC - 1e-9


def test_reportioning_never_commands_below_the_charger_minimum() -> None:
    """Every commanded slot stays runnable after energy is moved around."""
    placed, _deficit, opened = _repair({0: 2.5}, 0.30, room={2: 5.0})
    assert opened == 2
    for dc in placed.values():
        assert dc >= _MIN_DC - 1e-9


def test_rounding_residue_does_not_churn_a_clean_plan() -> None:
    """Sub-milliwatt-hour residues are immaterial and must be left alone.

    Rounding the rated power to whole watts leaves a tiny residue on every
    solve.  Re-portioning that would open a whole extra slot for nothing.
    """
    values = {0: 2.4843, 1: 2.4843}
    placed, deficit, opened = _repair(values, 0.0003, room={2: 99.0, 3: 99.0})
    assert opened is None
    assert set(placed) == {0, 1}
    assert 0.0 < deficit < 0.001


def test_no_eligible_candidate_leaves_the_residue_unplaced() -> None:
    """Without a candidate with headroom, the residue is reported, not lost."""
    values = {0: 2.5, 1: 0.30}
    placed, deficit, opened = _repair(values, 0.30, room={})
    assert opened is None
    assert deficit == pytest.approx(0.30, abs=1e-9)
    assert placed == values


def test_no_deadline_never_opens_a_slot() -> None:
    """Past-target (surplus-only) EVs have no target to protect."""
    placed, deficit, opened = _redistribute_below_minimum_power(
        {0: 2.5},
        minimum_dc=_MIN_DC,
        deadline_lp_limit=None,
        session_slots_set=set(),
        room_dc=lambda t: 99.0,
        donor_energy=0.30,
    )
    assert opened is None
    assert deficit == pytest.approx(0.30, abs=1e-9)
    assert placed == {0: 2.5}


def test_reportioning_preserves_total_energy() -> None:
    """Energy is moved between slots, never created."""
    values = {0: 1.9, 1: 1.9, 2: 0.40}
    donor_energy = 0.40
    total_before = sum(values.values()) + donor_energy
    placed, deficit, _opened = _repair(values, donor_energy, room={3: 5.0})
    assert sum(placed.values()) + deficit == pytest.approx(total_before, abs=1e-9)
