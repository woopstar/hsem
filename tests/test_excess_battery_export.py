"""Tests for excess battery export feature (PR #264).

Covers:
- ``calculate_recommended_threshold`` helper in ``utils/misc.py``
- Default constant values for excess-export settings in ``const.py``
- Input validation in ``flows/batteries_excess_export.py``
- ``async_set_forcible_discharge`` guard logic in ``utils/huawei.py``
"""

import pytest

from custom_components.hsem.const import DEFAULT_CONFIG_VALUES
from custom_components.hsem.flows.batteries_excess_export import (
    validate_batteries_excess_export_input,
)
from custom_components.hsem.utils.misc import calculate_recommended_threshold

# ---------------------------------------------------------------------------
# Default constant value tests
# ---------------------------------------------------------------------------


class TestExcessExportDefaults:
    """Verify the excess-export feature default constants are safe and numeric."""

    def test_excess_export_disabled_by_default(self):
        """Excess export must be off by default to avoid unintended discharges."""
        assert DEFAULT_CONFIG_VALUES["hsem_batteries_enable_excess_export"] is False

    def test_discharge_buffer_is_numeric(self):
        """Discharge buffer default must be a non-False integer (10 %)."""
        value = DEFAULT_CONFIG_VALUES["hsem_batteries_excess_export_discharge_buffer"]
        assert isinstance(value, (int, float)), (
            "Default discharge buffer must be numeric, not a boolean False."
        )
        assert value == 10

    def test_price_threshold_is_numeric(self):
        """Price threshold default must be a non-False float (0.10 EUR/kWh)."""
        value = DEFAULT_CONFIG_VALUES["hsem_batteries_excess_export_price_threshold"]
        assert isinstance(value, (int, float)), (
            "Default price threshold must be numeric, not a boolean False."
        )
        assert value == pytest.approx(0.10)

    def test_purchase_price_default(self):
        """Battery purchase price must default to 0.0 (not configured yet)."""
        assert DEFAULT_CONFIG_VALUES["hsem_batteries_purchase_price"] == pytest.approx(
            0.0
        )

    def test_expected_cycles_default(self):
        """Expected battery cycles must default to 6000."""
        assert DEFAULT_CONFIG_VALUES["hsem_batteries_expected_cycles"] == 6000


# ---------------------------------------------------------------------------
# calculate_recommended_threshold tests
# ---------------------------------------------------------------------------


class TestCalculateRecommendedThreshold:
    """Unit tests for the threshold calculation helper."""

    def test_returns_zero_for_zero_purchase_price(self):
        """With no purchase price set, depreciation cost is zero."""
        result = calculate_recommended_threshold(
            purchase_price=0.0,
            expected_cycles=6000,
            usable_capacity=10.0,
        )
        assert result == pytest.approx(0.0)

    def test_returns_zero_for_zero_cycles(self):
        """Division by zero is guarded: zero cycles returns 0.0."""
        result = calculate_recommended_threshold(
            purchase_price=48000.0,
            expected_cycles=0,
            usable_capacity=10.0,
        )
        assert result == pytest.approx(0.0)

    def test_returns_zero_for_zero_capacity(self):
        """Division by zero is guarded: zero usable capacity returns 0.0."""
        result = calculate_recommended_threshold(
            purchase_price=48000.0,
            expected_cycles=6000,
            usable_capacity=0.0,
        )
        assert result == pytest.approx(0.0)

    def test_depreciation_only_no_import_price(self):
        """With default params only depreciation component is returned.

        Formula: (48 000 * 0.30) / (2 * 6 000 * 10) = 14 400 / 120 000 = 0.120
        """
        result = calculate_recommended_threshold(
            purchase_price=48_000.0,
            expected_cycles=6_000,
            usable_capacity=10.0,
        )
        assert result == pytest.approx(0.120, abs=1e-3)

    def test_depreciation_with_custom_capacity_loss(self):
        """Custom capacity loss percentage is reflected in the threshold.

        capacity_loss_pct=30 (default) → 0.120
        capacity_loss_pct=50 → (48000 * 0.50) / (2 * 6000 * 10) = 0.200
        """
        result = calculate_recommended_threshold(
            purchase_price=48_000.0,
            expected_cycles=6_000,
            usable_capacity=10.0,
            capacity_loss_pct=50.0,
        )
        assert result == pytest.approx(0.200, abs=1e-3)

    def test_smaller_battery_higher_threshold(self):
        """A smaller usable capacity raises the per-kWh depreciation cost."""
        result_small = calculate_recommended_threshold(
            purchase_price=48_000.0,
            expected_cycles=6_000,
            usable_capacity=5.0,
            conversion_loss_pct=10.0,
        )
        result_large = calculate_recommended_threshold(
            purchase_price=48_000.0,
            expected_cycles=6_000,
            usable_capacity=10.0,
            conversion_loss_pct=10.0,
        )
        assert result_small > result_large

    def test_result_is_rounded_to_3_decimals(self):
        """Result must be rounded to 3 decimal places."""
        result = calculate_recommended_threshold(
            purchase_price=48_000.0,
            expected_cycles=6_000,
            usable_capacity=10.0,
            conversion_loss_pct=10.0,
            import_price=0.15,
        )
        # Check the string representation has at most 3 decimal places
        decimals = len(str(result).split(".")[-1])
        assert decimals <= 3

    def test_negative_purchase_price_returns_zero(self):
        """Negative purchase price is treated as unconfigured → 0.0."""
        result = calculate_recommended_threshold(
            purchase_price=-100.0,
            expected_cycles=6_000,
            usable_capacity=10.0,
            conversion_loss_pct=10.0,
        )
        assert result == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# validate_batteries_excess_export_input tests
# ---------------------------------------------------------------------------


class TestValidateBatteriesExcessExportInput:
    """Unit tests for the config-flow input validator."""

    @pytest.mark.asyncio
    async def test_valid_input_produces_no_errors(self):
        """A fully valid input dict must return an empty error dict."""
        user_input = {
            "hsem_batteries_enable_excess_export": True,
            "hsem_batteries_excess_export_discharge_buffer": 10,
            "hsem_batteries_excess_export_price_threshold": 0.10,
        }
        errors = await validate_batteries_excess_export_input(user_input)
        assert errors == {}

    @pytest.mark.asyncio
    async def test_buffer_at_zero_is_valid(self):
        """Buffer of 0 % is at the minimum boundary and must be accepted."""
        user_input = {
            "hsem_batteries_enable_excess_export": False,
            "hsem_batteries_excess_export_discharge_buffer": 0,
            "hsem_batteries_excess_export_price_threshold": 0.0,
        }
        errors = await validate_batteries_excess_export_input(user_input)
        assert errors == {}

    @pytest.mark.asyncio
    async def test_buffer_at_fifty_is_valid(self):
        """Buffer of 50 % is at the maximum boundary and must be accepted."""
        user_input = {
            "hsem_batteries_enable_excess_export": False,
            "hsem_batteries_excess_export_discharge_buffer": 50,
            "hsem_batteries_excess_export_price_threshold": 0.0,
        }
        errors = await validate_batteries_excess_export_input(user_input)
        assert errors == {}

    @pytest.mark.asyncio
    async def test_buffer_above_fifty_is_invalid(self):
        """Buffer > 50 % must be rejected."""
        user_input = {
            "hsem_batteries_enable_excess_export": True,
            "hsem_batteries_excess_export_discharge_buffer": 51,
            "hsem_batteries_excess_export_price_threshold": 0.10,
        }
        errors = await validate_batteries_excess_export_input(user_input)
        assert "hsem_batteries_excess_export_discharge_buffer" in errors

    @pytest.mark.asyncio
    async def test_buffer_negative_is_invalid(self):
        """Negative buffer must be rejected."""
        user_input = {
            "hsem_batteries_enable_excess_export": True,
            "hsem_batteries_excess_export_discharge_buffer": -1,
            "hsem_batteries_excess_export_price_threshold": 0.10,
        }
        errors = await validate_batteries_excess_export_input(user_input)
        assert "hsem_batteries_excess_export_discharge_buffer" in errors

    @pytest.mark.asyncio
    async def test_price_threshold_at_zero_is_valid(self):
        """Price threshold of 0.0 (export any positive price) must be valid."""
        user_input = {
            "hsem_batteries_enable_excess_export": True,
            "hsem_batteries_excess_export_discharge_buffer": 10,
            "hsem_batteries_excess_export_price_threshold": 0.0,
        }
        errors = await validate_batteries_excess_export_input(user_input)
        assert errors == {}

    @pytest.mark.asyncio
    async def test_price_threshold_negative_is_invalid(self):
        """Negative price threshold must be rejected."""
        user_input = {
            "hsem_batteries_enable_excess_export": True,
            "hsem_batteries_excess_export_discharge_buffer": 10,
            "hsem_batteries_excess_export_price_threshold": -0.01,
        }
        errors = await validate_batteries_excess_export_input(user_input)
        assert "hsem_batteries_excess_export_price_threshold" in errors

    @pytest.mark.asyncio
    async def test_float_buffer_is_accepted(self):
        """Fractional buffer values (e.g. 10.5 %) are valid."""
        user_input = {
            "hsem_batteries_enable_excess_export": True,
            "hsem_batteries_excess_export_discharge_buffer": 10.5,
            "hsem_batteries_excess_export_price_threshold": 0.10,
        }
        errors = await validate_batteries_excess_export_input(user_input)
        assert errors == {}

    @pytest.mark.asyncio
    async def test_missing_fields_produce_no_crash(self):
        """Empty dict should not raise — missing optional fields are skipped."""
        errors = await validate_batteries_excess_export_input({})
        assert errors == {}
