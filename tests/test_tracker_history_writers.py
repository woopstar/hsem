"""Tests for the trackers' atomic history writers and the service lookup.

Each tracker persists through a temp-file-then-rename write so a crash mid-save
can never leave a truncated history behind. A write that fails must report
``False`` rather than raise into the coordinator cycle, and must not leave a
stray temp file in ``.storage``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from homeassistant.config_entries import ConfigEntryState

from custom_components.hsem.coordinator import HSEMDataUpdateCoordinator
from custom_components.hsem.models.financial_tracker import FinancialTracker
from custom_components.hsem.models.savings_tracker import SavingsTracker
from custom_components.hsem.services import _get_coordinator
from custom_components.hsem.utils.prediction_tracker import PredictionTracker


def _temp_files(directory: Path) -> list[Path]:
    """Return any leftover temp files in *directory*."""
    return [p for p in directory.iterdir() if p.name.startswith(".hsem_")]


class TestSaveHistoryRequiresAPath:
    """An uninitialised tracker reports that it did not persist."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "tracker",
        [
            pytest.param(PredictionTracker(), id="prediction"),
            pytest.param(SavingsTracker(), id="savings"),
            pytest.param(FinancialTracker(), id="financial"),
        ],
    )
    async def test_without_a_history_file_nothing_is_written(
        self, tracker: Any
    ) -> None:
        """No configured path → an honest ``False``, not a crash."""
        assert await tracker.save_history() is False


class TestAtomicHistoryWrites:
    """Each tracker writes valid JSON and leaves no temp file behind."""

    @pytest.mark.asyncio
    async def test_prediction_history_round_trips(self, tmp_path: Path) -> None:
        """The scorecard writes a file another tracker can load."""
        path = tmp_path / ".storage" / "hsem_prediction_history.json"
        tracker = PredictionTracker(history_file=str(path))

        assert await tracker.save_history() is True
        written = json.loads(path.read_text(encoding="utf-8"))
        expected = tracker.to_persistence_dict()
        # ``updated_at`` is stamped per write, so compare the rest.
        assert {k: v for k, v in written.items() if k != "updated_at"} == {
            k: v for k, v in expected.items() if k != "updated_at"
        }
        assert "updated_at" in written
        assert _temp_files(path.parent) == []

    @pytest.mark.asyncio
    async def test_savings_history_round_trips(self, tmp_path: Path) -> None:
        """Savings totals are written and restored."""
        path = tmp_path / ".storage" / "hsem_savings_history.json"
        tracker = SavingsTracker(history_file=str(path))
        tracker.actual_savings = 12.5

        assert await tracker.save_history() is True
        restored = SavingsTracker(history_file=str(path))
        await restored.load_history()

        assert restored.actual_savings == pytest.approx(12.5)
        assert _temp_files(path.parent) == []

    @pytest.mark.asyncio
    async def test_financial_history_round_trips(self, tmp_path: Path) -> None:
        """Cumulative cost and income are written and restored."""
        path = tmp_path / ".storage" / "hsem_financial_history.json"
        tracker = FinancialTracker(
            history_file=str(path), import_cost_total=40.0, export_income_total=15.0
        )

        assert await tracker.save_history() is True
        restored = FinancialTracker.from_dict(
            json.loads(path.read_text(encoding="utf-8"))
        )

        assert restored.import_cost_total == pytest.approx(40.0)
        assert restored.export_income_total == pytest.approx(15.0)
        assert _temp_files(path.parent) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("tracker_factory", "filename"),
        [
            pytest.param(PredictionTracker, "prediction.json", id="prediction"),
            pytest.param(SavingsTracker, "savings.json", id="savings"),
            pytest.param(FinancialTracker, "financial.json", id="financial"),
        ],
    )
    async def test_unwritable_location_reports_failure(
        self, tmp_path: Path, tracker_factory: Any, filename: str
    ) -> None:
        """A rejected write returns ``False`` instead of raising."""
        tracker = tracker_factory(history_file=str(tmp_path / filename))

        with patch("tempfile.mkstemp", side_effect=OSError("read-only filesystem")):
            assert await tracker.save_history() is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("tracker_factory", "filename"),
        [
            pytest.param(PredictionTracker, "prediction.json", id="prediction"),
            pytest.param(SavingsTracker, "savings.json", id="savings"),
            pytest.param(FinancialTracker, "financial.json", id="financial"),
        ],
    )
    async def test_failed_serialisation_cleans_up_the_temp_file(
        self, tmp_path: Path, tracker_factory: Any, filename: str
    ) -> None:
        """A failure mid-write removes the temp file and propagates."""
        tracker = tracker_factory(history_file=str(tmp_path / filename))

        with (
            patch("json.dump", side_effect=ValueError("not serialisable")),
            pytest.raises(ValueError, match="not serialisable"),
        ):
            await tracker.save_history()

        assert _temp_files(tmp_path) == []
        assert not (tmp_path / filename).exists()


class TestGetCoordinator:
    """Services resolve the coordinator from a loaded config entry."""

    @staticmethod
    def _hass(entries: list[MagicMock]) -> MagicMock:
        """Return a ``hass`` whose HSEM entries are *entries*."""
        hass = MagicMock()
        hass.config_entries.async_entries.return_value = entries
        return hass

    @staticmethod
    def _entry(
        *,
        state: ConfigEntryState = ConfigEntryState.LOADED,
        coordinator: object | None = None,
        runtime_data: object | None = None,
    ) -> MagicMock:
        """Return a config entry exposing *coordinator* via runtime data."""
        entry = MagicMock()
        entry.state = state
        if runtime_data is not None:
            entry.runtime_data = runtime_data
        elif coordinator is None:
            entry.runtime_data = None
        else:
            entry.runtime_data = MagicMock(coordinator=coordinator)
        return entry

    def test_returns_the_coordinator_of_a_loaded_entry(self) -> None:
        """A loaded entry's coordinator is handed to the service."""
        coordinator = MagicMock(spec=HSEMDataUpdateCoordinator)
        hass = self._hass([self._entry(coordinator=coordinator)])

        assert _get_coordinator(hass) is coordinator

    def test_no_configured_entry_returns_nothing(self) -> None:
        """Without a config entry there is no coordinator."""
        assert _get_coordinator(self._hass([])) is None

    def test_unloaded_entries_are_skipped(self) -> None:
        """An entry that is not loaded has no usable runtime data."""
        coordinator = MagicMock(spec=HSEMDataUpdateCoordinator)
        hass = self._hass(
            [self._entry(state=ConfigEntryState.NOT_LOADED, coordinator=coordinator)]
        )

        assert _get_coordinator(hass) is None

    def test_entry_without_runtime_data_is_skipped(self) -> None:
        """A loaded entry mid-setup may not have runtime data yet."""
        assert _get_coordinator(self._hass([self._entry()])) is None

    def test_runtime_data_without_a_coordinator_is_skipped(self) -> None:
        """Runtime data of another shape is ignored."""
        hass = self._hass([self._entry(runtime_data=object())])

        assert _get_coordinator(hass) is None

    def test_foreign_coordinator_object_is_rejected(self) -> None:
        """Only a real HSEM coordinator is returned."""
        hass = self._hass([self._entry(coordinator=object())])

        assert _get_coordinator(hass) is None

    def test_first_loaded_entry_wins(self) -> None:
        """The first loaded entry provides the coordinator."""
        coordinator = MagicMock(spec=HSEMDataUpdateCoordinator)
        other = MagicMock(spec=HSEMDataUpdateCoordinator)
        hass = self._hass(
            [
                self._entry(state=ConfigEntryState.SETUP_ERROR, coordinator=other),
                self._entry(coordinator=coordinator),
            ]
        )

        assert _get_coordinator(hass) is coordinator
