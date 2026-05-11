"""Tests for the update-loop lock on HSEMWorkingModeSensor (P0-06, issue #270).

Acceptance criteria:
- Only one planner/apply cycle runs at a time.
- A second concurrent call is skipped (not queued) while the first is active.
- No double inverter write occurs during concurrent calls.

These tests exercise ``_async_handle_update`` in isolation, using a minimal
stub sensor so that no Home Assistant runtime or real inverter is required.
"""

import asyncio

import pytest


# ---------------------------------------------------------------------------
# Minimal stub that mimics only the locking API of HSEMWorkingModeSensor
# ---------------------------------------------------------------------------


class _StubSensor:
    """Minimal stub exposing the same lock behaviour as HSEMWorkingModeSensor."""

    def __init__(self) -> None:
        self._update_lock = asyncio.Lock()
        self._cycle_call_count = 0
        self._skipped_count = 0
        self._name = "stub_sensor"

    async def _async_logger(self, msg: str) -> None:  # noqa: D102 (no docstring needed)
        pass

    async def _async_handle_update(self, event=None) -> None:
        """Exact copy of the production guard logic."""
        if self._update_lock.locked():
            await self._async_logger(
                "------ Update skipped: a previous update cycle is still running."
            )
            self._skipped_count += 1
            return

        async with self._update_lock:
            await self._async_run_update_cycle(event)

    async def _async_run_update_cycle(self, event=None) -> None:
        """Simulates a slow update cycle (2 event-loop ticks)."""
        self._cycle_call_count += 1
        # Yield control so a concurrent caller can attempt to acquire the lock.
        await asyncio.sleep(0)
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestUpdateLoopLock:
    """Verify the asyncio.Lock guard on the update handler."""

    @pytest.mark.asyncio
    async def test_lock_exists_on_sensor(self) -> None:
        """HSEMWorkingModeSensor.__init__ creates an asyncio.Lock.

        We inspect the source of ``__init__`` rather than constructing a real
        sensor instance, because instantiation requires a complete HA runtime
        (config entry, voluptuous selectors, etc.).  The stub sensor
        (``_StubSensor``) already exercises the identical locking logic end-to-
        end, so the only thing we need to confirm here is that the production
        class actually initialises the lock attribute.
        """
        from custom_components.hsem.custom_sensors.working_mode_sensor import (
            HSEMWorkingModeSensor,
        )
        import inspect

        source = inspect.getsource(HSEMWorkingModeSensor.__init__)

        assert "_update_lock = asyncio.Lock()" in source, (
            "HSEMWorkingModeSensor.__init__ must create self._update_lock = asyncio.Lock()"
        )

    @pytest.mark.asyncio
    async def test_single_update_runs_cycle(self) -> None:
        """A lone call to _async_handle_update executes the cycle exactly once."""
        sensor = _StubSensor()

        await sensor._async_handle_update()

        assert sensor._cycle_call_count == 1
        assert sensor._skipped_count == 0

    @pytest.mark.asyncio
    async def test_concurrent_second_call_is_skipped(self) -> None:
        """While the first update is running the second call is skipped."""
        sensor = _StubSensor()

        # Launch both coroutines concurrently; they share the same event loop.
        await asyncio.gather(
            sensor._async_handle_update(),
            sensor._async_handle_update(),
        )

        # The cycle must have run exactly once; the duplicate was dropped.
        assert sensor._cycle_call_count == 1, (
            f"Expected cycle to run once, ran {sensor._cycle_call_count} times"
        )
        assert sensor._skipped_count == 1, (
            f"Expected one skip, got {sensor._skipped_count}"
        )

    @pytest.mark.asyncio
    async def test_no_double_write_on_concurrent_calls(self) -> None:
        """Verify the inverter write method is called at most once for two concurrent updates."""
        write_calls: list[str] = []

        class _WriteTrackingSensor(_StubSensor):
            async def _async_run_update_cycle(self, event=None) -> None:  # type: ignore[override]
                self._cycle_call_count += 1
                write_calls.append("write")
                # Simulate async I/O latency so the second caller can arrive.
                await asyncio.sleep(0)
                await asyncio.sleep(0)

        sensor = _WriteTrackingSensor()

        await asyncio.gather(
            sensor._async_handle_update(),
            sensor._async_handle_update(),
        )

        assert len(write_calls) == 1, (
            f"Inverter write must happen exactly once; happened {len(write_calls)} times"
        )

    @pytest.mark.asyncio
    async def test_sequential_updates_both_run(self) -> None:
        """Two sequential (non-overlapping) updates must both execute the cycle."""
        sensor = _StubSensor()

        await sensor._async_handle_update()
        await sensor._async_handle_update()

        assert sensor._cycle_call_count == 2
        assert sensor._skipped_count == 0

    @pytest.mark.asyncio
    async def test_lock_released_after_cycle(self) -> None:
        """After a completed update the lock must be released for the next call."""
        sensor = _StubSensor()

        await sensor._async_handle_update()

        assert not sensor._update_lock.locked(), (
            "Lock must be released after the update cycle completes"
        )

    @pytest.mark.asyncio
    async def test_three_concurrent_calls_only_one_runs(self) -> None:
        """Three simultaneous calls: exactly one cycle runs and two are skipped."""
        sensor = _StubSensor()

        await asyncio.gather(
            sensor._async_handle_update(),
            sensor._async_handle_update(),
            sensor._async_handle_update(),
        )

        assert sensor._cycle_call_count == 1
        assert sensor._skipped_count == 2
