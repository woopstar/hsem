"""Guard-clause tests for small planner helpers.

Each of these fails closed: an EV discharge ceiling is zero unless explicitly
opted in with a usable value, a live session needs positive telemetry to
count, post-write inventory validation rejects anything it cannot verify, and
window hysteresis returns "nothing to hold" rather than guessing when there is
no current slot.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest

from custom_components.hsem.models.ev_config import EVConfig
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.planner._scipy_probe import _check_scipy
from custom_components.hsem.planner.milp._ev_amp_lattice import (
    ev_discharge_cap_kwh,
    ev_has_live_session,
)
from custom_components.hsem.planner.milp._postwrite_validation import (
    validate_primary_inventory,
)
from custom_components.hsem.planner.window_hysteresis import apply_window_hysteresis
from custom_components.hsem.utils.recommendations import Recommendations

_SLOT = timedelta(hours=1)
_NOW = datetime(2026, 6, 1, 12, 30, tzinfo=UTC)
_CURRENT_START = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


class TestEvDischargeCap:
    """Vehicle-to-home discharge is off unless explicitly opted in."""

    def test_opted_in_ev_gets_its_energy_ceiling(self) -> None:
        """5 kW over a 15-minute slot allows 1.25 kWh."""
        ev = EVConfig(force_max_discharge_power=True, max_discharge_power_w=5000)

        assert ev_discharge_cap_kwh(ev, 0.25) == pytest.approx(1.25)

    def test_not_opted_in_is_zero(self) -> None:
        """Without the opt-in the EV may not discharge at all."""
        ev = EVConfig(force_max_discharge_power=False, max_discharge_power_w=5000)

        assert ev_discharge_cap_kwh(ev, 1.0) == pytest.approx(0.0)

    @pytest.mark.parametrize(
        "power",
        [
            pytest.param(0, id="zero"),
            pytest.param(-5000, id="negative"),
            pytest.param(float("nan"), id="not_a_number"),
            pytest.param(float("inf"), id="infinite"),
            pytest.param("not a number", id="unparseable"),
            pytest.param(None, id="missing"),
        ],
    )
    def test_unusable_ceiling_is_zero(self, power: Any) -> None:
        """An opt-in without a usable ceiling still means no discharge."""
        ev = EVConfig(force_max_discharge_power=True, max_discharge_power_w=power)

        assert ev_discharge_cap_kwh(ev, 1.0) == pytest.approx(0.0)

    def test_negative_slot_length_cannot_create_energy(self) -> None:
        """A nonsensical slot length yields no allowance rather than a negative."""
        ev = EVConfig(force_max_discharge_power=True, max_discharge_power_w=5000)

        assert ev_discharge_cap_kwh(ev, -1.0) == pytest.approx(0.0)


class TestEvLiveSession:
    """A live session must be proven by positive charger telemetry."""

    def test_positive_session_power_proves_a_session(self) -> None:
        """A measured 7.4 kW draw is a live session."""
        assert ev_has_live_session(EVConfig(session_charge_kw=7.4)) is True

    @pytest.mark.parametrize(
        "session_kw",
        [
            pytest.param(None, id="no_telemetry"),
            pytest.param(0.0, id="zero"),
            pytest.param(-1.0, id="negative"),
            pytest.param(float("nan"), id="not_a_number"),
            pytest.param("not a number", id="unparseable"),
        ],
    )
    def test_unproven_session_is_false(self, session_kw: Any) -> None:
        """Anything short of positive finite telemetry is not a session."""
        assert ev_has_live_session(EVConfig(session_charge_kw=session_kw)) is False


def _inventory_slot(charge: float = 0.0, discharge: float = 0.0) -> PlannedSlot:
    """Return a slot carrying decoded battery energy fields."""
    return PlannedSlot(
        start=_CURRENT_START,
        end=_CURRENT_START + _SLOT,
        batteries_charged_kwh=charge,
        batteries_discharged_kwh=discharge,
    )


class TestPrimaryInventoryValidation:
    """Rounded write-back energy must keep the battery inside its bounds."""

    def test_a_balanced_plan_is_valid(self) -> None:
        """Charging then discharging within capacity validates."""
        slots = [_inventory_slot(charge=2.0), _inventory_slot(discharge=1.0)]

        result = validate_primary_inventory(
            slots, [0, 1], current_kwh=3.0, usable_kwh=9.0
        )

        assert result["valid"] is True
        assert result["reason"] == "ok"

    @pytest.mark.parametrize(
        ("current_kwh", "usable_kwh"),
        [
            pytest.param(float("nan"), 9.0, id="non_finite_current"),
            pytest.param(3.0, float("inf"), id="non_finite_capacity"),
            pytest.param(3.0, -1.0, id="negative_capacity"),
        ],
    )
    def test_unusable_bounds_are_refused(
        self, current_kwh: float, usable_kwh: float
    ) -> None:
        """Without trustworthy bounds nothing can be validated."""
        result = validate_primary_inventory(
            [_inventory_slot()], [0], current_kwh=current_kwh, usable_kwh=usable_kwh
        )

        assert result == {"valid": False, "reason": "invalid_inventory_bounds"}

    @pytest.mark.parametrize(
        ("charge", "discharge"),
        [
            pytest.param(float("nan"), 0.0, id="non_finite_charge"),
            pytest.param(0.0, float("nan"), id="non_finite_discharge"),
            pytest.param(-1.0, 0.0, id="negative_charge"),
            pytest.param(0.0, -1.0, id="negative_discharge"),
        ],
    )
    def test_unusable_slot_energy_is_refused_with_its_position(
        self, charge: float, discharge: float
    ) -> None:
        """The offending slot's sequence number is reported."""
        slots = [_inventory_slot(), _inventory_slot(charge=charge, discharge=discharge)]

        result = validate_primary_inventory(
            slots, [0, 1], current_kwh=3.0, usable_kwh=9.0
        )

        assert result["valid"] is False
        assert result["reason"] == "invalid_primary_energy"
        assert result["slot"] == 1

    def test_discharging_below_the_floor_is_refused(self) -> None:
        """A plan that empties past zero inventory is rejected."""
        slots = [_inventory_slot(discharge=5.0)]

        result = validate_primary_inventory(slots, [0], current_kwh=3.0, usable_kwh=9.0)

        assert result["valid"] is False
        assert result["reason"] == "primary_inventory_below_floor"
        assert result["inventory_kwh"] == pytest.approx(-2.0)

    def test_charging_above_the_ceiling_is_refused(self) -> None:
        """A plan that overfills the battery is rejected."""
        slots = [_inventory_slot(charge=10.0)]

        result = validate_primary_inventory(slots, [0], current_kwh=3.0, usable_kwh=9.0)

        assert result["valid"] is False
        assert result["reason"] == "primary_inventory_above_ceiling"
        assert result["inventory_kwh"] == pytest.approx(13.0)


def _hysteresis_slot(recommendation: str) -> PlannedSlot:
    """Return the current slot carrying *recommendation*."""
    return PlannedSlot(
        start=_CURRENT_START, end=_CURRENT_START + _SLOT, recommendation=recommendation
    )


class TestWindowHysteresis:
    """Without a current slot there is nothing to hold or report."""

    def test_disabled_feature_reports_the_current_recommendation(self) -> None:
        """A zero hold time passes the current slot through untouched."""
        slot = _hysteresis_slot(Recommendations.BatteriesChargeGrid.value)

        held, start = apply_window_hysteresis(
            [slot],
            _NOW,
            window_hysteresis_minutes=0,
            previous_current_recommendation=None,
            previous_current_slot_start=None,
        )

        assert held == Recommendations.BatteriesChargeGrid.value
        assert start == _CURRENT_START

    def test_disabled_feature_without_a_current_slot_reports_nothing(self) -> None:
        """No slot contains ``now``, so there is nothing to report."""
        future = PlannedSlot(
            start=_NOW + _SLOT,
            end=_NOW + 2 * _SLOT,
            recommendation=Recommendations.BatteriesChargeGrid.value,
        )

        assert apply_window_hysteresis(
            [future],
            _NOW,
            window_hysteresis_minutes=0,
            previous_current_recommendation=None,
            previous_current_slot_start=None,
        ) == (None, None)

    def test_enabled_feature_without_a_current_slot_reports_nothing(self) -> None:
        """With hysteresis on, a missing current slot is still nothing to hold."""
        future = PlannedSlot(
            start=_NOW + _SLOT,
            end=_NOW + 2 * _SLOT,
            recommendation=Recommendations.BatteriesChargeGrid.value,
        )

        assert apply_window_hysteresis(
            [future],
            _NOW,
            window_hysteresis_minutes=10,
            previous_current_recommendation=Recommendations.BatteriesWaitMode.value,
            previous_current_slot_start=_CURRENT_START,
        ) == (None, None)

    def test_unchanged_recommendation_needs_no_hold(self) -> None:
        """Repeating the previous decision is not a transition."""
        slot = _hysteresis_slot(Recommendations.BatteriesChargeGrid.value)

        held, start = apply_window_hysteresis(
            [slot],
            _NOW,
            window_hysteresis_minutes=10,
            previous_current_recommendation=Recommendations.BatteriesChargeGrid.value,
            previous_current_slot_start=_CURRENT_START,
        )

        assert held == Recommendations.BatteriesChargeGrid.value
        assert start == _CURRENT_START


class TestScipyProbe:
    """The solver probe is a one-time import check."""

    def test_available_scipy_is_reported(self) -> None:
        """scipy is installed in this environment."""
        assert _check_scipy() is True

    def test_missing_scipy_is_reported_without_raising(self) -> None:
        """A deployment without scipy degrades instead of crashing."""
        with patch.dict("sys.modules", {"scipy.optimize": None, "scipy": None}):
            # ``import scipy.optimize`` raises ImportError when the entry is None.
            assert _check_scipy() is False
