"""Tests for savings tracker persistence in coordinator_tracking.

Regression coverage: the savings tracker sensor lost its state on every
Home Assistant restart because ``accumulate_savings`` accumulated data in
memory but never called ``SavingsTracker.save_history()``, unlike the
sibling ``accumulate_financials`` helper which persists every cycle.
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


@pytest.mark.asyncio
async def test_accumulate_savings_persists_to_disk(tmp_path: Path) -> None:
    """accumulate_savings must write the savings history file every cycle.

    Without this, the tracker's totals only ever live in memory and are
    lost whenever Home Assistant restarts, even though ``load_history``
    would happily restore them on the next startup if a file existed.
    """
    hass = _make_hass(tmp_path)
    savings_tracker = SavingsTracker()
    daily_tracker = DailyPlanVsActualTracker()
    live = LiveState()
    output = PlannerOutput()

    await accumulate_savings(
        now=datetime(2026, 6, 26, 12, 0, tzinfo=UTC),
        live=live,
        output=output,
        savings_tracker=savings_tracker,
        daily_tracker=daily_tracker,
        hourly_recommendation=None,
        hass=hass,
    )

    history_path = tmp_path / ".storage" / "hsem_savings_history.json"
    assert history_path.exists(), (
        "accumulate_savings did not persist the savings tracker state to disk"
    )

    # A tracker restored from that file should reflect the same totals,
    # simulating a Home Assistant restart.
    restored = SavingsTracker()
    restored.history_file = str(history_path)
    await restored.load_history()

    assert restored.actual_savings == pytest.approx(savings_tracker.actual_savings)
    assert restored.missed_savings == pytest.approx(savings_tracker.missed_savings)
    assert restored.baseline_cost == pytest.approx(savings_tracker.baseline_cost)


@pytest.mark.asyncio
async def test_accumulate_savings_survives_simulated_restart(tmp_path: Path) -> None:
    """Totals accumulated across cycles must survive a fresh tracker instance.

    This mirrors what happens on a real Home Assistant restart: the
    coordinator (and its in-memory SavingsTracker) is thrown away and a
    brand-new tracker is constructed and initialised from disk.
    """
    hass = _make_hass(tmp_path)
    daily_tracker = DailyPlanVsActualTracker()
    live = LiveState()
    output = PlannerOutput()

    tracker_before_restart = SavingsTracker()
    for _ in range(3):
        daily_tracker.actual.grid_export_rev += 0.1
        daily_tracker.actual.grid_import_cost += 0.2
        await accumulate_savings(
            now=datetime(2026, 6, 26, 12, 0, tzinfo=UTC),
            live=live,
            output=output,
            savings_tracker=tracker_before_restart,
            daily_tracker=daily_tracker,
            hourly_recommendation=None,
            hass=hass,
        )

    assert tracker_before_restart.baseline_cost > 0.0

    # Simulate a restart: a brand-new tracker, re-initialised from disk.
    tracker_after_restart = SavingsTracker()
    await accumulate_savings(
        now=datetime(2026, 6, 26, 12, 5, tzinfo=UTC),
        live=live,
        output=output,
        savings_tracker=tracker_after_restart,
        daily_tracker=daily_tracker,
        hourly_recommendation=None,
        hass=hass,
    )

    assert tracker_after_restart.baseline_cost >= tracker_before_restart.baseline_cost
