"""ML mode keeps the rolling window averages apart from the forecast (issue #1485).

With the ML consumption forecast on, ``sensor.hsem_workingmode_sensor`` showed
the same value in ``avg_house_consumption_kwh`` and in all four
``avg_house_consumption_{1,3,7,14}d_kwh`` fields, and the five consumption
curves of the generated dashboard lay on top of each other. The ML populator
wrote its forecast into all five, because the planner blended the four
windows and four equal values blend to that value.

The forecast now has its own way to the planner. These tests follow the data
from the real producers (the rolling-average populator and the ML populator)
through the planner input into a solved plan.
"""

from __future__ import annotations

from collections.abc import Iterator
from copy import deepcopy
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from custom_components.hsem.coordinator_builder import (
    build_planner_input,
    generate_recommendation_intervals,
)
from custom_components.hsem.coordinator_helpers import assess_load_forecast
from custom_components.hsem.custom_sensors.hourly_data_populator.consumption import (
    populate_avg_house_consumption_from_snapshot,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.models.state_snapshot import StateSnapshot
from custom_components.hsem.planner.engine_core import run_planner
from custom_components.hsem.utils.sensornames.energy import (
    get_energy_average_sensor_unique_id,
)
from tests.ml.test_populator_time_alignment import (
    _cfg,
    _FakeHass,
    _FakeReader,
    _history,
    _populate,
    _reset_ml_caches,
)

_NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
_ENTRY_ID = "entry_1485"
#: The fake predictor of the ML tests forecasts this for every slot.
_ML_SLOT_KWH = 0.5
#: A 15 kWh battery at 20 %, so the evening price peak is worth charging for.
_LIVE = LiveState(
    huawei_batteries_soc_pct=20.0,
    huawei_batteries_rated_capacity_wh=15000,
    huawei_batteries_end_of_discharge_soc_pct=5.0,
    huawei_batteries_charging_cutoff_capacity_pct=100.0,
    huawei_batteries_max_charge_power_w=5000.0,
    battery_usable_capacity_kwh=14.25,
)


def _hour_windows(hour: int) -> tuple[float, float, float, float]:
    """Return four different hourly averages (1d, 3d, 7d, 14d) for *hour*."""
    step = hour * 0.04
    return 1.68 + step, 1.56 + step, 1.44 + step, 1.32 + step


@pytest.fixture(autouse=True)
def _clear_ml_caches() -> Iterator[None]:
    _reset_ml_caches()
    yield
    _reset_ml_caches()


def _config() -> SensorConfig:
    cfg = _cfg()
    cfg.recommendation_interval_length = 24
    cfg.house_consumption_energy_weight_1d = 25
    cfg.house_consumption_energy_weight_3d = 30
    cfg.house_consumption_energy_weight_7d = 30
    cfg.house_consumption_energy_weight_14d = 15
    return cfg


async def _populated_recommendations(cfg: SensorConfig) -> list[HourlyRecommendation]:
    """Run the rolling-average populator, then the ML populator, as a cycle does."""
    with patch("homeassistant.util.dt.now", return_value=_NOW):
        recs = generate_recommendation_intervals(15, 24)
    for rec in recs:
        rec.import_price = 2.5 if 17 <= rec.start.hour < 21 else 0.5
        rec.export_price = 0.1

    cache: dict[str, str] = {}
    values: dict[str, float] = {}
    for hour in range(24):
        for days, value in zip((1, 3, 7, 14), _hour_windows(hour), strict=True):
            uid = get_energy_average_sensor_unique_id(
                _ENTRY_ID, hour, (hour + 1) % 24, days
            )
            cache[uid] = f"sensor.energy_avg_{hour:02d}_{days}d"
            values[cache[uid]] = value
    averages = populate_avg_house_consumption_from_snapshot(
        recs,
        StateSnapshot(live=LiveState(), energy_average_values=values),
        cfg,
        cache,
        _ENTRY_ID,
    )
    assert averages.ok is True

    ml_ok, _predictor = await _populate(
        _FakeHass(),
        _FakeReader({"sensor.import": _history(_NOW)}),
        cfg,
        recs,
        now=_NOW,
    )
    assert ml_ok is True
    return recs


def _planner_input(
    cfg: SensorConfig, recs: list[HourlyRecommendation], *, direct: bool
) -> PlannerInput:
    with patch("homeassistant.util.dt.now", return_value=_NOW):
        return build_planner_input(
            cfg=cfg,
            live=_LIVE,
            hourly_recommendations=recs,
            previous_winner_name=None,
            previous_winner_score=0.0,
            direct_load_forecast=direct,
        )


@pytest.mark.asyncio
async def test_ml_forecast_leaves_the_window_averages_in_the_slots() -> None:
    """The reported symptom: five identical values in every slot."""
    recs = await _populated_recommendations(_config())

    assert len(recs) == 96
    for rec in recs:
        v1, v3, v7, v14 = _hour_windows(rec.start.hour)
        assert rec.avg_house_consumption_kwh == pytest.approx(_ML_SLOT_KWH)
        assert rec.avg_house_consumption_1d_kwh == pytest.approx(v1 / 4)
        assert rec.avg_house_consumption_3d_kwh == pytest.approx(v3 / 4)
        assert rec.avg_house_consumption_7d_kwh == pytest.approx(v7 / 4)
        assert rec.avg_house_consumption_14d_kwh == pytest.approx(v14 / 4)

    readiness = assess_load_forecast(
        recs,
        _NOW,
        population_succeeded=True,
        live_house_demand_w=400.0,
        direct_forecast=True,
    )
    assert readiness.ready is True


@pytest.mark.asyncio
async def test_planner_plans_with_the_ml_forecast_not_the_window_blend() -> None:
    cfg = _config()
    recs = await _populated_recommendations(cfg)

    inp = _planner_input(cfg, recs, direct=True)

    assert len(inp.consumption_averages) == 24
    assert len(inp.load_forecast_slots) == 96
    assert [entry.slot_in_day for entry in inp.load_forecast_slots] == list(range(96))
    assert [entry.load_kwh for entry in inp.load_forecast_slots] == pytest.approx(
        [_ML_SLOT_KWH] * 96
    )
    for average in inp.consumption_averages:
        v1, v3, v7, v14 = _hour_windows(average.hour)
        assert average.avg_1d == pytest.approx(v1)
        assert average.avg_3d == pytest.approx(v3)
        assert average.avg_7d == pytest.approx(v7)
        assert average.avg_14d == pytest.approx(v14)

    out = run_planner(inp)

    assert len(out.slots) == 96
    for slot, rec in zip(out.slots, recs, strict=True):
        assert slot.avg_house_consumption_kwh == pytest.approx(_ML_SLOT_KWH)
        assert slot.avg_house_consumption_1d_kwh == pytest.approx(
            rec.avg_house_consumption_1d_kwh
        )
        assert slot.avg_house_consumption_14d_kwh == pytest.approx(
            rec.avg_house_consumption_14d_kwh
        )


@pytest.mark.asyncio
async def test_plan_is_the_one_solved_before_the_fix() -> None:
    """Carrying the forecast in its own field must not change the plan.

    Before the fix the forecast sat in all four windows and the planner
    blended them. For a forecast that is flat within each hour, as here,
    both ways must give the same load, energy flows and cost.
    """
    cfg = _config()
    recs = await _populated_recommendations(cfg)
    legacy_recs = deepcopy(recs)
    for rec in legacy_recs:
        rec.avg_house_consumption_1d_kwh = rec.avg_house_consumption_kwh
        rec.avg_house_consumption_3d_kwh = rec.avg_house_consumption_kwh
        rec.avg_house_consumption_7d_kwh = rec.avg_house_consumption_kwh
        rec.avg_house_consumption_14d_kwh = rec.avg_house_consumption_kwh
    legacy_inp = _planner_input(cfg, legacy_recs, direct=False)
    assert legacy_inp.load_forecast_slots == []

    out = run_planner(_planner_input(cfg, recs, direct=True))
    legacy_out = run_planner(legacy_inp)

    assert out.plan_cost is not None
    assert legacy_out.plan_cost is not None
    assert out.plan_cost.total_cost == pytest.approx(legacy_out.plan_cost.total_cost)
    assert [s.recommendation for s in out.slots] == [
        s.recommendation for s in legacy_out.slots
    ]
    for field in (
        "avg_house_consumption_kwh",
        "estimated_net_consumption_kwh",
        "batteries_charged_kwh",
        "batteries_discharged_kwh",
        "grid_import_kwh",
        "grid_export_kwh",
        "estimated_battery_soc_pct",
    ):
        assert [getattr(s, field) for s in out.slots] == pytest.approx(
            [getattr(s, field) for s in legacy_out.slots]
        ), field
    # The battery did something, so the comparison is not of two idle plans.
    assert sum(s.batteries_charged_kwh for s in out.slots) > 0.0
