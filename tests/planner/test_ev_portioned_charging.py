"""EV charging ceiling: amp conversion and stranded-residue re-portioning.

Ported from Ambilights/hsem-ambilights#27 (commit 372d70f7).  HSEM already
decides how much to charge in every future slot; these tests pin the
behaviour that makes the published ceiling trustworthy: the amp conversion
always rounds down, and a residue too small for the charger to run must be
re-portioned into a further slot rather than discarded.

As of issue #797, production managed-EV write-out no longer calls
``_redistribute_below_minimum_power`` at all: the solver-native whole-amp
lattice (``planner/milp/_ev_amp_lattice.py``) links every managed EV's
charge energy to an executable amp command by equality during the solve
itself, so the published schedule is already whole-amp-exact and needs no
post-solve concentration or quantization.  ``_redistribute_below_minimum_power``
and ``_quantize_one_ev_allocation`` (``planner/milp/_ev_quantize.py``) remain
as standalone pure functions for direct/compatibility callers.  These tests
exercise the re-portioning tail directly, with ``values`` representing a
supplied per-slot EV allocation.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.planner.engine_ev_milp import _build_ev_configs_for_milp
from custom_components.hsem.planner.milp._ev_quantize import (
    _redistribute_below_minimum_power,
)
from custom_components.hsem.planner.milp_optimizer import is_scipy_available, solve_milp
from custom_components.hsem.utils.phase_power import (
    EV_TOPOLOGY_SINGLE_PHASE,
    EV_TOPOLOGY_THREE_PHASE_BALANCED,
    charger_current_to_power_w,
    charger_max_power_to_current_a,
    charger_min_power_to_current_a,
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


def test_balanced_nameplate_and_threshold_use_asymmetric_rounding() -> None:
    """11.0 kW is a 16 A nameplate while 3.6 kW starts at 6 A.

    Nameplate: 11,000 / (230 * 3) = 15.94 A -> nearest = 16 A.
    Threshold: 3,600 / (230 * 3) = 5.22 A -> ceiling = 6 A.
    An individual 11,000 W slot remains a floor and therefore exposes 15 A.
    """
    topology = EV_TOPOLOGY_THREE_PHASE_BALANCED

    assert charger_max_power_to_current_a(11_000.0, topology) == 16
    assert charger_min_power_to_current_a(3_600.0, topology) == 6
    assert charger_power_to_current_a(11_000.0, topology) == 15
    assert charger_current_to_power_w(16, topology) == pytest.approx(11_040.0)
    assert charger_current_to_power_w(6, topology) == pytest.approx(4_140.0)


@pytest.mark.parametrize("current_a", [0.0, -1.0, float("nan"), float("inf")])
def test_current_to_power_rejects_unusable_current(current_a: float) -> None:
    """Zero, negative and non-finite currents publish no power."""
    assert (
        charger_current_to_power_w(current_a, EV_TOPOLOGY_THREE_PHASE_BALANCED) == 0.0
    )


@pytest.mark.parametrize("power_w", [0.0, -1.0, float("nan"), float("inf")])
def test_max_power_rounding_rejects_unusable_power(power_w: float) -> None:
    """Zero, negative and non-finite nameplate power rounds to 0 A."""
    assert (
        charger_max_power_to_current_a(power_w, EV_TOPOLOGY_THREE_PHASE_BALANCED) == 0
    )


@pytest.mark.parametrize("power_w", [0.0, -1.0, float("nan"), float("inf")])
def test_min_power_rounding_rejects_unusable_power(power_w: float) -> None:
    """Zero, negative and non-finite threshold power rounds to 0 A."""
    assert (
        charger_min_power_to_current_a(power_w, EV_TOPOLOGY_THREE_PHASE_BALANCED) == 0
    )


# ---------------------------------------------------------------------------
# Nameplate cap (issue #789)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not is_scipy_available(),
    reason="scipy not available in this environment",
)
def test_managed_session_cannot_expand_the_configured_nameplate() -> None:
    """A measured 6 kW draw cannot turn a configured 3 kW cap into a command.

    The single-phase configured nameplate snaps to 13 A / 2990 W.  A managed
    (smart-controlled) live session is capped there even if its power sensor
    currently reports a higher draw; only an unmanaged accounting-only
    session (``fixed_session_only``) may retain the larger physical
    observation.
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
    assert ev.max_charge_per_slot == pytest.approx(2.99)

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
    commands = [slot.ev_charger_calculated_power for slot in planned]
    delivered_dc = sum(slot.ev_total_planned_load_kwh for slot in planned)
    assert all(command <= 2_990.0 for command in commands)
    assert all(command % 230.0 == pytest.approx(0.0) for command in commands)
    assert 8.0 <= delivered_dc <= 8.23 + 1e-9
    # The 6 kW observation is telemetry only; no command follows or exceeds it.
    assert all(command != pytest.approx(6_000.0) for command in commands)


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


# ---------------------------------------------------------------------------
# Discharge permission (issue #797)
# ---------------------------------------------------------------------------


def test_ev_discharge_permission_reaches_both_milp_configs() -> None:
    """Primary and second-EV Huawei-discharge opt-ins remain independent."""
    slots = _build_slots(
        4,
        start_hour=14,
        import_price=0.20,
        consumption_kwh=0.0,
        interval_minutes=60,
    )
    deadline = _NOW + timedelta(hours=4)
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
        ev_planned_load_deadline=deadline,
        ev_planned_load_force_max_discharge_power=False,
        ev_planned_load_max_discharge_power_w=2_400.0,
        ev_second_planned_load_enabled=True,
        ev_second_planned_load_connected=True,
        ev_second_planned_load_smart_charging_enabled=True,
        ev_second_planned_load_current_soc_pct=0.0,
        ev_second_planned_load_target_soc_pct=80.0,
        ev_second_planned_load_battery_capacity_kwh=10.0,
        ev_second_planned_load_charger_power_kw=3.0,
        ev_second_planned_load_charger_efficiency_pct=100.0,
        ev_second_planned_load_deadline=deadline,
        ev_second_planned_load_force_max_discharge_power=True,
        ev_second_planned_load_max_discharge_power_w=5_000.0,
    )

    configs = _build_ev_configs_for_milp(inp, slots, _NOW)

    assert configs is not None
    assert {
        ev.is_second: (ev.force_max_discharge_power, ev.max_discharge_power_w)
        for ev in configs
    } == {
        False: (False, 2_400.0),
        True: (True, 5_000.0),
    }


@pytest.mark.skipif(
    not is_scipy_available(),
    reason="scipy not available in this environment",
)
def test_snapped_11_kw_nameplate_retains_16_amp_solver_bound() -> None:
    """Independent efficiency rounding cannot turn 11.04 kW into 15 A."""
    slots = _build_slots(
        1,
        start_hour=14,
        import_price=0.20,
        consumption_kwh=0.0,
        interval_minutes=15,
    )
    inp = PlannerInput(
        now_iso=_NOW.isoformat(),
        interval_minutes=15,
        ev_planned_load_enabled=True,
        ev_planned_load_connected=True,
        ev_planned_load_smart_charging_enabled=True,
        ev_planned_load_current_soc_pct=0.0,
        ev_planned_load_target_soc_pct=10.0,
        ev_planned_load_battery_capacity_kwh=30.0,
        ev_planned_load_charger_power_kw=11.0,
        ev_planned_load_charger_efficiency_pct=93.127,
        ev_planned_load_charger_min_power_w=3_600.0,
        ev_planned_load_charger_phase_topology=EV_TOPOLOGY_THREE_PHASE_BALANCED,
        ev_planned_load_deadline=_NOW + timedelta(minutes=15),
    )
    configs = _build_ev_configs_for_milp(inp, slots, _NOW)

    assert configs is not None
    assert len(configs) == 1
    ev = configs[0]
    recovered_ac_power_w = (
        ev.max_charge_per_slot / ev.charger_efficiency / 0.25 * 1000.0
    )
    assert recovered_ac_power_w == pytest.approx(11_040.0)

    result = solve_milp(
        slots,
        _NOW,
        current_kwh=0.0,
        usable_kwh=10.0,
        max_charge_per_slot=1.0,
        max_discharge_per_slot=0.0,
        ev_configs=configs,
        no_export=True,
    )

    assert result is not None
    planned, _diagnostics = result
    assert planned[0].ev_charger_calculated_power == pytest.approx(11_040.0)
