"""Tests for financial tracking in ``coordinator_tracking`` (issue #599).

Covers lazy initialisation of the :class:`FinancialTracker` from its JSON
history file (including corrupt and missing files), and per-cycle cost and
income accumulation from cumulative grid meters, including the stale-baseline
warning and day rollover.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from homeassistant.core import HomeAssistant

from custom_components.hsem.coordinator_tracking import (
    accumulate_financials,
    init_financial_tracker,
)
from custom_components.hsem.models.financial_tracker import (
    FinancialDayEntry,
    FinancialTracker,
)
from custom_components.hsem.models.live_state import LiveState

_MODULE = "custom_components.hsem.coordinator_tracking"
_T0 = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def _make_hass(config_dir: Path) -> HomeAssistant:
    """Build a minimal fake HomeAssistant exposing only ``config.config_dir``."""
    return cast(
        HomeAssistant,
        SimpleNamespace(config=SimpleNamespace(config_dir=str(config_dir))),
    )


def _history_path(config_dir: Path) -> Path:
    """Return the financial history file path under *config_dir*."""
    return config_dir / ".storage" / "hsem_financial_history.json"


def _live(import_kwh: float, export_kwh: float) -> LiveState:
    """Return a live snapshot with meter readings and authoritative prices."""
    live = LiveState()
    live.grid_import_energy_kwh = import_kwh
    live.grid_export_energy_kwh = export_kwh
    live.import_electricity_price = 2.0
    live.export_electricity_price = 0.5
    live.import_electricity_price_available = True
    live.export_electricity_price_available = True
    return live


class TestInitFinancialTracker:
    """The tracker is pointed at ``.storage`` and restored once."""

    @pytest.mark.asyncio
    async def test_restores_persisted_state_into_the_existing_instance(
        self, tmp_path: Path
    ) -> None:
        """Totals, day baselines, meter baselines, and the daily log survive."""
        saved = FinancialTracker(
            import_cost_total=12.5,
            export_income_total=3.25,
            _today_start_import_cost=10.0,
            _today_start_export_income=3.0,
            today="2026-06-01",
            history_file=str(_history_path(tmp_path)),
        )
        saved._last_import_energy_kwh = 101.0
        saved._last_export_energy_kwh = 42.0
        saved._last_import_sample_at = _T0
        saved._last_export_sample_at = _T0
        saved._last_import_price = 2.0
        saved._last_export_price = 0.5
        saved.daily_log["2026-05-31"] = FinancialDayEntry(
            date="2026-05-31", import_cost=4.0, export_income=1.0
        )
        assert await saved.save_history()

        tracker = FinancialTracker()
        await init_financial_tracker(tracker, _make_hass(tmp_path))

        assert tracker.history_file == str(_history_path(tmp_path))
        assert tracker.import_cost_total == pytest.approx(12.5)
        assert tracker.export_income_total == pytest.approx(3.25)
        assert tracker._today_start_import_cost == pytest.approx(10.0)
        assert tracker._today_start_export_income == pytest.approx(3.0)
        assert tracker.today == "2026-06-01"
        assert tracker._last_import_energy_kwh == pytest.approx(101.0)
        assert tracker._last_export_energy_kwh == pytest.approx(42.0)
        assert tracker._last_import_sample_at == _T0
        assert tracker._last_export_sample_at == _T0
        assert tracker._last_import_price == pytest.approx(2.0)
        assert tracker._last_export_price == pytest.approx(0.5)
        assert tracker.daily_log["2026-05-31"].import_cost == pytest.approx(4.0)

    @pytest.mark.asyncio
    async def test_initialises_only_once(self, tmp_path: Path) -> None:
        """A second call does not reload and overwrite in-memory totals."""
        assert await FinancialTracker(
            import_cost_total=1.0, history_file=str(_history_path(tmp_path))
        ).save_history()
        tracker = FinancialTracker()
        hass = _make_hass(tmp_path)
        await init_financial_tracker(tracker, hass)
        assert tracker.import_cost_total == pytest.approx(1.0)
        tracker.import_cost_total = 99.0

        await init_financial_tracker(tracker, hass)

        assert tracker.import_cost_total == pytest.approx(99.0)

    @pytest.mark.asyncio
    async def test_missing_history_file_starts_empty(self, tmp_path: Path) -> None:
        """No file on disk → a fresh, initialised tracker."""
        tracker = FinancialTracker()

        await init_financial_tracker(tracker, _make_hass(tmp_path))

        assert tracker._initialized is True  # type: ignore[attr-defined]
        assert tracker.import_cost_total == pytest.approx(0.0)
        assert tracker.daily_log == {}

    @pytest.mark.asyncio
    async def test_unparseable_history_file_is_ignored(self, tmp_path: Path) -> None:
        """Invalid JSON leaves the tracker empty without logging an error."""
        path = _history_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        tracker = FinancialTracker()
        log = MagicMock()

        with patch(f"{_MODULE}.async_log", log):
            await init_financial_tracker(tracker, _make_hass(tmp_path))

        assert tracker.import_cost_total == pytest.approx(0.0)
        log.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_history_values_are_logged_and_ignored(
        self, tmp_path: Path
    ) -> None:
        """A structurally valid file with bad values logs an error."""
        path = _history_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"import_cost_total": "not a number"}), encoding="utf-8"
        )
        tracker = FinancialTracker()
        log = MagicMock()

        with patch(f"{_MODULE}.async_log", log):
            await init_financial_tracker(tracker, _make_hass(tmp_path))

        assert tracker.import_cost_total == pytest.approx(0.0)
        assert tracker._initialized is True  # type: ignore[attr-defined]
        log.assert_called_once()
        assert log.call_args.args[0] == "error"

    @pytest.mark.asyncio
    async def test_broken_hass_is_logged_and_does_not_raise(self) -> None:
        """Without a config dir the tracker is marked initialised anyway."""
        tracker = FinancialTracker()
        log = MagicMock()

        with patch(f"{_MODULE}.async_log", log):
            await init_financial_tracker(
                tracker, cast(HomeAssistant, SimpleNamespace())
            )

        assert tracker._initialized is True  # type: ignore[attr-defined]
        log.assert_called_once()
        assert log.call_args.args[0] == "error"


class TestAccumulateFinancials:
    """Each cycle prices the meter deltas since the previous sample."""

    @pytest.mark.asyncio
    async def test_contiguous_samples_are_priced(self, tmp_path: Path) -> None:
        """1 kWh imported at 2.0 and 1 kWh exported at 0.5 are booked."""
        tracker = FinancialTracker(today=_T0.date().isoformat())
        hass = _make_hass(tmp_path)
        log = MagicMock()

        with patch(f"{_MODULE}.async_log", log):
            await accumulate_financials(
                now=_T0,
                live=_live(10.0, 4.0),
                financial_tracker=tracker,
                hass=hass,
                update_interval_minutes=5,
            )
            await accumulate_financials(
                now=_T0 + timedelta(minutes=5),
                live=_live(11.0, 5.0),
                financial_tracker=tracker,
                hass=hass,
                update_interval_minutes=5,
            )

        assert tracker.import_cost_total == pytest.approx(2.0)
        assert tracker.export_income_total == pytest.approx(0.5)
        log.assert_not_called()

    @pytest.mark.asyncio
    async def test_stale_baseline_is_not_replayed(self, tmp_path: Path) -> None:
        """A gap beyond twice the interval rebaselines and warns."""
        tracker = FinancialTracker(today=_T0.date().isoformat())
        tracker._initialized = True  # type: ignore[attr-defined]  # skip lazy init
        hass = _make_hass(tmp_path)
        log = MagicMock()

        with patch(f"{_MODULE}.async_log", log):
            await accumulate_financials(
                now=_T0,
                live=_live(10.0, 4.0),
                financial_tracker=tracker,
                hass=hass,
                update_interval_minutes=5,
            )
            await accumulate_financials(
                now=_T0 + timedelta(hours=1),
                live=_live(20.0, 8.0),
                financial_tracker=tracker,
                hass=hass,
                update_interval_minutes=5,
            )

        assert tracker.import_cost_total == pytest.approx(0.0)
        assert tracker.export_income_total == pytest.approx(0.0)
        assert tracker._last_import_energy_kwh == pytest.approx(20.0)
        log.assert_called_once()
        assert log.call_args.args[0] == "warning"

    @pytest.mark.asyncio
    async def test_first_sample_after_midnight_rolls_the_day(
        self, tmp_path: Path
    ) -> None:
        """The interval is priced first, then yesterday is snapshotted."""
        yesterday = _T0 - timedelta(days=1)
        tracker = FinancialTracker(today=yesterday.date().isoformat())
        tracker._initialized = True  # type: ignore[attr-defined]  # skip lazy init
        hass = _make_hass(tmp_path)
        before_midnight = datetime(2026, 5, 31, 23, 58, tzinfo=UTC)

        await accumulate_financials(
            now=before_midnight,
            live=_live(10.0, 4.0),
            financial_tracker=tracker,
            hass=hass,
            update_interval_minutes=5,
        )
        await accumulate_financials(
            now=before_midnight + timedelta(minutes=4),
            live=_live(11.0, 4.0),
            financial_tracker=tracker,
            hass=hass,
            update_interval_minutes=5,
        )

        assert tracker.today == "2026-06-01"
        assert tracker.daily_log["2026-05-31"].import_cost == pytest.approx(2.0)
