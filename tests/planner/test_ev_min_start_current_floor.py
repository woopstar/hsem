"""EV minimum-power-to-current floor: no command below 6 A (issue #968).

``charger_min_power_w`` is documented and defaulted as a single-phase watt
figure (1380 W = 230 V x 6 A), but the conversion to an amp command is
phase-topology-aware. On a ``three_phase_balanced`` charger the same 1380 W
value divides across 3 phases and computes to 2 A -- below any real EVSE's
minimum start current (6 A per IEC 61851). Reproduced live against a
go-eCharger V4: HSEM published ``SetChargingProfile limit: 2``, and the
charger flipped to ``SuspendedEVSE``.

Every site that turns a configured minimum-power threshold into an
executable amp floor must go through
``utils.phase_power.ev_min_start_current_a``, which applies a hard 6 A
floor on top of the raw (unfloored) conversion.
"""

from __future__ import annotations

import numpy as np
import pytest

from custom_components.hsem.models.ev_config import EVConfig
from custom_components.hsem.planner.milp._ev_amp_lattice import (
    resolve_ev_amp_plan,
    target_cap_activation_quantum_dc,
)
from custom_components.hsem.utils.phase_power import (
    EV_MIN_START_CURRENT_A,
    EV_TOPOLOGY_SINGLE_PHASE,
    EV_TOPOLOGY_THREE_PHASE_BALANCED,
    charger_min_power_to_current_a,
    ev_min_start_current_a,
)

# HSEM's documented default -- correct for single-phase (230 V x 6 A) but
# silently wrong once spread across a three-phase charger's 3 phases.
_DEFAULT_MIN_POWER_W = 1380.0


def test_default_min_power_raw_conversion_is_2a_on_three_phase() -> None:
    """Pin the underlying bug in the raw (unfloored) conversion.

    ``charger_min_power_to_current_a`` stays a pure conversion primitive --
    it is ``ev_min_start_current_a`` that must apply the real-world floor,
    so callers that only need the raw arithmetic (whole-amp bookkeeping,
    diagnostics) keep an unmodified building block.
    """
    assert (
        charger_min_power_to_current_a(
            _DEFAULT_MIN_POWER_W, EV_TOPOLOGY_THREE_PHASE_BALANCED
        )
        == 2
    )


def test_ev_min_start_current_floors_three_phase_default_to_6a() -> None:
    """The default 1380 W min-power never computes below 6 A on 3-phase."""
    assert (
        ev_min_start_current_a(_DEFAULT_MIN_POWER_W, EV_TOPOLOGY_THREE_PHASE_BALANCED)
        == EV_MIN_START_CURRENT_A
        == 6
    )


def test_ev_min_start_current_leaves_correct_single_phase_default_unchanged() -> None:
    """The single-phase default already computes exactly 6 A -- no change."""
    assert ev_min_start_current_a(_DEFAULT_MIN_POWER_W, EV_TOPOLOGY_SINGLE_PHASE) == 6


def test_ev_min_start_current_floors_zero_configured_power() -> None:
    """A misconfigured 0 W threshold still floors to the real EVSE minimum."""
    assert (
        ev_min_start_current_a(0.0, EV_TOPOLOGY_THREE_PHASE_BALANCED)
        == EV_MIN_START_CURRENT_A
    )


@pytest.mark.parametrize(
    "topology", [EV_TOPOLOGY_SINGLE_PHASE, EV_TOPOLOGY_THREE_PHASE_BALANCED]
)
def test_ev_min_start_current_never_lowers_an_already_adequate_floor(
    topology: str,
) -> None:
    """A generously configured minimum is never dragged down to 6 A."""
    assert ev_min_start_current_a(11_000.0, topology) > EV_MIN_START_CURRENT_A


def _ev(topology: str, **overrides: object) -> EVConfig:
    base: dict[str, object] = {
        "enabled": True,
        "initial_soc_kwh": 0.0,
        "target_kwh": 10.0,
        "capacity_kwh": 40.0,
        "max_charge_per_slot": 11.0,
        "charger_efficiency": 1.0,
        "charger_min_power_w": _DEFAULT_MIN_POWER_W,
        "charger_phase_topology": topology,
        "deadline_slot": 3,
    }
    base.update(overrides)
    return EVConfig(**base)  # type: ignore[arg-type]


def test_resolve_ev_amp_plan_floors_three_phase_default_minimum_to_6a() -> None:
    """The MILP amp lattice's bound is the real-world floor, not raw math.

    Reproduces the go-eCharger V4 field failure (issue #968): a
    ``three_phase_balanced`` charger left at the default 1380 W minimum used
    to bound the amp lattice at 2 A, which HSEM then published as
    ``SetChargingProfile limit: 2`` -- below any real EVSE's start current,
    so the charger flipped to ``SuspendedEVSE``.
    """
    ev = _ev(EV_TOPOLOGY_THREE_PHASE_BALANCED)
    plan = resolve_ev_amp_plan([ev], max_dis=0.0, slot_hours=1.0)

    assert len(plan.specs) == 1
    spec = plan.specs[0]
    assert spec.managed is True
    assert spec.minimum_current_a == EV_MIN_START_CURRENT_A == 6
    # The charger can still run: its rated current is well above the floor.
    assert spec.runnable is True


def test_resolve_ev_amp_plan_single_phase_default_is_unaffected() -> None:
    """The correct single-phase default (6 A exactly) is unchanged."""
    ev = _ev(EV_TOPOLOGY_SINGLE_PHASE)
    plan = resolve_ev_amp_plan([ev], max_dis=0.0, slot_hours=1.0)

    assert plan.specs[0].minimum_current_a == 6


def test_target_cap_activation_quantum_uses_the_floored_minimum() -> None:
    """The target-cap slack quantum must match the lattice's real floor.

    If this used the raw (un-floored) 2 A conversion while the lattice
    itself floors to 6 A, the quantum would under-estimate the largest
    single-slot jump the solver can actually take, reopening the
    under-budgeted-slack class of bug issue #797 fixed.
    """
    ev = _ev(EV_TOPOLOGY_THREE_PHASE_BALANCED)
    available_slot_hours = np.array([1.0, 1.0, 1.0, 1.0])
    quantum = target_cap_activation_quantum_dc(
        ev, d=0, available_slot_hours=available_slot_hours
    )
    # 6 A * 230 V * 3 phases * 1 h / 1000 = 4.14 kWh at 100% efficiency.
    assert quantum == pytest.approx(4.14)
