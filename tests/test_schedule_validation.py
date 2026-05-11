"""Tests for battery schedule validation logic (issue #268).

Verifies:
- schedule_3 is disabled by default (enabled=False) with non-ambiguous placeholder times
- A zero-length active schedule (start == end) raises a validation error
- Disabled schedules bypass time-window validation entirely
- Valid cross-midnight windows are accepted
- Invalid time formats produce the correct error key
"""

import pytest

from custom_components.hsem.const import DEFAULT_CONFIG_VALUES
from custom_components.hsem.flows.batteries_schedule_1 import (
    validate_batteries_schedule_1_input,
)
from custom_components.hsem.flows.batteries_schedule_2 import (
    validate_batteries_schedule_2_input,
)
from custom_components.hsem.flows.batteries_schedule_3 import (
    validate_batteries_schedule_3_input,
)


# ---------------------------------------------------------------------------
# Default constant tests
# ---------------------------------------------------------------------------


class TestSchedule3DefaultValues:
    """Verify the schedule_3 defaults satisfy the acceptance criteria."""

    def test_schedule_3_disabled_by_default(self):
        """schedule_3 must be disabled (False) out of the box."""
        assert (
            DEFAULT_CONFIG_VALUES["hsem_batteries_enable_batteries_schedule_3"] is False
        )

    def test_schedule_3_default_start_is_not_midnight(self):
        """Default start time must not be 00:00:00 to avoid ambiguous zero-length window."""
        start = DEFAULT_CONFIG_VALUES[
            "hsem_batteries_enable_batteries_schedule_3_start"
        ]
        assert start != "00:00:00", (
            "Default start '00:00:00' combined with default end '00:00:00' creates an "
            "ambiguous zero-length window. Use explicit placeholder times instead."
        )

    def test_schedule_3_default_end_is_not_midnight(self):
        """Default end time must not be 00:00:00 to avoid ambiguous zero-length window."""
        end = DEFAULT_CONFIG_VALUES["hsem_batteries_enable_batteries_schedule_3_end"]
        assert end != "00:00:00", (
            "Default end '00:00:00' combined with default start '00:00:00' creates an "
            "ambiguous zero-length window. Use explicit placeholder times instead."
        )

    def test_schedule_3_default_start_and_end_differ(self):
        """Default start and end must differ so the window is non-zero when enabled."""
        start = DEFAULT_CONFIG_VALUES[
            "hsem_batteries_enable_batteries_schedule_3_start"
        ]
        end = DEFAULT_CONFIG_VALUES["hsem_batteries_enable_batteries_schedule_3_end"]
        assert start != end, (
            "Default schedule_3 has a zero-length window (start == end)."
        )

    def test_schedule_1_and_2_enabled_by_default(self):
        """Schedules 1 and 2 should remain enabled in their defaults."""
        assert (
            DEFAULT_CONFIG_VALUES["hsem_batteries_enable_batteries_schedule_1"] is True
        )
        assert (
            DEFAULT_CONFIG_VALUES["hsem_batteries_enable_batteries_schedule_2"] is True
        )


# ---------------------------------------------------------------------------
# Zero-length window rejection tests
# ---------------------------------------------------------------------------


class TestZeroLengthWindowRejected:
    """An active schedule with start == end must return a validation error."""

    @pytest.mark.asyncio
    async def test_schedule_1_zero_length_active_raises_error(self):
        """Schedule 1 rejects start == end when enabled."""
        user_input = {
            "hsem_batteries_enable_batteries_schedule_1": True,
            "hsem_batteries_enable_batteries_schedule_1_start": "08:00:00",
            "hsem_batteries_enable_batteries_schedule_1_end": "08:00:00",
            "hsem_batteries_enable_batteries_schedule_1_min_price_difference": 0.0,
        }
        errors = await validate_batteries_schedule_1_input(user_input)
        assert errors.get("base") == "start_time_equals_end_time"

    @pytest.mark.asyncio
    async def test_schedule_2_zero_length_active_raises_error(self):
        """Schedule 2 rejects start == end when enabled."""
        user_input = {
            "hsem_batteries_enable_batteries_schedule_2": True,
            "hsem_batteries_enable_batteries_schedule_2_start": "17:00:00",
            "hsem_batteries_enable_batteries_schedule_2_end": "17:00:00",
            "hsem_batteries_enable_batteries_schedule_2_min_price_difference": 0.0,
        }
        errors = await validate_batteries_schedule_2_input(user_input)
        assert errors.get("base") == "start_time_equals_end_time"

    @pytest.mark.asyncio
    async def test_schedule_3_zero_length_active_raises_error(self):
        """Schedule 3 rejects start == end when enabled."""
        user_input = {
            "hsem_batteries_enable_batteries_schedule_3": True,
            "hsem_batteries_enable_batteries_schedule_3_start": "00:00:00",
            "hsem_batteries_enable_batteries_schedule_3_end": "00:00:00",
            "hsem_batteries_enable_batteries_schedule_3_min_price_difference": 0.0,
        }
        errors = await validate_batteries_schedule_3_input(user_input)
        assert errors.get("base") == "start_time_equals_end_time"

    @pytest.mark.asyncio
    async def test_schedule_3_midnight_to_midnight_active_raises_error(self):
        """The original ambiguous 00:00->00:00 is explicitly rejected when enabled."""
        user_input = {
            "hsem_batteries_enable_batteries_schedule_3": True,
            "hsem_batteries_enable_batteries_schedule_3_start": "00:00:00",
            "hsem_batteries_enable_batteries_schedule_3_end": "00:00:00",
            "hsem_batteries_enable_batteries_schedule_3_min_price_difference": 0.0,
        }
        errors = await validate_batteries_schedule_3_input(user_input)
        assert "base" in errors, "00:00->00:00 with enabled=True must be rejected"
        assert errors["base"] == "start_time_equals_end_time"


# ---------------------------------------------------------------------------
# Disabled schedule skips validation
# ---------------------------------------------------------------------------


class TestDisabledScheduleSkipsValidation:
    """Disabled schedules must not trigger time-window validation."""

    @pytest.mark.asyncio
    async def test_schedule_1_disabled_skips_time_validation(self):
        """Disabled schedule 1 should return no errors even with ambiguous times."""
        user_input = {
            "hsem_batteries_enable_batteries_schedule_1": False,
            "hsem_batteries_enable_batteries_schedule_1_start": "00:00:00",
            "hsem_batteries_enable_batteries_schedule_1_end": "00:00:00",
            "hsem_batteries_enable_batteries_schedule_1_min_price_difference": 0.0,
        }
        errors = await validate_batteries_schedule_1_input(user_input)
        assert errors == {}

    @pytest.mark.asyncio
    async def test_schedule_2_disabled_skips_time_validation(self):
        """Disabled schedule 2 should return no errors even with ambiguous times."""
        user_input = {
            "hsem_batteries_enable_batteries_schedule_2": False,
            "hsem_batteries_enable_batteries_schedule_2_start": "00:00:00",
            "hsem_batteries_enable_batteries_schedule_2_end": "00:00:00",
            "hsem_batteries_enable_batteries_schedule_2_min_price_difference": 0.0,
        }
        errors = await validate_batteries_schedule_2_input(user_input)
        assert errors == {}

    @pytest.mark.asyncio
    async def test_schedule_3_disabled_skips_time_validation(self):
        """Disabled schedule 3 should return no errors even with ambiguous times."""
        user_input = {
            "hsem_batteries_enable_batteries_schedule_3": False,
            "hsem_batteries_enable_batteries_schedule_3_start": "00:00:00",
            "hsem_batteries_enable_batteries_schedule_3_end": "00:00:00",
            "hsem_batteries_enable_batteries_schedule_3_min_price_difference": 0.0,
        }
        errors = await validate_batteries_schedule_3_input(user_input)
        assert errors == {}


# ---------------------------------------------------------------------------
# Valid window tests
# ---------------------------------------------------------------------------


class TestValidScheduleWindows:
    """Valid non-zero and cross-midnight windows must be accepted."""

    @pytest.mark.asyncio
    async def test_schedule_1_valid_daytime_window(self):
        """A normal daytime window is accepted."""
        user_input = {
            "hsem_batteries_enable_batteries_schedule_1": True,
            "hsem_batteries_enable_batteries_schedule_1_start": "07:00:00",
            "hsem_batteries_enable_batteries_schedule_1_end": "09:00:00",
            "hsem_batteries_enable_batteries_schedule_1_min_price_difference": 0.0,
        }
        errors = await validate_batteries_schedule_1_input(user_input)
        assert errors == {}

    @pytest.mark.asyncio
    async def test_schedule_2_valid_evening_window(self):
        """A normal evening window is accepted."""
        user_input = {
            "hsem_batteries_enable_batteries_schedule_2": True,
            "hsem_batteries_enable_batteries_schedule_2_start": "17:00:00",
            "hsem_batteries_enable_batteries_schedule_2_end": "21:00:00",
            "hsem_batteries_enable_batteries_schedule_2_min_price_difference": 0.0,
        }
        errors = await validate_batteries_schedule_2_input(user_input)
        assert errors == {}

    @pytest.mark.asyncio
    async def test_schedule_3_valid_cross_midnight_window(self):
        """A cross-midnight window (23:00->02:00) is accepted."""
        user_input = {
            "hsem_batteries_enable_batteries_schedule_3": True,
            "hsem_batteries_enable_batteries_schedule_3_start": "23:00:00",
            "hsem_batteries_enable_batteries_schedule_3_end": "02:00:00",
            "hsem_batteries_enable_batteries_schedule_3_min_price_difference": 0.0,
        }
        errors = await validate_batteries_schedule_3_input(user_input)
        assert errors == {}

    @pytest.mark.asyncio
    async def test_schedule_3_default_placeholder_times_are_valid_when_enabled(self):
        """The new default placeholder times (23:00->02:00) pass validation when enabled."""
        start = DEFAULT_CONFIG_VALUES[
            "hsem_batteries_enable_batteries_schedule_3_start"
        ]
        end = DEFAULT_CONFIG_VALUES["hsem_batteries_enable_batteries_schedule_3_end"]
        user_input = {
            "hsem_batteries_enable_batteries_schedule_3": True,
            "hsem_batteries_enable_batteries_schedule_3_start": start,
            "hsem_batteries_enable_batteries_schedule_3_end": end,
            "hsem_batteries_enable_batteries_schedule_3_min_price_difference": 0.0,
        }
        errors = await validate_batteries_schedule_3_input(user_input)
        assert errors == {}, (
            f"Default times {start}->{end} should be valid when schedule_3 is enabled, "
            f"but got errors: {errors}"
        )


# ---------------------------------------------------------------------------
# Invalid time format tests
# ---------------------------------------------------------------------------


class TestInvalidTimeFormat:
    """Invalid time strings must produce the correct error key."""

    @pytest.mark.asyncio
    async def test_schedule_1_invalid_time_format(self):
        """Bad time format produces 'invalid_time_format' error."""
        user_input = {
            "hsem_batteries_enable_batteries_schedule_1": True,
            "hsem_batteries_enable_batteries_schedule_1_start": "not-a-time",
            "hsem_batteries_enable_batteries_schedule_1_end": "09:00:00",
            "hsem_batteries_enable_batteries_schedule_1_min_price_difference": 0.0,
        }
        errors = await validate_batteries_schedule_1_input(user_input)
        assert errors.get("base") == "invalid_time_format"

    @pytest.mark.asyncio
    async def test_schedule_3_invalid_time_format(self):
        """Bad time format in schedule_3 produces 'invalid_time_format' error."""
        user_input = {
            "hsem_batteries_enable_batteries_schedule_3": True,
            "hsem_batteries_enable_batteries_schedule_3_start": "25:00:00",
            "hsem_batteries_enable_batteries_schedule_3_end": "02:00:00",
            "hsem_batteries_enable_batteries_schedule_3_min_price_difference": 0.0,
        }
        errors = await validate_batteries_schedule_3_input(user_input)
        assert errors.get("base") == "invalid_time_format"
