"""Tests for the savings tracker's baseline-cost counterfactual (issue #962).

Before this fix, ``accumulate_savings`` set ``baseline_cost_delta =
import_cost_delta``, simply re-using the actual (HSEM-optimised) grid-import
cost delta from the daily tracker.  That value already reflects any avoided
import from battery discharge, so it is not an independent "what passive
mode would have cost" figure even though the ``today_baseline`` /
``total_baseline`` sensor attributes are documented and presented as one.

This module verifies the fix: ``baseline_cost_delta`` is now computed
independently from live house-consumption and solar-production power,
integrated into energy over the elapsed cycle time, exactly like a
battery-less, HSEM-less installation would experience.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from homeassistant.core import HomeAssistant

from custom_components.hsem.coordinator_tracking import accumulate_savings
from custom_components.hsem.models.daily_plan_vs_actual_tracker import (
    DailyPlanVsActualTracker,
)
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.models.savings_tracker import SavingsTracker


def _make_hass(config_dir: Path) -> HomeAssistant:
    """Build a minimal fake HomeAssistant with just a config_dir."""
    return cast(
        HomeAssistant,
        SimpleNamespace(config=SimpleNamespace(config_dir=str(config_dir))),
    )


async def _run_two_cycles(
    hass: HomeAssistant,
    live: LiveState,
    savings_tracker: SavingsTracker,
    daily_tracker: DailyPlanVsActualTracker,
) -> None:
    """Run two accumulate_savings cycles 15 minutes apart.

    The first call only establishes the baseline-sample baseline timestamp
    (no previous sample exists yet, so no delta can be computed). The second
    call is where any baseline-cost delta is actually credited.
    """
    output = PlannerOutput()
    t0 = datetime(2026, 6, 26, 12, 0, tzinfo=UTC)
    await accumulate_savings(
        now=t0,
        live=live,
        output=output,
        savings_tracker=savings_tracker,
        daily_tracker=daily_tracker,
        hourly_recommendation=None,
        hass=hass,
    )
    t1 = datetime(2026, 6, 26, 12, 15, tzinfo=UTC)  # 900s later
    await accumulate_savings(
        now=t1,
        live=live,
        output=output,
        savings_tracker=savings_tracker,
        daily_tracker=daily_tracker,
        hourly_recommendation=None,
        hass=hass,
    )


@pytest.mark.asyncio
async def test_baseline_exceeds_actual_cost_when_discharge_avoided_import(
    tmp_path: Path,
) -> None:
    """Baseline (no-battery counterfactual) must exceed the discounted actual cost.

    A 2 kW house load with no PV, over a battery-less installation, would
    cost the full import price.  The daily tracker's actual grid-import cost
    is set to a much smaller value here to stand in for what HSEM's battery
    discharge actually caused to be bought from the grid (issue #960).  The
    computed baseline must not collapse to that discounted actual figure.
    """
    hass = _make_hass(tmp_path)
    savings_tracker = SavingsTracker()
    daily_tracker = DailyPlanVsActualTracker()
    # Stand-in for HSEM having avoided most of the import via discharge.
    daily_tracker.actual.grid_import_cost = 0.1

    live = LiveState()
    live.house_consumption_power_w = 2000.0
    live.solar_production_power_w = 0.0
    live.import_electricity_price = 2.0
    live.export_electricity_price = 0.5

    await _run_two_cycles(hass, live, savings_tracker, daily_tracker)

    # 2 kW for 900s = 0.5 kWh, valued at 2.0/kWh = 1.0.
    assert savings_tracker.today_baseline == pytest.approx(1.0, rel=1e-6)
    assert savings_tracker.today_baseline > daily_tracker.actual.grid_import_cost


@pytest.mark.asyncio
async def test_baseline_equals_actual_cost_with_no_pv_or_battery_activity(
    tmp_path: Path,
) -> None:
    """With no PV/battery activity, baseline matches what was actually bought.

    When HSEM does nothing (no discharge, no PV), the passive counterfactual
    and the actual outcome are identical: the grid supplies the full house
    load at the live import price.
    """
    hass = _make_hass(tmp_path)
    savings_tracker = SavingsTracker()
    daily_tracker = DailyPlanVsActualTracker()
    # No PV, no battery: the grid literally bought the whole house load.
    daily_tracker.actual.grid_import_cost = 0.5

    live = LiveState()
    live.house_consumption_power_w = 1000.0
    live.solar_production_power_w = 0.0
    live.import_electricity_price = 2.0
    live.export_electricity_price = 0.5

    await _run_two_cycles(hass, live, savings_tracker, daily_tracker)

    # 1 kW for 900s = 0.25 kWh, valued at 2.0/kWh = 0.5.
    assert savings_tracker.today_baseline == pytest.approx(
        daily_tracker.actual.grid_import_cost, rel=1e-6
    )


@pytest.mark.asyncio
async def test_pv_surplus_reduces_baseline_cost(tmp_path: Path) -> None:
    """PV surplus in the counterfactual is valued at the live export price."""
    hass = _make_hass(tmp_path)
    savings_tracker = SavingsTracker()
    daily_tracker = DailyPlanVsActualTracker()

    live = LiveState()
    live.house_consumption_power_w = 500.0
    live.solar_production_power_w = 1500.0  # 1 kW surplus
    live.import_electricity_price = 2.0
    live.export_electricity_price = 0.8

    await _run_two_cycles(hass, live, savings_tracker, daily_tracker)

    # 1 kW surplus for 900s = 0.25 kWh exported, valued at 0.8/kWh = -0.2.
    assert savings_tracker.today_baseline == pytest.approx(-0.2, rel=1e-6)


@pytest.mark.asyncio
async def test_first_cycle_credits_zero_baseline(tmp_path: Path) -> None:
    """The very first cycle has no previous sample, so it credits zero."""
    hass = _make_hass(tmp_path)
    savings_tracker = SavingsTracker()
    daily_tracker = DailyPlanVsActualTracker()
    output = PlannerOutput()

    live = LiveState()
    live.house_consumption_power_w = 2000.0
    live.solar_production_power_w = 0.0
    live.import_electricity_price = 2.0

    await accumulate_savings(
        now=datetime(2026, 6, 26, 12, 0, tzinfo=UTC),
        live=live,
        output=output,
        savings_tracker=savings_tracker,
        daily_tracker=daily_tracker,
        hourly_recommendation=None,
        hass=hass,
    )

    assert savings_tracker.today_baseline == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_missing_house_consumption_credits_zero_baseline(
    tmp_path: Path,
) -> None:
    """A ``None`` house-consumption reading must not crash and credits zero."""
    hass = _make_hass(tmp_path)
    savings_tracker = SavingsTracker()
    daily_tracker = DailyPlanVsActualTracker()

    live = LiveState()
    live.house_consumption_power_w = None
    live.solar_production_power_w = 0.0
    live.import_electricity_price = 2.0

    await _run_two_cycles(hass, live, savings_tracker, daily_tracker)

    assert savings_tracker.today_baseline == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_nan_house_consumption_credits_zero_baseline(tmp_path: Path) -> None:
    """A non-finite house-consumption reading must not crash and credits zero."""
    hass = _make_hass(tmp_path)
    savings_tracker = SavingsTracker()
    daily_tracker = DailyPlanVsActualTracker()

    live = LiveState()
    live.house_consumption_power_w = float("nan")
    live.solar_production_power_w = 0.0
    live.import_electricity_price = 2.0

    await _run_two_cycles(hass, live, savings_tracker, daily_tracker)

    assert savings_tracker.today_baseline == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_nan_solar_production_credits_zero_baseline(tmp_path: Path) -> None:
    """A non-finite solar-production reading must not crash and credits zero."""
    hass = _make_hass(tmp_path)
    savings_tracker = SavingsTracker()
    daily_tracker = DailyPlanVsActualTracker()

    live = LiveState()
    live.house_consumption_power_w = 1000.0
    live.solar_production_power_w = float("nan")
    live.import_electricity_price = 2.0

    await _run_two_cycles(hass, live, savings_tracker, daily_tracker)

    assert savings_tracker.today_baseline == pytest.approx(0.0)
