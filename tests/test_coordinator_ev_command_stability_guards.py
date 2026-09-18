"""Guard-clause tests for the EV command-stability layer.

``test_ev_command_stability.py`` covers the deadband, stub-floor, and
slot-tail behaviour. These tests cover the fail-closed guards around them:
an unprovable remaining need, an unmeasurable holding cost, an unusable
planned command, and a cycle with no current slot to write into.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from custom_components.hsem.coordinator import HSEMDataUpdateCoordinator
from custom_components.hsem.coordinator_ev_command_stability import _EvCommandSpec
from custom_components.hsem.models.live_state import EVLiveState, LiveState
from tests.coordinator_fixtures import make_real_coordinator
from tests.test_coordinator_tracking_forecast import _rec

_NOW = datetime(2026, 6, 1, 12, 5, tzinfo=UTC)
_SLOT_START = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
_SLOT_END = _SLOT_START + timedelta(minutes=15)


def _spec(
    *,
    capacity_kwh: float = 100.0,
    target_soc_pct: float = 80.0,
    deadline: Any = _NOW + timedelta(hours=4),
    effective_soc_pct: float | None = 50.0,
) -> _EvCommandSpec:
    """Return a command spec for the primary EV."""
    ev_live = EVLiveState()
    ev_live.effective_soc_pct = effective_soc_pct
    return _EvCommandSpec(
        key="ev",
        label="EV",
        is_second=False,
        deadband_a=1.0,
        stub_floor_minutes=5.0,
        topology="three_phase",
        rated_current_a=16,
        min_current_a=6,
        managed=True,
        ev_live=ev_live,
        capacity_kwh=capacity_kwh,
        target_soc_pct=target_soc_pct,
        deadline=deadline,
    )


class TestEvHasUnmetNeed:
    """Suppressing a stop needs a *proven* remaining need."""

    def test_energy_below_target_before_the_deadline_is_a_need(self) -> None:
        """The baseline case: 50 % now, 80 % wanted, hours left."""
        coordinator = make_real_coordinator()

        assert coordinator._ev_has_unmet_need(_spec(), _NOW) is True

    @pytest.mark.parametrize(
        ("kwargs", "reason"),
        [
            pytest.param({"capacity_kwh": 0.0}, "no capacity", id="no_capacity"),
            pytest.param({"deadline": None}, "no deadline", id="no_deadline"),
            pytest.param(
                {"deadline": _NOW - timedelta(minutes=1)},
                "deadline passed",
                id="deadline_passed",
            ),
            pytest.param(
                {"effective_soc_pct": None}, "no SoC reading", id="no_soc_reading"
            ),
            pytest.param(
                {"effective_soc_pct": 90.0}, "target reached", id="target_reached"
            ),
        ],
    )
    def test_unprovable_need_fails_closed(
        self, kwargs: dict[str, Any], reason: str
    ) -> None:
        """Each guard clause reports "no proven need"."""
        coordinator = make_real_coordinator()

        assert coordinator._ev_has_unmet_need(_spec(**kwargs), _NOW) is False, reason


class TestHoldingCostBypass:
    """A hold is only bypassed when its extra cost is measurable."""

    @pytest.mark.parametrize(
        ("kwargs", "reason"),
        [
            pytest.param({"price_alt": None}, "no alternative slot", id="no_alt_price"),
            pytest.param(
                {"remaining_hours": 0.0}, "no time left", id="no_remaining_time"
            ),
            pytest.param({"price_now": 0.0}, "no planned cost", id="zero_planned_cost"),
            pytest.param({"planned_w": 0.0}, "nothing planned", id="nothing_planned"),
        ],
    )
    def test_unmeasurable_cost_keeps_the_hold(
        self, kwargs: dict[str, Any], reason: str
    ) -> None:
        """Without a comparable cost the deadband is never bypassed."""
        base: dict[str, Any] = {
            "held_w": 11_000.0,
            "planned_w": 3_700.0,
            "remaining_hours": 0.25,
            "price_now": 2.0,
            "price_alt": 0.5,
        }

        assert (
            HSEMDataUpdateCoordinator._holding_cost_exceeds_bypass(**{**base, **kwargs})
            is False
        ), reason

    def test_materially_more_expensive_hold_is_bypassed(self) -> None:
        """Holding far above the plan at a much worse price bypasses."""
        assert (
            HSEMDataUpdateCoordinator._holding_cost_exceeds_bypass(
                held_w=11_000.0,
                planned_w=3_700.0,
                remaining_hours=0.25,
                price_now=2.0,
                price_alt=0.5,
            )
            is True
        )

    def test_cheaper_hold_is_never_bypassed(self) -> None:
        """When holding is cheaper than the plan the deadband stands."""
        assert (
            HSEMDataUpdateCoordinator._holding_cost_exceeds_bypass(
                held_w=11_000.0,
                planned_w=3_700.0,
                remaining_hours=0.25,
                price_now=0.5,
                price_alt=2.0,
            )
            is False
        )


class TestPlannedCommand:
    """The plan's command is read defensively."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param(3_700.0, 3_700.0, id="plain_value"),
            pytest.param(-1.0, 0.0, id="negative_clamped"),
            pytest.param("not a number", 0.0, id="non_numeric"),
            pytest.param(None, 0.0, id="missing"),
        ],
    )
    def test_unusable_values_read_as_zero(self, value: object, expected: float) -> None:
        """A corrupted command field never becomes a charger setpoint."""
        slot = _rec(_SLOT_START, _SLOT_END)
        slot.ev_charger_calculated_power = value  # type: ignore[assignment]  # corrupted plan field

        assert HSEMDataUpdateCoordinator._planned_command_w(
            slot, is_second=False
        ) == pytest.approx(expected)


class TestNoCurrentSlot:
    """Without a current slot there is nothing to stabilise."""

    def test_stability_pass_is_a_noop(self) -> None:
        """A recommendation list not covering ``now`` is left untouched."""
        coordinator = make_real_coordinator()
        slot = _rec(_SLOT_START, _SLOT_END)
        slot.ev_charger_calculated_power = 3_700.0
        coordinator._hourly_recommendations = [slot]

        coordinator._apply_ev_command_stability(
            _SLOT_END + timedelta(minutes=1), LiveState(), coordinator._cfg
        )

        assert slot.ev_charger_calculated_power == pytest.approx(3_700.0)

    def test_exhausted_slot_is_a_noop(self) -> None:
        """A slot with no time left cannot take a new command."""
        coordinator = make_real_coordinator()
        slot = _rec(_SLOT_START, _SLOT_END)
        slot.ev_charger_calculated_power = 3_700.0
        coordinator._hourly_recommendations = [slot]

        coordinator._apply_ev_command_stability(
            _SLOT_END, LiveState(), coordinator._cfg
        )

        assert slot.ev_charger_calculated_power == pytest.approx(3_700.0)
