"""Edge-case tests for the Huawei grid-charge transition latch.

The latch suppresses raw battery-power feedback while a verified downward cap
change settles, so every way out of it matters: a cancelled deadline task, a
task cleared outside the event loop, a transition superseded before its
deadline fires, a stale timeout belonging to an earlier slot, and a cap that
moves back up within the same slot.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.custom_sensors.phase_charge_transition import (
    PrimaryGridChargeTransition,
)
from custom_components.hsem.models.live_state import LiveState
from tests.test_phase_charge_transition_safety import (
    _config,
    _make_sensor,
    _rec,
    _verified_summary,
)

_MODULE = "custom_components.hsem.custom_sensors.phase_charge_transition"
_NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


def _live(limit_w: float = 8900.0) -> LiveState:
    """Return a live snapshot reporting *limit_w* as the current cap."""
    live = LiveState()
    live.huawei_batteries_grid_charge_max_power_w = limit_w
    return live


def _transition(*, expires_at_monotonic: float = 1e12) -> PrimaryGridChargeTransition:
    """Return a transition for the fixed slot with the given deadline."""
    return PrimaryGridChargeTransition(
        previous_limit_w=8900.0,
        target_limit_w=5900.0,
        slot_start=_NOW,
        slot_end=_NOW + timedelta(hours=1),
        expires_at_monotonic=expires_at_monotonic,
    )


class TestClearOutsideTheEventLoop:
    """Clearing the latch synchronously must still cancel the deadline task."""

    def test_a_pending_task_is_cancelled_without_a_running_loop(self) -> None:
        """A config reload happens off the loop but must not leak the task."""
        sensor = _make_sensor()
        task = MagicMock()
        task.done.return_value = False
        sensor._primary_grid_charge_deadline_task = task
        sensor._primary_grid_charge_transition = _transition()

        sensor._clear_primary_grid_charge_transition()

        task.cancel.assert_called_once()
        assert sensor._primary_grid_charge_deadline_task is None
        assert sensor._primary_grid_charge_transition is None

    def test_a_finished_task_is_not_cancelled(self) -> None:
        """A task that already ran needs no cancellation."""
        sensor = _make_sensor()
        task = MagicMock()
        task.done.return_value = True
        sensor._primary_grid_charge_deadline_task = task

        sensor._clear_primary_grid_charge_transition()

        task.cancel.assert_not_called()


class TestScheduleWithoutALoop:
    """Arming the deadline is a no-op outside Home Assistant's loop."""

    def test_no_task_is_created_in_a_synchronous_context(self) -> None:
        """A pure unit-test call must not try to schedule anything."""
        sensor = _make_sensor()
        sensor._transition_deadline_tasks_enabled = True

        sensor._schedule_primary_grid_charge_deadline(_transition())

        assert sensor._primary_grid_charge_deadline_task is None
        sensor.hass.async_create_task.assert_not_called()

    def test_a_previous_pending_task_is_cancelled_first(self) -> None:
        """Re-arming replaces the old deadline rather than racing it."""
        sensor = _make_sensor()
        sensor._transition_deadline_tasks_enabled = True
        existing = MagicMock()
        existing.done.return_value = False
        sensor._primary_grid_charge_deadline_task = existing

        sensor._schedule_primary_grid_charge_deadline(_transition())

        existing.cancel.assert_called_once()


class TestDeadlineTaskBody:
    """The deadline task writes only for the transition it was armed for."""

    @pytest.mark.asyncio
    async def test_a_superseded_transition_writes_nothing(self) -> None:
        """A transition cleared before the deadline must not force a write."""
        sensor = _make_sensor()
        transition = _transition(expires_at_monotonic=0.0)
        sensor._primary_grid_charge_transition = None
        apply = AsyncMock()
        sensor._async_apply_hardware_writes = apply  # type: ignore[method-assign]  # test spy

        await sensor._async_primary_grid_charge_deadline(transition)

        apply.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cancellation_propagates(self) -> None:
        """An unloading entity cancels the task instead of logging an error."""
        sensor = _make_sensor()
        transition = _transition()

        with (
            patch(
                f"{_MODULE}.asyncio.sleep",
                AsyncMock(side_effect=asyncio.CancelledError),
            ),
            patch(f"{_MODULE}._LOGGER") as logger,
            pytest.raises(asyncio.CancelledError),
        ):
            await sensor._async_primary_grid_charge_deadline(transition)

        logger.exception.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_failing_write_is_logged_not_raised(self) -> None:
        """A background safety task must never surface as an unhandled error."""
        sensor = _make_sensor()
        transition = _transition(expires_at_monotonic=0.0)
        sensor._transition_deadline_tasks_enabled = True
        sensor._primary_grid_charge_transition = transition
        sensor.coordinator.data = MagicMock()
        sensor._async_apply_hardware_writes = AsyncMock(  # type: ignore[method-assign]  # force failure
            side_effect=RuntimeError("inverter offline")
        )

        with patch(f"{_MODULE}._LOGGER") as logger:
            await sensor._async_primary_grid_charge_deadline(transition)

        logger.exception.assert_called_once()


class TestStaleTimeoutLatch:
    """A timeout belongs to one slot only."""

    def test_a_timeout_from_an_earlier_slot_is_dropped(self) -> None:
        """A new slot starts with full feedback, not an inherited timeout."""
        sensor = _make_sensor()
        # Timed out in the previous slot, with the transition already cleared.
        sensor._primary_grid_charge_transition = None
        sensor._primary_grid_charge_timed_out_slot = (
            _NOW - timedelta(hours=1),
            _NOW,
        )

        reference_w, timed_out = sensor._primary_grid_charge_transition_status(
            _config(), _live(), _rec(start=_NOW)
        )

        assert (reference_w, timed_out) == (None, False)
        assert sensor._primary_grid_charge_timed_out_slot is None


class TestUpwardChangeWithinTheSlot:
    """A cap that goes back up ends the latch immediately."""

    def test_raising_the_cap_again_clears_the_transition(self) -> None:
        """Once the cap is back up there is no downward change to settle."""
        sensor = _make_sensor()
        cfg = _config()
        rec = _rec(start=_NOW)
        live = _live()
        sensor._record_verified_primary_grid_charge_transition(
            cfg, live, rec, 5900.0, _verified_summary(5900.0)
        )
        assert sensor._primary_grid_charge_transition is not None

        # Same slot, but the new verified target is back at the live cap.
        sensor._record_verified_primary_grid_charge_transition(
            cfg, live, rec, 8900.0, _verified_summary(8900.0)
        )

        assert sensor._primary_grid_charge_transition is None
