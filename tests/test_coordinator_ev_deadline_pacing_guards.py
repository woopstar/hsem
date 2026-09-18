"""Guard-clause tests for the EV deadline-pacing replan trigger (issue #845).

``test_coordinator.py`` covers the main decision (reachable vs unreachable
target and the cadence gate). These tests cover the cases where the trigger
must stay silent because the question cannot be asked meaningfully: no SoC
reading, an already-reached target, an unusable deadline, or an
unconfigured charger.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.hsem.coordinator import HSEMDataUpdateCoordinator
from custom_components.hsem.models.live_state import LiveState
from tests.coordinator_fixtures import make_real_coordinator

_NOW = datetime(2026, 8, 23, 8, 0, tzinfo=UTC)
_DEADLINE = _NOW + timedelta(hours=1)


def _coordinator() -> HSEMDataUpdateCoordinator:
    """Return a coordinator with both chargers configured and past cadence."""
    coordinator = make_real_coordinator()
    for prefix in ("ev_planned_load", "ev_second_planned_load"):
        setattr(coordinator._cfg, f"{prefix}_battery_capacity_kwh", 100.0)
        setattr(coordinator._cfg, f"{prefix}_charger_power_kw", 7.0)
        setattr(coordinator._cfg, f"{prefix}_charger_efficiency_pct", 100.0)
        setattr(coordinator._cfg, f"{prefix}_deadline_safety_margin_pct", 10.0)
    # Old enough that the minimum-cadence gate is already satisfied.
    coordinator._last_plan_slot_start = _NOW - timedelta(seconds=200)
    return coordinator


def _unreachable_live() -> LiveState:
    """Return a live snapshot whose EV cannot reach its target in time."""
    live = LiveState()
    live.ev_planned_load_connected = True
    live.ev_planned_load_smart_charging_enabled = True
    live.ev_planned_load_target_soc_pct = 80.0
    live.ev_planned_load_deadline = _DEADLINE
    live.ev.effective_soc_pct = 20.0
    return live


class TestSecondEvIsCheckedToo:
    """The second EV gets the same pacing guarantee as the first."""

    def test_second_ev_alone_can_trigger_a_replan(self) -> None:
        """An unreachable second-EV deadline requests a replan."""
        coordinator = _coordinator()
        live = LiveState()
        live.ev_second_planned_load_connected = True
        live.ev_second_planned_load_smart_charging_enabled = True
        live.ev_second_planned_load_target_soc_pct = 80.0
        live.ev_second_planned_load_deadline = _DEADLINE
        live.ev_second.effective_soc_pct = 20.0

        assert coordinator._ev_deadline_pacing_requires_replan(live, _NOW) is True


def _no_soc_reading(live: LiveState) -> None:
    live.ev.effective_soc_pct = None


def _target_already_reached(live: LiveState) -> None:
    live.ev.effective_soc_pct = 85.0


def _deadline_in_the_past(live: LiveState) -> None:
    live.ev_planned_load_deadline = _NOW - timedelta(minutes=1)


def _deadline_not_a_datetime(live: LiveState) -> None:
    live.ev_planned_load_deadline = "tomorrow"


def _smart_charging_off(live: LiveState) -> None:
    live.ev_planned_load_smart_charging_enabled = False


_SILENT_CASES: list[Callable[[LiveState], None]] = [
    _no_soc_reading,
    _target_already_reached,
    _deadline_in_the_past,
    _deadline_not_a_datetime,
    _smart_charging_off,
]


class TestPacingStaysSilent:
    """Unanswerable or already-satisfied cases never force a replan."""

    def test_baseline_case_would_trigger(self) -> None:
        """Sanity check: the unmodified snapshot does request a replan."""
        assert (
            _coordinator()._ev_deadline_pacing_requires_replan(
                _unreachable_live(), _NOW
            )
            is True
        )

    @pytest.mark.parametrize(
        "make_silent", _SILENT_CASES, ids=lambda fn: fn.__name__.lstrip("_")
    )
    def test_guard_clause_suppresses_the_trigger(
        self, make_silent: Callable[[LiveState], None]
    ) -> None:
        """Each guard clause on its own silences the trigger."""
        live = _unreachable_live()
        make_silent(live)

        assert _coordinator()._ev_deadline_pacing_requires_replan(live, _NOW) is False

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("ev_planned_load_battery_capacity_kwh", 0.0),
            ("ev_planned_load_charger_power_kw", 0.0),
        ],
    )
    def test_unconfigured_charger_is_skipped(self, field: str, value: float) -> None:
        """Without a capacity or charger rating there is nothing to pace."""
        coordinator = _coordinator()
        setattr(coordinator._cfg, field, value)

        assert (
            coordinator._ev_deadline_pacing_requires_replan(_unreachable_live(), _NOW)
            is False
        )
