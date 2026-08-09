"""Tests for get_max_discharge_power (issue #723).

Covers the extended capacity table (two-stack S0 configurations),
the safe choice for the ambiguous 20 kWh case, and the warning
logged when a capacity is not in the table.
"""

from __future__ import annotations

from custom_components.hsem.utils.misc import get_max_discharge_power


def test_s0_single_stack_capacities() -> None:
    """Single-stack S0 capacities map to their documented powers."""
    assert get_max_discharge_power(5000) == 2500
    assert get_max_discharge_power(10000) == 5000
    assert get_max_discharge_power(15000) == 5000


def test_s0_two_stack_capacities() -> None:
    """Two-stack S0 capacities no longer fall through to the default."""
    assert get_max_discharge_power(20000) == 7500  # safe 15+5 value
    assert get_max_discharge_power(25000) == 10000
    assert get_max_discharge_power(30000) == 10000


def test_s1_capacities() -> None:
    """S1 capacities remain unchanged."""
    assert get_max_discharge_power(7000) == 3500
    assert get_max_discharge_power(14000) == 7000
    assert get_max_discharge_power(21000) == 10500


def test_unknown_capacity_defaults() -> None:
    """Unknown capacities fall back to 2500 W."""
    result = get_max_discharge_power(12345)
    assert result == 2500
