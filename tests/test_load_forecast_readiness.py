"""Regression tests for restart-safe house-load forecast readiness."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.coordinator import HSEMDataUpdateCoordinator
from custom_components.hsem.coordinator_helpers import (
    apply_load_forecast_hold,
    assess_load_forecast,
    load_forecast_signatures_match,
)
from custom_components.hsem.custom_sensors.avg_sensor import HSEMAvgSensor
from custom_components.hsem.custom_sensors.state_collector import (
    async_collect_all_states,
)
from custom_components.hsem.models.data_quality import DataQuality
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.recommendations import Recommendations


def _rec(start: datetime, load: float = 0.0) -> HourlyRecommendation:
    """Return a fully populated recommendation for load-readiness tests."""
    return HourlyRecommendation(
        start=start,
        end=start + timedelta(minutes=15),
        recommendation=Recommendations.BatteriesWaitMode.value,
        avg_house_consumption_kwh=load,
        avg_house_consumption_1d_kwh=load,
        avg_house_consumption_3d_kwh=load,
        avg_house_consumption_7d_kwh=load,
        avg_house_consumption_14d_kwh=load,
        batteries_charged_kwh=0.0,
        batteries_discharged_kwh=0.0,
        estimated_battery_capacity_kwh=0.0,
        estimated_battery_soc_pct=0.0,
        estimated_cost_currency=0.0,
        estimated_net_consumption_kwh=0.0,
        export_price=0.0,
        grid_export_kwh=0.0,
        grid_import_kwh=0.0,
        import_price=0.0,
        solcast_pv_estimate_kwh=0.0,
    )


def _profile(now: datetime, load: float = 0.0) -> list[HourlyRecommendation]:
    """Return a profile containing the current and next future slot."""
    return [
        _rec(now - timedelta(minutes=5), load),
        _rec(now + timedelta(minutes=10), load),
    ]


class TestLoadForecastAssessment:
    """Validate provenance, values, and zero-load/live-demand handling."""

    def test_missing_population_provenance_fails_closed(self) -> None:
        now = datetime(2026, 8, 21, 12, 5, tzinfo=UTC)
        result = assess_load_forecast(
            _profile(now, 0.2),
            now,
            population_succeeded=False,
            live_house_demand_w=0.0,
        )
        assert (result.ready, result.reason, result.signature) == (
            False,
            "source_unavailable",
            None,
        )

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.01])
    def test_nonfinite_or_negative_value_fails_closed(self, value: float) -> None:
        now = datetime(2026, 8, 21, 12, 5, tzinfo=UTC)
        profile = _profile(now, 0.2)
        profile[1].avg_house_consumption_7d_kwh = value
        result = assess_load_forecast(
            profile,
            now,
            population_succeeded=True,
            live_house_demand_w=0.0,
        )
        assert result.ready is False
        assert result.reason == "invalid_future_values"
        assert result.signature is None

    @pytest.mark.parametrize("live_w", [None, 0.0, 49.9, 50.0])
    def test_complete_zero_is_valid_at_or_below_threshold(
        self, live_w: float | None
    ) -> None:
        now = datetime(2026, 8, 21, 12, 5, tzinfo=UTC)
        result = assess_load_forecast(
            _profile(now),
            now,
            population_succeeded=True,
            live_house_demand_w=live_w,
        )
        assert result.ready is True
        assert result.reason is None
        assert result.signature is not None

    def test_complete_zero_fails_above_live_threshold(self) -> None:
        now = datetime(2026, 8, 21, 12, 5, tzinfo=UTC)
        result = assess_load_forecast(
            _profile(now),
            now,
            population_succeeded=True,
            live_house_demand_w=50.1,
        )
        assert result.ready is False
        assert result.reason == "zero_forecast_with_live_demand"

    def test_signature_uses_all_five_load_fields_and_epsilon(self) -> None:
        now = datetime(2026, 8, 21, 12, 5, tzinfo=UTC)
        profile = _profile(now, 0.25)
        profile[1].avg_house_consumption_1d_kwh = 0.11
        profile[1].avg_house_consumption_3d_kwh = 0.22
        profile[1].avg_house_consumption_7d_kwh = 0.33
        profile[1].avg_house_consumption_14d_kwh = 0.44
        result = assess_load_forecast(
            profile,
            now,
            population_succeeded=True,
            live_house_demand_w=1200.0,
        )
        assert result.signature is not None
        assert result.signature[-1][1:] == pytest.approx((0.25, 0.11, 0.22, 0.33, 0.44))
        assert load_forecast_signatures_match(result.signature, result.signature)


class TestLoadForecastHold:
    """Verify the automatic safety hold clears stale primary-storage intent."""

    def test_auto_clears_primary_motion_and_grid_flows(self) -> None:
        now = datetime(2026, 8, 21, 12, 5, tzinfo=UTC)
        current = _profile(now, 0.2)[0]
        current.recommendation = Recommendations.ForceBatteriesDischarge.value
        current.batteries_charged_kwh = 1.0
        current.batteries_discharged_kwh = 2.0
        current.grid_import_kwh = 3.0
        current.grid_export_kwh = 4.0
        current.estimated_cost_currency = -12.0
        current.estimated_net_consumption_kwh = 5.0
        current.ev_planned_load_kwh = 1.0
        current.ev_accounted_load_kwh = 2.0
        current.ev_total_planned_load_kwh = 3.0
        current.ev_charger_calculated_power = 7000.0
        current.ev_second_charger_calculated_power = 11000.0
        held = apply_load_forecast_hold(
            [current],
            LiveState(force_working_mode_state="auto"),
            now,
            load_forecast_ready=False,
        )
        assert held is current
        assert current.recommendation == Recommendations.BatteriesWaitMode.value
        assert current.batteries_charged_kwh == pytest.approx(0.0)
        assert current.batteries_discharged_kwh == pytest.approx(0.0)
        assert current.grid_import_kwh == pytest.approx(0.0)
        assert current.grid_export_kwh == pytest.approx(0.0)
        assert current.estimated_cost_currency == pytest.approx(0.0)
        assert current.estimated_net_consumption_kwh == pytest.approx(0.0)
        assert current.ev_planned_load_kwh == pytest.approx(0.0)
        assert current.ev_accounted_load_kwh == pytest.approx(0.0)
        assert current.ev_total_planned_load_kwh == pytest.approx(0.0)
        assert current.ev_charger_calculated_power == pytest.approx(0.0)
        assert current.ev_second_charger_calculated_power == pytest.approx(0.0)

    def test_manual_force_remains_higher_authority(self) -> None:
        now = datetime(2026, 8, 21, 12, 5, tzinfo=UTC)
        current = _profile(now, 0.2)[0]
        current.recommendation = Recommendations.ForceBatteriesDischarge.value
        held = apply_load_forecast_hold(
            [current],
            LiveState(
                force_working_mode_state=Recommendations.ForceBatteriesDischarge.value
            ),
            now,
            load_forecast_ready=False,
        )
        assert held is None
        assert current.recommendation == Recommendations.ForceBatteriesDischarge.value


def test_data_quality_reports_load_readiness() -> None:
    """Load readiness participates in aggregate data quality."""
    quality = DataQuality(
        load_forecast_ready=False,
        load_forecast_reason="source_unavailable",
    )
    assert quality.is_complete is False
    assert quality.as_dict()["load_forecast_ready"] is False
    assert quality.as_dict()["load_forecast_reason"] == "source_unavailable"


def test_recovery_replans_until_accepted_signature_is_persisted() -> None:
    """Only an accepted plan clears the durable recovery requirement."""
    now = datetime(2026, 8, 21, 12, 5, tzinfo=UTC)
    readiness = assess_load_forecast(
        _profile(now, 0.2),
        now,
        population_succeeded=True,
        live_house_demand_w=1200.0,
    )
    assert readiness.signature is not None
    coordinator = object.__new__(HSEMDataUpdateCoordinator)
    coordinator._cfg = SensorConfig()
    coordinator._last_planner_output = PlannerOutput()
    coordinator._last_plan_slot_start = now
    coordinator._last_plan_load_forecast_signature = None
    coordinator._load_forecast_recovery_replan_pending = True
    live = LiveState()

    assert coordinator._should_replan(
        live, now, load_forecast_signature=readiness.signature
    )
    assert coordinator._load_forecast_recovery_replan_pending is True

    coordinator._persist_plan_state(live, load_forecast_signature=readiness.signature)
    assert coordinator._load_forecast_recovery_replan_pending is False
    assert load_forecast_signatures_match(
        coordinator._last_plan_load_forecast_signature,
        readiness.signature,
    )
    assert not coordinator._should_replan(
        live, now, load_forecast_signature=readiness.signature
    )


@pytest.mark.asyncio
async def test_state_event_received_in_flight_triggers_follow_up_cycle() -> None:
    """A state event received while locked is deferred rather than dropped."""
    coordinator = object.__new__(HSEMDataUpdateCoordinator)
    coordinator._update_lock = asyncio.Lock()
    coordinator._update_generation = 0
    coordinator._event_update_pending = False
    calls = 0

    async def run_cycle() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            await coordinator._async_handle_update(object())  # type: ignore[arg-type]

    coordinator._async_run_update_cycle = AsyncMock(side_effect=run_cycle)  # type: ignore[method-assign]
    await coordinator._async_handle_update()

    assert calls == 2
    assert coordinator._update_generation == 1
    assert coordinator._event_update_pending is False


def _avg_sensor() -> HSEMAvgSensor:
    """Return an average sensor with its HA dependencies mocked."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.options = {}
    entry.data = {}
    sensor = HSEMAvgSensor(
        config_entry=entry,
        hour_start=10,
        hour_end=11,
        avg=7,
        tracked_entity="sensor.utility",
        name="Test average",
        unique_id="test_average",
        entity_id="sensor.test_average",
    )
    sensor.hass = MagicMock()
    return sensor


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("measurements", "expected"),
    [({}, None), ({"2026-08-20": 0.0}, 0.0)],
)
async def test_average_distinguishes_empty_from_measured_zero(
    measurements: dict[str, float], expected: float | None
) -> None:
    """Empty history is unavailable while a measured finite zero is valid."""
    sensor = _avg_sensor()
    sensor._measurements = measurements
    sensor._state = 5.0
    sensor._async_track_entities = AsyncMock()  # type: ignore[method-assign]
    sensor._async_store_utility_meter_value = AsyncMock()  # type: ignore[method-assign]
    sensor.async_write_ha_state = MagicMock()  # type: ignore[method-assign,misc]
    with patch(
        "custom_components.hsem.custom_sensors.avg_sensor.dt_util.now",
        return_value=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
    ):
        await sensor._async_handle_update()
    if expected is None:
        assert sensor.state is None
    else:
        assert sensor.state == pytest.approx(expected)
    assert sensor.available is (expected is not None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("restored", "expected"),
    [
        ("unavailable", None),
        ("unknown", None),
        ("nan", None),
        ("inf", None),
        ("0", 0.0),
    ],
)
async def test_average_restore_accepts_only_finite_state(
    restored: str, expected: float | None
) -> None:
    """Restore must reject HA sentinels and non-finite numeric strings."""
    sensor = _avg_sensor()
    sensor.async_get_last_state = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(state=restored, attributes={})
    )
    sensor._async_handle_update = AsyncMock()  # type: ignore[method-assign]
    with (
        patch(
            "custom_components.hsem.custom_sensors.avg_sensor.async_track_time_interval",
            return_value=MagicMock(),
        ),
        patch.object(RestoreEntity, "async_added_to_hass", new_callable=AsyncMock),
    ):
        await sensor.async_added_to_hass()
    if expected is None:
        assert sensor.state is None
    else:
        assert sensor.state == pytest.approx(expected)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_value", "expected_count"),
    [(None, 0), (float("nan"), 0), (0.0, 96)],
)
async def test_snapshot_preserves_average_availability(
    raw_value: float | None, expected_count: int
) -> None:
    """Snapshot collection must omit missing averages but retain real zero."""
    sensor = MagicMock()
    sensor.hass.states.get.return_value = None

    async def resolve(_sensor: object, _cache: dict[str, str], unique_id: str) -> str:
        return f"sensor.{unique_id}"

    with (
        patch(
            "custom_components.hsem.custom_sensors.state_collector.async_collect_live_state",
            new=AsyncMock(return_value=(LiveState(), None, [])),
        ),
        patch(
            "custom_components.hsem.custom_sensors.state_collector._resolve_cached",
            new=resolve,
        ),
        patch(
            "custom_components.hsem.custom_sensors.state_collector.ha_get_entity_state_and_convert",
            return_value=raw_value,
        ),
    ):
        snapshot, _force, _unsubs = await async_collect_all_states(
            sensor, SensorConfig(), None, set(), {}, entry_id="test_entry"
        )
    assert len(snapshot.energy_average_values) == expected_count
    if expected_count:
        for value in snapshot.energy_average_values.values():
            assert value == pytest.approx(0.0)
