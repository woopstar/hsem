"""Behaviour tests for ``persist_all_trackers`` (issue #890).

``test_coordinator_persistence_registry.py`` asserts the registry is
*complete*; these tests assert it is *used correctly*: every selected
tracker is written, the prediction tracker is skipped until it has a
history file, and a failed write is reported with the tracker's short name.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.coordinator_persistence import (
    TRACKER_REGISTRY,
    persist_all_trackers,
)
from custom_components.hsem.coordinator_state import CoordinatorSharedState
from custom_components.hsem.models.financial_tracker import FinancialTracker
from custom_components.hsem.models.savings_tracker import SavingsTracker
from custom_components.hsem.utils.prediction_tracker import PredictionTracker

_MODULE = "custom_components.hsem.coordinator_persistence"


def _coordinator(
    tmp_path: Path, *, prediction_history: bool = True
) -> CoordinatorSharedState:
    """Return a stand-in coordinator owning all three persistable trackers."""
    coordinator = MagicMock()
    coordinator._savings_tracker = SavingsTracker(
        history_file=str(tmp_path / "savings.json")
    )
    coordinator._financial_tracker = FinancialTracker(
        history_file=str(tmp_path / "financial.json")
    )
    coordinator._prediction_tracker = PredictionTracker(
        history_file=str(tmp_path / "prediction.json") if prediction_history else ""
    )
    return cast(CoordinatorSharedState, coordinator)


class TestPersistAllTrackers:
    """The registry loop writes exactly the trackers it was asked for."""

    @pytest.mark.asyncio
    async def test_persists_every_registered_tracker_by_default(
        self, tmp_path: Path
    ) -> None:
        """No ``only`` filter → every registered tracker is written."""
        coordinator = _coordinator(tmp_path)

        await persist_all_trackers(coordinator)

        for name in ("savings.json", "financial.json", "prediction.json"):
            assert (tmp_path / name).exists(), name

    @pytest.mark.asyncio
    async def test_only_filter_restricts_the_writes(self, tmp_path: Path) -> None:
        """Callers with their own cadence persist just their tracker."""
        coordinator = _coordinator(tmp_path)

        await persist_all_trackers(coordinator, only=["_savings_tracker"])

        assert (tmp_path / "savings.json").exists()
        assert not (tmp_path / "financial.json").exists()
        assert not (tmp_path / "prediction.json").exists()

    @pytest.mark.asyncio
    async def test_prediction_tracker_is_skipped_before_setup(
        self, tmp_path: Path
    ) -> None:
        """Without a history file the prediction tracker is not persisted."""
        coordinator = _coordinator(tmp_path, prediction_history=False)
        save_history = AsyncMock(return_value=True)
        coordinator._prediction_tracker.save_history = save_history  # type: ignore[method-assign]  # test spy

        await persist_all_trackers(coordinator, only=["_prediction_tracker"])

        save_history.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_write_is_logged_with_the_tracker_name(
        self, tmp_path: Path
    ) -> None:
        """A tracker that reports failure is named in the warning."""
        coordinator = _coordinator(tmp_path)
        coordinator._savings_tracker.save_history = AsyncMock(return_value=False)  # type: ignore[method-assign]  # force failure
        log = MagicMock()

        with patch(f"{_MODULE}.async_log", log):
            await persist_all_trackers(coordinator, only=["_savings_tracker"])

        log.assert_called_once_with(
            "warning", "Failed to persist %s tracker state", "savings"
        )

    def test_registry_keys_match_coordinator_attributes(self) -> None:
        """Every registry key is the attribute name it reads."""
        assert set(TRACKER_REGISTRY) == {
            "_savings_tracker",
            "_financial_tracker",
            "_prediction_tracker",
        }
