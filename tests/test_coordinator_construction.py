"""Tests for the real ``HSEMDataUpdateCoordinator.__init__``.

Uses :func:`tests.coordinator_fixtures.make_real_coordinator` so the initial
per-cycle state, tracker sizing, and the HA fallback update hook are exercised
as they are in production rather than on an ``object.__new__`` stub.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.hsem.const import FORCE_MODE_AUTO
from custom_components.hsem.coordinator_data import CoordinatorData
from custom_components.hsem.models.sensor_config import SensorConfig
from tests.coordinator_fixtures import make_real_coordinator as _make_coordinator


class TestCoordinatorInit:
    """A new coordinator is idle, unscheduled, and has fresh trackers."""

    def test_builtin_polling_is_disabled_and_nothing_is_scheduled(self) -> None:
        """HSEM owns its timers, so HA's built-in interval stays off."""
        coordinator = _make_coordinator()

        assert coordinator.name == "HSEM"
        assert coordinator.update_interval is None
        assert coordinator.data is None
        assert isinstance(coordinator._update_lock, asyncio.Lock)
        assert not coordinator._update_lock.locked()
        assert coordinator._update_generation == 0
        assert coordinator._event_update_pending is False
        assert coordinator._interval_timer_unsub is None
        assert coordinator._hourly_timer_unsub is None
        assert coordinator._live_power_timer_unsub is None
        assert coordinator._timer_interval is None
        assert coordinator._options_update_task is None
        assert coordinator._options_update_debounce_task is None
        assert coordinator._ocpp_event_task is None
        assert coordinator._ocpp_event_debounce_task is None

    def test_sensor_config_is_built_from_the_entry(self) -> None:
        """Options on the entry are reflected in the initial sensor config."""
        coordinator = _make_coordinator(
            {"hsem_read_only": True, "hsem_update_interval": 7}
        )

        assert isinstance(coordinator._cfg, SensorConfig)
        assert coordinator._cfg.read_only is True
        assert coordinator._cfg.update_interval == 7

    def test_no_plan_or_live_state_before_the_first_cycle(self) -> None:
        """Planner, live, and replan-tracking state start empty."""
        coordinator = _make_coordinator()

        assert coordinator._live is None
        assert coordinator._snapshot is None
        assert coordinator._hourly_recommendations == []
        assert coordinator._hourly_recommendation is None
        assert coordinator._last_planner_input is None
        assert coordinator._last_planner_output is None
        assert coordinator._last_plan_force_mode == FORCE_MODE_AUTO
        assert coordinator._last_plan_ev_connected is False
        assert coordinator._last_plan_slot_start is None
        assert coordinator._previous_planner_winner_name is None
        assert coordinator._previous_planner_winner_score == pytest.approx(0.0)
        assert coordinator._live_power_replan_count == 0
        assert coordinator._override_expiry is None
        assert coordinator._ocpp_server is None
        assert coordinator._ocpp_second_server is None
        assert coordinator._ml_predictor is None

    def test_trackers_are_fresh_and_sized_for_long_horizons(self) -> None:
        """Forecast and prediction trackers retain 30 days of 15-min slots."""
        coordinator = _make_coordinator()
        other = _make_coordinator()

        assert coordinator._forecast_tracker._max_slots == 2880
        assert coordinator._prediction_tracker.max_records == 2880
        assert coordinator._daily_plan_last_accumulated is None
        assert coordinator._last_accumulation_ts is None
        # Every coordinator owns its trackers — nothing is shared.
        for attr in (
            "_forecast_tracker",
            "_prediction_tracker",
            "_daily_tracker",
            "_savings_tracker",
            "_financial_tracker",
            "_solar_corrector",
            "_dynamic_floor",
            "_capacity_learner",
            "_live_power_window",
            "_ev_delivered_energy_tracker",
            "_ev_second_delivered_energy_tracker",
        ):
            assert getattr(coordinator, attr) is not getattr(other, attr), attr
        assert (
            coordinator._ev_delivered_energy_tracker
            is not coordinator._ev_second_delivered_energy_tracker
        )


class TestAsyncUpdateDataFallback:
    """HA's fallback update hook delegates to the guarded update handler."""

    @pytest.mark.asyncio
    async def test_returns_empty_snapshot_before_first_cycle(self) -> None:
        """Without data yet, an empty ``CoordinatorData`` is returned."""
        coordinator = _make_coordinator()
        handler = AsyncMock()

        with patch.object(coordinator, "_async_handle_update", handler):
            result = await coordinator._async_update_data()

        handler.assert_awaited_once_with(None)
        assert isinstance(result, CoordinatorData)
        assert result.live is None

    @pytest.mark.asyncio
    async def test_returns_the_published_snapshot(self) -> None:
        """Once a cycle has published data, that same snapshot is returned."""
        coordinator = _make_coordinator()
        published = CoordinatorData()
        coordinator.data = published

        with patch.object(coordinator, "_async_handle_update", AsyncMock()):
            result = await coordinator._async_update_data()

        assert result is published
