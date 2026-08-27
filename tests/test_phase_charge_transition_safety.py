"""Tests for the phase-aware grid-charge transition safety (issue #831, Part 3).

Covers the feedback-free floor during a verified downward Huawei grid-charge
cap change, and the 45-second fail-closed deadline that bounds how long a
verified-but-unsettled transition may suppress the raw battery-power
feedback.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.hsem.custom_sensors.phase_charge_limiter import (
    build_phase_aware_charge_commands,
)
from custom_components.hsem.custom_sensors.phase_charge_transition import (
    PrimaryGridChargeTransition,
)
from custom_components.hsem.custom_sensors.working_mode_sensor import (
    HSEMWorkingModeSensor,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.inverter_verify import (
    ApplyResult,
    ApplyStatus,
    CycleApplySummary,
)
from custom_components.hsem.utils.recommendations import Recommendations

_NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


def _rec(
    *,
    start: datetime = _NOW,
    recommendation: str | None = Recommendations.BatteriesChargeGrid.value,
    batteries_charged_kwh: float = 2.5,
) -> HourlyRecommendation:
    """Return a minimal HourlyRecommendation for a one-hour slot."""
    return HourlyRecommendation(
        start=start,
        end=start + timedelta(hours=1),
        recommendation=recommendation,
        avg_house_consumption_kwh=0.0,
        avg_house_consumption_1d_kwh=0.0,
        avg_house_consumption_3d_kwh=0.0,
        avg_house_consumption_7d_kwh=0.0,
        avg_house_consumption_14d_kwh=0.0,
        batteries_charged_kwh=batteries_charged_kwh,
        batteries_discharged_kwh=0.0,
        estimated_battery_capacity_kwh=0.0,
        estimated_battery_soc_pct=50.0,
        estimated_cost_currency=0.0,
        estimated_net_consumption_kwh=0.0,
        export_price=0.0,
        grid_export_kwh=0.0,
        grid_import_kwh=0.0,
        import_price=1.0,
        solcast_pv_estimate_kwh=0.0,
    )


def _config() -> SensorConfig:
    cfg = SensorConfig()
    cfg.phase_aware_charging_enabled = True
    cfg.main_fuse_amps = 25
    cfg.main_fuse_phases = 3
    cfg.batteries_charge_efficiency = 98.0
    cfg.batteries_discharge_efficiency = 98.0
    cfg.huawei_solar_batteries_grid_charge_maximum_power = "number.gcmp"
    return cfg


def _verified_summary(target_limit_w: float) -> CycleApplySummary:
    """Return one verified Huawei maximum-grid-charge-power write."""
    return CycleApplySummary(
        results=[
            ApplyResult(
                entity_id="number.gcmp",
                desired=target_limit_w,
                actual=target_limit_w,
                status=ApplyStatus.OK,
                attempts=1,
            )
        ]
    )


def _make_config_entry() -> MagicMock:
    """Minimal mock config entry sufficient for HSEMWorkingModeSensor."""
    cfg = MagicMock()
    cfg.entry_id = "test_entry_id_phase_transition"
    cfg.options = {}
    cfg.data = {}
    return cfg


def _make_coordinator() -> MagicMock:
    coord = MagicMock()
    coord.data = None
    coord.last_update_success = True
    return coord


def _make_sensor() -> HSEMWorkingModeSensor:
    """Build a sensor instance with mocked coordinator and hass."""
    cfg = _make_config_entry()
    coord = _make_coordinator()
    sensor = HSEMWorkingModeSensor(cfg, coord)

    hass = MagicMock()

    def _fake_create_task(coro, *, name=None):
        loop = asyncio.get_event_loop()
        return loop.create_task(coro, name=name)

    hass.async_create_task = MagicMock(side_effect=_fake_create_task)
    sensor.hass = hass
    return sensor


# ---------------------------------------------------------------------------
# build_phase_aware_charge_commands — feedback-free floor
# ---------------------------------------------------------------------------


class TestFeedbackFreeFloor:
    def test_transition_reference_overrides_lower_live_battery_power(self):
        """A stale-low battery-power reading cannot manufacture headroom."""
        cfg = _config()
        live = LiveState()
        # 8900 W was the previous command; live power has only partially
        # ramped down to 5900 W so far. If the limiter subtracted only the
        # low live reading, it would think there is more headroom than
        # physically exists.
        live.grid_phase_power_w = (
            700.0 + 8900.0 / 3,
            1200.0 + 8900.0 / 3,
            1700.0 + 8900.0 / 3,
        )
        live.huawei_batteries_charge_discharge_power_w = 5900.0

        without_reference = build_phase_aware_charge_commands(
            cfg, live, _rec(batteries_charged_kwh=2.5)
        )
        with_reference = build_phase_aware_charge_commands(
            cfg,
            live,
            _rec(batteries_charged_kwh=2.5),
            primary_grid_charge_transition_reference_w=8900.0,
        )

        assert with_reference.primary_grid_charge_power_w is not None
        assert without_reference.primary_grid_charge_power_w is not None
        # Using the higher reference must not grant MORE headroom than the
        # unmodified (lower battery-power) computation.
        assert (
            with_reference.primary_grid_charge_power_w
            <= without_reference.primary_grid_charge_power_w
        )

    def test_transition_reference_lower_than_live_power_is_ignored(self):
        """The reference only matters when it exceeds the live reading."""
        cfg = _config()
        live = LiveState()
        live.grid_phase_power_w = (700.0, 1200.0, 1700.0)
        live.huawei_batteries_charge_discharge_power_w = 5000.0

        with_low_reference = build_phase_aware_charge_commands(
            cfg,
            live,
            _rec(batteries_charged_kwh=2.5),
            primary_grid_charge_transition_reference_w=1000.0,
        )
        without_reference = build_phase_aware_charge_commands(
            cfg, live, _rec(batteries_charged_kwh=2.5)
        )

        assert (
            with_low_reference.primary_grid_charge_power_w
            == without_reference.primary_grid_charge_power_w
        )

    def test_timed_out_transition_fails_closed(self):
        cfg = _config()
        live = LiveState()
        live.grid_phase_power_w = (700.0, 1200.0, 1700.0)
        live.huawei_batteries_charge_discharge_power_w = 0.0

        commands = build_phase_aware_charge_commands(
            cfg,
            live,
            _rec(batteries_charged_kwh=2.5),
            primary_grid_charge_transition_timed_out=True,
        )

        assert commands.primary_grid_charge_power_w == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# _primary_grid_charge_transition_status
# ---------------------------------------------------------------------------


class TestPrimaryGridChargeTransitionStatus:
    def test_no_transition_returns_none_and_not_timed_out(self):
        sensor = _make_sensor()
        cfg = _config()
        live = LiveState()
        rec = _rec()

        reference_w, timed_out = sensor._primary_grid_charge_transition_status(
            cfg, live, rec
        )

        assert reference_w is None
        assert timed_out is False

    def test_non_grid_charge_recommendation_clears_transition(self):
        sensor = _make_sensor()
        cfg = _config()
        live = LiveState()
        live.huawei_batteries_grid_charge_max_power_w = 5900.0
        rec = _rec()
        sensor._record_verified_primary_grid_charge_transition(
            cfg, live, rec, 5900.0, _verified_summary(5900.0)
        )
        # Pretend the previous live cap was higher so a transition is armed.
        live.huawei_batteries_grid_charge_max_power_w = 8900.0
        sensor._record_verified_primary_grid_charge_transition(
            cfg, live, rec, 5900.0, _verified_summary(5900.0)
        )

        non_charge_rec = _rec(recommendation=Recommendations.BatteriesWaitMode.value)
        reference_w, timed_out = sensor._primary_grid_charge_transition_status(
            cfg, live, non_charge_rec
        )

        assert reference_w is None
        assert timed_out is False
        assert sensor._primary_grid_charge_transition is None

    def test_disabled_feature_clears_transition(self):
        sensor = _make_sensor()
        cfg = _config()
        live = LiveState()
        live.huawei_batteries_grid_charge_max_power_w = 8900.0
        rec = _rec()
        sensor._record_verified_primary_grid_charge_transition(
            cfg, live, rec, 5900.0, _verified_summary(5900.0)
        )
        assert sensor._primary_grid_charge_transition is not None

        cfg.phase_aware_charging_enabled = False
        reference_w, timed_out = sensor._primary_grid_charge_transition_status(
            cfg, live, rec
        )

        assert reference_w is None
        assert timed_out is False
        assert sensor._primary_grid_charge_transition is None

    def test_active_transition_returns_previous_limit_until_settled(self):
        sensor = _make_sensor()
        cfg = _config()
        live = LiveState()
        live.huawei_batteries_grid_charge_max_power_w = 8900.0
        rec = _rec()
        sensor._record_verified_primary_grid_charge_transition(
            cfg, live, rec, 5900.0, _verified_summary(5900.0)
        )
        transition = sensor._primary_grid_charge_transition
        assert isinstance(transition, PrimaryGridChargeTransition)
        assert transition.previous_limit_w == pytest.approx(8900.0)
        assert transition.target_limit_w == pytest.approx(5900.0)

        # Cap has now verified at the lower value, but battery power has not
        # caught up yet — not settled.
        live.huawei_batteries_grid_charge_max_power_w = 5900.0
        live.huawei_batteries_charge_discharge_power_w = 8900.0

        reference_w, timed_out = sensor._primary_grid_charge_transition_status(
            cfg, live, rec
        )

        assert reference_w == pytest.approx(8900.0)
        assert timed_out is False

    def test_settled_transition_clears_and_returns_none(self):
        sensor = _make_sensor()
        cfg = _config()
        live = LiveState()
        live.huawei_batteries_grid_charge_max_power_w = 8900.0
        rec = _rec()
        sensor._record_verified_primary_grid_charge_transition(
            cfg, live, rec, 5900.0, _verified_summary(5900.0)
        )

        # Both the cap and the battery-power echo now agree with the target.
        live.huawei_batteries_grid_charge_max_power_w = 5900.0
        live.huawei_batteries_charge_discharge_power_w = 5900.0

        reference_w, timed_out = sensor._primary_grid_charge_transition_status(
            cfg, live, rec
        )

        assert reference_w is None
        assert timed_out is False
        assert sensor._primary_grid_charge_transition is None

    def test_expired_deadline_times_out_for_the_rest_of_the_slot(self):
        sensor = _make_sensor()
        cfg = _config()
        live = LiveState()
        live.huawei_batteries_grid_charge_max_power_w = 8900.0
        rec = _rec()
        sensor._record_verified_primary_grid_charge_transition(
            cfg, live, rec, 5900.0, _verified_summary(5900.0)
        )
        transition = sensor._primary_grid_charge_transition
        assert transition is not None
        # Force immediate expiry.
        sensor._primary_grid_charge_transition = PrimaryGridChargeTransition(
            previous_limit_w=transition.previous_limit_w,
            target_limit_w=transition.target_limit_w,
            slot_start=transition.slot_start,
            slot_end=transition.slot_end,
            expires_at_monotonic=-1.0,
        )

        # Not settled: cap still shows the old value.
        live.huawei_batteries_charge_discharge_power_w = 8900.0

        reference_w, timed_out = sensor._primary_grid_charge_transition_status(
            cfg, live, rec
        )

        assert reference_w is None
        assert timed_out is True
        assert sensor._primary_grid_charge_timed_out_slot is not None

        # A second call within the same slot must remain timed out even if
        # telemetry still hasn't settled.
        reference_w2, timed_out2 = sensor._primary_grid_charge_transition_status(
            cfg, live, rec
        )
        assert reference_w2 is None
        assert timed_out2 is True

    def test_timeout_does_not_leak_into_the_next_slot(self):
        sensor = _make_sensor()
        cfg = _config()
        live = LiveState()
        live.huawei_batteries_grid_charge_max_power_w = 8900.0
        rec = _rec()
        sensor._record_verified_primary_grid_charge_transition(
            cfg, live, rec, 5900.0, _verified_summary(5900.0)
        )
        transition = sensor._primary_grid_charge_transition
        assert transition is not None
        sensor._primary_grid_charge_transition = PrimaryGridChargeTransition(
            previous_limit_w=transition.previous_limit_w,
            target_limit_w=transition.target_limit_w,
            slot_start=transition.slot_start,
            slot_end=transition.slot_end,
            expires_at_monotonic=-1.0,
        )
        live.huawei_batteries_charge_discharge_power_w = 8900.0
        sensor._primary_grid_charge_transition_status(cfg, live, rec)
        assert sensor._primary_grid_charge_timed_out_slot is not None

        next_rec = _rec(start=rec.end)
        reference_w, timed_out = sensor._primary_grid_charge_transition_status(
            cfg, live, next_rec
        )

        assert reference_w is None
        assert timed_out is False
        assert sensor._primary_grid_charge_transition is None
        assert sensor._primary_grid_charge_timed_out_slot is None


# ---------------------------------------------------------------------------
# _record_verified_primary_grid_charge_transition
# ---------------------------------------------------------------------------


class TestRecordVerifiedTransition:
    def test_upward_change_does_not_arm_a_transition(self):
        sensor = _make_sensor()
        cfg = _config()
        live = LiveState()
        live.huawei_batteries_grid_charge_max_power_w = 3000.0
        rec = _rec()

        sensor._record_verified_primary_grid_charge_transition(
            cfg, live, rec, 5900.0, _verified_summary(5900.0)
        )

        assert sensor._primary_grid_charge_transition is None

    def test_unverified_write_does_not_arm_a_transition(self):
        sensor = _make_sensor()
        cfg = _config()
        live = LiveState()
        live.huawei_batteries_grid_charge_max_power_w = 8900.0
        rec = _rec()
        failed_summary = CycleApplySummary(
            results=[
                ApplyResult(
                    entity_id="number.gcmp",
                    desired=5900.0,
                    actual=8900.0,
                    status=ApplyStatus.FAILED,
                    attempts=3,
                )
            ]
        )

        sensor._record_verified_primary_grid_charge_transition(
            cfg, live, rec, 5900.0, failed_summary
        )

        assert sensor._primary_grid_charge_transition is None

    def test_repeated_same_target_does_not_extend_deadline(self):
        sensor = _make_sensor()
        cfg = _config()
        live = LiveState()
        live.huawei_batteries_grid_charge_max_power_w = 8900.0
        rec = _rec()
        sensor._record_verified_primary_grid_charge_transition(
            cfg, live, rec, 5900.0, _verified_summary(5900.0)
        )
        original = sensor._primary_grid_charge_transition
        assert original is not None
        original_expiry = original.expires_at_monotonic

        live.huawei_batteries_grid_charge_max_power_w = 5900.0
        sensor._record_verified_primary_grid_charge_transition(
            cfg, live, rec, 5900.0, _verified_summary(5900.0)
        )

        retained = sensor._primary_grid_charge_transition
        assert retained is not None
        assert retained.expires_at_monotonic == pytest.approx(original_expiry)

    def test_nested_decrease_keeps_the_original_physical_reference(self):
        """A second lower target must not forget the still-physical first cap."""
        sensor = _make_sensor()
        cfg = _config()
        live = LiveState()
        live.huawei_batteries_grid_charge_max_power_w = 8900.0
        rec = _rec()
        sensor._record_verified_primary_grid_charge_transition(
            cfg, live, rec, 5900.0, _verified_summary(5900.0)
        )

        live.huawei_batteries_grid_charge_max_power_w = 5900.0
        sensor._record_verified_primary_grid_charge_transition(
            cfg, live, rec, 5000.0, _verified_summary(5000.0)
        )

        transition = sensor._primary_grid_charge_transition
        assert transition is not None
        assert transition.previous_limit_w == pytest.approx(8900.0)
        assert transition.target_limit_w == pytest.approx(5000.0)

    def test_no_entity_configured_is_a_no_op(self):
        sensor = _make_sensor()
        cfg = _config()
        cfg.huawei_solar_batteries_grid_charge_maximum_power = None
        live = LiveState()
        live.huawei_batteries_grid_charge_max_power_w = 8900.0
        rec = _rec()

        sensor._record_verified_primary_grid_charge_transition(
            cfg, live, rec, 5900.0, _verified_summary(5900.0)
        )

        assert sensor._primary_grid_charge_transition is None


# ---------------------------------------------------------------------------
# Deadline task lifecycle
# ---------------------------------------------------------------------------


class TestDeadlineTaskLifecycle:
    @pytest.mark.asyncio
    async def test_deadline_fail_closes_without_a_telemetry_event(self):
        """A frozen battery echo cannot defer the 45s stop to the next poll."""
        sensor = _make_sensor()
        sensor._transition_deadline_tasks_enabled = True
        cfg = _config()
        live = LiveState()
        live.huawei_batteries_grid_charge_max_power_w = 8900.0
        rec = _rec()
        sensor._record_verified_primary_grid_charge_transition(
            cfg, live, rec, 5900.0, _verified_summary(5900.0)
        )
        transition = sensor._primary_grid_charge_transition
        assert transition is not None
        expired = PrimaryGridChargeTransition(
            previous_limit_w=transition.previous_limit_w,
            target_limit_w=transition.target_limit_w,
            slot_start=transition.slot_start,
            slot_end=transition.slot_end,
            expires_at_monotonic=-1.0,
        )
        sensor._primary_grid_charge_transition = expired

        apply_writes = AsyncMock()
        sensor._async_apply_hardware_writes = apply_writes  # type: ignore[method-assign]
        sensor.coordinator.data = MagicMock()

        sensor._schedule_primary_grid_charge_deadline(expired)
        deadline_task = sensor._primary_grid_charge_deadline_task
        assert deadline_task is not None
        await deadline_task

        apply_writes.assert_awaited()

    @pytest.mark.asyncio
    async def test_unload_cancels_the_deadline_task(self):
        sensor = _make_sensor()
        sensor._transition_deadline_tasks_enabled = True
        cfg = _config()
        live = LiveState()
        live.huawei_batteries_grid_charge_max_power_w = 8900.0
        rec = _rec()
        sensor._record_verified_primary_grid_charge_transition(
            cfg, live, rec, 5900.0, _verified_summary(5900.0)
        )
        transition = sensor._primary_grid_charge_transition
        assert transition is not None
        far_future = PrimaryGridChargeTransition(
            previous_limit_w=transition.previous_limit_w,
            target_limit_w=transition.target_limit_w,
            slot_start=transition.slot_start,
            slot_end=transition.slot_end,
            expires_at_monotonic=1e9,
        )
        sensor._primary_grid_charge_transition = far_future
        sensor._schedule_primary_grid_charge_deadline(far_future)
        deadline_task = sensor._primary_grid_charge_deadline_task
        assert deadline_task is not None

        await sensor.async_will_remove_from_hass()

        assert deadline_task.cancelled()
        assert sensor._primary_grid_charge_deadline_task is None
        assert sensor._primary_grid_charge_transition is None

    @pytest.mark.asyncio
    async def test_deadline_task_not_scheduled_without_event_loop_enablement(self):
        """Synchronous callers (tests, sync code paths) must not attempt scheduling."""
        sensor = _make_sensor()
        sensor._transition_deadline_tasks_enabled = False
        cfg = _config()
        live = LiveState()
        live.huawei_batteries_grid_charge_max_power_w = 8900.0
        rec = _rec()

        sensor._record_verified_primary_grid_charge_transition(
            cfg, live, rec, 5900.0, _verified_summary(5900.0)
        )

        assert sensor._primary_grid_charge_deadline_task is None
