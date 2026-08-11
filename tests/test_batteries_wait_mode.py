"""Tests for battery wait-mode self-consumption feature (issue #742).

Covers:
- Default constant value for wait-mode behaviour in ``const.py``
- Input validation in ``flows/batteries_wait_mode.py``
- Discharge cap helper in ``custom_sensors/applier.py``
"""

import pytest

from custom_components.hsem.const import DEFAULT_CONFIG_VALUES
from custom_components.hsem.custom_sensors.applier import (
    _wait_mode_self_consumption_cap_w,
)
from custom_components.hsem.flows.batteries_wait_mode import (
    validate_batteries_wait_mode_input,
)

# ---------------------------------------------------------------------------
# Default constant value tests
# ---------------------------------------------------------------------------


class TestWaitModeDefaults:
    """Verify the wait-mode behaviour default is safe."""

    def test_wait_mode_strict_by_default(self):
        """Wait mode must default to strict to preserve existing behaviour."""
        assert DEFAULT_CONFIG_VALUES["hsem_batteries_wait_mode_behavior"] == "strict"


# ---------------------------------------------------------------------------
# _wait_mode_self_consumption_cap_w tests
# ---------------------------------------------------------------------------


class TestWaitModeSelfConsumptionCapW:
    """Unit tests for the reserve-preserving discharge cap helper."""

    def test_no_surplus_returns_zero(self):
        cap = _wait_mode_self_consumption_cap_w(
            battery_capacity_kwh=2.0,
            required_capacity_kwh=2.0,
            slot_hours=0.25,
            max_discharge_power_w=5000,
        )
        assert cap == 0

    def test_below_reserve_returns_zero(self):
        cap = _wait_mode_self_consumption_cap_w(
            battery_capacity_kwh=1.5,
            required_capacity_kwh=2.0,
            slot_hours=0.25,
            max_discharge_power_w=5000,
        )
        assert cap == 0

    def test_surplus_converted_to_power(self):
        """1 kWh surplus over a 1-hour slot -> 1000 W cap."""
        cap = _wait_mode_self_consumption_cap_w(
            battery_capacity_kwh=3.0,
            required_capacity_kwh=2.0,
            slot_hours=1.0,
            max_discharge_power_w=5000,
        )
        assert cap == 1000

    def test_surplus_over_short_slot(self):
        """1 kWh surplus over a 15-minute slot -> 4000 W cap."""
        cap = _wait_mode_self_consumption_cap_w(
            battery_capacity_kwh=3.0,
            required_capacity_kwh=2.0,
            slot_hours=0.25,
            max_discharge_power_w=5000,
        )
        assert cap == 4000

    def test_cap_limited_by_max_discharge_power(self):
        cap = _wait_mode_self_consumption_cap_w(
            battery_capacity_kwh=10.0,
            required_capacity_kwh=0.0,
            slot_hours=0.25,
            max_discharge_power_w=2500,
        )
        assert cap == 2500

    def test_zero_slot_hours_returns_zero(self):
        cap = _wait_mode_self_consumption_cap_w(
            battery_capacity_kwh=5.0,
            required_capacity_kwh=0.0,
            slot_hours=0.0,
            max_discharge_power_w=5000,
        )
        assert cap == 0


# ---------------------------------------------------------------------------
# validate_batteries_wait_mode_input tests
# ---------------------------------------------------------------------------


class TestValidateBatteriesWaitModeInput:
    """Unit tests for the wait-mode config-flow input validator."""

    @pytest.mark.asyncio
    async def test_strict_value_is_valid(self):
        errors = await validate_batteries_wait_mode_input(
            {"hsem_batteries_wait_mode_behavior": "strict"}
        )
        assert errors == {}

    @pytest.mark.asyncio
    async def test_self_consumption_value_is_valid(self):
        errors = await validate_batteries_wait_mode_input(
            {"hsem_batteries_wait_mode_behavior": "self_consumption_with_reserve"}
        )
        assert errors == {}

    @pytest.mark.asyncio
    async def test_invalid_value_is_rejected(self):
        errors = await validate_batteries_wait_mode_input(
            {"hsem_batteries_wait_mode_behavior": "something_else"}
        )
        assert "hsem_batteries_wait_mode_behavior" in errors

    @pytest.mark.asyncio
    async def test_missing_field_is_rejected(self):
        errors = await validate_batteries_wait_mode_input({})
        assert "hsem_batteries_wait_mode_behavior" in errors
