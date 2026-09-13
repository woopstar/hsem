"""EV charger phase topology across every hard per-phase site.

The per-phase fuse model is expressed in three independent places: the hard
MILP constraint rows, the reconstruction from a solved decision vector, and
the validation of the final published plan.  All three must agree on how much
of an EV command a single phase can carry, or the solver can produce a plan
that its own validator later erases.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.models.ev_config import EVConfig
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.planner.milp._phase_fuse import (
    phase_envelope_from_published_slots,
)
from custom_components.hsem.planner.milp_optimizer import (
    is_scipy_available,
    solve_milp,
)
from custom_components.hsem.utils.phase_power import (
    EV_TOPOLOGY_SINGLE_PHASE,
    EV_TOPOLOGY_THREE_PHASE_BALANCED,
    PHASE_COUNT,
    charger_max_power_to_current_a,
    charger_power_to_current_a,
    ev_phase_share,
    ev_phase_share_for_power_w,
    ev_switchable_single_phase_max_power_w,
    normalize_ev_phase_topology,
    switchable_power_to_current_and_power_w,
)
from custom_components.hsem.utils.prices import SlotPrice

_TZ = ZoneInfo("Europe/Copenhagen")
_SLOT_START = datetime(2024, 6, 15, 14, 0, tzinfo=_TZ)

# 16 A x 230 V = 3680 W of single-phase headroom, below the 6 A three-phase
# charger minimum of 4140 W.  This is the real installation shape: the charger
# cannot start at all while the whole command is assumed to be single-phase.
_FUSE_AMPS = 16.0
_CHARGER_MIN_W = 4140.0
_CHARGER_KW = 4.14


def _ev(topology: str, **overrides: object) -> EVConfig:
    """Return an EV needing exactly one full slot at the charger minimum."""
    base: dict[str, object] = {
        "enabled": True,
        "initial_soc_kwh": 0.0,
        "target_kwh": _CHARGER_KW,
        "capacity_kwh": 40.0,
        "max_charge_per_slot": _CHARGER_KW,
        "charger_efficiency": 1.0,
        "charger_min_power_w": _CHARGER_MIN_W,
        "charger_phase_topology": topology,
        "deadline_slot": 2,
    }
    base.update(overrides)
    return EVConfig(**base)  # type: ignore[arg-type]


def _slots(count: int = 3) -> list[PlannedSlot]:
    """Return cheap hourly slots so only the fuse model can block charging."""
    return [
        PlannedSlot(
            start=_SLOT_START + timedelta(hours=offset),
            end=_SLOT_START + timedelta(hours=offset + 1),
            price=SlotPrice(import_price=0.1, export_price=0.0),
        )
        for offset in range(count)
    ]


def _solve(ev: EVConfig) -> tuple[list[PlannedSlot], dict] | None:
    """Solve with per-phase protection active and nothing else competing."""
    return solve_milp(
        _slots(),
        _SLOT_START,
        current_kwh=0.0,
        usable_kwh=10.0,
        max_charge_per_slot=1.0,
        max_discharge_per_slot=0.0,
        charge_efficiency_pct=100.0,
        discharge_efficiency_pct=100.0,
        ev_configs=[ev],
        main_fuse_amps=_FUSE_AMPS,
        main_fuse_phases=PHASE_COUNT,
        no_export=True,
    )


def test_ev_phase_share_defaults_to_the_whole_command() -> None:
    """Unknown, missing and single-phase topologies keep the safe envelope."""
    assert ev_phase_share(EV_TOPOLOGY_SINGLE_PHASE) == pytest.approx(1.0)
    assert ev_phase_share(None) == pytest.approx(1.0)
    assert ev_phase_share("not_a_topology") == pytest.approx(1.0)


def test_ev_phase_share_splits_a_balanced_three_phase_charger() -> None:
    """A balanced charger places exactly one third on each phase."""
    assert ev_phase_share(EV_TOPOLOGY_THREE_PHASE_BALANCED) == pytest.approx(
        1.0 / PHASE_COUNT
    )


@pytest.mark.parametrize(
    "stored",
    [None, "", "three_phase", 3, True, "SINGLE_PHASE"],
)
def test_normalize_rejects_unsupported_values(stored: object) -> None:
    """Pre-feature and hand-edited entries never relax the fuse constraint."""
    assert normalize_ev_phase_topology(stored) == EV_TOPOLOGY_SINGLE_PHASE


def test_normalize_preserves_supported_values() -> None:
    """A configured topology survives the round trip unchanged."""
    for topology in (EV_TOPOLOGY_SINGLE_PHASE, EV_TOPOLOGY_THREE_PHASE_BALANCED):
        assert normalize_ev_phase_topology(topology) == topology


def test_ev_config_defaults_to_single_phase() -> None:
    """An EV built without a topology keeps the pre-feature behaviour."""
    ev = EVConfig()
    assert ev.charger_phase_topology == EV_TOPOLOGY_SINGLE_PHASE
    assert ev.phase_share == pytest.approx(1.0)


@pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available in this environment"
)
def test_single_phase_charger_cannot_start_below_phase_headroom() -> None:
    """Regression: the conservative envelope still blocks the charger.

    16 A of single-phase headroom is 3680 W, under the 4140 W minimum, so no
    slot can host the command and the EV is left uncharged.
    """
    result = _solve(_ev(EV_TOPOLOGY_SINGLE_PHASE))

    assert result is not None
    planned, _diagnostics = result
    assert sum(slot.ev_total_planned_load_kwh for slot in planned) == pytest.approx(0.0)


@pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available in this environment"
)
def test_three_phase_charger_fits_the_same_fuse() -> None:
    """A balanced charger needs only 6 A per phase and is schedulable."""
    result = _solve(_ev(EV_TOPOLOGY_THREE_PHASE_BALANCED))

    assert result is not None
    planned, _diagnostics = result
    charged = sum(slot.ev_total_planned_load_kwh for slot in planned)
    assert charged == pytest.approx(_CHARGER_KW, rel=1e-3)


@pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available in this environment"
)
def test_published_plan_survives_its_own_phase_validation() -> None:
    """Constraint rows and post-solve validation must not disagree.

    A three-phase plan that the solver accepts has to survive reconstruction
    and published-plan validation too.  If any of those sites still assumed a
    single-phase charger the EV command would be erased before write-out.
    """
    result = _solve(_ev(EV_TOPOLOGY_THREE_PHASE_BALANCED))

    assert result is not None
    planned, diagnostics = result
    assert sum(slot.ev_total_planned_load_kwh for slot in planned) > 1e-9
    assert not diagnostics.get("has_violations", False)
    # The executable command survives to write-out rather than being zeroed.
    assert max(slot.ev_charger_calculated_power for slot in planned) > 1e-9
    # No phase envelope exceeds the rated fuse for the solved plan.
    phase_limit_kwh = _FUSE_AMPS * 230.0 / 1000.0
    assert diagnostics["max_phase_import_kwh"] <= phase_limit_kwh + 1e-6


def test_phase_envelope_from_published_slots_returns_a_plain_float() -> None:
    """Regression: the function returns just the envelope, not a diagnostic pair.

    ``total_excess_kwh`` was declared and returned but never updated, and its
    sole caller discarded it — the dead second tuple element was removed.
    """
    slot = _slots(1)[0]
    slot.grid_import_kwh = 3.0
    slot.grid_export_kwh = 0.0

    max_phase_kwh = phase_envelope_from_published_slots(
        out_slots=[slot],
        future_idx=[0],
        active_evs=[],
        session_slots_by_ev={},
        slot_hours=1.0,
    )

    assert isinstance(max_phase_kwh, float)
    assert max_phase_kwh == pytest.approx(3.0 / PHASE_COUNT)


# ---------------------------------------------------------------------------
# Auto-phase-switching chargers (issue #1001)
#
# A switchable charger starts at 6 A on ONE phase (1380 W) and switches to
# balanced three-phase above its one-phase ceiling (230 V x rated amps).
# ---------------------------------------------------------------------------

_SWITCHABLE = "three_phase_switchable"
_SWITCHABLE_MAX_KWH = 11.04  # 16 A x 230 V x 3 x 1 h


def _switchable_ev(target_kwh: float, **overrides: object) -> EVConfig:
    """Return an 11 kW auto-switching charger with a 1380 W minimum."""
    base: dict[str, object] = {
        "enabled": True,
        "initial_soc_kwh": 0.0,
        "target_kwh": target_kwh,
        "capacity_kwh": 40.0,
        "max_charge_per_slot": _SWITCHABLE_MAX_KWH,
        "charger_efficiency": 1.0,
        "charger_min_power_w": 1380.0,
        "charger_phase_topology": _SWITCHABLE,
        "deadline_slot": 0,
    }
    base.update(overrides)
    return EVConfig(**base)  # type: ignore[arg-type]


def _solve_fused(
    ev: EVConfig, fuse_amps: float, count: int = 4
) -> tuple[list[PlannedSlot], dict] | None:
    """Solve a cheap-price horizon with per-phase protection at *fuse_amps*."""
    return solve_milp(
        _slots(count),
        _SLOT_START,
        current_kwh=0.0,
        usable_kwh=10.0,
        max_charge_per_slot=1.0,
        max_discharge_per_slot=0.0,
        charge_efficiency_pct=100.0,
        discharge_efficiency_pct=100.0,
        ev_configs=[ev],
        main_fuse_amps=fuse_amps,
        main_fuse_phases=PHASE_COUNT,
        no_export=True,
    )


def test_normalize_preserves_switchable() -> None:
    """The switchable topology survives the normalization round trip."""
    assert normalize_ev_phase_topology(_SWITCHABLE) == _SWITCHABLE


def test_switchable_static_share_stays_conservative() -> None:
    """The static share helper never relaxes a mode-dependent charger."""
    assert ev_phase_share(_SWITCHABLE) == pytest.approx(1.0)


@pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available in this environment"
)
def test_switchable_starts_at_single_phase_minimum() -> None:
    """1380 W (6 A x 230 V x 1) is executable — the #1001 headline case."""
    result = _solve_fused(_switchable_ev(1.380), fuse_amps=16.0)

    assert result is not None
    planned, _ = result
    powers = [slot.ev_charger_calculated_power for slot in planned]
    assert max(powers) == pytest.approx(1380.0)


@pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available in this environment"
)
def test_switchable_reaches_three_phase_nameplate_at_fuse_limit() -> None:
    """16 A three-phase is exactly 3680 W per phase — the 16 A fuse itself."""
    result = _solve_fused(_switchable_ev(11.04), fuse_amps=16.0)

    assert result is not None
    planned, diagnostics = result
    assert planned[0].ev_charger_calculated_power == pytest.approx(11_040.0)
    assert not diagnostics.get("has_violations", False)


@pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available in this environment"
)
def test_switchable_throttles_to_fuse_in_three_phase_mode() -> None:
    """A 15 A fuse caps the charger at 15 A three-phase (10.35 kW)."""
    result = _solve_fused(_switchable_ev(11.04), fuse_amps=15.0)

    assert result is not None
    planned, _ = result
    assert planned[0].ev_charger_calculated_power == pytest.approx(10_350.0)


@pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available in this environment"
)
def test_switchable_never_plans_the_unexecutable_gap() -> None:
    """Powers between the one-phase ceiling and the 3-phase minimum are impossible.

    For a 16 A charger that gap is 3681–4139 W; a 4.0 kWh one-slot target can
    only be served by the 4140 W lattice point (activation-quantum overshoot),
    never by a power inside the gap.
    """
    result = _solve_fused(_switchable_ev(4.0), fuse_amps=25.0)

    assert result is not None
    planned, _ = result
    for slot in planned:
        power = slot.ev_charger_calculated_power
        assert not (3680.0 < power < 4140.0), f"gap power planned: {power}"


@pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available in this environment"
)
def test_switchable_one_phase_mode_is_checked_as_full_single_phase_load() -> None:
    """A one-phase-mode command may land entirely on one phase.

    With a 10 A fuse (2300 W per phase), a 1.38 kWh target still fits (6 A
    one-phase = 1380 W ≤ 2300 W) but a 2.53 kWh target (11 A = 2530 W) must
    not be served in one-phase mode — and 3-phase 6 A (4140 W = 1380 W per
    phase) fits again. The mode-aware rows must reproduce both outcomes.
    """
    ok = _solve_fused(_switchable_ev(1.380), fuse_amps=10.0)
    assert ok is not None
    assert max(s.ev_charger_calculated_power for s in ok[0]) == pytest.approx(1380.0)

    # 2.53 kWh in one slot: 1-phase 11 A would exceed the 10 A fuse, so the
    # solver must jump the gap to 3-phase 6 A (4140 W, 1380 W per phase).
    jumped = _solve_fused(_switchable_ev(2.53), fuse_amps=10.0)
    assert jumped is not None
    powers = [s.ev_charger_calculated_power for s in jumped[0]]
    assert not any(2300.0 < p <= 3680.0 for p in powers), powers


@pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available in this environment"
)
def test_switchable_fixed_session_matches_balanced_envelope() -> None:
    """A measured 11 kW session is physically balanced — share must be 1/3.

    The same session under ``single_phase`` makes the 16 A fuse model
    infeasible; switchable must reproduce the ``three_phase_balanced``
    outcome exactly.
    """
    fixed = {
        "fixed_session_only": True,
        "session_charge_kw": 11.04,
    }
    switchable = _solve_fused(_switchable_ev(0.0, **fixed), fuse_amps=16.0)  # type: ignore[arg-type]
    balanced = _solve_fused(
        _switchable_ev(
            0.0, charger_phase_topology=EV_TOPOLOGY_THREE_PHASE_BALANCED, **fixed
        ),  # type: ignore[arg-type]
        fuse_amps=16.0,
    )
    single = _solve_fused(
        _switchable_ev(0.0, charger_phase_topology=EV_TOPOLOGY_SINGLE_PHASE, **fixed),  # type: ignore[arg-type]
        fuse_amps=16.0,
    )

    assert switchable is not None and balanced is not None
    assert single is None  # conservative single-phase envelope cannot host it
    assert [s.grid_import_kwh for s in switchable[0]] == pytest.approx(
        [s.grid_import_kwh for s in balanced[0]]
    )


def test_switchable_conversion_helpers() -> None:
    """Unit-check the switchable watt/amp conversion contract (issue #1001)."""
    # Nameplate snap uses the three-phase basis: 11.0 kW -> 16 A.
    assert charger_max_power_to_current_a(11_000.0, _SWITCHABLE) == 16
    # Mode-aware power->amps with a 16 A rating.
    assert charger_power_to_current_a(2300.0, _SWITCHABLE, rated_current_a=16) == 10
    assert charger_power_to_current_a(6900.0, _SWITCHABLE, rated_current_a=16) == 10
    # The 3681-4139 W gap publishes the one-phase ceiling, never sub-6A amps.
    assert charger_power_to_current_a(4000.0, _SWITCHABLE, rated_current_a=16) == 16
    assert charger_power_to_current_a(11_040.0, _SWITCHABLE, rated_current_a=16) == 16
    # Round-trip helper agrees and returns executable powers.
    assert switchable_power_to_current_and_power_w(2300.0, 16) == (10, 2300.0)
    assert switchable_power_to_current_and_power_w(6900.0, 16) == (10, 6900.0)
    assert switchable_power_to_current_and_power_w(4000.0, 16) == (16, 3680.0)


def test_switchable_power_aware_share() -> None:
    """The share follows the physical mode: whole command vs balanced third."""
    assert ev_phase_share_for_power_w(
        _SWITCHABLE, 2300.0, single_phase_max_power_w=3680.0
    ) == pytest.approx(1.0)
    assert ev_phase_share_for_power_w(
        _SWITCHABLE, 11_040.0, single_phase_max_power_w=3680.0
    ) == pytest.approx(1.0 / PHASE_COUNT)
    # Boundary: exactly at the ceiling is still one-phase mode.
    assert ev_phase_share_for_power_w(
        _SWITCHABLE, 3680.0, single_phase_max_power_w=3680.0
    ) == pytest.approx(1.0)
    # Other topologies ignore the power argument.
    assert ev_phase_share_for_power_w(
        EV_TOPOLOGY_THREE_PHASE_BALANCED, 500.0, single_phase_max_power_w=3680.0
    ) == pytest.approx(1.0 / PHASE_COUNT)


def test_switchable_single_phase_max_power_derives_from_the_ev_envelope() -> None:
    """The mode boundary mirrors the lattice's rated-current derivation."""
    assert ev_switchable_single_phase_max_power_w(
        max_charge_per_slot=11.04, charger_efficiency=1.0, slot_hours=1.0
    ) == pytest.approx(3680.0)
    # Defensive: degenerate envelopes produce a zero boundary (share stays 1.0).
    assert ev_switchable_single_phase_max_power_w(
        max_charge_per_slot=0.0, charger_efficiency=1.0, slot_hours=1.0
    ) == pytest.approx(0.0)
