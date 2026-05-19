"""Tests for explicit unit-conversion helpers (issue #290).

Verifies that every conversion function in
:mod:`custom_components.hsem.utils.units` produces correct results and
handles edge cases (zero, negative, large values, division by zero).
"""

from __future__ import annotations

import pytest

from custom_components.hsem.utils.units import (
    energy_cost,
    energy_to_power_kw,
    implied_price_per_kwh,
    kilowatt_to_watt,
    kilowatthours_to_watthours,
    power_to_energy_kwh,
    watt_to_kilowatt,
    watthours_to_kilowatthours,
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


class TestKilowattToWatt:
    """Tests for :func:`kilowatt_to_watt`."""

    def test_typical_value(self) -> None:
        """5.0 kW → 5000 W."""
        assert kilowatt_to_watt(5.0) == pytest.approx(5000.0)

    def test_zero(self) -> None:
        """0.0 kW → 0 W."""
        assert kilowatt_to_watt(0.0) == pytest.approx(0.0)

    def test_small_value(self) -> None:
        """0.001 kW → 1 W."""
        assert kilowatt_to_watt(0.001) == pytest.approx(1.0)

    def test_negative(self) -> None:
        """-2.5 kW → -2500 W."""
        assert kilowatt_to_watt(-2.5) == pytest.approx(-2500.0)

    def test_roundtrip(self) -> None:
        """Round-trip: W → kW → W preserves value."""
        original = 7642.0
        assert kilowatt_to_watt(watt_to_kilowatt(original)) == pytest.approx(original)


# ---------------------------------------------------------------------------
# Energy conversions (Wh ↔ kWh)
# ---------------------------------------------------------------------------


class TestWatthoursToKilowatthours:
    """Tests for :func:`watthours_to_kilowatthours`."""

    def test_typical_value(self) -> None:
        """10000 Wh → 10.0 kWh."""
        assert watthours_to_kilowatthours(10000.0) == pytest.approx(10.0)

    def test_zero(self) -> None:
        """0 Wh → 0.0 kWh."""
        assert watthours_to_kilowatthours(0.0) == pytest.approx(0.0)

    def test_single_wh(self) -> None:
        """1 Wh → 0.001 kWh."""
        assert watthours_to_kilowatthours(1.0) == pytest.approx(0.001)

    def test_negative(self) -> None:
        """-5000 Wh → -5.0 kWh."""
        assert watthours_to_kilowatthours(-5000.0) == pytest.approx(-5.0)


class TestKilowatthoursToWatthours:
    """Tests for :func:`kilowatthours_to_watthours`."""

    def test_typical_value(self) -> None:
        """10.0 kWh → 10000 Wh."""
        assert kilowatthours_to_watthours(10.0) == pytest.approx(10000.0)

    def test_zero(self) -> None:
        """0.0 kWh → 0 Wh."""
        assert kilowatthours_to_watthours(0.0) == pytest.approx(0.0)

    def test_negative(self) -> None:
        """-2.5 kWh → -2500 Wh."""
        assert kilowatthours_to_watthours(-2.5) == pytest.approx(-2500.0)

    def test_roundtrip(self) -> None:
        """Round-trip: Wh → kWh → Wh preserves value."""
        original = 12345.0
        assert kilowatthours_to_watthours(
            watthours_to_kilowatthours(original)
        ) == pytest.approx(original)


# ---------------------------------------------------------------------------
# Duration-aware conversions (power ⇄ energy)
# ---------------------------------------------------------------------------


class TestPowerToEnergyKwh:
    """Tests for :func:`power_to_energy_kwh`."""

    def test_typical_value(self) -> None:
        """5 kW × 2 h → 10 kWh."""
        assert power_to_energy_kwh(power_kw=5.0, duration_h=2.0) == pytest.approx(10.0)

    def test_zero_power(self) -> None:
        """0 kW × 2 h → 0 kWh."""
        assert power_to_energy_kwh(power_kw=0.0, duration_h=2.0) == pytest.approx(0.0)

    def test_zero_duration(self) -> None:
        """5 kW × 0 h → 0 kWh."""
        assert power_to_energy_kwh(power_kw=5.0, duration_h=0.0) == pytest.approx(0.0)

    def test_quarter_hour(self) -> None:
        """5 kW × 0.25 h → 1.25 kWh (15-min slot)."""
        assert power_to_energy_kwh(power_kw=5.0, duration_h=0.25) == pytest.approx(
            1.25
        )

    def test_negative_power(self) -> None:
        """-3 kW × 1 h → -3 kWh (discharge / export)."""
        assert power_to_energy_kwh(power_kw=-3.0, duration_h=1.0) == pytest.approx(
            -3.0
        )


class TestEnergyToPowerKw:
    """Tests for :func:`energy_to_power_kw`."""

    def test_typical_value(self) -> None:
        """10 kWh ÷ 2 h → 5 kW."""
        assert energy_to_power_kw(energy_kwh=10.0, duration_h=2.0) == pytest.approx(
            5.0
        )

    def test_zero_energy(self) -> None:
        """0 kWh ÷ 2 h → 0 kW."""
        assert energy_to_power_kw(energy_kwh=0.0, duration_h=2.0) == pytest.approx(0.0)

    def test_quarter_hour(self) -> None:
        """1.25 kWh ÷ 0.25 h → 5 kW."""
        assert energy_to_power_kw(energy_kwh=1.25, duration_h=0.25) == pytest.approx(
            5.0
        )

    def test_negative_energy(self) -> None:
        """-3 kWh ÷ 1 h → -3 kW."""
        assert energy_to_power_kw(energy_kwh=-3.0, duration_h=1.0) == pytest.approx(
            -3.0
        )

    def test_roundtrip(self) -> None:
        """Round-trip: kW → kWh → kW preserves value."""
        original_kw = 7.5
        duration = 0.5
        kwh = power_to_energy_kwh(original_kw, duration)
        assert energy_to_power_kw(kwh, duration) == pytest.approx(original_kw)


# ---------------------------------------------------------------------------
# Price / cost helpers
# ---------------------------------------------------------------------------


class TestEnergyCost:
    """Tests for :func:`energy_cost`."""

    def test_typical_value(self) -> None:
        """10 kWh × 0.50 DKK/kWh → 5.0 DKK."""
        assert energy_cost(energy_kwh=10.0, price_per_kwh=0.50) == pytest.approx(5.0)

    def test_zero_energy(self) -> None:
        """0 kWh × 0.50 → 0.0."""
        assert energy_cost(energy_kwh=0.0, price_per_kwh=0.50) == pytest.approx(0.0)

    def test_zero_price(self) -> None:
        """10 kWh × 0.0 → 0.0."""
        assert energy_cost(energy_kwh=10.0, price_per_kwh=0.0) == pytest.approx(0.0)

    def test_negative_price(self) -> None:
        """10 kWh × -0.10 → -1.0 (negative price = revenue)."""
        assert energy_cost(energy_kwh=10.0, price_per_kwh=-0.10) == pytest.approx(-1.0)

    def test_negative_energy(self) -> None:
        """-5 kWh × 0.50 → -2.5 (export)."""
        assert energy_cost(energy_kwh=-5.0, price_per_kwh=0.50) == pytest.approx(-2.5)


class TestImpliedPricePerKwh:
    """Tests for :func:`implied_price_per_kwh`."""

    def test_typical_value(self) -> None:
        """5.0 DKK ÷ 10 kWh → 0.50 DKK/kWh."""
        assert implied_price_per_kwh(total_cost=5.0, energy_kwh=10.0) == pytest.approx(
            0.50
        )

    def test_zero_energy_returns_zero(self) -> None:
        """Division by zero guard: 5.0 ÷ 0 → 0.0."""
        assert implied_price_per_kwh(total_cost=5.0, energy_kwh=0.0) == pytest.approx(
            0.0
        )

    def test_negative_energy_returns_zero(self) -> None:
        """Guard against nonsensical negative energy: 5.0 ÷ -1 → 0.0."""
        assert implied_price_per_kwh(
            total_cost=5.0, energy_kwh=-1.0
        ) == pytest.approx(0.0)

    def test_zero_cost(self) -> None:
        """0.0 ÷ 10 kWh → 0.0."""
        assert implied_price_per_kwh(
            total_cost=0.0, energy_kwh=10.0
        ) == pytest.approx(0.0)

    def test_negative_cost(self) -> None:
        """-5.0 ÷ 10 kWh → -0.50 (net revenue)."""
        assert implied_price_per_kwh(
            total_cost=-5.0, energy_kwh=10.0
        ) == pytest.approx(-0.50)


# ---------------------------------------------------------------------------
# Cross-category consistency
# ---------------------------------------------------------------------------


class TestCrossCategoryConsistency:
    """Verify that related conversions produce consistent results."""

    def test_w_to_kw_to_energy(self) -> None:
        """W → kW → kWh chain produces the same result as direct W × h → Wh → kWh."""
        power_w = 5000.0
        duration_h = 1.5
        # Path A: W → kW → kWh
        energy_a = power_to_energy_kwh(watt_to_kilowatt(power_w), duration_h)
        # Path B: W × h → Wh → kWh
        energy_b = watthours_to_kilowatthours(power_w * duration_h)
        assert energy_a == pytest.approx(energy_b)

    def test_kwh_to_cost_to_implied_price(self) -> None:
        """energy_cost then implied_price_per_kwh recovers the original price."""
        energy = 12.5
        price = 0.45
        cost = energy_cost(energy, price)
        recovered = implied_price_per_kwh(cost, energy)
        assert recovered == pytest.approx(price)

    def test_power_energy_roundtrip(self) -> None:
        """power_to_energy_kwh then energy_to_power_kw recovers original power."""
        power = 3.6
        duration = 0.25
        energy = power_to_energy_kwh(power, duration)
        assert energy_to_power_kw(energy, duration) == pytest.approx(power)
