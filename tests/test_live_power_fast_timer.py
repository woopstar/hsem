"""Tests for the live-power fast timer and its HA reads (issue #797).

``test_live_power_coordinator.py`` covers the pure materiality, direction,
and budget helpers. This module covers the surrounding I/O path: the
ten-second tick, the raw HA reads it performs, the EV-ambiguity fail-closed
rule for an inclusive house meter, and the retained-state resets.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.coordinator import HSEMDataUpdateCoordinator
from custom_components.hsem.coordinator_live_power import (
    LIVE_POWER_MISMATCH_DEBOUNCE_SECONDS,
    LIVE_POWER_REPLAN_MAX_CORRECTIONS_PER_SLOT,
)
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.utils.degraded_mode import DegradedMode
from custom_components.hsem.utils.live_power import LivePowerWindow
from tests.coordinator_fixtures import make_real_coordinator
from tests.test_live_power_coordinator import _estimate, _FakeState, _hass

_NOW = datetime(2026, 6, 1, 12, 0, 30, tzinfo=UTC)
_SLOT_START = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
_HOUSE = "sensor.house_power"
_SOLAR = "sensor.solar_power"
_EV_STATUS = "binary_sensor.ev_charging"
_EV_POWER = "sensor.ev_power"


def _coordinator(
    states: dict[str, _FakeState] | None = None,
    *,
    includes_ev: bool = False,
) -> HSEMDataUpdateCoordinator:
    """Return a real coordinator wired to a fake HA state machine."""
    coordinator = make_real_coordinator()
    coordinator.hass = _hass(states or {})  # type: ignore[assignment]  # test monkey-patch
    cfg = coordinator._cfg
    cfg.recommendation_interval_minutes = 15
    cfg.house_consumption_power = _HOUSE
    cfg.solar_production_power = _SOLAR
    cfg.house_power_includes_ev_charger_power = includes_ev
    cfg.ev.status_entity = _EV_STATUS
    cfg.ev.power_entity = _EV_POWER
    coordinator._live = LiveState()
    # One sample is enough to produce an estimate in these tests.
    coordinator._live_power_window = LivePowerWindow(
        window_seconds=60, minimum_samples=1, maximum_sample_age_seconds=60
    )
    return coordinator


class TestReadLivePowerBoolean:
    """Boolean timer reads never turn an unavailable entity into ``False``."""

    @pytest.mark.parametrize(
        ("state", "expected"),
        [("on", True), ("off", False), ("unknown", None), ("unavailable", None)],
    )
    def test_reads_state(self, state: str, expected: bool | None) -> None:
        """Valid states map to booleans; HA sentinel states map to ``None``."""
        coordinator = _coordinator({_EV_STATUS: _FakeState(state)})

        assert coordinator._read_live_power_boolean(_EV_STATUS) is expected

    def test_unconfigured_entity_is_none(self) -> None:
        """No entity configured → nothing to read."""
        assert _coordinator()._read_live_power_boolean(None) is None

    def test_unknown_entity_is_none(self) -> None:
        """An entity missing from the state machine is not ``False``."""
        assert _coordinator()._read_live_power_boolean(_EV_STATUS) is None


class TestReadLivePowerPositiveRaw:
    """Raw EV power reads answer 'is this charger drawing anything?'."""

    @pytest.mark.parametrize(
        ("state", "expected"),
        [("1500", True), ("0", False), ("-5", False), ("unavailable", False)],
    )
    def test_reads_state(self, state: str, expected: bool) -> None:
        """Only a strictly positive reading counts as drawing power."""
        coordinator = _coordinator({_EV_POWER: _FakeState(state)})

        assert coordinator._read_live_power_positive_raw(_EV_POWER) is expected

    def test_unconfigured_entity_is_false(self) -> None:
        """No entity configured → no evidence of EV draw."""
        assert _coordinator()._read_live_power_positive_raw(None) is False

    def test_unknown_entity_is_false(self) -> None:
        """A missing entity is not evidence of EV draw."""
        assert _coordinator()._read_live_power_positive_raw(_EV_POWER) is False


class TestTickEvAmbiguity:
    """An inclusive house meter is unusable while an EV may be charging."""

    def test_exclusive_meter_is_never_ambiguous(self) -> None:
        """A house meter that excludes the charger is always usable."""
        coordinator = _coordinator({_EV_STATUS: _FakeState("on")}, includes_ev=False)

        assert (
            coordinator._live_power_tick_ev_ambiguous(
                coordinator._cfg, coordinator._live
            )
            is False
        )

    def test_snapshot_charging_is_ambiguous(self) -> None:
        """The cycle snapshot alone can prove the EV is charging."""
        coordinator = _coordinator(includes_ev=True)
        live = LiveState()
        live.ev.is_charging = True

        assert coordinator._live_power_tick_ev_ambiguous(coordinator._cfg, live) is True

    def test_same_tick_status_entity_is_ambiguous(self) -> None:
        """A charger status of ``on`` this tick is enough to fail closed."""
        coordinator = _coordinator({_EV_STATUS: _FakeState("on")}, includes_ev=True)

        assert (
            coordinator._live_power_tick_ev_ambiguous(
                coordinator._cfg, coordinator._live
            )
            is True
        )

    def test_unavailable_status_entity_is_ambiguous(self) -> None:
        """An unavailable configured charger status fails closed."""
        coordinator = _coordinator(
            {_EV_STATUS: _FakeState("unavailable")}, includes_ev=True
        )

        assert (
            coordinator._live_power_tick_ev_ambiguous(
                coordinator._cfg, coordinator._live
            )
            is True
        )

    def test_same_tick_power_reading_is_ambiguous(self) -> None:
        """Positive charger power this tick is enough to fail closed."""
        coordinator = _coordinator(
            {_EV_STATUS: _FakeState("off"), _EV_POWER: _FakeState("1500")},
            includes_ev=True,
        )

        assert (
            coordinator._live_power_tick_ev_ambiguous(
                coordinator._cfg, coordinator._live
            )
            is True
        )

    def test_idle_charger_is_not_ambiguous(self) -> None:
        """An idle charger leaves the inclusive meter usable."""
        coordinator = _coordinator(
            {_EV_STATUS: _FakeState("off"), _EV_POWER: _FakeState("0")},
            includes_ev=True,
        )

        assert (
            coordinator._live_power_tick_ev_ambiguous(
                coordinator._cfg, coordinator._live
            )
            is False
        )


class TestSampleLivePowerWindow:
    """Each tick adds one sample and reports what it read."""

    def test_returns_estimate_and_raw_readings(self) -> None:
        """Both meters feed the window and the estimate reflects them."""
        coordinator = _coordinator(
            {_HOUSE: _FakeState("1200", unit="W"), _SOLAR: _FakeState("3.6", unit="kW")}
        )

        estimate, raw_house, solar, ambiguous = coordinator._sample_live_power_window(
            _NOW
        )

        assert raw_house == pytest.approx(1200.0)
        assert solar == pytest.approx(3600.0)
        assert ambiguous is False
        assert estimate.house_power_w == pytest.approx(1200.0)
        assert estimate.solar_power_w == pytest.approx(3600.0)

    def test_ambiguous_house_reading_is_withheld_from_the_window(self) -> None:
        """The raw value is still reported, but never enters the estimate."""
        coordinator = _coordinator(
            {
                _HOUSE: _FakeState("1200", unit="W"),
                _SOLAR: _FakeState("500", unit="W"),
                _EV_STATUS: _FakeState("on"),
            },
            includes_ev=True,
        )

        estimate, raw_house, _solar, ambiguous = coordinator._sample_live_power_window(
            _NOW
        )

        assert ambiguous is True
        assert raw_house == pytest.approx(1200.0)
        assert estimate.house_power_w is None
        assert estimate.solar_power_w == pytest.approx(500.0)


class TestSeedLivePowerWindow:
    """The full-cycle snapshot seeds the same window as the fast timer."""

    def test_missing_or_unconfigured_channels_are_not_sampled(self) -> None:
        """An unconfigured solar meter and a reported-missing house meter."""
        coordinator = _coordinator()
        coordinator._cfg.solar_production_power = None
        live = LiveState()
        live.house_consumption_power_w = 900.0
        live.solar_production_power_w = 400.0
        live.missing_entities_list = ["house_consumption_power"]  # type: ignore[attr-defined]  # snapshot field

        estimate = coordinator._seed_live_power_window(_NOW, coordinator._cfg, live)

        assert estimate.house_power_w is None
        assert estimate.solar_power_w is None

    def test_ambiguous_ev_load_withholds_the_house_channel(self) -> None:
        """An inclusive meter is withheld while the EV draws power."""
        coordinator = _coordinator(includes_ev=True)
        live = LiveState()
        live.house_consumption_power_w = 900.0
        live.solar_production_power_w = 400.0
        live.ev.power_w = 3_700.0

        estimate = coordinator._seed_live_power_window(_NOW, coordinator._cfg, live)

        assert estimate.house_power_w is None
        assert estimate.solar_power_w == pytest.approx(400.0)


class TestRetainedStateResets:
    """Changing the power sources drops retained authority."""

    def test_source_change_clears_window_and_budget(self) -> None:
        """A different meter topology invalidates samples and the budget."""
        coordinator = _coordinator({_HOUSE: _FakeState("1200", unit="W")})
        coordinator._sample_live_power_window(_NOW)
        coordinator._live_power_replanned_slot_start = _SLOT_START
        coordinator._live_power_replan_count = 1
        coordinator._cfg.house_consumption_power = "sensor.other_house_power"

        estimate, _raw, _solar, _ambiguous = coordinator._sample_live_power_window(
            _NOW + timedelta(seconds=10)
        )

        assert estimate.house_power_w is None
        assert coordinator._live_power_replanned_slot_start is None
        assert coordinator._live_power_replan_count == 0

    def test_reset_clears_everything(self) -> None:
        """A config reload drops the window, the budget, and the baseline."""
        coordinator = _coordinator({_HOUSE: _FakeState("1200", unit="W")})
        coordinator._sample_live_power_window(_NOW)
        coordinator._last_plan_live_power_estimate = _estimate(1000.0, 0.0)
        coordinator._live_power_replan_count = 1

        coordinator.reset_live_power_state()

        assert coordinator._live_power_window.estimate(_NOW).house_power_w is None
        assert coordinator._live_power_source_signature is None
        assert coordinator._last_plan_live_power_estimate is None
        assert coordinator._live_power_replan_count == 0


class TestInvalidSlotInterval:
    """A corrupted slot interval falls back to 15 minutes."""

    def test_slot_context_falls_back(self) -> None:
        """A non-numeric interval still yields a canonical slot."""
        coordinator = _coordinator()
        coordinator._cfg.recommendation_interval_minutes = "bad"  # type: ignore[assignment]  # corrupted config

        slot_start, remaining_seconds = coordinator._live_power_slot_context(_NOW)

        assert slot_start == _SLOT_START
        assert remaining_seconds == pytest.approx(14 * 60 + 30)

    def test_zero_interval_falls_back(self) -> None:
        """A zero interval is rejected in favour of the default."""
        coordinator = _coordinator()
        coordinator._cfg.recommendation_interval_minutes = 0

        slot_start, _remaining = coordinator._live_power_slot_context(_NOW)

        assert slot_start == _SLOT_START

    def test_materiality_falls_back_to_a_quarter_hour(self) -> None:
        """A corrupted interval still yields a usable materiality window."""
        coordinator = _coordinator()
        coordinator._cfg.recommendation_interval_minutes = "bad"  # type: ignore[assignment]  # corrupted config
        coordinator._last_plan_live_power_estimate = _estimate(500.0, 0.0)

        assert (
            coordinator._live_power_estimate_changed_materially(
                _estimate(1500.0, 0.0), house_ambiguous=False
            )
            is True
        )


def _armed(coordinator: HSEMDataUpdateCoordinator) -> None:
    """Arm the coordinator so a material change can request a replan."""
    coordinator._last_plan_slot_start = _SLOT_START
    coordinator._last_plan_live_power_estimate = _estimate(500.0, 0.0)


class TestTrackLivePowerMismatch:
    """The sustained-mismatch debounce gates every replan request."""

    def test_immaterial_change_clears_pending_state(self) -> None:
        """A returning-to-normal reading drops the in-progress mismatch."""
        coordinator = _coordinator()
        _armed(coordinator)
        coordinator._live_power_mismatch_slot_start = _SLOT_START
        coordinator._live_power_mismatch_since = _SLOT_START

        pending = coordinator._track_live_power_mismatch(
            _NOW, _estimate(500.0, 0.0), house_ambiguous=False
        )

        assert pending is False
        assert coordinator._live_power_mismatch_since is None

    def test_exhausted_budget_clears_pending_state(self) -> None:
        """Once the slot's corrections are spent, mismatches stop arming."""
        coordinator = _coordinator()
        _armed(coordinator)
        coordinator._live_power_replanned_slot_start = _SLOT_START
        coordinator._live_power_replan_count = (
            LIVE_POWER_REPLAN_MAX_CORRECTIONS_PER_SLOT
        )
        coordinator._live_power_mismatch_slot_start = _SLOT_START
        coordinator._live_power_mismatch_since = _SLOT_START

        pending = coordinator._track_live_power_mismatch(
            _NOW, _estimate(2500.0, 0.0), house_ambiguous=False
        )

        assert pending is False
        assert coordinator._live_power_mismatch_slot_start is None

    def test_sustained_mismatch_arms_then_stays_pending(self) -> None:
        """The first sample starts the debounce; a later one requests a replan."""
        coordinator = _coordinator()
        _armed(coordinator)
        estimate = _estimate(2500.0, 0.0)

        first = coordinator._track_live_power_mismatch(
            _NOW, estimate, house_ambiguous=False
        )
        debounced = coordinator._track_live_power_mismatch(
            _NOW + timedelta(seconds=LIVE_POWER_MISMATCH_DEBOUNCE_SECONDS),
            estimate,
            house_ambiguous=False,
        )
        still_pending = coordinator._track_live_power_mismatch(
            _NOW + timedelta(seconds=LIVE_POWER_MISMATCH_DEBOUNCE_SECONDS + 10),
            estimate,
            house_ambiguous=False,
        )

        assert first is False
        assert debounced is True
        assert still_pending is True
        assert coordinator._live_power_replan_pending_slot == _SLOT_START


class TestReplanBudgetEdges:
    """Reversal authority needs a proven first-correction direction."""

    def test_zero_completed_corrections_allows_a_replan(self) -> None:
        """A slot marked replanned but with no completed count still allows one."""
        coordinator = _coordinator()
        coordinator._live_power_replanned_slot_start = _SLOT_START
        coordinator._live_power_replan_count = 0

        assert (
            coordinator._live_power_replan_budget_allows(
                _SLOT_START, _estimate(2500.0, 0.0), house_ambiguous=False
            )
            is True
        )

    def test_unknown_first_direction_blocks_a_reversal(self) -> None:
        """Without a proven first direction no reversal can be justified."""
        coordinator = _coordinator()
        coordinator._live_power_replanned_slot_start = _SLOT_START
        coordinator._live_power_replan_count = 1
        coordinator._live_power_first_replan_direction = None

        assert (
            coordinator._live_power_replan_budget_allows(
                _SLOT_START, _estimate(2500.0, 0.0), house_ambiguous=False
            )
            is False
        )


class TestAsyncMonitorLivePower:
    """The ten-second tick samples, then conditionally triggers a cycle."""

    @pytest.mark.asyncio
    async def test_teardown_skips_sampling(self) -> None:
        """A tick during teardown does nothing at all."""
        coordinator = _coordinator()
        coordinator._tearing_down = True  # type: ignore[attr-defined]  # teardown flag
        sample = MagicMock()

        with patch.object(coordinator, "_sample_live_power_window", sample):
            await coordinator.async_monitor_live_power(_NOW)

        sample.assert_not_called()

    @pytest.mark.asyncio
    async def test_tick_before_the_first_cycle_is_ignored(self) -> None:
        """Without a live snapshot there is nothing to compare against."""
        coordinator = _coordinator()
        coordinator._live = None
        sample = MagicMock()

        with patch.object(coordinator, "_sample_live_power_window", sample):
            await coordinator.async_monitor_live_power(_NOW)

        sample.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "make_ineligible",
        [
            pytest.param(
                lambda coordinator: setattr(
                    coordinator._live,
                    "force_working_mode_state",
                    "batteries_charge_grid",
                ),
                id="forced_mode",
            ),
            pytest.param(
                lambda coordinator: setattr(
                    coordinator, "_last_load_forecast_readiness_reason", "zero_forecast"
                ),
                id="unsafe_load_forecast",
            ),
        ],
    )
    async def test_ineligible_state_clears_pending_and_skips_replan(
        self, make_ineligible: object
    ) -> None:
        """A forced mode or unsafe forecast suppresses live-power replans."""
        coordinator = _coordinator({_HOUSE: _FakeState("2500", unit="W")})
        _armed(coordinator)
        coordinator._live_power_mismatch_slot_start = _SLOT_START
        coordinator._live_power_mismatch_since = _SLOT_START
        cast(object, make_ineligible)(coordinator)  # type: ignore[operator]  # parametrised setter
        handle_update = AsyncMock()

        with patch.object(coordinator, "_async_handle_update", handle_update):
            await coordinator.async_monitor_live_power(_NOW)

        handle_update.assert_not_awaited()
        assert coordinator._live_power_mismatch_since is None

    @pytest.mark.asyncio
    async def test_degraded_error_mode_skips_replan(self) -> None:
        """A degraded (error) integration never drives extra solves."""
        coordinator = _coordinator({_HOUSE: _FakeState("2500", unit="W")})
        _armed(coordinator)
        live = MagicMock()
        live.force_working_mode_state = "auto"
        live.degraded_mode = DegradedMode.Error
        live.any_ev_charging = False
        coordinator._live = cast(LiveState, live)
        handle_update = AsyncMock()

        with patch.object(coordinator, "_async_handle_update", handle_update):
            await coordinator.async_monitor_live_power(_NOW)

        handle_update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sustained_mismatch_triggers_a_cycle(self) -> None:
        """A matured mismatch runs the coordinator cycle immediately."""
        coordinator = _coordinator({_HOUSE: _FakeState("2500", unit="W")})
        _armed(coordinator)
        coordinator._live_power_replan_pending_slot = _SLOT_START
        handle_update = AsyncMock()

        with patch.object(coordinator, "_async_handle_update", handle_update):
            await coordinator.async_monitor_live_power(_NOW)

        handle_update.assert_awaited_once_with(None)

    @pytest.mark.asyncio
    async def test_no_cycle_while_one_is_already_running(self) -> None:
        """A busy update lock defers to the in-flight cycle."""
        coordinator = _coordinator({_HOUSE: _FakeState("2500", unit="W")})
        _armed(coordinator)
        coordinator._live_power_replan_pending_slot = _SLOT_START
        handle_update = AsyncMock()

        async with coordinator._update_lock:
            with patch.object(coordinator, "_async_handle_update", handle_update):
                await coordinator.async_monitor_live_power(_NOW)

        handle_update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stable_power_does_not_trigger_a_cycle(self) -> None:
        """Readings matching the accepted plan leave the plan alone."""
        coordinator = _coordinator({_HOUSE: _FakeState("500", unit="W")})
        _armed(coordinator)
        handle_update = AsyncMock()

        with patch.object(coordinator, "_async_handle_update", handle_update):
            await coordinator.async_monitor_live_power(_NOW)

        handle_update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tick_without_an_explicit_time_uses_the_clock(self) -> None:
        """The HA timer passes no time, so the tick reads the HSEM clock."""
        coordinator = _coordinator({_HOUSE: _FakeState("500", unit="W")})
        _armed(coordinator)

        with patch(
            "custom_components.hsem.utils.datetime_utils.now", return_value=_NOW
        ) as clock:
            await coordinator.async_monitor_live_power()

        clock.assert_called()
        assert coordinator._live_power_window.estimate(_NOW).house_power_w == (
            pytest.approx(500.0)
        )
