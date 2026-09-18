"""Regression tests for charge-savings tracking (issue #1053)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from homeassistant.core import HomeAssistant

from custom_components.hsem.coordinator_tracking import (
    _compute_daily_avg_import_price,
    accumulate_savings,
)
from custom_components.hsem.models.daily_plan_vs_actual_tracker import (
    DailyPlanVsActualTracker,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.models.savings_tracker import SavingsTracker
from custom_components.hsem.utils.prices import SlotPrice
from custom_components.hsem.utils.recommendations import Recommendations

_NOW = datetime(2024, 6, 15, 12, 0, tzinfo=UTC)
_SLOT = timedelta(hours=1)


def _make_hass(config_dir: Path) -> HomeAssistant:
    """Build a minimal fake Home Assistant with a config directory."""
    return cast(
        HomeAssistant,
        SimpleNamespace(config=SimpleNamespace(config_dir=str(config_dir))),
    )


def _make_output(*prices: float) -> PlannerOutput:
    """Build a planner output with real slots on the cycle's calendar day."""
    start = _NOW.replace(hour=0)
    return PlannerOutput(
        slots=[
            PlannedSlot(
                start=start + index * _SLOT,
                end=start + (index + 1) * _SLOT,
                price=SlotPrice(import_price=price, export_price=0.0),
            )
            for index, price in enumerate(prices)
        ]
    )


def _make_charge_recommendation(*, charged_kwh: float) -> HourlyRecommendation:
    """Build a current grid-charge recommendation."""
    return HourlyRecommendation(
        start=_NOW,
        end=_NOW + _SLOT,
        avg_house_consumption_kwh=0.0,
        avg_house_consumption_1d_kwh=0.0,
        avg_house_consumption_3d_kwh=0.0,
        avg_house_consumption_7d_kwh=0.0,
        avg_house_consumption_14d_kwh=0.0,
        batteries_charged_kwh=charged_kwh,
        batteries_discharged_kwh=0.0,
        estimated_battery_capacity_kwh=0.0,
        estimated_battery_soc_pct=0.0,
        estimated_cost_currency=0.0,
        estimated_net_consumption_kwh=0.0,
        export_price=0.0,
        grid_export_kwh=0.0,
        grid_import_kwh=charged_kwh,
        import_price=0.0,
        recommendation=Recommendations.BatteriesChargeGrid.value,
        solcast_pv_estimate_kwh=0.0,
    )


def test_daily_average_uses_planned_slot_prices_and_cycle_day() -> None:
    """Use SlotPrice values from the day identified by the cycle timestamp."""
    output = _make_output(2.0, 4.0)
    output.slots.append(
        PlannedSlot(
            start=_NOW + timedelta(days=1),
            end=_NOW + timedelta(days=1, hours=1),
            price=SlotPrice(import_price=100.0, export_price=0.0),
        )
    )

    assert _compute_daily_avg_import_price(output, _NOW) == pytest.approx(3.0)


def test_daily_average_excludes_nan_and_zero_prices() -> None:
    """Exclude missing and non-positive import prices from the daily mean."""
    output = _make_output(float("nan"), 0.0, -1.0, 2.0, 4.0)

    assert _compute_daily_avg_import_price(output, _NOW) == pytest.approx(3.0)
    assert _compute_daily_avg_import_price(
        _make_output(float("nan"), 0.0), _NOW
    ) == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_below_average_charge_credits_savings(tmp_path: Path) -> None:
    """Credit charged energy by the difference from today's average price."""
    live = LiveState()
    live.import_electricity_price = 1.0

    tracker = SavingsTracker()
    await accumulate_savings(
        now=_NOW,
        live=live,
        output=_make_output(1.0, 3.0),
        savings_tracker=tracker,
        daily_tracker=DailyPlanVsActualTracker(),
        hourly_recommendation=_make_charge_recommendation(charged_kwh=0.5),
        hass=_make_hass(tmp_path),
    )

    assert tracker.today_actual == pytest.approx(0.5)


@pytest.mark.parametrize("import_price", [2.0, 2.5])
@pytest.mark.asyncio
async def test_charge_at_or_above_average_credits_no_savings(
    tmp_path: Path,
    import_price: float,
) -> None:
    """Do not credit charge savings at or above today's average price."""
    live = LiveState()
    live.import_electricity_price = import_price

    tracker = SavingsTracker()
    await accumulate_savings(
        now=_NOW,
        live=live,
        output=_make_output(1.0, 3.0),
        savings_tracker=tracker,
        daily_tracker=DailyPlanVsActualTracker(),
        hourly_recommendation=_make_charge_recommendation(charged_kwh=0.5),
        hass=_make_hass(tmp_path),
    )

    assert tracker.today_actual == pytest.approx(0.0)
