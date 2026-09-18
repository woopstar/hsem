"""Tests for the working-mode sensor's attributes and update entry points.

This is the entity that performs hardware writes, so the paths that decide
*not* to write matter as much as the ones that do: a partially initialised
snapshot, missing input entities, and a coordinator that has published nothing
yet. The sensor also surfaces those conditions as attributes so the user can
see why HSEM is holding off.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.coordinator_data import CoordinatorData
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from tests.test_working_mode_task_lifecycle import _make_sensor


def _coordinator_mock(sensor: Any) -> MagicMock:
    """Return the sensor's coordinator typed as the mock the factory installed."""
    return cast(MagicMock, sensor.coordinator)


_MODULE = "custom_components.hsem.custom_sensors.working_mode_sensor"


def _live(**kwargs: Any) -> LiveState:
    """Return a live snapshot with *kwargs* applied."""
    live = LiveState()
    for key, value in kwargs.items():
        setattr(live, key, value)
    return live


class TestAttributesBeforeReady:
    """The attributes explain why no plan is being applied yet."""

    def test_snapshot_without_config_reports_waiting(self) -> None:
        """A partially initialised snapshot is a wait, not an error."""
        sensor = _make_sensor()
        sensor.coordinator.data = CoordinatorData(cfg=None, live=_live())

        attributes = sensor.extra_state_attributes

        assert attributes["status"] == "wait"
        assert "Waiting for coordinator configuration" in attributes["description"]
        assert attributes["unique_id"] == sensor.unique_id

    def test_missing_input_entities_are_listed(self) -> None:
        """Missing sources are named so the user can fix the configuration."""
        sensor = _make_sensor()
        sensor.coordinator.data = CoordinatorData(
            cfg=SensorConfig(),
            live=_live(
                missing_entities=True,
                missing_entities_list=["batteries_state_of_capacity"],
            ),
            last_updated="2026-06-01T12:00:00+00:00",
            next_update="2026-06-01T12:05:00+00:00",
        )

        attributes = sensor.extra_state_attributes

        assert attributes["status"] == "error"
        assert attributes["missing_input_entities_list"] == [
            "batteries_state_of_capacity"
        ]
        assert attributes["last_updated"] == "2026-06-01T12:00:00+00:00"


class TestHardwareWriteGuards:
    """Writes are skipped whenever the snapshot cannot be acted on."""

    @pytest.mark.asyncio
    async def test_no_snapshot_writes_nothing(self) -> None:
        """Before the first cycle there is nothing to apply."""
        sensor = _make_sensor()

        await sensor._async_apply_hardware_writes(None)

        assert sensor._write_phase_active is False

    @pytest.mark.asyncio
    async def test_snapshot_without_config_or_live_clears_the_transition(self) -> None:
        """An unusable snapshot clears any pending grid-charge transition."""
        sensor = _make_sensor()
        clear = MagicMock()
        sensor._clear_primary_grid_charge_transition = clear  # type: ignore[method-assign]  # test spy

        await sensor._async_apply_hardware_writes(CoordinatorData(cfg=None, live=None))

        clear.assert_called_once()
        # The write phase flag is always released.
        assert sensor._write_phase_active is False


class TestAddedToHass:
    """Joining HA applies the already-published plan immediately."""

    @pytest.mark.asyncio
    async def test_existing_snapshot_is_applied_on_add(self) -> None:
        """A coordinator that already has data is not left stale."""
        sensor = _make_sensor()
        data = CoordinatorData(cfg=SensorConfig(), live=_live())
        sensor.coordinator.data = data
        apply = AsyncMock()
        sensor._async_apply_hardware_writes = apply  # type: ignore[method-assign]  # test spy

        with patch(
            "homeassistant.helpers.update_coordinator.CoordinatorEntity"
            ".async_added_to_hass",
            AsyncMock(),
        ):
            await sensor.async_added_to_hass()

        apply.assert_awaited_once_with(data)
        assert sensor._transition_deadline_tasks_enabled is True

    @pytest.mark.asyncio
    async def test_without_a_snapshot_nothing_is_applied(self) -> None:
        """A cold coordinator leaves the hardware untouched."""
        sensor = _make_sensor()
        sensor.coordinator.data = None  # type: ignore[assignment]  # cold coordinator
        apply = AsyncMock()
        sensor._async_apply_hardware_writes = apply  # type: ignore[method-assign]  # test spy

        with patch(
            "homeassistant.helpers.update_coordinator.CoordinatorEntity"
            ".async_added_to_hass",
            AsyncMock(),
        ):
            await sensor.async_added_to_hass()

        apply.assert_not_awaited()


class TestCoordinatorUpdateTask:
    """The background write task reports failures and drains coalesced pushes."""

    @pytest.mark.asyncio
    async def test_a_missing_snapshot_ends_the_task_quietly(self) -> None:
        """No data means no write and no state publication."""
        sensor = _make_sensor()
        sensor.coordinator.data = None  # type: ignore[assignment]  # cold coordinator
        apply = AsyncMock()
        sensor._async_apply_hardware_writes = apply  # type: ignore[method-assign]  # test spy

        await sensor._async_on_coordinator_update()

        apply.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failing_write_is_logged_not_raised(self) -> None:
        """A hardware-write failure must not take the entity down."""
        sensor = _make_sensor()
        sensor.coordinator.data = CoordinatorData(cfg=SensorConfig(), live=_live())
        sensor._async_apply_hardware_writes = AsyncMock(  # type: ignore[method-assign]  # force failure
            side_effect=RuntimeError("inverter offline")
        )

        with patch(f"{_MODULE}._LOGGER") as logger:
            await sensor._async_on_coordinator_update()

        logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancellation_propagates(self) -> None:
        """An unloading entity cancels cleanly rather than logging an error."""
        sensor = _make_sensor()
        sensor.coordinator.data = CoordinatorData(cfg=SensorConfig(), live=_live())
        sensor._async_apply_hardware_writes = AsyncMock(  # type: ignore[method-assign]  # force cancel
            side_effect=asyncio.CancelledError
        )

        with pytest.raises(asyncio.CancelledError):
            await sensor._async_on_coordinator_update()

    def test_a_task_exception_is_logged(self) -> None:
        """An unhandled exception in the task is surfaced in the log."""
        sensor = _make_sensor()
        task = MagicMock()
        task.cancelled.return_value = False
        task.exception.return_value = RuntimeError("boom")
        sensor._coordinator_update_pending = False

        with patch(f"{_MODULE}._LOGGER") as logger:
            sensor._on_update_task_done(task)

        logger.error.assert_called_once()

    def test_a_coalesced_push_starts_one_follow_up_task(self) -> None:
        """A push received mid-write is applied instead of dropped (issue #951)."""
        sensor = _make_sensor()
        task = MagicMock()
        task.cancelled.return_value = False
        task.exception.return_value = None
        sensor._coordinator_update_pending = True
        start = MagicMock()
        sensor._start_update_task = start  # type: ignore[method-assign, assignment]  # test spy

        sensor._on_update_task_done(task)

        start.assert_called_once()
        assert sensor._coordinator_update_pending is False

    def test_a_cancelled_task_is_ignored(self) -> None:
        """A cancelled task neither logs nor reschedules."""
        sensor = _make_sensor()
        task = MagicMock()
        task.cancelled.return_value = True
        start = MagicMock()
        sensor._start_update_task = start  # type: ignore[method-assign, assignment]  # test spy

        with patch(f"{_MODULE}._LOGGER") as logger:
            sensor._on_update_task_done(task)

        logger.error.assert_not_called()
        start.assert_not_called()


class TestDelegatedEntryPoints:
    """Manual update and options changes are handled by the coordinator."""

    @pytest.mark.asyncio
    async def test_manual_update_requests_a_refresh(self) -> None:
        """A service-call update asks the coordinator to refresh."""
        sensor = _make_sensor()
        _coordinator_mock(sensor).async_request_refresh = AsyncMock()

        await sensor.async_update()

        _coordinator_mock(sensor).async_request_refresh.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_options_update_is_delegated_to_the_coordinator(self) -> None:
        """All entities benefit from one coordinator-level options handler."""
        sensor = _make_sensor()
        _coordinator_mock(sensor).async_options_updated = AsyncMock()

        await sensor.async_options_updated(MagicMock())

        _coordinator_mock(sensor).async_options_updated.assert_awaited_once()
