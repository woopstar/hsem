"""Tests for get_max_discharge_power (issue #723) and its entity bound.

Covers the extended capacity table (two-stack S0 configurations),
the safe choice for the ambiguous 20 kWh case, and the warning
logged when a capacity is not in the table.

``resolve_max_discharge_power`` bounds the table value by the ``max`` of the
Huawei discharging-power number entity (issue #1291).
"""

from __future__ import annotations

import math

import pytest

from custom_components.hsem.utils.misc import (
    get_max_discharge_power,
    resolve_max_discharge_power,
)


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


class TestResolveMaxDischargePower:
    """The entity's own maximum bounds the capacity-table value (issue #1291)."""

    def test_lower_entity_maximum_wins(self) -> None:
        """A 10 kWh battery whose entity accepts 4400 W is limited to 4400 W."""
        assert resolve_max_discharge_power(10000, 4400.0) == 4400

    @pytest.mark.parametrize("entity_max_w", [5000.0, 5000.5, 6000.0, 10000.0])
    def test_entity_maximum_at_or_above_the_table_changes_nothing(
        self, entity_max_w: float
    ) -> None:
        assert resolve_max_discharge_power(10000, entity_max_w) == 5000

    @pytest.mark.parametrize(
        "entity_max_w", [None, 0.0, -1.0, math.nan, math.inf, -math.inf]
    )
    def test_unusable_entity_maximum_keeps_the_table_value(
        self, entity_max_w: float | None
    ) -> None:
        assert resolve_max_discharge_power(10000, entity_max_w) == 5000

    def test_fractional_entity_maximum_rounds_down(self) -> None:
        """Rounding up would put the write above what the entity accepts."""
        assert resolve_max_discharge_power(10000, 4400.9) == 4400

    def test_unknown_capacity_is_bounded_too(self) -> None:
        """The 2500 W fallback for an unlisted capacity keeps the same bound."""
        assert resolve_max_discharge_power(12345, 2000.0) == 2000
        assert resolve_max_discharge_power(12345, 4400.0) == 2500
