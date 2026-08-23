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

import pytest

from custom_components.hsem.planner.milp._write_results import (
    _redistribute_below_minimum_power,
)
from custom_components.hsem.utils.phase_power import (
    EV_TOPOLOGY_SINGLE_PHASE,
    EV_TOPOLOGY_THREE_PHASE_BALANCED,
    charger_power_to_current_a,
)

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
