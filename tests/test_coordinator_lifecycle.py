"""Tests for ``CoordinatorLifecycleMixin`` — setup, teardown, and timers.

``test_coordinator.py`` covers first-refresh ordering and the options-update
background task. These tests cover the rest of the lifecycle: tracker and
OCPP server startup (including failures), the full teardown sweep, the
debounce cancellation paths, and interval-timer registration.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.coordinator import HSEMDataUpdateCoordinator
from tests.coordinator_fixtures import make_real_coordinator

_MODULE = "custom_components.hsem.coordinator_lifecycle"
_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

_OCPP_OPTIONS: dict[str, Any] = {
    "hsem_ocpp_enabled": True,
    "hsem_ocpp_port": 9000,
    "hsem_ocpp_start_window_s": 60,
    "hsem_ocpp_stop_window_s": 180,
    "hsem_ocpp_second_enabled": True,
    "hsem_ocpp_second_port": 9001,
    "hsem_ev_second_planned_load_enabled": True,
}


def _coordinator(options: dict[str, Any] | None = None) -> HSEMDataUpdateCoordinator:
    """Return a real coordinator whose ``hass`` records task creation."""
    hass = MagicMock()
    hass.async_create_task = MagicMock(return_value=MagicMock())
    return make_real_coordinator(options, hass=hass)


def _pending_task() -> MagicMock:
    """Return a mock task that reports itself as still running."""
    task = MagicMock()
    task.done.return_value = False
    return task


class TestAsyncSetup:
    """Setup restores trackers, starts OCPP servers, and registers timers."""

    @pytest.mark.asyncio
    async def test_registers_timers_without_ocpp(self) -> None:
        """With OCPP disabled only the hourly and live-power timers start."""
        coordinator = _coordinator()
        hourly_unsub = MagicMock()
        interval_unsub = MagicMock()

        with (
            patch(
                f"{_MODULE}.async_track_time_change", return_value=hourly_unsub
            ) as track_time_change,
            patch(
                f"{_MODULE}.async_track_time_interval", return_value=interval_unsub
            ) as track_interval,
        ):
            await coordinator.async_setup()

        assert coordinator._ocpp_server is None
        assert coordinator._ocpp_second_server is None
        assert coordinator._hourly_timer_unsub is hourly_unsub
        assert coordinator._live_power_timer_unsub is interval_unsub
        assert track_time_change.call_args.kwargs == {
            "hour": "*",
            "minute": 0,
            "second": 10,
        }
        assert track_interval.call_args.args[2] == timedelta(seconds=10)

    @pytest.mark.asyncio
    async def test_tracker_initialisation_failures_are_logged(self) -> None:
        """A failing tracker restore never blocks coordinator setup."""
        coordinator = _coordinator()
        log = MagicMock()

        with (
            patch(
                f"{_MODULE}.init_prediction_tracker",
                AsyncMock(side_effect=OSError("no disk")),
            ),
            patch(
                f"{_MODULE}.init_financial_tracker",
                AsyncMock(side_effect=OSError("no disk")),
            ),
            patch(f"{_MODULE}.async_track_time_change"),
            patch(f"{_MODULE}.async_track_time_interval"),
            patch(f"{_MODULE}.async_log", log),
        ):
            await coordinator.async_setup()

        logged = [call.args[1] for call in log.call_args_list]
        assert "Failed to initialise prediction tracker: %s" in logged
        assert "Failed to initialise financial tracker: %s" in logged

    @pytest.mark.asyncio
    async def test_starts_both_ocpp_servers_on_their_own_ports(self) -> None:
        """Each EV gets its own OCPP server on its configured port."""
        coordinator = _coordinator(_OCPP_OPTIONS)
        servers = [MagicMock(start=AsyncMock()), MagicMock(start=AsyncMock())]

        with (
            patch(f"{_MODULE}.OCPPServer", side_effect=servers) as server_cls,
            patch(f"{_MODULE}.async_track_time_change"),
            patch(f"{_MODULE}.async_track_time_interval"),
        ):
            await coordinator.async_setup()

        assert coordinator._ocpp_server is servers[0]
        assert coordinator._ocpp_second_server is servers[1]
        ports = [call.kwargs["port"] for call in server_cls.call_args_list]
        assert ports == [9000, 9001]
        for server in servers:
            server.start.assert_awaited_once()
        assert server_cls.call_args_list[0].kwargs["on_significant_event"] == (
            coordinator.async_ocpp_event
        )

    @pytest.mark.asyncio
    async def test_second_server_needs_the_second_ev(self) -> None:
        """Without a second EV configured its server is not started."""
        coordinator = _coordinator(
            {**_OCPP_OPTIONS, "hsem_ev_second_planned_load_enabled": False}
        )

        with (
            patch(f"{_MODULE}.OCPPServer", return_value=MagicMock(start=AsyncMock())),
            patch(f"{_MODULE}.async_track_time_change"),
            patch(f"{_MODULE}.async_track_time_interval"),
        ):
            await coordinator.async_setup()

        assert coordinator._ocpp_server is not None
        assert coordinator._ocpp_second_server is None

    @pytest.mark.asyncio
    async def test_failed_ocpp_start_leaves_no_half_built_server(self) -> None:
        """A port that cannot be bound is logged and the handle cleared."""
        coordinator = _coordinator(_OCPP_OPTIONS)
        log = MagicMock()

        with (
            patch(
                f"{_MODULE}.OCPPServer",
                return_value=MagicMock(start=AsyncMock(side_effect=OSError("in use"))),
            ),
            patch(f"{_MODULE}.async_track_time_change"),
            patch(f"{_MODULE}.async_track_time_interval"),
            patch(f"{_MODULE}.async_log", log),
        ):
            await coordinator.async_setup()

        assert coordinator._ocpp_server is None
        assert coordinator._ocpp_second_server is None
        logged = [call.args[1] for call in log.call_args_list]
        assert "Failed to start OCPP server: %s" in logged
        assert "Failed to start second OCPP server: %s" in logged


class TestAsyncTeardown:
    """Teardown releases every timer, listener, server, and pending task."""

    @pytest.mark.asyncio
    async def test_releases_everything_it_owns(self) -> None:
        """All handles are invoked once and cleared."""
        coordinator = _coordinator()
        refresh_unsub = MagicMock()
        listener_unsub = MagicMock()
        midnight_unsub = MagicMock()
        coordinator._unsub_refresh = refresh_unsub
        coordinator._hourly_timer_unsub = MagicMock()
        coordinator._interval_timer_unsub = MagicMock()
        coordinator._live_power_timer_unsub = MagicMock()
        coordinator._listener_unsubs = [listener_unsub]
        coordinator._daily_tracker._midnight_unsub = midnight_unsub  # type: ignore[attr-defined]
        ocpp = MagicMock(stop=AsyncMock())
        ocpp_second = MagicMock(stop=AsyncMock())
        coordinator._ocpp_server = ocpp
        coordinator._ocpp_second_server = ocpp_second
        tasks = {
            "_options_update_task": _pending_task(),
            "_options_update_debounce_task": _pending_task(),
            "_ocpp_event_task": _pending_task(),
            "_ocpp_event_debounce_task": _pending_task(),
        }
        for name, task in tasks.items():
            setattr(coordinator, name, task)

        await coordinator.async_teardown()

        refresh_unsub.assert_called_once()
        listener_unsub.assert_called_once()
        midnight_unsub.assert_called_once()
        assert coordinator._listener_unsubs == []
        assert coordinator._daily_tracker._midnight_unsub is None  # type: ignore[attr-defined]
        ocpp.stop.assert_awaited_once()
        ocpp_second.stop.assert_awaited_once()
        assert coordinator._ocpp_server is None
        assert coordinator._ocpp_second_server is None
        for name, task in tasks.items():
            task.cancel.assert_called_once()
            assert getattr(coordinator, name) is None
        assert coordinator._live_power_source_signature is None

    @pytest.mark.asyncio
    async def test_completed_tasks_are_not_cancelled(self) -> None:
        """Already-finished background tasks are left alone."""
        coordinator = _coordinator()
        finished = MagicMock()
        finished.done.return_value = True
        coordinator._options_update_task = finished
        coordinator._ocpp_event_task = finished

        await coordinator.async_teardown()

        finished.cancel.assert_not_called()


class TestDebouncedRefreshes:
    """Options changes and OCPP events coalesce into one refresh."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("trigger", "attr", "task_name"),
        [
            pytest.param(
                "async_options_updated",
                "_options_update_debounce_task",
                "hsem_options_update_debounce",
                id="options",
            ),
            pytest.param(
                "async_ocpp_event",
                "_ocpp_event_debounce_task",
                "hsem_ocpp_event_debounce",
                id="ocpp_event",
            ),
        ],
    )
    async def test_new_trigger_supersedes_a_pending_debounce(
        self, trigger: str, attr: str, task_name: str
    ) -> None:
        """A second trigger cancels the first debounce and starts its own."""
        coordinator = _coordinator()
        pending = _pending_task()
        setattr(coordinator, attr, pending)

        await getattr(coordinator, trigger)()

        pending.cancel.assert_called_once()
        create_task = coordinator.hass.async_create_task
        assert create_task.call_args.kwargs["name"] == task_name  # type: ignore[attr-defined]
        assert create_task.call_args.kwargs["eager_start"] is False  # type: ignore[attr-defined]
        # The scheduled coroutine is never awaited in this test.
        create_task.call_args.args[0].close()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("method", "expected_log"),
        [
            pytest.param(
                "_async_options_update_debounced",
                "[coordinator] options-update debounce cancelled — "
                "superseded by a newer options change.",
                id="options",
            ),
            pytest.param(
                "_async_ocpp_event_debounced",
                "[coordinator] OCPP-event debounce cancelled — superseded by a "
                "newer OCPP event.",
                id="ocpp_event",
            ),
        ],
    )
    async def test_cancelled_debounce_schedules_nothing(
        self, method: str, expected_log: str
    ) -> None:
        """Cancelling during the debounce window is not an error."""
        coordinator = _coordinator()
        log = MagicMock()

        with patch(f"{_MODULE}.async_log", log):
            task = asyncio.ensure_future(getattr(coordinator, method)())
            await asyncio.sleep(0)
            task.cancel()
            await task

        log.assert_called_once_with("debug", expected_log)
        coordinator.hass.async_create_task.assert_not_called()  # type: ignore[attr-defined]


class TestUpdateInterval:
    """The polling cadence follows the configured interval."""

    @pytest.mark.asyncio
    async def test_registers_the_configured_interval_once(self) -> None:
        """The timer is registered on change and reused when unchanged."""
        coordinator = _coordinator({"hsem_update_interval": 3})
        unsub = MagicMock()

        with (
            patch(
                f"{_MODULE}.async_track_time_interval", return_value=unsub
            ) as track_interval,
            patch(f"{_MODULE}.hsem_now", return_value=_NOW),
        ):
            await coordinator._set_update_interval()
            await coordinator._set_update_interval()

        track_interval.assert_called_once()
        assert track_interval.call_args.args[2] == timedelta(minutes=3)
        assert coordinator._timer_interval == timedelta(minutes=3)
        assert coordinator._next_update == (_NOW + timedelta(minutes=3)).isoformat()

    @pytest.mark.asyncio
    async def test_override_replaces_the_previous_timer(self) -> None:
        """A forced interval cancels the old timer before registering anew."""
        coordinator = _coordinator({"hsem_update_interval": 5})
        first_unsub = MagicMock()
        second_unsub = MagicMock()

        with (
            patch(
                f"{_MODULE}.async_track_time_interval",
                side_effect=[first_unsub, second_unsub],
            ),
            patch(f"{_MODULE}.hsem_now", return_value=_NOW),
        ):
            await coordinator._set_update_interval()
            await coordinator._set_update_interval(override_minutes=1)

        first_unsub.assert_called_once()
        assert coordinator._interval_timer_unsub is second_unsub
        assert coordinator._timer_interval == timedelta(minutes=1)
