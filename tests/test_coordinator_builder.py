"""Tests for coordinator_builder helper functions.

Covers ``_resolve_max_discharge_power_w`` — the guard against the EV
discharge-cap feedback loop (issue #592, beta7).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
import voluptuous as vol

import homeassistant.util.dt as dt_util

from custom_components.hsem.coordinator_builder import (
    _resolve_max_discharge_power_w,
    build_planner_input,
    generate_recommendation_intervals,
)
from custom_components.hsem.custom_sensors.hourly_data_populator.prices_solcast import (
    populate_price_and_solcast_from_snapshot,
)
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.models.state_snapshot import StateSnapshot
from custom_components.hsem.utils.ev_accounting import normalized_baseline_includes_ev


class TestResolveMaxDischargePowerW:
    """The planner must see the battery's physical capability, not the
    applier's EV-capped write-back value."""

    @staticmethod
    def _live(
        *,
        ev_charging: bool,
        max_discharge_w: float,
        rated_wh: int = 10000,
    ) -> LiveState:
        live = LiveState()
        live.ev.is_charging = ev_charging
        live.huawei_batteries_max_discharge_power_w = max_discharge_w
        live.huawei_batteries_rated_capacity_wh = rated_wh
        return live

    def test_no_ev_charging_uses_rated_capability(self) -> None:
        """The planner always gets the physical capability derived from the
        rated capacity — the live entity is a commanded value, never a
        capability (issue #592)."""
        live = self._live(ev_charging=False, max_discharge_w=321.0)
        assert _resolve_max_discharge_power_w(live) == pytest.approx(5000.0)

    def test_ev_charging_uses_rated_capability(self) -> None:
        """During an EV session the applier caps the entity (e.g. 321 W).
        The planner must get the physical capability (5000 W for a
        10 kWh battery), not the capped value — otherwise the entire
        planning horizon is limited to the EV cap (issue #592)."""
        live = self._live(ev_charging=True, max_discharge_w=321.0)
        assert _resolve_max_discharge_power_w(live) == pytest.approx(5000.0)

    def test_ev_status_flicker_does_not_leak_cap(self) -> None:
        """A single-cycle EV boolean flicker must not let the still-capped
        entity poison an off-schedule planner run (beta8 regression)."""
        live = self._live(ev_charging=False, max_discharge_w=40.0)
        assert _resolve_max_discharge_power_w(live) == pytest.approx(5000.0)

    def test_missing_rated_capacity_falls_back_to_live(self) -> None:
        """Missing rated capacity → keep the live value (degraded but safe)."""
        live = self._live(ev_charging=True, max_discharge_w=321.0, rated_wh=0)
        assert _resolve_max_discharge_power_w(live) == pytest.approx(321.0)

    def test_missing_rated_capacity_and_live_value_returns_none(self) -> None:
        live = self._live(ev_charging=False, max_discharge_w=0.0, rated_wh=0)
        assert _resolve_max_discharge_power_w(live) is None

    def test_entity_maximum_bounds_the_rated_capability(self) -> None:
        """A 10 kWh battery whose number entity accepts 4400 W is planned at
        4400 W, not the table's 5000 W (issue #1291)."""
        live = self._live(ev_charging=False, max_discharge_w=4400.0)
        live.huawei_batteries_max_discharge_power_limit_w = 4400.0
        assert _resolve_max_discharge_power_w(live) == pytest.approx(4400.0)

    def test_capped_entity_state_does_not_lower_the_limit(self) -> None:
        """Only the entity's ``max`` attribute bounds the limit. Its state,
        2000 W in the report, is a commanded cap (issues #592, #1291)."""
        live = self._live(ev_charging=True, max_discharge_w=2000.0)
        live.huawei_batteries_max_discharge_power_limit_w = 4400.0
        assert _resolve_max_discharge_power_w(live) == pytest.approx(4400.0)

    @pytest.mark.parametrize("entity_max_w", [None, 0.0, 5000.0, 6000.0])
    def test_unusable_or_higher_entity_maximum_changes_nothing(
        self, entity_max_w: float | None
    ) -> None:
        live = self._live(ev_charging=False, max_discharge_w=5000.0)
        live.huawei_batteries_max_discharge_power_limit_w = entity_max_w
        assert _resolve_max_discharge_power_w(live) == pytest.approx(5000.0)

    def test_planner_input_carries_the_bounded_limit(self) -> None:
        """The bound reaches ``PlannerInput``, so no slot is planned above it."""
        live = self._live(ev_charging=False, max_discharge_w=2000.0)
        live.huawei_batteries_max_discharge_power_limit_w = 4400.0

        planner_input = build_planner_input(
            cfg=SensorConfig(),
            live=live,
            batteries_schedules=[],
            hourly_recommendations=[],
            previous_winner_name=None,
            previous_winner_score=0.0,
        )

        assert planner_input.battery_max_discharge_power_w == pytest.approx(4400.0)


def test_builder_prefers_bounded_effective_ev_soc() -> None:
    """Credited energy must reduce planner demand without replacing raw telemetry."""
    cfg = SensorConfig()
    cfg.ev_planned_load_enabled = True
    live = LiveState()
    live.ev_planned_load_current_soc_pct = 40.0
    live.ev.effective_soc_pct = 45.5

    planner_input = build_planner_input(
        cfg=cfg,
        live=live,
        hourly_recommendations=[],
        batteries_schedules=[],
        previous_winner_name=None,
        previous_winner_score=0.0,
    )

    assert planner_input.ev_planned_load_enabled is True
    assert planner_input.ev_planned_load_current_soc_pct == pytest.approx(45.5)


class TestNormalizedEvBaselineAccounting:
    """Raw CT inclusion and normalized planner-baseline inclusion are distinct."""

    @staticmethod
    def _build(cfg: SensorConfig) -> PlannerInput:
        return build_planner_input(
            cfg=cfg,
            live=LiveState(),
            hourly_recommendations=[],
            batteries_schedules=[],
            previous_winner_name=None,
            previous_winner_score=0.0,
        )

    def test_undefined_optional_power_entity_is_not_configured(self) -> None:
        assert (
            normalized_baseline_includes_ev(
                raw_house_meter_includes_ev=True,
                ev_power_entity=vol.UNDEFINED,
            )
            is True
        )

    def test_configured_ev_power_is_removed_from_inclusive_baseline(self) -> None:
        cfg = SensorConfig()
        cfg.house_power_includes_ev_charger_power = True
        cfg.ev.power_entity = "sensor.ev_power"

        planner_input = self._build(cfg)

        assert planner_input.house_power_includes_ev is True
        assert planner_input.ev_planned_load_base_load_includes_ev is False

    def test_two_ev_baseline_accounting_is_independent(self) -> None:
        cfg = SensorConfig()
        cfg.house_power_includes_ev_charger_power = True
        cfg.ev.power_entity = "sensor.ev_power"
        cfg.ev_second.power_entity = None

        planner_input = self._build(cfg)

        assert planner_input.ev_planned_load_base_load_includes_ev is False
        assert planner_input.ev_second_planned_load_base_load_includes_ev is True

    def test_ev_exclusive_raw_meter_keeps_both_baselines_exclusive(self) -> None:
        cfg = SensorConfig()
        cfg.house_power_includes_ev_charger_power = False

        planner_input = self._build(cfg)

        assert planner_input.ev_planned_load_base_load_includes_ev is False
        assert planner_input.ev_second_planned_load_base_load_includes_ev is False

    def test_session_zero_is_distinct_from_missing(self) -> None:
        cfg = SensorConfig()

        known_zero = build_planner_input(
            cfg=cfg,
            live=LiveState(),
            hourly_recommendations=[],
            batteries_schedules=[],
            previous_winner_name=None,
            previous_winner_score=0.0,
            ev_session_kw={"ev": 0.0},
        )
        missing = self._build(cfg)

        assert known_zero.ev_session_charge_kw == pytest.approx(0.0)
        assert missing.ev_session_charge_kw is None


class TestFalsyZeroPreservation:
    """A genuine 0 %/0.0 configured value must survive the builder unchanged.

    ``convert_to_float(...) or <default>`` silently replaces a valid 0 with
    the fallback because ``0.0`` is falsy in Python. The builder must check
    ``is not None`` instead (issue #807).
    """

    @staticmethod
    def _build(cfg: SensorConfig, live: LiveState) -> PlannerInput:
        return build_planner_input(
            cfg=cfg,
            live=live,
            hourly_recommendations=[],
            batteries_schedules=[],
            previous_winner_name=None,
            previous_winner_score=0.0,
        )

    def test_battery_soc_pct_zero_is_preserved(self) -> None:
        live = LiveState()
        live.huawei_batteries_soc_pct = 0.0
        planner_input = self._build(SensorConfig(), live)
        assert planner_input.battery_soc_pct == pytest.approx(0.0)

    def test_battery_soc_pct_missing_falls_back_to_default(self) -> None:
        live = LiveState()
        live.huawei_batteries_soc_pct = None
        planner_input = self._build(SensorConfig(), live)
        assert planner_input.battery_soc_pct == pytest.approx(50.0)

    def test_end_of_discharge_soc_pct_zero_is_preserved(self) -> None:
        live = LiveState()
        live.huawei_batteries_end_of_discharge_soc_pct = 0.0
        planner_input = self._build(SensorConfig(), live)
        assert planner_input.battery_end_of_discharge_soc_pct == pytest.approx(0.0)

    def test_excess_export_discharge_buffer_pct_zero_is_preserved(self) -> None:
        cfg = SensorConfig()
        cfg.batteries_excess_export_discharge_buffer = 0.0
        planner_input = self._build(cfg, LiveState())
        assert planner_input.excess_export_discharge_buffer_pct == pytest.approx(0.0)

    def test_forecast_reserve_pct_zero_is_preserved(self) -> None:
        cfg = SensorConfig()
        cfg.batteries_forecast_reserve_pct = 0.0
        planner_input = self._build(cfg, LiveState())
        assert planner_input.battery_forecast_reserve_pct == pytest.approx(0.0)

    def test_forecast_reserve_pct_configured_value_is_plumbed_through(self) -> None:
        cfg = SensorConfig()
        cfg.batteries_forecast_reserve_pct = 12.5
        planner_input = self._build(cfg, LiveState())
        assert planner_input.battery_forecast_reserve_pct == pytest.approx(12.5)


_BERLIN = ZoneInfo("Europe/Berlin")
_FLAT_NOW = datetime(2026, 10, 20, 9, 5, tzinfo=_BERLIN)
_FLAT_MIDNIGHT = _FLAT_NOW.replace(hour=0, minute=0)
_EPEX = "sensor.epex_spot_data_total_price"
_FEED_IN = "sensor.feed_in_tariff"


def _epex_data(day_start: datetime, quarters: Iterable[int]) -> dict[str, Any]:
    """Return EPEX Spot ``data`` attributes, one 15-minute price per quarter."""
    return {
        "data": [
            {
                "start_time": (day_start + timedelta(minutes=15 * q)).isoformat(),
                "end_time": (day_start + timedelta(minutes=15 * (q + 1))).isoformat(),
                "price_per_kwh": round(0.3 + q / 1000.0, 5),
            }
            for q in quarters
        ]
    }


class TestFlatTariffPriceSensor:
    """Issue #978: a flat-tariff price sensor must not empty the planner input.

    Reported on v6.3.13: EPEX Spot import prices in the ``data`` attribute and a
    fixed feed-in sensor for export gave ``planner_input.price_points == []``.
    The feed-in sensor has a numeric state and no forecast attribute, so it
    covered no slot, and a slot needs both prices to reach the planner.
    """

    @pytest.fixture(autouse=True)
    def _berlin_ha_tz(self) -> Iterator[None]:
        previous = dt_util.get_default_time_zone()
        dt_util.set_default_time_zone(_BERLIN)
        try:
            yield
        finally:
            dt_util.set_default_time_zone(previous)

    @staticmethod
    def _planner_input(
        sensor_states: dict[str, Any], sensor_attributes: dict[str, dict[str, Any]]
    ) -> PlannerInput:
        """Populate and build exactly as the coordinator does."""
        cfg = SensorConfig()
        cfg.recommendation_interval_minutes = 15
        cfg.recommendation_interval_length = 48
        cfg.electricity_price_update_interval = 15
        cfg.import_electricity_price_sensor = _EPEX
        cfg.export_electricity_price_sensor = _FEED_IN
        with patch("homeassistant.util.dt.now", return_value=_FLAT_NOW):
            recs = generate_recommendation_intervals(15, 48)
            coverage = populate_price_and_solcast_from_snapshot(
                recs,
                StateSnapshot(
                    live=LiveState(),
                    sensor_attributes=sensor_attributes,
                    sensor_states=sensor_states,
                ),
                cfg,
            )
            return build_planner_input(
                cfg=cfg,
                live=LiveState(),
                hourly_recommendations=recs,
                batteries_schedules=[],
                previous_winner_name=None,
                previous_winner_score=0.0,
                forecast_coverage=coverage,
            )

    def test_flat_export_with_forecast_import_reaches_the_planner(self) -> None:
        """The reported setup: today's and tomorrow's quarter-hourly prices."""
        planner_input = self._planner_input(
            {_EPEX: "0.348741", _FEED_IN: "0.0778"},
            {
                _EPEX: {
                    "data": _epex_data(_FLAT_MIDNIGHT, range(96))["data"]
                    + _epex_data(_FLAT_MIDNIGHT + timedelta(days=1), range(96))["data"]
                },
                _FEED_IN: {"unit_of_measurement": "EUR/kWh"},
            },
        )

        points = planner_input.price_points
        assert len(points) == 192
        assert [p.export_price for p in points] == pytest.approx([0.0778] * 192)
        assert points[5].import_price == pytest.approx(0.305)
        assert (points[100].day_offset, points[100].slot_in_day) == (1, 4)
        assert points[100].import_price == pytest.approx(0.304)

    def test_flat_import_and_export_price_every_slot(self) -> None:
        planner_input = self._planner_input(
            {_EPEX: "0.25", _FEED_IN: "0.08"}, {_EPEX: {}, _FEED_IN: {}}
        )

        points = planner_input.price_points
        assert len(points) == 192
        assert [p.import_price for p in points] == pytest.approx([0.25] * 192)
        assert [p.export_price for p in points] == pytest.approx([0.08] * 192)

    def test_unavailable_flat_sensor_leaves_the_slots_to_the_estimate(self) -> None:
        """No price is invented: the planner's missing-price path still runs."""
        planner_input = self._planner_input(
            {_EPEX: "0.348741", _FEED_IN: "unavailable"},
            {_EPEX: _epex_data(_FLAT_MIDNIGHT, range(96)), _FEED_IN: {}},
        )

        assert planner_input.price_points == []

    def test_gaps_in_a_forecast_stay_gaps(self) -> None:
        """Tomorrow is not published yet: the import sensor's state is not flat."""
        planner_input = self._planner_input(
            {_EPEX: "0.348741", _FEED_IN: "0.08"},
            {
                _EPEX: _epex_data(
                    _FLAT_MIDNIGHT, (q for q in range(96) if not 40 <= q < 48)
                ),
                _FEED_IN: {},
            },
        )

        points = planner_input.price_points
        assert len(points) == 88
        assert {p.day_offset for p in points} == {0}
        assert {p.slot_in_day for p in points} == set(range(96)) - set(range(40, 48))
