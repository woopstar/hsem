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
    ev_phase_share,
    normalize_ev_phase_topology,
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
