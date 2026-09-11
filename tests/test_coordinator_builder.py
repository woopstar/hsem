"""Tests for coordinator_builder helper functions.

Covers ``_resolve_max_discharge_power_w`` — the guard against the EV
discharge-cap feedback loop (issue #592, beta7).
"""

from __future__ import annotations

import pytest

from custom_components.hsem.coordinator_builder import (
    _resolve_max_discharge_power_w,
    build_planner_input,
)
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.sensor_config import SensorConfig


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
        previous_winner_name=None,
        previous_winner_score=0.0,
    )

    assert planner_input.ev_planned_load_enabled is True
    assert planner_input.ev_planned_load_current_soc_pct == pytest.approx(45.5)


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
