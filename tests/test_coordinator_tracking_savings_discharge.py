"""Tests for savings tracker avoided-grid-import discharge credit (issue #960).

Battery energy discharged to serve house load avoids buying that energy from
the grid at the current import price.  Before this fix, ``accumulate_savings``
only credited export revenue and below-average-price charging, so
self-consumption-heavy installations were reported as saving nothing even
while the battery was actively reducing grid import.
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

    The first call only establishes the discharge-sample baseline timestamp
    (no previous sample exists yet, so no delta can be computed). The second
    call is where any discharge-savings delta is actually credited.
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
async def test_discharge_credits_savings_at_import_price(tmp_path: Path) -> None:
    """Battery discharge to the house is credited at the live import price."""
    hass = _make_hass(tmp_path)
    savings_tracker = SavingsTracker()
    daily_tracker = DailyPlanVsActualTracker()

    live = LiveState()
    live.import_electricity_price = 2.0
    live.huawei_batteries_charge_discharge_power_w = -1000.0  # 1 kW discharging

    await _run_two_cycles(hass, live, savings_tracker, daily_tracker)

    # 1 kW for 900s = 0.25 kWh, valued at 2.0/kWh = 0.50.
    assert savings_tracker.actual_savings == pytest.approx(0.50, rel=1e-6)


@pytest.mark.asyncio
async def test_first_cycle_credits_no_discharge_savings(tmp_path: Path) -> None:
    """The very first cycle has no previous sample, so it credits zero."""
    hass = _make_hass(tmp_path)
    savings_tracker = SavingsTracker()
    daily_tracker = DailyPlanVsActualTracker()
    output = PlannerOutput()

    live = LiveState()
    live.import_electricity_price = 2.0
    live.huawei_batteries_charge_discharge_power_w = -1000.0

    await accumulate_savings(
        now=datetime(2026, 6, 26, 12, 0, tzinfo=UTC),
        live=live,
        output=output,
        savings_tracker=savings_tracker,
        daily_tracker=daily_tracker,
        hourly_recommendation=None,
        hass=hass,
    )

    assert savings_tracker.actual_savings == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_charging_power_credits_no_discharge_savings(tmp_path: Path) -> None:
    """Positive (charging) power must not be credited as discharge savings."""
    hass = _make_hass(tmp_path)
    savings_tracker = SavingsTracker()
    daily_tracker = DailyPlanVsActualTracker()

    live = LiveState()
    live.import_electricity_price = 2.0
    live.huawei_batteries_charge_discharge_power_w = 1000.0  # charging

    await _run_two_cycles(hass, live, savings_tracker, daily_tracker)

    assert savings_tracker.actual_savings == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_missing_power_reading_credits_no_discharge_savings(
    tmp_path: Path,
) -> None:
    """A ``None`` power reading must not crash and must credit zero."""
    hass = _make_hass(tmp_path)
    savings_tracker = SavingsTracker()
    daily_tracker = DailyPlanVsActualTracker()

    live = LiveState()
    live.import_electricity_price = 2.0
    live.huawei_batteries_charge_discharge_power_w = None

    await _run_two_cycles(hass, live, savings_tracker, daily_tracker)

    assert savings_tracker.actual_savings == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_nan_power_reading_credits_no_discharge_savings(
    tmp_path: Path,
) -> None:
    """A non-finite power reading must not crash and must credit zero."""
    hass = _make_hass(tmp_path)
    savings_tracker = SavingsTracker()
    daily_tracker = DailyPlanVsActualTracker()

    live = LiveState()
    live.import_electricity_price = 2.0
    live.huawei_batteries_charge_discharge_power_w = float("nan")

    await _run_two_cycles(hass, live, savings_tracker, daily_tracker)

    assert savings_tracker.actual_savings == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_zero_import_price_credits_no_discharge_savings(
    tmp_path: Path,
) -> None:
    """A non-positive import price must not credit discharge savings."""
    hass = _make_hass(tmp_path)
    savings_tracker = SavingsTracker()
    daily_tracker = DailyPlanVsActualTracker()

    live = LiveState()
    live.import_electricity_price = 0.0
    live.huawei_batteries_charge_discharge_power_w = -1000.0

    await _run_two_cycles(hass, live, savings_tracker, daily_tracker)

    assert savings_tracker.actual_savings == pytest.approx(0.0)
