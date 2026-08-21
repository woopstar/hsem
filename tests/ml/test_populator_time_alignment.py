"""Regression tests for ML populator source, cache, and physical-time safety."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from homeassistant.core import HomeAssistant

from custom_components.hsem.ml import populator
from custom_components.hsem.ml.consumption_predictor import ConsumptionPredictor
from custom_components.hsem.ml.history_reader import HistoryReader
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.datetime_utils import slot_key, utc_key

STOCKHOLM = ZoneInfo("Europe/Stockholm")
NOW = datetime(2026, 8, 20, 12, 0, tzinfo=STOCKHOLM)

type _HistorySample = tuple[datetime, int, float]
type _TrainingContext = tuple[str, str | None, bool, int, int, str | None]


class _FakeHass:
    async def async_add_executor_job(
        self,
        target: Callable[..., Any],
        *args: Any,
    ) -> Any:
        return target(*args)


class _FakeReader:
    def __init__(
        self,
        histories: dict[str, list[_HistorySample]],
        *,
        actuals: dict[str, dict[datetime, float]] | None = None,
        temperatures: dict[str, list[tuple[datetime, float]]] | None = None,
    ) -> None:
        self.histories = histories
        self.actuals = actuals or {}
        self.temperatures = temperatures or {}
        self.energy_calls: list[str] = []
        self.actual_calls: list[str] = []
        self.temperature_calls: list[str] = []

    async def read_energy_history(
        self,
        entity_id: str,
        **_kwargs: object,
    ) -> list[_HistorySample]:
        self.energy_calls.append(entity_id)
        return list(self.histories.get(entity_id, []))

    async def read_today_actuals(
        self,
        entity_id: str,
        **_kwargs: object,
    ) -> dict[datetime, float]:
        self.actual_calls.append(entity_id)
        return dict(self.actuals.get(entity_id, {}))

    async def read_instantaneous_history(
        self,
        entity_id: str,
        **_kwargs: object,
    ) -> list[tuple[datetime, float]]:
        self.temperature_calls.append(entity_id)
        return list(self.temperatures.get(entity_id, []))


class _FakePredictor:
    def __init__(
        self,
        decay_days: float = 7.0,
        alpha: float = 1.0,
        slots_per_day: int = 96,
        retrain_min_new_samples: int = 4,
        use_temperature: bool = False,
        use_sequential: bool = False,
        *,
        trained: bool = False,
        remain_untrained: bool = False,
    ) -> None:
        del alpha, retrain_min_new_samples
        self.decay_days = decay_days
        self.slots_per_day = slots_per_day
        self.use_temperature = use_temperature
        self.use_sequential = use_sequential
        self.trained = trained
        self.remain_untrained = remain_untrained
        self.training_context: _TrainingContext | None = None
        self.actual_history_days = 0.0
        self.last_fit_samples = 0
        self.group_count = 0
        self.last_fit_time: datetime | None = NOW if trained else None
        self._raw_groups: dict[tuple[int, int], list[tuple[float, float]]] = {}
        self.training_histories: list[list[_HistorySample]] = []
        self.training_temperatures: list[dict[datetime, float] | None] = []
        self.prediction_temperatures: list[float | None] = []
        self.prediction_requests: list[tuple[int, int]] = []
        self.sequential_requests: list[list[datetime]] = []
        self.sequential_temperature_requests: list[dict[datetime, float] | None] = []

    def train(
        self,
        history: list[_HistorySample],
        reference_time: datetime,
        temperatures: dict[datetime, float] | None,
    ) -> None:
        self.training_histories.append(list(history))
        self.training_temperatures.append(
            dict(temperatures) if temperatures is not None else None
        )
        if self.remain_untrained:
            return
        self.trained = True
        self.last_fit_samples = len(history)
        self.group_count = len(
            {(timestamp.weekday(), slot) for timestamp, slot, _ in history}
        )
        self.last_fit_time = reference_time

    def predict_with_std(
        self,
        _slot: int,
        _day_offset: int,
        _reference_time: datetime,
        temperature: float | None,
    ) -> tuple[float, float]:
        self.prediction_temperatures.append(temperature)
        self.prediction_requests.append((_slot, _day_offset))
        return 0.5, 0.0

    def predict_sequential(
        self,
        slot_starts: list[datetime],
        temperatures: dict[datetime, float] | None,
    ) -> dict[datetime, float]:
        self.sequential_requests.append(list(slot_starts))
        self.sequential_temperature_requests.append(
            dict(temperatures) if temperatures is not None else None
        )
        return {
            utc_key(start): (index + 1) / 10 for index, start in enumerate(slot_starts)
        }

    @staticmethod
    def _weighted_std(_samples: list[tuple[float, float]]) -> float:
        return 0.0


@pytest.fixture(autouse=True)
def _ha_local_timezone():
    def as_stockholm(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=STOCKHOLM)
        return value.astimezone(STOCKHOLM)

    with patch(
        "custom_components.hsem.utils.datetime_utils.dt_util.as_local",
        side_effect=as_stockholm,
    ):
        yield


@pytest.fixture(autouse=True)
def _clear_ml_caches():
    populator._processed_history_cache.clear()
    populator._temperature_history_cache.clear()
    yield
    populator._processed_history_cache.clear()
    populator._temperature_history_cache.clear()


def _history(now: datetime, base: float = 1.0) -> list[_HistorySample]:
    return [
        (now - timedelta(days=15), 0, base),
        (now - timedelta(days=14, minutes=15), 1, base + 0.2),
    ]


def _cfg(
    *,
    energy_entity: str = "sensor.import",
    export_entity: str | None = None,
    net: bool = False,
    temperature_entity: str | None = None,
    history_days: int = 14,
    sequential: bool = False,
) -> SensorConfig:
    cfg = SensorConfig()
    cfg.recommendation_interval_minutes = 15
    cfg.ml_consumption_energy_entity = energy_entity
    cfg.grid_export_energy_entity = export_entity
    cfg.ml_consumption_net_consumption = net
    cfg.ml_consumption_temperature_entity = temperature_entity
    cfg.ml_consumption_history_days = history_days
    cfg.ml_consumption_sequential = sequential
    return cfg


def _training_context(
    cfg: SensorConfig,
    *,
    use_temperature: bool,
) -> _TrainingContext:
    energy_entity = cfg.ml_consumption_energy_entity or cfg.grid_import_energy_entity
    assert energy_entity is not None
    return (
        energy_entity,
        cfg.grid_export_energy_entity if cfg.ml_consumption_net_consumption else None,
        cfg.ml_consumption_net_consumption,
        cfg.recommendation_interval_minutes,
        cfg.ml_consumption_history_days,
        cfg.ml_consumption_temperature_entity if use_temperature else None,
    )


def _recommendation(start: datetime, consumption: float = 9.9) -> HourlyRecommendation:
    return HourlyRecommendation(
        start=start,
        end=start + timedelta(minutes=15),
        avg_house_consumption_kwh=consumption,
        avg_house_consumption_1d_kwh=consumption,
        avg_house_consumption_3d_kwh=consumption,
        avg_house_consumption_7d_kwh=consumption,
        avg_house_consumption_14d_kwh=consumption,
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
        recommendation=None,
        solcast_pv_estimate_kwh=0.0,
    )


async def _populate(
    hass: _FakeHass,
    reader: _FakeReader,
    cfg: SensorConfig,
    recommendations: list[HourlyRecommendation],
    *,
    now: datetime = NOW,
    predictor: _FakePredictor | None = None,
) -> tuple[bool, _FakePredictor | None]:
    with (
        patch.object(populator, "HistoryReader", return_value=reader),
        patch.object(populator, "ConsumptionPredictor", _FakePredictor),
        patch.object(populator, "hsem_now", return_value=now),
    ):
        result = await populator.populate_ml_house_consumption(
            cast(HomeAssistant, hass),
            recommendations,
            cfg,
            cast(ConsumptionPredictor | None, predictor),
        )
    return cast(tuple[bool, _FakePredictor | None], result)


@pytest.mark.asyncio
async def test_processed_cache_is_config_keyed_and_predictor_resets_with_source() -> (
    None
):
    hass = _FakeHass()
    reader = _FakeReader(
        {
            "sensor.import": [
                *_history(NOW, 1.0),
                (NOW - timedelta(days=1), 2, 3.0),
            ],
            "sensor.export": _history(NOW, 0.2),
            "sensor.other": _history(NOW, 2.0),
        }
    )
    net_cfg = _cfg(export_entity="sensor.export", net=True)

    success, net_predictor = await _populate(hass, reader, net_cfg, [])

    assert success is True
    assert net_predictor is not None
    assert [sample[2] for sample in net_predictor.training_histories[-1]] == [
        0.8,
        0.8,
    ]
    assert reader.energy_calls == ["sensor.import", "sensor.export"]

    success, cached_predictor = await _populate(
        hass,
        reader,
        net_cfg,
        [],
        predictor=net_predictor,
    )

    assert success is True
    assert cached_predictor is net_predictor
    assert cached_predictor is not None
    assert [sample[2] for sample in cached_predictor.training_histories[-1]] == [
        0.8,
        0.8,
    ]
    assert reader.energy_calls == ["sensor.import", "sensor.export"]

    gross_cfg = _cfg()
    success, gross_predictor = await _populate(
        hass,
        reader,
        gross_cfg,
        [],
        predictor=net_predictor,
    )

    assert success is True
    assert gross_predictor is not None
    assert gross_predictor is not net_predictor
    assert [sample[2] for sample in gross_predictor.training_histories[-1]] == [
        1.0,
        1.2,
        3.0,
    ]
    assert reader.energy_calls == [
        "sensor.import",
        "sensor.export",
        "sensor.import",
    ]

    other_cfg = _cfg(energy_entity="sensor.other")
    success, other_predictor = await _populate(
        hass,
        reader,
        other_cfg,
        [],
        predictor=gross_predictor,
    )

    assert success is True
    assert other_predictor is not None
    assert other_predictor is not gross_predictor
    assert [sample[2] for sample in other_predictor.training_histories[-1]] == [
        2.0,
        2.2,
    ]
    assert reader.energy_calls[-1] == "sensor.other"


@pytest.mark.asyncio
async def test_net_mode_fails_closed_when_export_history_is_unavailable() -> None:
    cfg = _cfg(export_entity="sensor.export", net=True)
    reader = _FakeReader({"sensor.import": _history(NOW)})
    recommendation = _recommendation(NOW + timedelta(minutes=15))

    success, predictor = await _populate(
        _FakeHass(),
        reader,
        cfg,
        [recommendation],
    )

    assert success is False
    assert predictor is None
    assert recommendation.avg_house_consumption_kwh == 9.9
    assert reader.energy_calls == ["sensor.import", "sensor.export"]
    assert reader.actual_calls == []


@pytest.mark.asyncio
async def test_net_mode_fails_closed_without_export_entity_configuration() -> None:
    cfg = _cfg(net=True)
    reader = _FakeReader({"sensor.import": _history(NOW)})

    success, predictor = await _populate(_FakeHass(), reader, cfg, [])

    assert success is False
    assert predictor is None
    assert reader.energy_calls == []


@pytest.mark.asyncio
async def test_temperature_history_is_used_for_training_and_inference() -> None:
    temperature_history = [
        (NOW - timedelta(days=15), 4.0),
        (NOW - timedelta(minutes=5), 11.5),
    ]
    reader = _FakeReader(
        {"sensor.import": _history(NOW)},
        temperatures={"sensor.temperature": temperature_history},
    )
    cfg = _cfg(temperature_entity="sensor.temperature")
    recommendation = _recommendation(NOW + timedelta(minutes=15))

    success, predictor = await _populate(
        _FakeHass(),
        reader,
        cfg,
        [recommendation],
    )

    assert success is True
    assert predictor is not None
    assert predictor.use_temperature is True
    assert predictor.training_temperatures[-1] == {
        utc_key(timestamp): value for timestamp, value in temperature_history
    }
    assert predictor.prediction_temperatures == [11.5]
    assert recommendation.avg_house_consumption_kwh == 0.5


@pytest.mark.asyncio
async def test_missing_temperature_history_disables_feature_safely() -> None:
    reader = _FakeReader({"sensor.import": _history(NOW)})
    cfg = _cfg(temperature_entity="sensor.temperature")
    recommendation = _recommendation(NOW + timedelta(minutes=15))

    success, predictor = await _populate(
        _FakeHass(),
        reader,
        cfg,
        [recommendation],
    )

    assert success is True
    assert predictor is not None
    assert predictor.use_temperature is False
    assert predictor.training_temperatures[-1] is None
    assert predictor.prediction_temperatures == [None]


@pytest.mark.asyncio
async def test_untrained_predictor_falls_back_without_mutating_recommendations() -> (
    None
):
    cfg = _cfg()
    reader = _FakeReader({"sensor.import": _history(NOW)})
    predictor = _FakePredictor(remain_untrained=True)
    predictor.training_context = _training_context(cfg, use_temperature=False)
    recommendation = _recommendation(NOW + timedelta(minutes=15))

    success, returned_predictor = await _populate(
        _FakeHass(),
        reader,
        cfg,
        [recommendation],
        predictor=predictor,
    )

    assert success is False
    assert returned_predictor is None
    assert recommendation.avg_house_consumption_kwh == 9.9
    assert len(predictor.training_histories) == 1
    assert reader.actual_calls == []


@pytest.mark.asyncio
async def test_today_actuals_match_each_autumn_fold_by_physical_slot() -> None:
    now = datetime(2026, 10, 25, 3, 0, tzinfo=STOCKHOLM)
    fold_zero = datetime(2026, 10, 25, 2, 0, tzinfo=STOCKHOLM, fold=0)
    fold_one = datetime(2026, 10, 25, 2, 0, tzinfo=STOCKHOLM, fold=1)
    cfg = _cfg()
    reader = _FakeReader(
        {"sensor.import": _history(now)},
        actuals={
            "sensor.import": {
                slot_key(fold_zero, 15): 0.4,
                slot_key(fold_one, 15): 0.9,
            }
        },
    )

    recommendations = [_recommendation(fold_zero), _recommendation(fold_one)]
    success, _predictor = await _populate(
        _FakeHass(),
        reader,
        cfg,
        recommendations,
        now=now,
    )

    assert success is True
    assert fold_zero.utcoffset() != fold_one.utcoffset()
    assert [
        recommendation.avg_house_consumption_kwh for recommendation in recommendations
    ] == [0.4, 0.9]


@pytest.mark.asyncio
async def test_cache_expires_by_physical_time_across_autumn_fold() -> None:
    fold_zero_now = datetime(2026, 10, 25, 2, 45, tzinfo=STOCKHOLM, fold=0)
    fold_one_now = datetime(2026, 10, 25, 2, 45, tzinfo=STOCKHOLM, fold=1)
    cfg = _cfg()
    reader = _FakeReader({"sensor.import": _history(fold_zero_now)})
    hass = _FakeHass()

    success, predictor = await _populate(
        hass,
        reader,
        cfg,
        [],
        now=fold_zero_now,
    )
    assert success is True
    assert predictor is not None

    success, returned_predictor = await _populate(
        hass,
        reader,
        cfg,
        [],
        now=fold_one_now,
        predictor=predictor,
    )

    assert success is True
    assert returned_predictor is predictor
    assert reader.energy_calls == ["sensor.import", "sensor.import"]


def test_predictor_uses_physical_age_and_temperature_identity_across_fold() -> None:
    fold_zero = datetime(2026, 10, 25, 2, 0, tzinfo=STOCKHOLM, fold=0)
    fold_one = datetime(2026, 10, 25, 2, 0, tzinfo=STOCKHOLM, fold=1)
    reference = datetime(2026, 10, 25, 3, 0, tzinfo=STOCKHOLM)
    temperatures = {utc_key(fold_zero): 10.0, utc_key(fold_one): 20.0}
    assert len(temperatures) == 2
    predictor = ConsumptionPredictor(
        retrain_min_new_samples=1,
        use_temperature=True,
    )

    assert predictor._lookup_temperature(temperatures, fold_one) == 20.0

    predictor.train(
        [(fold_zero, 8, 1.0), (fold_one, 8, 1.1)],
        reference,
        temperatures,
    )

    ages = [age for age, _energy in predictor._raw_groups[(6, 8)]]
    assert ages == pytest.approx([2 / 24, 1 / 24])
    assert predictor.last_fit_time == reference


@pytest.mark.asyncio
async def test_partially_misaligned_net_history_falls_back_and_is_not_cached() -> None:
    import_history = _history(NOW)
    export_history = [
        (import_history[0][0], import_history[0][1], 0.2),
        (NOW - timedelta(days=13), 3, 0.3),
    ]
    cfg = _cfg(export_entity="sensor.export", net=True)
    reader = _FakeReader(
        {
            "sensor.import": import_history,
            "sensor.export": export_history,
        }
    )
    hass = _FakeHass()

    first_success, first_predictor = await _populate(hass, reader, cfg, [])
    second_success, second_predictor = await _populate(hass, reader, cfg, [])

    assert first_success is False
    assert first_predictor is None
    assert second_success is False
    assert second_predictor is None
    assert reader.energy_calls == [
        "sensor.import",
        "sensor.export",
        "sensor.import",
        "sensor.export",
    ]
    assert populator._processed_history_cache == {}


@pytest.mark.asyncio
async def test_net_today_actuals_require_both_physical_meter_slots() -> None:
    missing_export_slot = NOW - timedelta(minutes=30)
    aligned_slot = NOW - timedelta(minutes=15)
    recommendations = [
        _recommendation(missing_export_slot),
        _recommendation(aligned_slot),
    ]
    cfg = _cfg(export_entity="sensor.export", net=True)
    reader = _FakeReader(
        {
            "sensor.import": _history(NOW, 1.0),
            "sensor.export": _history(NOW, 0.2),
        },
        actuals={
            "sensor.import": {
                slot_key(missing_export_slot, 15): 0.4,
                slot_key(aligned_slot, 15): 0.8,
            },
            "sensor.export": {
                slot_key(aligned_slot, 15): 0.2,
            },
        },
    )

    success, predictor = await _populate(
        _FakeHass(),
        reader,
        cfg,
        recommendations,
    )

    assert success is True
    assert predictor is not None
    assert [rec.avg_house_consumption_kwh for rec in recommendations] == [0.5, 0.6]
    assert reader.actual_calls == ["sensor.import", "sensor.export"]


@pytest.mark.asyncio
async def test_history_window_change_replaces_predictor_with_same_sample_count() -> (
    None
):
    hass = _FakeHass()
    reader = _FakeReader({"sensor.import": _history(NOW)})
    initial_cfg = _cfg(history_days=14)

    initial_success, initial_predictor = await _populate(
        hass,
        reader,
        initial_cfg,
        [],
    )
    assert initial_success is True
    assert initial_predictor is not None

    changed_cfg = _cfg(history_days=21)
    changed_success, changed_predictor = await _populate(
        hass,
        reader,
        changed_cfg,
        [],
        predictor=initial_predictor,
    )

    assert changed_success is True
    assert changed_predictor is not None
    assert changed_predictor is not initial_predictor
    assert reader.energy_calls == ["sensor.import", "sensor.import"]


@pytest.mark.asyncio
async def test_temperature_history_preserves_both_autumn_fold_keys() -> None:
    fold_zero = datetime(2026, 10, 25, 2, 0, tzinfo=STOCKHOLM, fold=0)
    fold_one = datetime(2026, 10, 25, 2, 0, tzinfo=STOCKHOLM, fold=1)
    reader = _FakeReader(
        {},
        temperatures={"sensor.temperature": [(fold_zero, 10.0), (fold_one, 20.0)]},
    )

    temperatures = await populator._read_temperature_history(
        cast(HistoryReader, reader),
        "sensor.temperature",
        14,
    )

    assert temperatures == {
        utc_key(fold_zero): 10.0,
        utc_key(fold_one): 20.0,
    }
    assert len(temperatures) == 2


@pytest.mark.asyncio
async def test_recommendation_calendar_features_are_ha_local_not_source_timezone() -> (
    None
):
    # 22:15 UTC is 00:15 on the next HA-local day during CEST.
    source_timestamp = datetime(2026, 8, 20, 22, 15, tzinfo=UTC)
    reader = _FakeReader({"sensor.import": _history(NOW)})

    success, predictor = await _populate(
        _FakeHass(),
        reader,
        _cfg(),
        [_recommendation(source_timestamp)],
    )

    assert success is True
    assert predictor is not None
    assert predictor.prediction_requests == [(1, 1)]


@pytest.mark.asyncio
async def test_sequential_predictions_follow_real_spring_slots() -> None:
    now = datetime(2026, 3, 29, 1, 30, tzinfo=STOCKHOLM)
    starts = [
        datetime(2026, 3, 29, 3, 15, tzinfo=STOCKHOLM),
        datetime(2026, 3, 29, 1, 45, tzinfo=STOCKHOLM),
        datetime(2026, 3, 29, 3, 0, tzinfo=STOCKHOLM),
    ]
    recommendations = [_recommendation(start) for start in starts]
    reader = _FakeReader({"sensor.import": _history(now)})

    success, predictor = await _populate(
        _FakeHass(),
        reader,
        _cfg(sequential=True),
        recommendations,
        now=now,
    )

    assert success is True
    assert predictor is not None
    request = predictor.sequential_requests[-1]
    assert [(start.hour, start.minute) for start in request] == [
        (1, 45),
        (3, 0),
        (3, 15),
    ]
    assert [utc_key(start) for start in request] == sorted(
        utc_key(start) for start in request
    )
    populated = {
        slot_key(rec.start, 15): rec.avg_house_consumption_kwh
        for rec in recommendations
    }
    assert populated == {
        slot_key(datetime(2026, 3, 29, 1, 45, tzinfo=STOCKHOLM), 15): 0.11,
        slot_key(datetime(2026, 3, 29, 3, 0, tzinfo=STOCKHOLM), 15): 0.22,
        slot_key(datetime(2026, 3, 29, 3, 15, tzinfo=STOCKHOLM), 15): 0.33,
    }


@pytest.mark.asyncio
async def test_sequential_predictions_keep_autumn_folds_distinct() -> None:
    now = datetime(2026, 10, 25, 1, 30, tzinfo=STOCKHOLM)
    first_fold = [
        datetime(2026, 10, 25, 2, minute, tzinfo=STOCKHOLM, fold=0)
        for minute in (0, 15, 30, 45)
    ]
    second_fold_start = datetime(2026, 10, 25, 2, 0, tzinfo=STOCKHOLM, fold=1)
    starts = [second_fold_start, *reversed(first_fold)]
    recommendations = [_recommendation(start) for start in starts]
    reader = _FakeReader({"sensor.import": _history(now)})

    success, predictor = await _populate(
        _FakeHass(),
        reader,
        _cfg(sequential=True),
        recommendations,
        now=now,
    )

    assert success is True
    assert predictor is not None
    request = predictor.sequential_requests[-1]
    assert [(start.hour, start.minute, start.fold) for start in request] == [
        (2, 0, 0),
        (2, 15, 0),
        (2, 30, 0),
        (2, 45, 0),
        (2, 0, 1),
    ]
    populated = {
        slot_key(rec.start, 15): rec.avg_house_consumption_kwh
        for rec in recommendations
    }
    assert len(populated) == 5
    assert populated[slot_key(first_fold[0], 15)] == 0.11
    assert populated[slot_key(second_fold_start, 15)] == 0.55


@pytest.mark.asyncio
async def test_nonfinite_temperature_history_disables_feature_safely() -> None:
    reader = _FakeReader(
        {"sensor.import": _history(NOW)},
        temperatures={
            "sensor.temperature": [
                (NOW - timedelta(days=15), float("nan")),
                (NOW - timedelta(minutes=5), float("inf")),
            ]
        },
    )

    success, predictor = await _populate(
        _FakeHass(),
        reader,
        _cfg(temperature_entity="sensor.temperature"),
        [_recommendation(NOW + timedelta(minutes=15))],
    )

    assert success is True
    assert predictor is not None
    assert predictor.use_temperature is False
    assert predictor.training_temperatures[-1] is None
    assert predictor.prediction_temperatures == [None]


@pytest.mark.asyncio
async def test_nonfinite_today_actual_is_replaced_by_finite_prediction() -> None:
    completed_slot = NOW - timedelta(minutes=15)
    reader = _FakeReader(
        {"sensor.import": _history(NOW)},
        actuals={"sensor.import": {slot_key(completed_slot, 15): float("nan")}},
    )
    recommendation = _recommendation(completed_slot)

    success, predictor = await _populate(_FakeHass(), reader, _cfg(), [recommendation])

    assert success is True
    assert predictor is not None
    assert recommendation.avg_house_consumption_kwh == 0.5
    assert reader.actual_calls == ["sensor.import"]


@pytest.mark.asyncio
async def test_reused_predictor_refreshes_decay_as_history_grows() -> None:
    hass = _FakeHass()
    reader = _FakeReader({"sensor.import": _history(NOW)})
    cfg = _cfg()

    first_success, predictor = await _populate(
        hass,
        reader,
        cfg,
        [],
        now=NOW,
    )

    assert first_success is True
    assert predictor is not None
    assert predictor.decay_days == pytest.approx(7.5)

    later = NOW + timedelta(minutes=61)
    reader.histories["sensor.import"] = [
        (later - timedelta(days=30), 0, 1.0),
        (later - timedelta(days=14), 1, 1.2),
    ]

    second_success, reused_predictor = await _populate(
        hass,
        reader,
        cfg,
        [],
        now=later,
        predictor=predictor,
    )

    assert second_success is True
    assert reused_predictor is predictor
    assert reused_predictor is not None
    assert reused_predictor.decay_days == pytest.approx(15.0)
    assert reused_predictor.actual_history_days == pytest.approx(30.0)
    assert reader.energy_calls == ["sensor.import", "sensor.import"]
