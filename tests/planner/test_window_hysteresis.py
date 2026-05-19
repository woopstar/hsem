"""Tests for window-level hysteresis (issue #315).

Window-level hysteresis prevents rapid toggling between charge and discharge
recommendations near schedule-window boundaries.  When the current slot's
recommendation changes category (charge ↔ discharge), the previous
recommendation is held for a configurable minimum time.

Acceptance criteria
-------------------
1. Charge/discharge does not flap around boundary conditions.
2. Minimum hold time is configurable.
3. Only cross-category flips (charge ↔ discharge) are held;
   same-category changes (e.g. grid-charge → solar-charge) are allowed.
4. Neutral recommendations (wait_mode, time_passed, None) do not trigger hold.
5. Feature disabled (0 min) always allows the switch.
6. First run (no previous state) always accepts the new recommendation.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from custom_components.hsem.planner.charge_scheduler import apply_window_hysteresis
from custom_components.hsem.utils.prices import SlotPrice
from custom_components.hsem.utils.recommendations import Recommendations

_TZ = ZoneInfo("Europe/Copenhagen")
_NOW = datetime(2024, 6, 15, 12, 0, tzinfo=_TZ)


def _make_slots(
    *recommendations: str | None,
) -> list:
    """Build a list of sequential PlannedSlots with the given recommendations."""
    from custom_components.hsem.models.planner_outputs import PlannedSlot

    slots: list[PlannedSlot] = []
    for i, rec in enumerate(recommendations):
        start = _NOW + timedelta(hours=i)
        end = start + timedelta(hours=1)
        slots.append(
            PlannedSlot(
                start=start,
                end=end,
                price=SlotPrice(import_price=0.20, export_price=0.05),
                recommendation=rec,
            )
        )
    return slots


class TestWindowHysteresis:
    """Window-level hysteresis acceptance tests."""

    # ------------------------------------------------------------------
    # First-run behaviour
    # ------------------------------------------------------------------

    def test_no_previous_state_first_run(self):
        """When there is no previous state, hysteresis is inactive."""
        slots = _make_slots(
            Recommendations.BatteriesChargeGrid.value,
        )
        rec, start = apply_window_hysteresis(
            slots,
            _NOW,
            window_hysteresis_minutes=30,
            previous_current_recommendation=None,
            previous_current_slot_start=None,
        )
        assert rec == Recommendations.BatteriesChargeGrid.value, (
            "First run must accept the new recommendation"
        )

    # ------------------------------------------------------------------
    # Same-category transitions (no hold needed)
    # ------------------------------------------------------------------

    def test_charge_to_charge_no_hold(self):
        """Same-category change (grid-charge → solar-charge) must not hold."""
        slots = _make_slots(
            Recommendations.BatteriesChargeSolar.value,
        )
        rec, start = apply_window_hysteresis(
            slots,
            _NOW,
            window_hysteresis_minutes=30,
            previous_current_recommendation=Recommendations.BatteriesChargeGrid.value,
            previous_current_slot_start=_NOW - timedelta(minutes=5),
        )
        assert rec == Recommendations.BatteriesChargeSolar.value, (
            "Same-category charge change must not be held"
        )

    def test_discharge_to_discharge_no_hold(self):
        """Same-category change (discharge → force-discharge) must not hold."""
        slots = _make_slots(
            Recommendations.ForceBatteriesDischarge.value,
        )
        rec, start = apply_window_hysteresis(
            slots,
            _NOW,
            window_hysteresis_minutes=30,
            previous_current_recommendation=Recommendations.BatteriesDischargeMode.value,
            previous_current_slot_start=_NOW - timedelta(minutes=5),
        )
        assert rec == Recommendations.ForceBatteriesDischarge.value, (
            "Same-category discharge change must not be held"
        )

    # ------------------------------------------------------------------
    # Cross-category transitions within hold time
    # ------------------------------------------------------------------

    def test_charge_to_discharge_within_hold(self):
        """Charge→discharge within hold time must keep the previous recommendation."""
        slots = _make_slots(
            Recommendations.BatteriesDischargeMode.value,
        )
        rec, start = apply_window_hysteresis(
            slots,
            _NOW,
            window_hysteresis_minutes=30,
            previous_current_recommendation=Recommendations.BatteriesChargeGrid.value,
            previous_current_slot_start=_NOW - timedelta(minutes=5),
        )
        assert rec == Recommendations.BatteriesChargeGrid.value, (
            "Charge→discharge within hold time must be held"
        )
        # The slot's recommendation should also be updated
        assert slots[0].recommendation == Recommendations.BatteriesChargeGrid.value, (
            "Slot recommendation must reflect the held value"
        )

    def test_discharge_to_charge_within_hold(self):
        """Discharge→charge within hold time must keep the previous recommendation."""
        slots = _make_slots(
            Recommendations.BatteriesChargeGrid.value,
        )
        rec, start = apply_window_hysteresis(
            slots,
            _NOW,
            window_hysteresis_minutes=30,
            previous_current_recommendation=Recommendations.BatteriesDischargeMode.value,
            previous_current_slot_start=_NOW - timedelta(minutes=5),
        )
        assert rec == Recommendations.BatteriesDischargeMode.value, (
            "Discharge→charge within hold time must be held"
        )
        assert (
            slots[0].recommendation == Recommendations.BatteriesDischargeMode.value
        ), "Slot recommendation must reflect the held value"

    def test_charge_to_force_export_within_hold(self):
        """Charge→force-export within hold time must keep charge."""
        slots = _make_slots(
            Recommendations.ForceExport.value,
        )
        rec, start = apply_window_hysteresis(
            slots,
            _NOW,
            window_hysteresis_minutes=15,
            previous_current_recommendation=Recommendations.EVSmartCharging.value,
            previous_current_slot_start=_NOW - timedelta(minutes=2),
        )
        assert rec == Recommendations.EVSmartCharging.value, (
            "Charge→force-export within hold time must be held"
        )

    # ------------------------------------------------------------------
    # Cross-category transitions after hold time expires
    # ------------------------------------------------------------------

    def test_charge_to_discharge_after_hold(self):
        """Charge→discharge after hold time must allow the switch."""
        slots = _make_slots(
            Recommendations.BatteriesDischargeMode.value,
        )
        rec, start = apply_window_hysteresis(
            slots,
            _NOW,
            window_hysteresis_minutes=10,
            previous_current_recommendation=Recommendations.BatteriesChargeGrid.value,
            previous_current_slot_start=_NOW - timedelta(minutes=15),
        )
        assert rec == Recommendations.BatteriesDischargeMode.value, (
            "Charge→discharge after hold time must be allowed"
        )

    def test_discharge_to_charge_after_hold(self):
        """Discharge→charge after hold time must allow the switch."""
        slots = _make_slots(
            Recommendations.BatteriesChargeGrid.value,
        )
        rec, start = apply_window_hysteresis(
            slots,
            _NOW,
            window_hysteresis_minutes=5,
            previous_current_recommendation=Recommendations.BatteriesDischargeMode.value,
            previous_current_slot_start=_NOW - timedelta(minutes=10),
        )
        assert rec == Recommendations.BatteriesChargeGrid.value, (
            "Discharge→charge after hold time must be allowed"
        )

    # ------------------------------------------------------------------
    # Neutral recommendations
    # ------------------------------------------------------------------

    def test_charge_to_neutral_no_hold(self):
        """Charge→neutral must not hold."""
        slots = _make_slots(
            Recommendations.BatteriesWaitMode.value,
        )
        rec, start = apply_window_hysteresis(
            slots,
            _NOW,
            window_hysteresis_minutes=30,
            previous_current_recommendation=Recommendations.BatteriesChargeGrid.value,
            previous_current_slot_start=_NOW - timedelta(minutes=2),
        )
        assert rec == Recommendations.BatteriesWaitMode.value, (
            "Charge→neutral must not be held"
        )

    def test_discharge_to_neutral_no_hold(self):
        """Discharge→neutral must not hold."""
        slots = _make_slots(
            None,
        )
        rec, start = apply_window_hysteresis(
            slots,
            _NOW,
            window_hysteresis_minutes=30,
            previous_current_recommendation=Recommendations.BatteriesDischargeMode.value,
            previous_current_slot_start=_NOW - timedelta(minutes=2),
        )
        assert rec is None, "Discharge→neutral must not be held"

    def test_neutral_to_charge_no_hold(self):
        """Neutral→charge must not hold."""
        slots = _make_slots(
            Recommendations.BatteriesChargeGrid.value,
        )
        rec, start = apply_window_hysteresis(
            slots,
            _NOW,
            window_hysteresis_minutes=30,
            previous_current_recommendation=Recommendations.TimePassed.value,
            previous_current_slot_start=_NOW - timedelta(minutes=2),
        )
        assert rec == Recommendations.BatteriesChargeGrid.value, (
            "Neutral→charge must not be held"
        )

    # ------------------------------------------------------------------
    # Feature disabled
    # ------------------------------------------------------------------

    def test_feature_disabled_always_allows_switch(self):
        """When window_hysteresis_minutes is 0, all transitions are allowed."""
        slots = _make_slots(
            Recommendations.BatteriesDischargeMode.value,
        )
        rec, start = apply_window_hysteresis(
            slots,
            _NOW,
            window_hysteresis_minutes=0,
            previous_current_recommendation=Recommendations.BatteriesChargeGrid.value,
            previous_current_slot_start=_NOW - timedelta(minutes=2),
        )
        assert rec == Recommendations.BatteriesDischargeMode.value, (
            "Transition must be allowed when feature is disabled"
        )

    # ------------------------------------------------------------------
    # Edge cases — exact boundary
    # ------------------------------------------------------------------

    def test_exactly_at_hold_time_boundary(self):
        """Transition exactly at hold time boundary must be allowed."""
        slots = _make_slots(
            Recommendations.BatteriesDischargeMode.value,
        )
        rec, start = apply_window_hysteresis(
            slots,
            _NOW,
            window_hysteresis_minutes=10,
            previous_current_recommendation=Recommendations.EVSmartCharging.value,
            previous_current_slot_start=_NOW - timedelta(minutes=10),
        )
        assert rec == Recommendations.BatteriesDischargeMode.value, (
            "Transition exactly at hold time boundary must be allowed (>=)"
        )

    def test_one_second_before_boundary(self):
        """Transition just before hold time boundary must be held."""
        slots = _make_slots(
            Recommendations.BatteriesDischargeMode.value,
        )
        rec, start = apply_window_hysteresis(
            slots,
            _NOW,
            window_hysteresis_minutes=10,
            previous_current_recommendation=Recommendations.EVSmartCharging.value,
            previous_current_slot_start=_NOW - timedelta(minutes=9, seconds=59),
        )
        assert rec == Recommendations.EVSmartCharging.value, (
            "Transition just before hold time boundary must be held"
        )

    # ------------------------------------------------------------------
    # Return value semantics
    # ------------------------------------------------------------------

    def test_returns_updated_start_time_on_switch(self):
        """When a switch is allowed, the returned start time must be the new slot start."""
        slots = _make_slots(
            Recommendations.BatteriesDischargeMode.value,
        )
        rec, start = apply_window_hysteresis(
            slots,
            _NOW,
            window_hysteresis_minutes=5,
            previous_current_recommendation=Recommendations.BatteriesChargeGrid.value,
            previous_current_slot_start=_NOW - timedelta(minutes=15),
        )
        assert start == slots[0].start, (
            "Returned start time must be from the current slot after a switch"
        )

    def test_returns_previous_start_time_on_hold(self):
        """When held, the returned start time must be the previous slot start."""
        slots = _make_slots(
            Recommendations.BatteriesDischargeMode.value,
        )
        prev_start = _NOW - timedelta(minutes=2)
        rec, start = apply_window_hysteresis(
            slots,
            _NOW,
            window_hysteresis_minutes=10,
            previous_current_recommendation=Recommendations.BatteriesChargeGrid.value,
            previous_current_slot_start=prev_start,
        )
        assert start == prev_start, (
            "Returned start time must be the previous slot start when held"
        )
