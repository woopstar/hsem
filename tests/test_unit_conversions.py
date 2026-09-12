"""Tests for explicit unit-conversion helpers (issue #290).

Verifies that every conversion function in
:mod:`custom_components.hsem.utils.units` produces correct results and
handles edge cases (zero, negative, large values, division by zero).
"""

from __future__ import annotations

import pytest

from custom_components.hsem.utils.units import (
    ev_ac_to_dc_kwh,
    ev_dc_to_ac_kwh,
    fuse_max_energy_per_slot_kwh,
    watt_to_kilowatt,
)

# ---------------------------------------------------------------------------
# Power conversions (W ↔ kW)
# ---------------------------------------------------------------------------


class TestWattToKilowatt:
    """Tests for :func:`watt_to_kilowatt`."""

    def test_typical_value(self) -> None:
        """5000 W → 5.0 kW."""
        assert watt_to_kilowatt(5000.0) == pytest.approx(5.0)

    def test_zero(self) -> None:
        """0 W → 0.0 kW."""
        assert watt_to_kilowatt(0.0) == pytest.approx(0.0)

    def test_small_value(self) -> None:
        """1 W → 0.001 kW."""
        assert watt_to_kilowatt(1.0) == pytest.approx(0.001)

    def test_large_value(self) -> None:
        """1_000_000 W → 1000.0 kW."""
        assert watt_to_kilowatt(1_000_000.0) == pytest.approx(1000.0)

    def test_negative(self) -> None:
        """-500 W → -0.5 kW (negative power is valid for reverse flow)."""
        assert watt_to_kilowatt(-500.0) == pytest.approx(-0.5)


# ---------------------------------------------------------------------------
# Grid fuse limit
# ---------------------------------------------------------------------------


class TestFuseMaxEnergyPerSlotKwh:
    """Tests for :func:`fuse_max_energy_per_slot_kwh`."""

    def test_three_phase_25a(self) -> None:
        """25 A, 3-phase, 1 h slot → 25*230*3/1000 = 17.25 kWh."""
        assert fuse_max_energy_per_slot_kwh(25.0, 3, 1.0) == pytest.approx(17.25)

    def test_single_phase(self) -> None:
        """10 A, 1-phase, 1 h slot → 10*230*1/1000 = 2.3 kWh."""
        assert fuse_max_energy_per_slot_kwh(10.0, 1, 1.0) == pytest.approx(2.3)

    def test_quarter_hour_slot(self) -> None:
        """1 A, 3-phase, 0.25 h slot → 0.69/4 = 0.1725 kWh."""
        assert fuse_max_energy_per_slot_kwh(1.0, 3, 0.25) == pytest.approx(0.1725)

    def test_disabled_fuse_returns_zero(self) -> None:
        """amps <= 0 disables the limit → 0.0."""
        assert fuse_max_energy_per_slot_kwh(0.0, 3, 1.0) == pytest.approx(0.0)

    def test_zero_slot_hours_returns_zero(self) -> None:
        """slot_hours <= 0 → 0.0 (degenerate slot)."""
        assert fuse_max_energy_per_slot_kwh(25.0, 3, 0.0) == pytest.approx(0.0)

    def test_matches_inline_formula(self) -> None:
        """Must equal the historical inline amps*230*phases/1000*hours."""
        assert fuse_max_energy_per_slot_kwh(16.0, 3, 0.5) == pytest.approx(
            16.0 * 230.0 * 3.0 / 1000.0 * 0.5
        )


# ---------------------------------------------------------------------------
# EV charger DC ↔ AC conversion
# ---------------------------------------------------------------------------


class TestEvDcAcConversion:
    """Tests for :func:`ev_dc_to_ac_kwh` and :func:`ev_ac_to_dc_kwh`."""

    def test_dc_to_ac_divides_by_efficiency(self) -> None:
        """5.0 kWh DC at 90 % → 5.0/0.9 ≈ 5.556 kWh AC."""
        assert ev_dc_to_ac_kwh(5.0, 0.9) == pytest.approx(5.0 / 0.9)

    def test_ac_to_dc_multiplies_by_efficiency(self) -> None:
        """5.0 kWh AC at 90 % → 4.5 kWh DC."""
        assert ev_ac_to_dc_kwh(5.0, 0.9) == pytest.approx(4.5)

    def test_roundtrip(self) -> None:
        """DC → AC → DC recovers the original value."""
        dc = 7.5
        eff = 0.92
        assert ev_ac_to_dc_kwh(ev_dc_to_ac_kwh(dc, eff), eff) == pytest.approx(dc)

    def test_zero_efficiency_dc_to_ac_is_safe(self) -> None:
        """Zero efficiency must not raise (returns 0.0)."""
        assert ev_dc_to_ac_kwh(5.0, 0.0) == pytest.approx(0.0)

    def test_unity_efficiency(self) -> None:
        """100 % efficiency → identity in both directions."""
        assert ev_dc_to_ac_kwh(5.0, 1.0) == pytest.approx(5.0)
        assert ev_ac_to_dc_kwh(5.0, 1.0) == pytest.approx(5.0)
