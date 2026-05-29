"""Tests for month validation and month matching logic."""

from __future__ import annotations

# Mock the logging to avoid file creation errors during testing
from logging.handlers import RotatingFileHandler
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.hsem.flows.months import get_months_schema, validate_months_input
from custom_components.hsem.utils.misc import convert_months_to_int

# Monkey-patch RotatingFileHandler to use NullHandler during tests
original_rotating_handler = RotatingFileHandler


class TestConvertMonthsToInt:
    """Test month conversion to integers."""

    def test_convert_string_months_to_int(self):
        """Test converting string months to integers."""
        result = convert_months_to_int(["1", "2", "3"])
        assert result == [1, 2, 3]

    def test_convert_int_months(self):
        """Test that integer months pass through correctly."""
        result = convert_months_to_int([1, 2, 3])
        assert result == [1, 2, 3]

    def test_convert_mixed_string_and_int_months(self):
        """Test converting mixed string and integer months."""
        result = convert_months_to_int(["1", 2, "3", 4])
        assert result == [1, 2, 3, 4]

    def test_january_only_matches_one(self):
        """Test that January (1) only matches month 1, not 10, 11, 12."""
        result = convert_months_to_int(["1"])
        assert result == [1]
        assert 10 not in result
        assert 11 not in result
        assert 12 not in result

    def test_october_only_matches_ten(self):
        """Test that October only matches 10, not 1 via string containment."""
        result = convert_months_to_int(["10"])
        assert result == [10]
        assert 1 not in result

    def test_all_valid_months(self):
        """Test all valid month numbers 1-12."""
        result = convert_months_to_int([str(i) for i in range(1, 13)])
        assert result == list(range(1, 13))

    def test_invalid_month_zero(self):
        """Test that month 0 is rejected."""
        with pytest.raises(ValueError, match="Month must be between 1 and 12"):
            convert_months_to_int(["0"])

    def test_invalid_month_thirteen(self):
        """Test that month 13 is rejected."""
        with pytest.raises(ValueError, match="Month must be between 1 and 12"):
            convert_months_to_int(["13"])

    def test_invalid_month_negative(self):
        """Test that negative months are rejected."""
        with pytest.raises(ValueError, match="Month must be between 1 and 12"):
            convert_months_to_int(["-1"])

    def test_invalid_month_non_numeric(self):
        """Test that non-numeric month values are rejected."""
        with pytest.raises(ValueError, match="Invalid month value"):
            convert_months_to_int(["abc"])

    def test_invalid_month_float(self):
        """Test that float values are converted if they represent valid months."""
        result = convert_months_to_int(["1.0", "6.5"])
        assert result == [1, 6]

    def test_empty_list(self):
        """Test that empty list returns empty list."""
        result = convert_months_to_int([])
        assert result == []

    def test_duplicate_months(self):
        """Test that duplicate months are preserved."""
        result = convert_months_to_int(["1", "1", "2"])
        assert result == [1, 1, 2]


@pytest.mark.asyncio
class TestValidateMonthsInput:
    """Test month validation in config flow."""

    async def test_validate_valid_winter_months(self):
        """Test validation passes for valid winter months."""
        user_input = {"hsem_months_winter": ["1", "2", "3", "4", "10", "11", "12"]}
        errors = await validate_months_input(None, user_input)
        assert not errors

    async def test_validate_valid_partial_winter_months(self):
        """Test validation passes for partial winter months."""
        user_input = {"hsem_months_winter": ["1", "2", "3"]}
        errors = await validate_months_input(None, user_input)
        assert not errors

    async def test_validate_missing_winter_months(self):
        """Test validation fails when winter months are missing."""
        user_input = {}
        errors = await validate_months_input(None, user_input)
        assert "hsem_months_winter" in errors
        assert errors["hsem_months_winter"] == "required"

    async def test_validate_invalid_month_zero(self):
        """Test validation fails for month 0."""
        user_input = {"hsem_months_winter": ["0"]}
        errors = await validate_months_input(None, user_input)
        assert "hsem_months_winter" in errors

    async def test_validate_invalid_month_thirteen(self):
        """Test validation fails for month 13."""
        user_input = {"hsem_months_winter": ["13"]}
        errors = await validate_months_input(None, user_input)
        assert "hsem_months_winter" in errors

    async def test_validate_invalid_month_string(self):
        """Test validation fails for non-numeric month."""
        user_input = {"hsem_months_winter": ["abc"]}
        errors = await validate_months_input(None, user_input)
        assert "hsem_months_winter" in errors

    async def test_validate_empty_winter_months(self):
        """Test validation fails when winter months list is empty."""
        user_input = {"hsem_months_winter": []}
        errors = await validate_months_input(None, user_input)
        assert "hsem_months_winter" in errors
        # Empty list is treated as a missing/required value by the centralized validator.
        assert errors["hsem_months_winter"] == "required"

    async def test_validate_all_months_winter(self):
        """Test validation fails when all months are winter (no summer)."""
        user_input = {"hsem_months_winter": [str(i) for i in range(1, 13)]}
        errors = await validate_months_input(None, user_input)
        assert "hsem_months_winter" in errors
        # Centralized validator returns a translation key, not a human-readable string.
        assert errors["hsem_months_winter"] == "months_summer_empty"

    async def test_validate_integer_months(self):
        """Test validation works with integer month values."""
        user_input = {"hsem_months_winter": [1, 2, 3, 4, 10, 11, 12]}
        errors = await validate_months_input(None, user_input)
        assert not errors

    async def test_validate_mixed_string_and_int(self):
        """Test validation works with mixed string and integer months."""
        user_input = {"hsem_months_winter": ["1", 2, "3", 4]}
        errors = await validate_months_input(None, user_input)
        assert not errors


@pytest.mark.asyncio
class TestGetMonthsSchema:
    """Test that get_months_schema converts stored integers back to strings for the UI."""

    def _get_default(self, schema: Any) -> list:
        """Extract the default value for hsem_months_winter from a vol.Schema."""
        import voluptuous as vol

        for key in schema.schema:
            if isinstance(key, vol.Required) and key.schema == "hsem_months_winter":
                return key.default()  # type: ignore[operator]  # voluptuous stubs incomplete
        raise KeyError("hsem_months_winter not found in schema")

    async def test_schema_default_is_strings_when_config_has_integers(self):
        """Stored integer months must be converted to strings for the multi-select."""
        config_entry = MagicMock()
        config_entry.options = {"hsem_months_winter": [1, 2, 3, 4, 10, 11, 12]}
        config_entry.data = {}

        schema = await get_months_schema(config_entry)
        default = self._get_default(schema)
        assert default == ["1", "2", "3", "4", "10", "11", "12"]
        assert all(isinstance(m, str) for m in default)

    async def test_schema_default_is_strings_when_config_has_strings(self):
        """String months stored in config must remain strings."""
        config_entry = MagicMock()
        config_entry.options = {"hsem_months_winter": ["1", "2", "12"]}
        config_entry.data = {}

        schema = await get_months_schema(config_entry)
        default = self._get_default(schema)
        assert default == ["1", "2", "12"]

    async def test_schema_default_uses_const_default_when_no_config(self):
        """With no config entry, the schema default must use the const default as strings."""
        schema = await get_months_schema(None)
        default = self._get_default(schema)
        # const default is [1, 2, 3, 4, 10, 11, 12] — must come back as strings
        assert set(default) == {"1", "2", "3", "4", "10", "11", "12"}
        assert all(isinstance(m, str) for m in default)


class TestMonthMembership:
    """Test that month membership checks work correctly (not string containment)."""

    def test_january_not_in_october_november_december_string(self):
        """
        Test that January (1) doesn't match in string "['10', '11', '12']".
        This was the original bug: "1" in "['10', '11', '12']" was True.
        """
        # Old buggy way (string containment)
        winter_str = str(["10", "11", "12"])
        assert "1" in winter_str  # This is True! (String containment bug)

        # New correct way (integer membership)
        winter_int = [10, 11, 12]
        assert 1 not in winter_int  # This is correct

    def test_all_months_as_integers(self):
        """Test that months work correctly as integers."""
        winter_months = [1, 2, 3, 4, 10, 11, 12]
        summer_months = [5, 6, 7, 8, 9]

        # Test each valid month
        for month in range(1, 13):
            if month in winter_months:
                assert month not in summer_months
            else:
                assert month in summer_months

    def test_october_november_december_not_in_january_winter(self):
        """Test that October, November, December don't match January check."""
        winter_months = [1]  # Only January

        assert 1 in winter_months
        assert 10 not in winter_months
        assert 11 not in winter_months
        assert 12 not in winter_months
