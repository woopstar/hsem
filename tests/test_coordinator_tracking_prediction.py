"""Tests for the prediction-accuracy wiring in ``coordinator_tracking``.

Covers how finalised forecast-tracker slots are matched against the last
planner output and fed into the :class:`PredictionTracker` scorecard
(issue #601), and how the scorecard's persisted history is restored.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from homeassistant.core import HomeAssistant

from custom_components.hsem.coordinator_tracking import (
    accumulate_forecast_actuals,
    init_prediction_tracker,
    register_forecasts_from_planner,
)
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.utils.forecast_tracker import ForecastTracker
from custom_components.hsem.utils.prediction_tracker import PredictionTracker
from custom_components.hsem.utils.recommendations import Recommendations
from custom_components.hsem.utils.solar_corrector import SolarForecastCorrector
from tests.test_coordinator_tracking_forecast import _live, _rec

_SLOT = timedelta(minutes=15)
_SLOT_A_START = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
_SLOT_B_START = _SLOT_A_START + _SLOT
_SLOT_C_START = _SLOT_B_START + _SLOT


class TestPredictionScorecardFeed:
    """Finalised slots with a matching planner slot are scored."""

    def test_only_finalised_slots_with_a_planner_match_are_recorded(self) -> None:
        """A and B finalise; only A exists in the plan; C is still open."""
        forecast_tracker = ForecastTracker()
        planned_a = PlannedSlot(
            start=_SLOT_A_START,
            end=_SLOT_B_START,
            estimated_battery_soc_pct=62.0,
            solcast_pv_estimate_kwh=0.4,
            avg_house_consumption_kwh=0.3,
            recommendation=Recommendations.BatteriesChargeGrid.value,
        )
        # A later, non-matching planner slot is listed first so the lookup
        # has to skip past it before finding slot A.
        planned_c = PlannedSlot(start=_SLOT_C_START, end=_SLOT_C_START + _SLOT)
        last_planner_output = PlannerOutput(slots=[planned_c, planned_a])
        register_forecasts_from_planner(
            last_planner_output,
            forecast_tracker,
            now=_SLOT_A_START - timedelta(hours=1),
        )

        live = _live(pv_w=2000.0, load_w=1000.0)
        live.huawei_batteries_soc_pct = 64.0
        prediction_tracker = MagicMock(spec=PredictionTracker)
        prediction_tracker.add_record.return_value = True

        _, record_added = accumulate_forecast_actuals(
            now=_SLOT_C_START + timedelta(minutes=5),
            live=live,
            hourly_recommendations=[
                _rec(_SLOT_A_START, _SLOT_B_START),
                _rec(_SLOT_B_START, _SLOT_C_START),
                _rec(_SLOT_C_START, _SLOT_C_START + _SLOT),
            ],
            forecast_tracker=forecast_tracker,
            last_accumulation_ts=_SLOT_A_START,
            solar_corrector=SolarForecastCorrector(),
            prediction_tracker=prediction_tracker,
            last_planner_output=last_planner_output,
            # 35 minutes elapsed; a 30-minute interval tolerates a 60-minute gap.
            update_interval_minutes=30,
        )

        assert record_added is True
        prediction_tracker.add_record.assert_called_once()
        kwargs = prediction_tracker.add_record.call_args.kwargs
        assert kwargs["slot_start"] == _SLOT_A_START
        assert kwargs["action"] == "charge"
        assert kwargs["predicted_soc"] == pytest.approx(62.0)
        assert kwargs["actual_soc"] == pytest.approx(64.0)
        assert kwargs["predicted_pv"] == pytest.approx(0.4)
        assert kwargs["predicted_load"] == pytest.approx(0.3)
        # 15 minutes at 2 kW PV / 1 kW load.
        assert kwargs["actual_pv"] == pytest.approx(0.5)
        assert kwargs["actual_load"] == pytest.approx(0.25)

    def test_missing_live_soc_is_scored_as_zero(self) -> None:
        """An unavailable SoC reading is passed to the scorecard as 0 %."""
        forecast_tracker = ForecastTracker()
        last_planner_output = PlannerOutput(
            slots=[PlannedSlot(start=_SLOT_A_START, end=_SLOT_B_START)]
        )
        prediction_tracker = MagicMock(spec=PredictionTracker)
        prediction_tracker.add_record.return_value = False

        _, record_added = accumulate_forecast_actuals(
            now=_SLOT_B_START + timedelta(minutes=1),
            live=_live(pv_w=0.0, load_w=0.0),
            hourly_recommendations=[
                _rec(_SLOT_A_START, _SLOT_B_START),
                _rec(_SLOT_B_START, _SLOT_C_START),
            ],
            forecast_tracker=forecast_tracker,
            last_accumulation_ts=_SLOT_A_START,
            solar_corrector=SolarForecastCorrector(),
            prediction_tracker=prediction_tracker,
            last_planner_output=last_planner_output,
            update_interval_minutes=15,
        )

        assert record_added is False
        kwargs = prediction_tracker.add_record.call_args.kwargs
        assert kwargs["actual_soc"] == pytest.approx(0.0)
        assert kwargs["action"] == "idle"


def _make_hass(config_dir: Path) -> HomeAssistant:
    """Build a minimal fake HomeAssistant exposing only ``config.config_dir``."""
    return cast(
        HomeAssistant,
        SimpleNamespace(config=SimpleNamespace(config_dir=str(config_dir))),
    )


class TestInitPredictionTracker:
    """The scorecard history lives under ``.storage`` and loads once."""

    @pytest.mark.asyncio
    async def test_sets_history_path_and_loads_history(self, tmp_path: Path) -> None:
        """A fresh tracker is pointed at its storage file and restored."""
        tracker = PredictionTracker()
        tracker.load_history = AsyncMock()  # type: ignore[method-assign]  # test spy

        await init_prediction_tracker(tracker, _make_hass(tmp_path))

        assert tracker.history_file == str(
            tmp_path / ".storage" / "hsem_prediction_history.json"
        )
        tracker.load_history.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_already_initialised_tracker_is_left_alone(
        self, tmp_path: Path
    ) -> None:
        """A tracker that already has a history file is not reloaded."""
        tracker = PredictionTracker()
        tracker.history_file = "/existing/history.json"
        tracker.load_history = AsyncMock()  # type: ignore[method-assign]  # test spy

        await init_prediction_tracker(tracker, _make_hass(tmp_path))

        assert tracker.history_file == "/existing/history.json"
        tracker.load_history.assert_not_awaited()
