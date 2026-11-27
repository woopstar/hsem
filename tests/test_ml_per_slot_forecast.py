"""The ML forecast reaches the planner slot by slot (issue #1490).

With ML consumption on and 15-minute slots, the planner input carried one
load value per hour: the first quarter's forecast, scaled to the hour. The
planner spread it evenly, so all four quarters were planned with the first
quarter's value while ``sensor.hsem_workingmode_sensor`` showed each
quarter's own forecast. For the hour of the report (0.0937 / 0.1487 / 0.2 /
0.25 kWh) the planner used 0.375 kWh, the sensor showed 0.69 kWh.

These tests run the real ML populator and follow its values through
``build_planner_input`` into a solved plan.
"""

from __future__ import annotations

from collections.abc import Iterator
from copy import deepcopy
from datetime import UTC, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

import homeassistant.util.dt as dt_util

from custom_components.hsem.coordinator_builder import (
    build_planner_input,
    generate_recommendation_intervals,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.planner.engine_core import run_planner
from tests.ml.test_populator_time_alignment import (
    _cfg,
    _FakeHass,
    _FakePredictor,
    _FakeReader,
    _history,
    _populate,
    _reset_ml_caches,
)

_NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
#: The four quarter-hour forecasts of the issue's example hour.
_QUARTERS = (0.0937, 0.1487, 0.2, 0.25)
_LIVE = LiveState(
    huawei_batteries_soc_pct=20.0,
    huawei_batteries_rated_capacity_wh=15000,
    huawei_batteries_end_of_discharge_soc_pct=5.0,
    huawei_batteries_charging_cutoff_capacity_pct=100.0,
    huawei_batteries_max_charge_power_w=5000.0,
    battery_usable_capacity_kwh=14.25,
)


@pytest.fixture(autouse=True)
def _clear_ml_caches() -> Iterator[None]:
    _reset_ml_caches()
    yield
    _reset_ml_caches()


def _config(interval_minutes: int) -> SensorConfig:
    cfg = _cfg()
    cfg.recommendation_interval_minutes = interval_minutes
    cfg.recommendation_interval_length = 24
    cfg.house_consumption_energy_weight_1d = 25
    cfg.house_consumption_energy_weight_3d = 30
    cfg.house_consumption_energy_weight_7d = 30
    cfg.house_consumption_energy_weight_14d = 15
    return cfg


def _priced(recs: list[HourlyRecommendation]) -> list[HourlyRecommendation]:
    for rec in recs:
        rec.import_price = 2.5 if 17 <= rec.start.hour < 21 else 0.5
        rec.export_price = 0.1
    return recs


async def _ml_recommendations(
    cfg: SensorConfig, forecast_of_slot: tuple[float, ...]
) -> list[HourlyRecommendation]:
    """Fill a day of slots with the real ML populator.

    The predictor is the ML tests' fake, here returning
    ``forecast_of_slot[slot % len(forecast_of_slot)]`` for a slot of the day.
    """
    interval = cfg.recommendation_interval_minutes
    with patch("homeassistant.util.dt.now", return_value=_NOW):
        recs = _priced(generate_recommendation_intervals(interval, 24))

    def predict(
        _self: object, slot: int, *_args: object, **_kwargs: object
    ) -> tuple[float, float]:
        return forecast_of_slot[slot % len(forecast_of_slot)], 0.0

    with patch.object(_FakePredictor, "predict_with_std", predict):
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
    cfg: SensorConfig,
    recs: list[HourlyRecommendation],
    *,
    direct: bool,
    now: datetime = _NOW,
) -> PlannerInput:
    with patch("homeassistant.util.dt.now", return_value=now):
        return build_planner_input(
            cfg=cfg,
            live=_LIVE,
            hourly_recommendations=recs,
            previous_winner_name=None,
            previous_winner_score=0.0,
            direct_load_forecast=direct,
        )


@pytest.mark.asyncio
async def test_each_quarter_hour_is_planned_with_its_own_forecast() -> None:
    cfg = _config(15)
    recs = await _ml_recommendations(cfg, _QUARTERS)
    assert [rec.avg_house_consumption_kwh for rec in recs[:4]] == pytest.approx(
        _QUARTERS
    )

    inp = _planner_input(cfg, recs, direct=True)

    assert [(e.day_offset, e.slot_in_day) for e in inp.load_forecast_slots] == [
        (0, index) for index in range(96)
    ]
    assert [e.load_kwh for e in inp.load_forecast_slots] == pytest.approx(
        [rec.avg_house_consumption_kwh for rec in recs]
    )
    assert [e.hour for e in inp.load_forecast_slots] == [r.start.hour for r in recs]

    out = run_planner(inp)

    assert len(out.slots) == 96
    # What the sensor shows is what the planner used, to the planner's 1 Wh.
    assert [s.avg_house_consumption_kwh for s in out.slots] == pytest.approx(
        [rec.avg_house_consumption_kwh for rec in recs], abs=5e-4
    )
    # The hour is planned with all four quarters (0.69 kWh), not with four
    # times the first one (0.375 kWh).
    for hour in range(24):
        planned = sum(
            s.avg_house_consumption_kwh for s in out.slots if s.start.hour == hour
        )
        assert planned == pytest.approx(sum(_QUARTERS), abs=2e-3)


@pytest.mark.asyncio
async def test_hourly_slots_plan_as_before() -> None:
    """At 60-minute slots a slot is an hour, so the plan must not move.

    The reference is the input as it was built before issue #1485: the
    forecast in all four windows, blended by the planner.
    """
    cfg = _config(60)
    hourly = tuple(0.3 + 0.05 * hour for hour in range(24))
    recs = await _ml_recommendations(cfg, hourly)
    assert [rec.avg_house_consumption_kwh for rec in recs] == pytest.approx(hourly)
    legacy_recs = deepcopy(recs)
    for rec in legacy_recs:
        rec.avg_house_consumption_1d_kwh = rec.avg_house_consumption_kwh
        rec.avg_house_consumption_3d_kwh = rec.avg_house_consumption_kwh
        rec.avg_house_consumption_7d_kwh = rec.avg_house_consumption_kwh
        rec.avg_house_consumption_14d_kwh = rec.avg_house_consumption_kwh

    out = run_planner(_planner_input(cfg, recs, direct=True))
    legacy_out = run_planner(_planner_input(cfg, legacy_recs, direct=False))

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
    assert sum(s.batteries_charged_kwh for s in out.slots) > 0.0


def test_window_blend_cycle_carries_no_per_slot_forecast() -> None:
    """Legacy mode keeps its planner input: one entry per hour, nothing else."""
    cfg = _config(15)
    with patch("homeassistant.util.dt.now", return_value=_NOW):
        recs = _priced(generate_recommendation_intervals(15, 24))
    for rec in recs:
        rec.avg_house_consumption_kwh = 0.2

    inp = _planner_input(cfg, recs, direct=False)

    assert inp.load_forecast_slots == []
    assert len(inp.consumption_averages) == 24


class TestDstFallBack:
    """Both occurrences of the repeated hour keep their own forecast."""

    _ZONE = ZoneInfo("Europe/Copenhagen")
    _DAY_START = datetime(2026, 10, 25, 0, 30, tzinfo=_ZONE)

    @pytest.fixture(autouse=True)
    def _copenhagen(self) -> Iterator[None]:
        previous = dt_util.get_default_time_zone()
        dt_util.set_default_time_zone(self._ZONE)
        try:
            yield
        finally:
            dt_util.set_default_time_zone(previous)

    def test_repeated_hour_is_not_merged(self) -> None:
        cfg = _config(15)
        with patch("homeassistant.util.dt.now", return_value=self._DAY_START):
            recs = _priced(generate_recommendation_intervals(15, 24))
        assert len(recs) == 100  # 25 real hours
        for index, rec in enumerate(recs):
            rec.avg_house_consumption_kwh = round(0.1 + index * 0.002, 4)

        inp = _planner_input(cfg, recs, direct=True, now=self._DAY_START)

        assert [e.slot_in_day for e in inp.load_forecast_slots] == list(range(100))
        repeated = [e for e in inp.load_forecast_slots if e.hour == 2]
        assert len(repeated) == 8
        assert len({e.load_kwh for e in repeated}) == 8

        out = run_planner(inp)

        by_utc = {s.start.astimezone(UTC): s for s in out.slots}
        assert len(by_utc) == 100
        for rec in recs:
            planned = by_utc[rec.start.astimezone(UTC)]
            assert planned.avg_house_consumption_kwh == pytest.approx(
                rec.avg_house_consumption_kwh, abs=5e-4
            )
