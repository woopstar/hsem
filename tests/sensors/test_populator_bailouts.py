"""Tests for the snapshot populators' bail-out and bad-data paths.

Both populators run at the very start of a coordinator cycle, before HSEM's
own average sensors necessarily exist and before any external price sensor is
guaranteed to publish a usable array.  Every branch here decides whether the
planner gets to run on this cycle at all, so each one has to be explicit
rather than silently planning on zeros.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest

from custom_components.hsem.custom_sensors.hourly_data_populator.consumption import (
    MAX_ESTIMATED_LOAD_HOURS,
    estimate_missing_hours,
    populate_avg_house_consumption_from_snapshot,
)
from custom_components.hsem.custom_sensors.hourly_data_populator.prices_solcast import (
    _detect_interval_minutes,
    _populate_from_attributes,
    populate_price_and_solcast_from_snapshot,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.models.state_snapshot import StateSnapshot
from custom_components.hsem.utils.sensornames.energy import (
    get_energy_average_sensor_unique_id,
)

_PRICES_MODULE = (
    "custom_components.hsem.custom_sensors.hourly_data_populator.prices_solcast"
)
_ENTRY_ID = "test_entry_id"
_BASE = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)


def _recs(count: int = 24, *, minutes: int = 60) -> list[HourlyRecommendation]:
    """Return *count* zeroed recommendation slots of *minutes* each."""
    zero = 0.0
    width = timedelta(minutes=minutes)
    return [
        HourlyRecommendation(
            start=_BASE + i * width,
            end=_BASE + (i + 1) * width,
            recommendation="idle",
            avg_house_consumption_kwh=zero,
            avg_house_consumption_1d_kwh=zero,
            avg_house_consumption_3d_kwh=zero,
            avg_house_consumption_7d_kwh=zero,
            avg_house_consumption_14d_kwh=zero,
            batteries_charged_kwh=zero,
            batteries_discharged_kwh=zero,
            estimated_battery_capacity_kwh=zero,
            estimated_battery_soc_pct=zero,
            estimated_cost_currency=zero,
            estimated_net_consumption_kwh=zero,
            export_price=zero,
            grid_export_kwh=zero,
            grid_import_kwh=zero,
            import_price=zero,
            solcast_pv_estimate_kwh=zero,
        )
        for i in range(count)
    ]


def _cfg(**weights: int | None) -> SensorConfig:
    """Return a config with the four consumption weights applied.

    A weight may be ``None`` to model a config entry saved before that option
    existed — the field itself is typed ``int``.
    """
    cfg = SensorConfig()
    cfg.recommendation_interval_minutes = 60
    for field, default in (
        ("house_consumption_energy_weight_1d", 25),
        ("house_consumption_energy_weight_3d", 30),
        ("house_consumption_energy_weight_7d", 30),
        ("house_consumption_energy_weight_14d", 15),
    ):
        key = f"w{field.rsplit('_', 1)[-1].removesuffix('d')}"
        setattr(cfg, field, weights.get(key, default))
    return cfg


def _cache_and_values(
    *, hours: range = range(24), value: float | None = 1.0
) -> tuple[dict[str, str], dict[str, float]]:
    """Return a unique-id→entity-id cache and the matching snapshot values."""
    cache: dict[str, str] = {}
    values: dict[str, float] = {}
    for hour in hours:
        for days in (1, 3, 7, 14):
            uid = get_energy_average_sensor_unique_id(
                _ENTRY_ID, hour, (hour + 1) % 24, days
            )
            eid = f"sensor.energy_avg_{hour:02d}_{days}d"
            cache[uid] = eid
            if value is not None:
                values[eid] = value
    return cache, values


class TestConsumptionPopulatorBailouts:
    """A cycle that cannot read every window must not plan on partial data."""

    @pytest.mark.parametrize("missing", ["w1", "w3", "w7", "w14"])
    def test_an_unset_weight_aborts_the_cycle(self, missing: str) -> None:
        """A weight that has not been configured yet is not treated as zero."""
        cache, values = _cache_and_values()

        with patch(
            "custom_components.hsem.custom_sensors.hourly_data_populator"
            ".consumption.log_planner"
        ) as log:
            populated = populate_avg_house_consumption_from_snapshot(
                _recs(),
                StateSnapshot(live=LiveState(), energy_average_values=values),
                _cfg(**{missing: None}),
                cache,
                entry_id=_ENTRY_ID,
            )

        assert populated.ok is False
        assert any(call.args[0] == "warning" for call in log.call_args_list)

    def test_an_unregistered_average_sensor_aborts_the_cycle(self) -> None:
        """Before HSEM's own average sensors exist there is nothing to read."""
        # Only the first 12 hours have entities registered.
        cache, values = _cache_and_values(hours=range(12))

        populated = populate_avg_house_consumption_from_snapshot(
            _recs(),
            StateSnapshot(live=LiveState(), energy_average_values=values),
            _cfg(),
            cache,
            entry_id=_ENTRY_ID,
        )

        assert populated.ok is False

    def test_too_many_hours_without_state_abort_the_cycle(self) -> None:
        """More than the estimate limit of unavailable hours fails closed."""
        cache, values = _cache_and_values()
        gap = list(range(MAX_ESTIMATED_LOAD_HOURS + 1))
        for hour in gap:
            del values[f"sensor.energy_avg_{hour:02d}_7d"]
        recs = _recs()

        populated = populate_avg_house_consumption_from_snapshot(
            recs,
            StateSnapshot(live=LiveState(), energy_average_values=values),
            _cfg(),
            cache,
            entry_id=_ENTRY_ID,
        )

        assert populated.ok is False
        assert populated.missing_hours == tuple(gap)
        assert populated.estimated_hours == ()


class TestMissingHourEstimate:
    """Hour blocks without a stored sample (issue #1110)."""

    def test_reporter_gaps_are_estimated_not_zero(self) -> None:
        """Hours 9 and 17 unavailable, the rest measured: plan with estimates."""
        cache, values = _cache_and_values()
        measured = {8: 7.45, 9: None, 10: 7.41, 16: 1.78, 17: None, 18: 1.72}
        for hour, kwh in measured.items():
            for days in (1, 3, 7, 14):
                eid = f"sensor.energy_avg_{hour:02d}_{days}d"
                if kwh is None:
                    del values[eid]
                else:
                    values[eid] = kwh
        recs = _recs()

        populated = populate_avg_house_consumption_from_snapshot(
            recs,
            StateSnapshot(live=LiveState(), energy_average_values=values),
            _cfg(),
            cache,
            entry_id=_ENTRY_ID,
        )

        assert populated.ok is True
        assert populated.missing_hours == (9, 17)
        assert populated.estimated_hours == (9, 17)
        # Conservative: the larger measured neighbour, never zero.
        assert recs[9].avg_house_consumption_1d_kwh == pytest.approx(7.45)
        assert recs[17].avg_house_consumption_1d_kwh == pytest.approx(1.78)
        assert recs[9].avg_house_consumption_kwh > 0.0
        assert recs[17].avg_house_consumption_kwh > 0.0
        # Measured hours are untouched.
        assert recs[10].avg_house_consumption_1d_kwh == pytest.approx(7.41)

    def test_one_missing_window_estimates_the_hour(self) -> None:
        """An entity that exists but reports nothing is not a zero reading."""
        cache, values = _cache_and_values(value=0.8)
        del values["sensor.energy_avg_05_7d"]
        recs = _recs()

        populated = populate_avg_house_consumption_from_snapshot(
            recs,
            StateSnapshot(live=LiveState(), energy_average_values=values),
            _cfg(),
            cache,
            entry_id=_ENTRY_ID,
        )

        assert populated.ok is True
        assert populated.estimated_hours == (5,)
        assert recs[5].avg_house_consumption_7d_kwh == pytest.approx(0.8)

    def test_limit_is_inclusive(self) -> None:
        cache, values = _cache_and_values()
        gap = list(range(MAX_ESTIMATED_LOAD_HOURS))
        for hour in gap:
            for days in (1, 3, 7, 14):
                del values[f"sensor.energy_avg_{hour:02d}_{days}d"]

        populated = populate_avg_house_consumption_from_snapshot(
            _recs(),
            StateSnapshot(live=LiveState(), energy_average_values=values),
            _cfg(),
            cache,
            entry_id=_ENTRY_ID,
        )

        assert populated.ok is True
        assert populated.estimated_hours == tuple(gap)

    def test_estimate_wraps_around_midnight(self) -> None:
        measured = {h: (float(h), float(h), float(h), float(h)) for h in range(1, 23)}
        estimates = estimate_missing_hours(measured, [0, 23])
        # Hour 0 neighbours 22 (before, wrapping) and 1 (after).
        assert estimates[0] == (22.0, 22.0, 22.0, 22.0)
        assert estimates[23] == (22.0, 22.0, 22.0, 22.0)

    def test_estimate_takes_per_window_max(self) -> None:
        measured = {3: (1.0, 5.0, 0.2, 0.3), 5: (2.0, 1.0, 0.4, 0.1)}
        assert estimate_missing_hours(measured, [4]) == {4: (2.0, 5.0, 0.4, 0.3)}

    def test_nothing_measured_gives_no_estimate(self) -> None:
        assert estimate_missing_hours({}, [1, 2]) == {}

    def test_weights_that_all_sum_to_zero_abort_the_cycle(self) -> None:
        """Zeroed weights cannot produce an average, so no plan is attempted."""
        cache, values = _cache_and_values()

        populated = populate_avg_house_consumption_from_snapshot(
            _recs(),
            StateSnapshot(live=LiveState(), energy_average_values=values),
            _cfg(w1=0, w3=0, w7=0, w14=0),
            cache,
            entry_id=_ENTRY_ID,
        )

        assert populated.ok is False


class TestDetectIntervalMinutes:
    """The cadence of a price array is measured, never assumed."""

    def test_a_quarter_hourly_array_is_detected(self) -> None:
        """Four points 15 minutes apart are a 15-minute array."""
        entries: list[dict[str, Any]] = [
            {"start": (_BASE + i * timedelta(minutes=15)).isoformat()} for i in range(4)
        ]

        assert _detect_interval_minutes(entries, "start", 60) == 15

    def test_datetime_entries_are_accepted_as_is(self) -> None:
        """Some integrations publish real datetimes, not strings."""
        entries: list[dict[str, Any]] = [
            {"start": _BASE + i * timedelta(minutes=30)} for i in range(3)
        ]

        assert _detect_interval_minutes(entries, "start", 60) == 30

    @pytest.mark.parametrize(
        "entries",
        [
            pytest.param([], id="empty"),
            pytest.param([{"start": _BASE.isoformat()}], id="single_point"),
            pytest.param([{"start": None}, {"start": ""}], id="no_timestamps"),
            pytest.param(
                [{"start": "not a timestamp"}, {"start": "also not"}], id="unparseable"
            ),
            pytest.param([{"start": 12345}, {"start": 67890}], id="wrong_type"),
            pytest.param([{"start": _BASE}, {"start": _BASE}], id="duplicate_points"),
        ],
    )
    def test_an_unmeasurable_array_uses_the_fallback(
        self, entries: list[dict[str, Any]]
    ) -> None:
        """Without two ordered, parseable timestamps the fallback stands."""
        assert _detect_interval_minutes(entries, "start", 60) == 60

    def test_a_sub_minute_cadence_uses_the_fallback(self) -> None:
        """A gap that rounds to zero minutes is not a usable interval."""
        entries: list[dict[str, Any]] = [
            {"start": (_BASE + i * timedelta(seconds=10)).isoformat()} for i in range(3)
        ]

        assert _detect_interval_minutes(entries, "start", 60) == 60

    def test_detection_stops_after_five_timestamps(self) -> None:
        """Only the head of a long array is measured."""
        entries: list[dict[str, Any]] = [
            {"start": (_BASE + i * timedelta(minutes=60)).isoformat()}
            for i in range(50)
        ]

        assert _detect_interval_minutes(entries, "start", 15) == 60


class TestPopulateFromAttributes:
    """Unusable data points are skipped, not written as zeros."""

    def test_a_datetime_keyed_point_is_matched(self) -> None:
        """A raw datetime timestamp needs no parsing to be matched."""
        recs = _recs(2)
        attributes = {
            "prices": [{"start": _BASE, "price": 1.25}],
        }

        matched = _populate_from_attributes(
            attributes, recs, "import_price", "pv50", 60
        )

        assert matched == 1
        assert recs[0].import_price == pytest.approx(1.25)

    @pytest.mark.parametrize(
        "point",
        [
            pytest.param({"start": "not a timestamp", "price": 1.0}, id="unparseable"),
            pytest.param({"start": 99, "price": 1.0}, id="wrong_type"),
            pytest.param({"start": None, "price": 1.0}, id="no_timestamp"),
            pytest.param({"start": _BASE, "price": "n/a"}, id="unparseable_value"),
            pytest.param({"start": _BASE}, id="no_value"),
        ],
    )
    def test_an_unusable_point_is_skipped(self, point: dict[str, Any]) -> None:
        """A bad data point leaves the slot at its previous value."""
        recs = _recs(2)

        matched = _populate_from_attributes(
            {"prices": [point]}, recs, "import_price", "pv50", 60
        )

        assert matched == 0
        assert recs[0].import_price == pytest.approx(0.0)

    def test_a_timestamp_that_cannot_be_normalised_is_skipped(self) -> None:
        """A timestamp the slot maths rejects is dropped, not planned on."""
        recs = _recs(2)

        with patch(
            f"{_PRICES_MODULE}.normalize_slot_start",
            side_effect=OSError("out of range"),
        ):
            matched = _populate_from_attributes(
                {"prices": [{"start": _BASE, "price": 1.0}]},
                recs,
                "import_price",
                "pv50",
                60,
            )

        assert matched == 0

    def test_no_attributes_matches_nothing(self) -> None:
        """A sensor that publishes no attributes contributes no data."""
        assert (
            _populate_from_attributes(None, _recs(2), "import_price", "pv50", 60) == 0
        )
        assert _populate_from_attributes({}, _recs(2), "import_price", "pv50", 60) == 0


class TestPriceSnapshotWarnings:
    """A price sensor that matched nothing is called out in the log."""

    def test_unmatched_prices_are_warned_about(self) -> None:
        """Planning on 0.00 currency/kWh silently would hide a broken sensor."""
        cfg = SensorConfig()
        cfg.import_electricity_price_sensor = "sensor.import_price"
        cfg.export_electricity_price_sensor = "sensor.export_price"
        cfg.electricity_price_update_interval = 60
        snapshot = StateSnapshot(live=LiveState(), sensor_attributes={})

        with patch(f"{_PRICES_MODULE}._LOGGER") as logger:
            populate_price_and_solcast_from_snapshot(_recs(2), snapshot, cfg)

        warned = " ".join(str(call.args[0]) for call in logger.warning.call_args_list)
        assert "No import price data matched" in warned
        assert "No export price data matched" in warned

    def test_a_forecast_sensor_contributes_to_the_match_count(self) -> None:
        """A separate forecast sensor keeps the "nothing matched" warning quiet."""
        cfg = SensorConfig()
        cfg.import_electricity_price_sensor = "sensor.import_price"
        cfg.import_electricity_price_forecast_sensor = "sensor.import_forecast"
        cfg.export_electricity_price_sensor = "sensor.export_price"
        cfg.export_electricity_price_forecast_sensor = "sensor.export_forecast"
        cfg.electricity_price_update_interval = 60
        recs = _recs(2)
        snapshot = StateSnapshot(
            live=LiveState(),
            sensor_attributes={
                "sensor.import_forecast": {
                    "forecast": [{"hour": _BASE.isoformat(), "price": 2.5}]
                },
                "sensor.export_forecast": {
                    "forecast": [{"hour": _BASE.isoformat(), "price": 0.5}]
                },
            },
        )

        with patch(f"{_PRICES_MODULE}._LOGGER") as logger:
            populate_price_and_solcast_from_snapshot(recs, snapshot, cfg)

        warned = " ".join(str(call.args[0]) for call in logger.warning.call_args_list)
        assert "No import price data matched" not in warned
        assert "No export price data matched" not in warned
        assert recs[0].import_price == pytest.approx(2.5)
        assert recs[0].export_price == pytest.approx(0.5)
