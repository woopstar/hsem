"""Tests for custom_sensors/applier.py.

The :func:`_parse_power_control_pct` helper is pure Python and fully testable
without Home Assistant.  The async hardware-write functions are covered by
integration tests; here we only test the deterministic helper.
"""

from __future__ import annotations

from custom_components.hsem.custom_sensors.applier import _parse_power_control_pct


class TestParsePowerControlPct:
    """Unit tests for the inverter power control state parser."""

    def test_unlimited_returns_100(self):
        assert _parse_power_control_pct("Unlimited") == 100

    def test_unlimited_case_insensitive(self):
        assert _parse_power_control_pct("unlimited") == 100
        assert _parse_power_control_pct("UNLIMITED") == 100

    def test_limited_to_80_percent(self):
        assert _parse_power_control_pct("Limited to 80%") == 80

    def test_limited_to_0_percent(self):
        assert _parse_power_control_pct("Limited to 0%") == 0

    def test_fractional_rounds_to_int(self):
        assert _parse_power_control_pct("Limited to 79.6%") == 80

    def test_none_returns_none(self):
        assert _parse_power_control_pct(None) is None

    def test_integer_returns_none(self):
        assert _parse_power_control_pct(100) is None  # type: ignore[arg-type]  # test passes mock where real type expected

    def test_empty_string_returns_none(self):
        assert _parse_power_control_pct("") is None

    def test_unknown_string_returns_none(self):
        assert _parse_power_control_pct("some other value") is None

    def test_whitespace_stripped(self):
        assert _parse_power_control_pct("  Limited to 50%  ") == 50

    # --- localization regression tests (bug fix) ---

    def test_danish_unlimited(self):
        """Danish HA translation of 'Unlimited'."""
        assert _parse_power_control_pct("Ikke begrænset") == 100

    def test_dutch_unlimited(self):
        """Dutch HA translation of 'Unlimited'."""
        assert _parse_power_control_pct("Onbeperkt") == 100

    def test_german_unlimited(self):
        """German HA translation of 'Unlimited'."""
        assert _parse_power_control_pct("Unbegrenzt") == 100

    def test_german_limited(self):
        """German 'Begrenzt auf 80 %' should yield 80."""
        assert _parse_power_control_pct("Begrenzt auf 80 %") == 80

    def test_dutch_limited(self):
        """Dutch 'Beperkt tot 75%' should yield 75."""
        assert _parse_power_control_pct("Beperkt tot 75%") == 75

    def test_fractional_localized(self):
        """Localized percentage with decimal rounds correctly."""
        assert _parse_power_control_pct("Begrenzt auf 79.6 %") == 80


# ---------------------------------------------------------------------------
# _planned_ev_discharge_cap_w — planner-authorised EV discharge ceiling
# ---------------------------------------------------------------------------


class TestPlannedEvDischargeCapW:
    """EV permission bounds positive planned discharge; it never creates it."""

    @staticmethod
    def _cap(**kwargs):
        from custom_components.hsem.custom_sensors.applier import (
            _planned_ev_discharge_cap_w,
        )

        return _planned_ev_discharge_cap_w(**kwargs)

    def test_zero_planned_discharge_returns_zero(self):
        """An opt-in cannot manufacture battery discharge absent from the plan."""
        assert (
            self._cap(
                planned_discharge_kwh=0.0,
                slot_hours=0.25,
                max_discharge_power_w=10000,
                ev_max_discharge_power_ws=(5000,),
            )
            == 0
        )

    def test_planned_energy_is_averaged_over_slot(self):
        assert (
            self._cap(
                planned_discharge_kwh=0.75,
                slot_hours=0.25,
                max_discharge_power_w=10000,
                ev_max_discharge_power_ws=(5000,),
            )
            == 3000
        )

    def test_all_relevant_ev_and_hardware_ceilings_apply(self):
        assert (
            self._cap(
                planned_discharge_kwh=2.0,
                slot_hours=0.25,
                max_discharge_power_w=6000,
                ev_max_discharge_power_ws=(5000, 3200),
            )
            == 3200
        )


# ---------------------------------------------------------------------------
# _wait_mode_self_consumption_cap_w — reserve-preserving discharge cap (issue #742)
# ---------------------------------------------------------------------------


class TestWaitModeSelfConsumptionCapW:
    """Unit tests for the wait-mode self-consumption discharge cap."""

    @staticmethod
    def _cap(**kwargs):
        from custom_components.hsem.custom_sensors.applier import (
            _wait_mode_self_consumption_cap_w,
        )

        return _wait_mode_self_consumption_cap_w(**kwargs)

    def test_no_surplus_returns_zero(self):
        cap = self._cap(
            battery_capacity_kwh=2.0,
            required_capacity_kwh=2.0,
            slot_hours=0.25,
            max_discharge_power_w=5000,
        )
        assert cap == 0

    def test_below_reserve_returns_zero(self):
        cap = self._cap(
            battery_capacity_kwh=1.5,
            required_capacity_kwh=2.0,
            slot_hours=0.25,
            max_discharge_power_w=5000,
        )
        assert cap == 0

    def test_surplus_converted_to_power(self):
        """1 kWh surplus over a 1-hour slot → 1000 W cap."""
        cap = self._cap(
            battery_capacity_kwh=3.0,
            required_capacity_kwh=2.0,
            slot_hours=1.0,
            max_discharge_power_w=5000,
        )
        assert cap == 1000

    def test_surplus_over_short_slot(self):
        """1 kWh surplus over a 15-minute slot → 4000 W cap."""
        cap = self._cap(
            battery_capacity_kwh=3.0,
            required_capacity_kwh=2.0,
            slot_hours=0.25,
            max_discharge_power_w=5000,
        )
        assert cap == 4000

    def test_cap_limited_by_max_discharge_power(self):
        cap = self._cap(
            battery_capacity_kwh=10.0,
            required_capacity_kwh=0.0,
            slot_hours=0.25,
            max_discharge_power_w=2500,
        )
        assert cap == 2500

    def test_zero_slot_hours_returns_zero(self):
        cap = self._cap(
            battery_capacity_kwh=5.0,
            required_capacity_kwh=0.0,
            slot_hours=0.0,
            max_discharge_power_w=5000,
        )
        assert cap == 0
