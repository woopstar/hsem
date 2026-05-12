"""Tests for write-and-verify inverter applier (issue #275).

These tests cover:
- :mod:`utils.inverter_verify`: core write-and-verify primitive and result types.
- :mod:`custom_sensors.applier`: :func:`_parse_power_control_pct` (no change) and
  the new read-back helper functions.
- ``CycleApplySummary`` aggregation logic.
- Edge cases: None readers, write errors, tolerance boundaries, string matching.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from custom_components.hsem.utils.inverter_verify import (
    ApplyResult,
    ApplyStatus,
    CycleApplySummary,
    _values_match,
    async_write_and_verify,
)

# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def _make_writer(fail: bool = False) -> AsyncMock:
    """Return an AsyncMock writer that optionally raises on the first call."""
    mock = AsyncMock()
    if fail:
        mock.side_effect = [RuntimeError("Service not found"), None, None]
    return mock


def _const_reader(value):
    """Return a sync callable that always returns *value*."""
    return lambda: value


def _async_const_reader(value):
    """Return a coroutine callable that always returns *value*."""

    async def _reader():
        return value

    return _reader


# ---------------------------------------------------------------------------
# _values_match
# ---------------------------------------------------------------------------


class TestValuesMatch:
    """Unit tests for the :func:`_values_match` helper."""

    def test_exact_int_match(self):
        assert _values_match(80, 80, 1.0) is True

    def test_int_within_tolerance(self):
        assert _values_match(79, 80, 1.0) is True

    def test_int_outside_tolerance(self):
        assert _values_match(78, 80, 1.0) is False

    def test_float_match(self):
        assert _values_match(80.0, 80, 1.0) is True

    def test_float_boundary_inclusive(self):
        """Exactly at the tolerance boundary should still pass."""
        assert _values_match(79.0, 80.0, 1.0) is True

    def test_float_just_outside_tolerance(self):
        assert _values_match(78.9, 80.0, 1.0) is False

    def test_string_exact_match(self):
        assert (
            _values_match("MaximizeSelfConsumption", "MaximizeSelfConsumption", 0)
            is True
        )

    def test_string_case_insensitive(self):
        assert (
            _values_match("maximizeselFConsumption", "MaximizeSelfConsumption", 0)
            is True
        )

    def test_string_mismatch(self):
        assert _values_match("TimeOfUse", "MaximizeSelfConsumption", 0) is False

    def test_zero_tolerance_numeric(self):
        assert _values_match(80, 80, 0) is True
        assert _values_match(79, 80, 0) is False

    def test_negative_numeric(self):
        """Negative values should work correctly with tolerance."""
        assert _values_match(-1, 0, 1.0) is True
        assert _values_match(-2, 0, 1.0) is False


# ---------------------------------------------------------------------------
# ApplyStatus enum
# ---------------------------------------------------------------------------


class TestApplyStatus:
    """Verify enum values are stable string literals."""

    def test_ok_value(self):
        assert ApplyStatus.OK.value == "ok"

    def test_failed_value(self):
        assert ApplyStatus.FAILED.value == "failed"

    def test_unverified_value(self):
        assert ApplyStatus.UNVERIFIED.value == "unverified"

    def test_skipped_value(self):
        assert ApplyStatus.SKIPPED.value == "skipped"

    def test_unique_values(self):
        values = [s.value for s in ApplyStatus]
        assert len(values) == len(set(values))


# ---------------------------------------------------------------------------
# CycleApplySummary aggregation
# ---------------------------------------------------------------------------


class TestCycleApplySummary:
    """Tests for :class:`CycleApplySummary` aggregation helpers."""

    def test_empty_summary_is_skipped(self):
        s = CycleApplySummary()
        assert s.overall_status == ApplyStatus.SKIPPED

    def test_all_ok(self):
        s = CycleApplySummary(
            results=[
                ApplyResult("e1", 80, 80, ApplyStatus.OK, 1),
                ApplyResult("e2", "TOU", "tou", ApplyStatus.OK, 1),
            ]
        )
        assert s.overall_status == ApplyStatus.OK

    def test_all_skipped(self):
        s = CycleApplySummary(
            results=[
                ApplyResult("e1", 80, 80, ApplyStatus.SKIPPED, 0),
            ]
        )
        assert s.overall_status == ApplyStatus.SKIPPED

    def test_failed_dominates_ok(self):
        s = CycleApplySummary(
            results=[
                ApplyResult("e1", 80, 80, ApplyStatus.OK, 1),
                ApplyResult("e2", 100, 50, ApplyStatus.FAILED, 3, "mismatch"),
            ]
        )
        assert s.overall_status == ApplyStatus.FAILED

    def test_unverified_dominates_ok(self):
        s = CycleApplySummary(
            results=[
                ApplyResult("e1", 80, 80, ApplyStatus.OK, 1),
                ApplyResult(
                    "e2", 100, None, ApplyStatus.UNVERIFIED, 3, "None read-back"
                ),
            ]
        )
        assert s.overall_status == ApplyStatus.UNVERIFIED

    def test_failed_dominates_unverified(self):
        s = CycleApplySummary(
            results=[
                ApplyResult("e1", 100, None, ApplyStatus.UNVERIFIED, 3),
                ApplyResult("e2", 100, 50, ApplyStatus.FAILED, 3, "mismatch"),
            ]
        )
        assert s.overall_status == ApplyStatus.FAILED

    def test_failed_entities_list(self):
        s = CycleApplySummary(
            results=[
                ApplyResult("entity_a", 80, 80, ApplyStatus.OK, 1),
                ApplyResult("entity_b", 100, 50, ApplyStatus.FAILED, 3),
                ApplyResult("entity_c", 100, 50, ApplyStatus.FAILED, 3),
            ]
        )
        assert s.failed_entities == ["entity_b", "entity_c"]

    def test_unverified_entities_list(self):
        s = CycleApplySummary(
            results=[
                ApplyResult("entity_x", 100, None, ApplyStatus.UNVERIFIED, 3),
                ApplyResult("entity_y", 80, 80, ApplyStatus.OK, 1),
            ]
        )
        assert s.unverified_entities == ["entity_x"]

    def test_no_failed_when_all_ok(self):
        s = CycleApplySummary(
            results=[
                ApplyResult("e1", 80, 80, ApplyStatus.OK, 1),
            ]
        )
        assert s.failed_entities == []
        assert s.unverified_entities == []


# ---------------------------------------------------------------------------
# async_write_and_verify — write success paths
# ---------------------------------------------------------------------------


class TestWriteAndVerifySuccess:
    """Tests for successful write-and-verify scenarios."""

    @pytest.mark.asyncio
    async def test_write_and_verify_ok_numeric(self):
        """Write and read-back within tolerance returns OK."""
        writer = AsyncMock()
        # Use skip_if_equal=False so the write path is exercised even when
        # current == desired (simulates TOU-style forced writes).
        result = await async_write_and_verify(
            entity_id="number.bat_discharge",
            desired=5000,
            writer=writer,
            reader=_const_reader(5000),
            settle_seconds=0,
            skip_if_equal=False,
        )
        assert result.status == ApplyStatus.OK
        assert result.attempts == 1
        assert result.actual == pytest.approx(5000)
        writer.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_write_and_verify_ok_string(self):
        """Write a working-mode string and verify exact match."""
        writer = AsyncMock()
        result = await async_write_and_verify(
            entity_id="select.working_mode",
            desired="MaximizeSelfConsumption",
            writer=writer,
            reader=_const_reader("MaximizeSelfConsumption"),
            settle_seconds=0,
            skip_if_equal=False,
        )
        assert result.status == ApplyStatus.OK
        assert result.entity_id == "select.working_mode"

    @pytest.mark.asyncio
    async def test_skipped_when_already_matches(self):
        """Write is skipped if the current value already matches desired."""
        writer = AsyncMock()
        result = await async_write_and_verify(
            entity_id="number.bat_discharge",
            desired=5000,
            writer=writer,
            reader=_const_reader(5000),
            settle_seconds=0,
            skip_if_equal=True,
        )
        assert result.status == ApplyStatus.SKIPPED
        assert result.attempts == 0
        writer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_not_skipped_when_skip_if_equal_false(self):
        """When skip_if_equal=False the write always executes even if values match."""
        writer = AsyncMock()
        result = await async_write_and_verify(
            entity_id="select.tou",
            desired="same_value",
            writer=writer,
            reader=_const_reader("same_value"),
            settle_seconds=0,
            skip_if_equal=False,
        )
        assert result.status == ApplyStatus.OK
        writer.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_verify_within_tolerance(self):
        """Read-back within tolerance (but not exact) is accepted."""
        writer = AsyncMock()
        call_count = 0

        def _reader():
            nonlocal call_count
            call_count += 1
            # Pre-flight returns 90 (outside tolerance of 1.0 vs desired 100),
            # so the skip is not triggered.  After the write it returns 99.5,
            # which IS within the 1.0 tolerance.
            return 90 if call_count == 1 else 99.5

        result = await async_write_and_verify(
            entity_id="number.bat",
            desired=100,
            writer=writer,
            reader=_reader,
            tolerance=1.0,
            settle_seconds=0,
        )
        assert result.status == ApplyStatus.OK

    @pytest.mark.asyncio
    async def test_async_reader_is_supported(self):
        """Coroutine readers are awaited correctly."""
        writer = AsyncMock()
        result = await async_write_and_verify(
            entity_id="select.mode",
            desired="TimeOfUse",
            writer=writer,
            reader=_async_const_reader("TimeOfUse"),
            settle_seconds=0,
            skip_if_equal=False,
        )
        assert result.status == ApplyStatus.OK

    @pytest.mark.asyncio
    async def test_current_reader_error_does_not_block_write(self):
        """Pre-flight reader failure falls through to the write attempt."""
        writer = AsyncMock()
        call_count = 0

        def _failing_then_ok():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Entity unavailable")
            return 80

        result = await async_write_and_verify(
            entity_id="number.bat",
            desired=80,
            writer=writer,
            reader=_failing_then_ok,
            settle_seconds=0,
        )
        assert result.status == ApplyStatus.OK
        writer.assert_awaited_once()


# ---------------------------------------------------------------------------
# async_write_and_verify — failure paths
# ---------------------------------------------------------------------------


class TestWriteAndVerifyFailure:
    """Tests for write failure and mismatch retry scenarios."""

    @pytest.mark.asyncio
    async def test_failed_after_all_retries_mismatch(self):
        """Returns FAILED when read-back never matches despite all retries."""
        writer = AsyncMock()
        result = await async_write_and_verify(
            entity_id="select.mode",
            desired="TimeOfUse",
            writer=writer,
            reader=_const_reader("MaximizeSelfConsumption"),
            settle_seconds=0,
            max_retries=3,
        )
        assert result.status == ApplyStatus.FAILED
        assert result.attempts == 3
        assert result.actual == "MaximizeSelfConsumption"
        assert "mismatch" in result.error_message.lower()
        assert writer.await_count == 3

    @pytest.mark.asyncio
    async def test_unverified_when_reader_always_returns_none(self):
        """Returns UNVERIFIED when the entity is never readable after write."""
        writer = AsyncMock()
        result = await async_write_and_verify(
            entity_id="number.bat",
            desired=5000,
            writer=writer,
            reader=_const_reader(None),
            settle_seconds=0,
            max_retries=2,
        )
        assert result.status == ApplyStatus.UNVERIFIED
        assert result.actual is None

    @pytest.mark.asyncio
    async def test_retries_on_write_error_then_succeeds(self):
        """Writer raises on first attempt but succeeds on second; overall OK."""
        call_count = 0

        async def _flaky_writer():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Transient error")

        read_count = 0

        def _reader():
            nonlocal read_count
            read_count += 1
            return 80 if read_count >= 2 else None

        result = await async_write_and_verify(
            entity_id="number.bat",
            desired=80,
            writer=_flaky_writer,
            reader=_reader,
            settle_seconds=0,
            max_retries=3,
        )
        assert result.status == ApplyStatus.OK
        assert result.attempts == 2

    @pytest.mark.asyncio
    async def test_all_write_errors_returns_failed_or_unverified(self):
        """Writer always raises; the last attempt cannot produce a read-back."""

        async def _always_fails():
            raise RuntimeError("Network error")

        result = await async_write_and_verify(
            entity_id="select.mode",
            desired="TimeOfUse",
            writer=_always_fails,
            reader=_const_reader(None),
            settle_seconds=0,
            max_retries=3,
        )
        # last_actual stays None (never read a value) → UNVERIFIED
        assert result.status == ApplyStatus.UNVERIFIED

    @pytest.mark.asyncio
    async def test_invalid_max_retries_raises(self):
        """max_retries < 1 must raise ValueError immediately."""
        with pytest.raises(ValueError, match="max_retries"):
            await async_write_and_verify(
                entity_id="x",
                desired=0,
                writer=AsyncMock(),
                reader=_const_reader(0),
                max_retries=0,
            )

    @pytest.mark.asyncio
    async def test_succeeds_on_second_attempt(self):
        """First read-back mismatches; second attempt's read-back succeeds."""
        read_call = 0

        def _reader():
            nonlocal read_call
            read_call += 1
            # Call 1: pre-flight (50 — not matching desired 80, so no skip)
            # Call 2: after write attempt 1 — returns wrong value (50)
            # Call 3: after write attempt 2 — returns correct value (80)
            if read_call <= 2:
                return 50
            return 80

        writer = AsyncMock()
        result = await async_write_and_verify(
            entity_id="number.bat",
            desired=80,
            writer=writer,
            reader=_reader,
            settle_seconds=0,
            max_retries=3,
        )
        assert result.status == ApplyStatus.OK
        assert result.attempts == 2

    @pytest.mark.asyncio
    async def test_outside_tolerance_fails(self):
        """Read-back outside tolerance does not count as a match."""
        writer = AsyncMock()
        result = await async_write_and_verify(
            entity_id="number.bat",
            desired=100,
            writer=writer,
            reader=_const_reader(95.0),
            tolerance=1.0,
            settle_seconds=0,
            max_retries=1,
        )
        assert result.status == ApplyStatus.FAILED


# ---------------------------------------------------------------------------
# ApplyResult dataclass
# ---------------------------------------------------------------------------


class TestApplyResult:
    """Verify ApplyResult defaults and field population."""

    def test_default_attempts_zero(self):
        r = ApplyResult("e", 1, 1, ApplyStatus.SKIPPED)
        assert r.attempts == 0

    def test_default_error_message_empty(self):
        r = ApplyResult("e", 1, 1, ApplyStatus.OK)
        assert r.error_message == ""

    def test_entity_id_preserved(self):
        r = ApplyResult("select.mode", "TOU", "TOU", ApplyStatus.OK, 1)
        assert r.entity_id == "select.mode"
