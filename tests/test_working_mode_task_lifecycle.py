"""Regression tests for P0-17: track and cancel working-mode update task on unload.

Acceptance criteria (issue #369)
---------------------------------
1. ``_update_task`` is stored after ``_handle_coordinator_update`` is called.
2. The stored task is cancelled when ``async_will_remove_from_hass`` is called.
3. Cancellation does not raise or propagate outside the entity.
4. No inverter/battery write can occur after the entity is unloaded.
5. A completed task is NOT cancelled again (``cancel()`` is a no-op on done tasks).
6. Calling ``_cancel_update_task`` when ``_update_task`` is ``None`` is safe.
7. ``_handle_coordinator_update`` cancels the *previous* task before creating a
   new one, so at most one task is in-flight at any time.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.coordinator_data import CoordinatorData
from custom_components.hsem.custom_sensors.working_mode_sensor import (
    HSEMWorkingModeSensor,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.degraded_mode import DegradedMode
from custom_components.hsem.utils.inverter_verify import CycleApplySummary

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config_entry() -> MagicMock:
    """Minimal mock config entry sufficient for HSEMWorkingModeSensor."""
    cfg = MagicMock()
    cfg.entry_id = "test_entry_id_p0_17"
    cfg.options = {}
    cfg.data = {}
    return cfg


def _make_coordinator() -> MagicMock:
    """Coordinator mock whose ``data`` is None by default."""
    coord = MagicMock()
    coord.data = None
    coord.last_update_success = True
    return coord


def _make_sensor() -> HSEMWorkingModeSensor:
    """Build a sensor instance with mocked coordinator and hass."""
    cfg = _make_config_entry()
    coord = _make_coordinator()

    sensor = HSEMWorkingModeSensor(cfg, coord)

    # Minimal hass mock — ``async_create_task`` returns a real asyncio.Task so
    # that cancellation tests work properly.
    hass = MagicMock()

    def _fake_create_task(coro, *, name=None):
        loop = asyncio.get_event_loop()
        return loop.create_task(coro, name=name)

    hass.async_create_task = MagicMock(side_effect=_fake_create_task)
    sensor.hass = hass
    return sensor


def _make_minimal_coordinator_data(
    recommendation: str = "batteries_wait_mode",
) -> CoordinatorData:
    """Return a minimal real ``CoordinatorData`` that reaches the write path.

    ``cfg.read_only=False`` and ``DegradedMode.OK`` so
    ``_async_apply_hardware_writes`` proceeds past its safety gates and into
    the (patched) applier calls, exercising the real ``_write_phase_active``
    bookkeeping around them.
    """
    cfg = SensorConfig()
    cfg.read_only = False

    live = LiveState()
    live._degraded_mode = DegradedMode.OK
    live.export_electricity_price = 0.20

    now = datetime.now(UTC)
    rec = HourlyRecommendation.__new__(HourlyRecommendation)
    object.__setattr__(rec, "start", now)
    object.__setattr__(rec, "end", now + timedelta(minutes=15))
    object.__setattr__(rec, "recommendation", recommendation)
    object.__setattr__(rec, "batteries_charged_kwh", 0.0)
    object.__setattr__(rec, "batteries_discharged_kwh", 0.0)
    object.__setattr__(rec, "grid_export_kwh", 0.0)

    return CoordinatorData(cfg=cfg, live=live, hourly_recommendation=rec)


# ---------------------------------------------------------------------------
# Task lifecycle tests
# ---------------------------------------------------------------------------


class TestUpdateTaskTracking:
    """Background task is stored after _handle_coordinator_update."""

    def test_update_task_is_none_initially(self) -> None:
        """``_update_task`` must be ``None`` before any coordinator update."""
        sensor = _make_sensor()
        assert sensor._update_task is None

    @pytest.mark.asyncio
    async def test_update_task_stored_after_coordinator_update(self) -> None:
        """``_update_task`` is populated after ``_handle_coordinator_update``."""
        sensor = _make_sensor()

        with patch.object(
            sensor,
            "_async_on_coordinator_update",
            new_callable=AsyncMock,
        ):
            sensor._handle_coordinator_update()

        assert sensor._update_task is not None

    @pytest.mark.asyncio
    async def test_update_task_is_asyncio_task(self) -> None:
        """The stored task must be an ``asyncio.Task`` instance."""
        sensor = _make_sensor()

        with patch.object(
            sensor,
            "_async_on_coordinator_update",
            new_callable=AsyncMock,
        ):
            sensor._handle_coordinator_update()

        assert isinstance(sensor._update_task, asyncio.Task)

        # Clean up
        await asyncio.gather(sensor._update_task, return_exceptions=True)


class TestTaskCancellationOnUnload:
    """Task is cancelled cleanly when the entity is unloaded."""

    @pytest.mark.asyncio
    async def test_done_callback_ignores_cancelled_task(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A cancelled task must not be logged as an unhandled exception.

        Regression for issue #736: ``task.exception()`` raises
        ``asyncio.CancelledError`` when the task was cancelled.  The done
        callback must detect this and return silently.
        """
        sensor = _make_sensor()
        event = asyncio.Event()

        async def _hanging_coro():
            await event.wait()

        task = asyncio.get_event_loop().create_task(_hanging_coro())
        task.add_done_callback(sensor._on_update_task_done)

        # Let the task start and block.
        await asyncio.sleep(0)

        task.cancel()

        # Must not raise through the done callback.
        await asyncio.gather(task, return_exceptions=True)

        assert task.cancelled()
        assert "Unhandled exception" not in caplog.text

    @pytest.mark.asyncio
    async def test_unload_cancels_pending_task(self) -> None:
        """A pending task must be cancelled on ``async_will_remove_from_hass``."""
        sensor = _make_sensor()

        # Create a coroutine that never returns so the task stays pending.
        event = asyncio.Event()

        async def _hanging_coro():
            await event.wait()  # Blocks until set — simulates in-flight work.

        sensor._update_task = asyncio.get_event_loop().create_task(_hanging_coro())

        # Yield once so the task can start and reach the ``await`` inside.
        await asyncio.sleep(0)

        await sensor.async_will_remove_from_hass()

        # Yield again so the event loop can transition the task to cancelled.
        await asyncio.sleep(0)

        assert sensor._update_task.cancelled()

    @pytest.mark.asyncio
    async def test_unload_does_not_raise_on_cancellation(self) -> None:
        """``async_will_remove_from_hass`` must not raise even if task is pending."""
        sensor = _make_sensor()

        event = asyncio.Event()

        async def _hanging_coro():
            await event.wait()

        sensor._update_task = asyncio.get_event_loop().create_task(_hanging_coro())

        # Must complete without raising any exception.
        await sensor.async_will_remove_from_hass()

    @pytest.mark.asyncio
    async def test_completed_task_not_cancelled_again(self) -> None:
        """Unload must not attempt to cancel an already-completed task."""
        sensor = _make_sensor()

        async def _quick_coro():
            return None

        task = asyncio.get_event_loop().create_task(_quick_coro())
        await task  # Ensure the task finishes before unload.
        sensor._update_task = task

        # cancel() on a done task is a no-op and must not raise.
        await sensor.async_will_remove_from_hass()

        # Task state must still be done, not cancelled.
        assert task.done()
        assert not task.cancelled()

    @pytest.mark.asyncio
    async def test_cancel_task_when_none_is_safe(self) -> None:
        """Calling ``_cancel_update_task`` with no stored task must be a no-op."""
        sensor = _make_sensor()
        assert sensor._update_task is None

        # Must not raise.
        sensor._cancel_update_task()

    @pytest.mark.asyncio
    async def test_unload_when_no_task_is_safe(self) -> None:
        """``async_will_remove_from_hass`` with no stored task must not raise."""
        sensor = _make_sensor()

        # Must complete without error.
        await sensor.async_will_remove_from_hass()


class TestNoWriteAfterUnload:
    """No inverter/battery write can occur after the entity is unloaded."""

    @pytest.mark.asyncio
    async def test_no_hardware_write_after_unload(self) -> None:
        """Hardware-write helpers must NOT be called after the entity is unloaded.

        Scenario:
        1. A coordinator update fires, creating a background task.
        2. The entity is unloaded immediately (``async_will_remove_from_hass``).
        3. The task is cancelled before it can execute the hardware-write path.
        """
        sensor = _make_sensor()
        write_called = False

        async def _spy_write(data):
            nonlocal write_called
            write_called = True

        sensor._async_apply_hardware_writes = _spy_write  # type: ignore[method-assign]  # test monkey-patch

        # Create a task that yields control once, giving us the window to cancel.
        event = asyncio.Event()

        async def _slow_update():
            await event.wait()  # Yields; allows cancellation before write.
            await sensor._async_apply_hardware_writes(None)

        with patch.object(
            sensor,
            "_async_on_coordinator_update",
            side_effect=_slow_update,
        ):
            sensor._handle_coordinator_update()

        # Unload immediately — must cancel before the write executes.
        await sensor.async_will_remove_from_hass()

        # Give the event loop a chance to run cancelled callbacks.
        await asyncio.sleep(0)

        assert not write_called, (
            "Hardware write was called after entity unload — stale task not cancelled."
        )

    @pytest.mark.asyncio
    async def test_cancelled_error_propagates_out_of_update_coro(self) -> None:
        """``CancelledError`` must propagate from ``_async_on_coordinator_update``.

        asyncio requires that ``CancelledError`` is re-raised so the task
        machinery can correctly transition the task to the cancelled state.
        """
        sensor = _make_sensor()
        event = asyncio.Event()

        async def _hanging_apply(_data):
            await event.wait()

        sensor._async_apply_hardware_writes = _hanging_apply  # type: ignore[method-assign]  # test monkey-patch
        sensor.coordinator.data = MagicMock()

        task = asyncio.get_event_loop().create_task(
            sensor._async_on_coordinator_update()
        )

        # Allow the task to start and block inside _async_apply_hardware_writes.
        await asyncio.sleep(0)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestSingleTaskInFlight:
    """At most one update task is in-flight at a time."""

    @pytest.mark.asyncio
    async def test_previous_task_cancelled_on_new_coordinator_update(self) -> None:
        """A second coordinator push must cancel the first still-pending task."""
        sensor = _make_sensor()

        event = asyncio.Event()

        async def _hanging_coro():
            await event.wait()

        # Simulate first coordinator push — create a slow task.
        first_task = asyncio.get_event_loop().create_task(_hanging_coro())
        sensor._update_task = first_task

        # Yield so the first task can start and block at ``await event.wait()``.
        await asyncio.sleep(0)

        # Simulate second coordinator push via _handle_coordinator_update.
        with patch.object(
            sensor,
            "_async_on_coordinator_update",
            new_callable=AsyncMock,
        ):
            sensor._handle_coordinator_update()

        # Allow the event loop to process the cancellation and schedule the new task.
        await asyncio.sleep(0)

        # First task must now be cancelled; new task is in-flight.
        assert first_task.cancelled()
        assert sensor._update_task is not first_task
        assert sensor._update_task is not None

        # Clean up the new task.
        if sensor._update_task and not sensor._update_task.done():
            sensor._update_task.cancel()
        await asyncio.gather(sensor._update_task, return_exceptions=True)


class TestWritePhaseCoalescing:
    """A routine coordinator push must not cancel an in-flight write sequence
    once it has entered its write phase (issue #951).

    Acceptance criteria (issue #951)
    ---------------------------------
    1. ``_write_phase_active`` is True only while
       ``_async_apply_hardware_writes`` is running.
    2. A coordinator push arriving while ``_write_phase_active`` is True does
       not cancel or replace ``_update_task`` — it is coalesced instead.
    3. Once the in-flight write sequence completes, a coalesced push starts
       exactly one follow-up task so the newer state is applied rather than
       dropped.
    4. Entity unload (``async_will_remove_from_hass``) still cancels
       immediately, even mid-write-phase — that safety path is unweakened.
    """

    @staticmethod
    def _patch_battery_settings(side_effect):
        """Patch the battery-settings applier call at its working_mode_sensor import site."""
        return patch(
            "custom_components.hsem.custom_sensors.working_mode_sensor."
            "async_apply_battery_settings",
            new_callable=AsyncMock,
            side_effect=side_effect,
        )

    @staticmethod
    def _patch_inverter_power_control():
        """Patch the inverter power-control applier call to return instantly."""
        return patch(
            "custom_components.hsem.custom_sensors.working_mode_sensor."
            "async_apply_inverter_power_control",
            new_callable=AsyncMock,
            return_value=CycleApplySummary(),
        )

    @pytest.mark.asyncio
    async def test_write_phase_active_during_hardware_writes(self) -> None:
        """``_write_phase_active`` is True only while writes are in flight."""
        sensor = _make_sensor()
        sensor.async_write_ha_state = MagicMock()  # type: ignore[method-assign,misc]  # avoid unrelated HA state-write plumbing
        sensor.coordinator.data = _make_minimal_coordinator_data()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def _slow_battery_write(*args, **kwargs):
            entered.set()
            await release.wait()
            return CycleApplySummary()

        assert sensor._write_phase_active is False

        with (
            self._patch_inverter_power_control(),
            self._patch_battery_settings(_slow_battery_write),
        ):
            sensor._handle_coordinator_update()
            await asyncio.wait_for(entered.wait(), timeout=1.0)
            assert sensor._write_phase_active is True
            task = sensor._update_task
            assert task is not None

            release.set()
            await asyncio.wait_for(task, timeout=1.0)

        assert sensor._write_phase_active is False

    @pytest.mark.asyncio
    async def test_coordinator_push_mid_write_does_not_cancel_task(self) -> None:
        """A push arriving mid-write-phase must not cancel the running task."""
        sensor = _make_sensor()
        sensor.async_write_ha_state = MagicMock()  # type: ignore[method-assign,misc]  # avoid unrelated HA state-write plumbing
        sensor.coordinator.data = _make_minimal_coordinator_data()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def _slow_battery_write(*args, **kwargs):
            entered.set()
            await release.wait()
            return CycleApplySummary()

        with (
            self._patch_inverter_power_control(),
            self._patch_battery_settings(_slow_battery_write),
        ):
            sensor._handle_coordinator_update()
            await asyncio.wait_for(entered.wait(), timeout=1.0)
            first_task = sensor._update_task
            assert first_task is not None

            # Simulate a routine replan push (e.g. the 10s live-power tick)
            # arriving while the write sequence is still in flight.
            sensor._handle_coordinator_update()

            assert not first_task.cancelled()
            assert not first_task.done()
            assert sensor._coordinator_update_pending is True
            # No replacement task was created — the push was coalesced.
            assert sensor._update_task is first_task

            release.set()
            await asyncio.wait_for(first_task, timeout=1.0)
            assert not first_task.cancelled()

            # Let the coalesced follow-up task (scheduled from the done
            # callback) finish so it doesn't leak into the next test.
            await asyncio.sleep(0)
            if (
                sensor._update_task is not first_task
                and sensor._update_task is not None
            ):
                await asyncio.wait_for(sensor._update_task, timeout=1.0)

    @pytest.mark.asyncio
    async def test_pending_push_starts_follow_up_task_after_completion(self) -> None:
        """A coalesced push starts exactly one follow-up task once writes finish."""
        sensor = _make_sensor()
        sensor.async_write_ha_state = MagicMock()  # type: ignore[method-assign,misc]  # avoid unrelated HA state-write plumbing
        sensor.coordinator.data = _make_minimal_coordinator_data()
        entered = asyncio.Event()
        release = asyncio.Event()
        call_count = 0

        async def _battery_write(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                entered.set()
                await release.wait()
            # The follow-up (second) call returns immediately.
            return CycleApplySummary()

        with (
            self._patch_inverter_power_control(),
            self._patch_battery_settings(_battery_write),
        ):
            sensor._handle_coordinator_update()
            await asyncio.wait_for(entered.wait(), timeout=1.0)
            first_task = sensor._update_task
            assert first_task is not None

            # Coalesced — no new task yet.
            sensor._handle_coordinator_update()
            assert sensor._update_task is first_task

            release.set()
            await asyncio.wait_for(first_task, timeout=1.0)
            # Let the done callback run and schedule the follow-up task.
            await asyncio.sleep(0)

            follow_up_task = sensor._update_task
            assert follow_up_task is not first_task
            assert follow_up_task is not None
            await asyncio.wait_for(follow_up_task, timeout=1.0)

        assert call_count == 2
        assert sensor._coordinator_update_pending is False

    @pytest.mark.asyncio
    async def test_unload_still_cancels_immediately_mid_write_phase(self) -> None:
        """Entity unload must still hard-cancel even while writing (issue #951).

        The fix must not weaken ``async_will_remove_from_hass`` — that path
        calls ``_cancel_update_task()`` directly and unconditionally,
        regardless of ``_write_phase_active``.
        """
        sensor = _make_sensor()
        sensor.async_write_ha_state = MagicMock()  # type: ignore[method-assign,misc]  # avoid unrelated HA state-write plumbing
        sensor.coordinator.data = _make_minimal_coordinator_data()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def _slow_battery_write(*args, **kwargs):
            entered.set()
            await release.wait()
            return CycleApplySummary()

        with (
            self._patch_inverter_power_control(),
            self._patch_battery_settings(_slow_battery_write),
        ):
            sensor._handle_coordinator_update()
            await asyncio.wait_for(entered.wait(), timeout=1.0)
            assert sensor._write_phase_active is True

            await sensor.async_will_remove_from_hass()
            await asyncio.sleep(0)

            task = sensor._update_task
            assert task is not None
            assert task.cancelled()
