"""A history file that failed to load is never overwritten (issue #1446).

Before the fix the shared reader returned ``None`` for a file it could not
read, each tracker started empty with its file path set, and the next save
replaced weeks of history. These tests cover the shared loader in
``utils/persistence.py`` and every tracker that restores through it:

- a missing file starts an empty tracker without a warning,
- a file that cannot be read (``OSError``) stays on disk, is not written,
  and is loaded on a later attempt,
- a file that cannot be parsed or restored is moved to
  ``<name>.corrupt-<timestamp>`` before a new file is written.
"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from homeassistant.core import HomeAssistant

from custom_components.hsem.coordinator_persistence import persist_all_trackers
from custom_components.hsem.coordinator_price_history import (
    init_price_history_tracker,
)
from custom_components.hsem.coordinator_state import CoordinatorSharedState
from custom_components.hsem.coordinator_tracking import (
    accumulate_daily_plan_actuals,
    init_financial_tracker,
)
from custom_components.hsem.models.daily_metrics import DailyMetrics
from custom_components.hsem.models.daily_plan_vs_actual_tracker import (
    DailyPlanVsActualTracker,
)
from custom_components.hsem.models.daily_record import DailyRecord
from custom_components.hsem.models.energy_tracker import EnergyTracker
from custom_components.hsem.models.financial_tracker import FinancialTracker
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.performance_tracker import PerformanceTracker
from custom_components.hsem.models.price_history_tracker import PriceHistoryTracker
from custom_components.hsem.models.savings_day import SavingsDay
from custom_components.hsem.models.savings_tracker import SavingsTracker
from custom_components.hsem.models.what_if_history import WhatIfHistory
from custom_components.hsem.utils import persistence
from custom_components.hsem.utils.efficiency_learner import (
    CHARGE,
    DISCHARGE,
    STORAGE_VERSION,
    EfficiencyLearner,
    EfficiencyStep,
)
from custom_components.hsem.utils.persistence import (
    async_load_history,
    ensure_history_loaded,
    history_load_pending,
    load_json_history_file,
    read_json_history_file,
    set_aside_history_file,
    write_json_history_file,
)
from custom_components.hsem.utils.prediction_tracker import PredictionTracker

_PERSISTENCE = "custom_components.hsem.utils.persistence"
_REAL_OPEN = os.open

TRACKER_CLASSES: list[type[Any]] = [
    EnergyTracker,
    PerformanceTracker,
    FinancialTracker,
    SavingsTracker,
    DailyPlanVsActualTracker,
    EfficiencyLearner,
    PriceHistoryTracker,
    WhatIfHistory,
    PredictionTracker,
]


@contextmanager
def _unreadable(path: Path) -> Iterator[None]:
    """Make opening *path* for reading fail, as a locked file would."""

    def _open(target: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if Path(target) == path:
            raise PermissionError(13, "Permission denied")
        return _REAL_OPEN(target, flags, *args, **kwargs)

    with patch(f"{_PERSISTENCE}.os.open", side_effect=_open):
        yield


@pytest.fixture
def warnings() -> Iterator[MagicMock]:
    """Capture the warnings of the persistence module.

    The HSEM logger does not propagate, so ``caplog`` does not see them.
    """
    with patch.object(persistence._LOGGER, "warning") as warning:
        yield warning


def _corrupt_copies(path: Path) -> list[Path]:
    """Return the set-aside copies of a history file."""
    return sorted(path.parent.glob(f"{path.name}.corrupt-*"))


def _save(tracker: Any) -> Awaitable[bool]:
    """Save a tracker the way production does, forcing a write."""
    if isinstance(tracker, PriceHistoryTracker):
        tracker._dirty = True
    if isinstance(tracker, DailyPlanVsActualTracker):
        return tracker._write_history_file()
    return cast(Callable[[], Awaitable[bool]], tracker.save_history)()


async def _seed(cls: type[Any], path: Path) -> bytes:
    """Write a file the tracker class produced itself, with a marker key."""
    assert await _save(cls(history_file=str(path))) is True
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["marker"] = "written before the failed load"
    path.write_text(json.dumps(payload, indent=4), encoding="utf-8")
    return path.read_bytes()


def _make_hass(config_dir: Path) -> HomeAssistant:
    """Build a minimal fake HomeAssistant exposing only ``config.config_dir``."""
    return cast(
        HomeAssistant,
        SimpleNamespace(config=SimpleNamespace(config_dir=str(config_dir))),
    )


class TestLoadJsonHistoryFile:
    """The shared reader tells missing, unreadable and invalid files apart."""

    def test_a_valid_file_is_returned(self, tmp_path: Path) -> None:
        """A JSON object is parsed and the file is not marked as pending."""
        path = tmp_path / "history.json"
        path.write_text('{"days": [1]}', encoding="utf-8")

        assert load_json_history_file(path) == {"days": [1]}
        assert not history_load_pending(path)

    def test_a_missing_file_is_empty_without_a_warning(
        self, tmp_path: Path, warnings: MagicMock
    ) -> None:
        """A first start has no file: nothing to restore, nothing to report."""
        path = tmp_path / "missing.json"

        assert load_json_history_file(path) is None

        assert not history_load_pending(path)
        warnings.assert_not_called()
        assert write_json_history_file(path, {"value": 1}) is True

    def test_a_file_that_vanishes_before_the_open_is_missing(
        self, tmp_path: Path, warnings: MagicMock
    ) -> None:
        """A file deleted between the stat and the open counts as missing."""
        path = tmp_path / "history.json"
        path.write_text("{}", encoding="utf-8")

        with patch(f"{_PERSISTENCE}.os.open", side_effect=FileNotFoundError):
            assert load_json_history_file(path) is None

        assert not history_load_pending(path)
        warnings.assert_not_called()

    def test_a_failed_open_blocks_the_write(
        self, tmp_path: Path, warnings: MagicMock
    ) -> None:
        """An ``OSError`` on open leaves the file alone and refuses a save."""
        path = tmp_path / "history.json"
        path.write_text('{"value": 1}', encoding="utf-8")

        with _unreadable(path):
            assert load_json_history_file(path) is None

        assert history_load_pending(path)
        assert write_json_history_file(path, {"value": 2}) is False
        assert json.loads(path.read_text(encoding="utf-8")) == {"value": 1}
        assert _corrupt_copies(path) == []
        warnings.assert_called_once()
        assert path in warnings.call_args.args

    def test_a_failed_stat_blocks_the_write(self, tmp_path: Path) -> None:
        """An ``OSError`` other than "not found" on the stat is a failed read."""
        path = tmp_path / "history.json"
        path.write_text('{"value": 1}', encoding="utf-8")

        with patch.object(Path, "lstat", side_effect=PermissionError(13, "denied")):
            assert load_json_history_file(path) is None

        assert history_load_pending(path)
        assert write_json_history_file(path, {"value": 2}) is False

    def test_a_failed_read_blocks_the_write(self, tmp_path: Path) -> None:
        """An I/O error while reading the opened file is a failed read."""
        path = tmp_path / "history.json"
        path.write_text('{"value": 1}', encoding="utf-8")

        with patch(f"{_PERSISTENCE}.json.load", side_effect=OSError(5, "I/O error")):
            assert load_json_history_file(path) is None

        assert history_load_pending(path)
        assert write_json_history_file(path, {"value": 2}) is False
        assert json.loads(path.read_text(encoding="utf-8")) == {"value": 1}

    def test_the_warning_is_logged_once_per_file(
        self, tmp_path: Path, warnings: MagicMock
    ) -> None:
        """A load retried every cycle does not repeat the warning."""
        path = tmp_path / "history.json"
        path.write_text("{}", encoding="utf-8")

        with _unreadable(path):
            for _ in range(3):
                assert load_json_history_file(path) is None

        warnings.assert_called_once()

    def test_a_later_successful_load_allows_the_write_again(
        self, tmp_path: Path
    ) -> None:
        """The block ends with the first load that gets through."""
        path = tmp_path / "history.json"
        path.write_text('{"value": 1}', encoding="utf-8")
        with _unreadable(path):
            load_json_history_file(path)

        assert load_json_history_file(path) == {"value": 1}

        assert not history_load_pending(path)
        assert write_json_history_file(path, {"value": 2}) is True

    def test_a_file_that_disappears_allows_the_write_again(
        self, tmp_path: Path
    ) -> None:
        """A file the user removed has nothing left to protect."""
        path = tmp_path / "history.json"
        path.write_text('{"value": 1}', encoding="utf-8")
        with _unreadable(path):
            load_json_history_file(path)
        path.unlink()

        assert load_json_history_file(path) is None

        assert not history_load_pending(path)
        assert write_json_history_file(path, {"value": 2}) is True

    @pytest.mark.parametrize(
        "content",
        [b"{not valid json", b"[1, 2, 3]", b"\xff\xfe\x00 not utf-8", b"[" * 100_000],
        ids=["invalid-json", "not-an-object", "not-utf-8", "nested-too-deep"],
    )
    def test_an_unparseable_file_is_set_aside(
        self, tmp_path: Path, warnings: MagicMock, content: bytes
    ) -> None:
        """A parse error keeps the file under another name and starts empty."""
        path = tmp_path / "history.json"
        path.write_bytes(content)

        assert load_json_history_file(path) is None

        assert not path.exists()
        (kept,) = _corrupt_copies(path)
        assert kept.read_bytes() == content
        assert not history_load_pending(path)
        warnings.assert_called_once()
        assert path in warnings.call_args.args
        assert kept.name in warnings.call_args.args
        assert write_json_history_file(path, {"value": 2}) is True
        assert kept.read_bytes() == content

    def test_an_oversized_file_is_set_aside(self, tmp_path: Path) -> None:
        """A file over the size cap is kept, not parsed and not overwritten."""
        path = tmp_path / "history.json"
        path.write_text('{"value": 1}', encoding="utf-8")

        assert load_json_history_file(path, max_bytes=4) is None

        assert not path.exists()
        assert len(_corrupt_copies(path)) == 1

    def test_a_symlink_is_set_aside_without_touching_its_target(
        self, tmp_path: Path
    ) -> None:
        """A symlink is never followed; the link itself is moved."""
        target = tmp_path / "target.json"
        target.write_text('{"value": 1}', encoding="utf-8")
        path = tmp_path / "history.json"
        path.symlink_to(target)

        assert load_json_history_file(path) is None

        assert not path.is_symlink()
        assert target.read_text(encoding="utf-8") == '{"value": 1}'
        assert len(_corrupt_copies(path)) == 1

    def test_a_file_that_cannot_be_moved_aside_blocks_the_write(
        self, tmp_path: Path, warnings: MagicMock
    ) -> None:
        """Without the rename the corrupt file stays and is not replaced."""
        path = tmp_path / "history.json"
        path.write_text("{not valid json", encoding="utf-8")

        with patch(f"{_PERSISTENCE}.os.replace", side_effect=OSError(30, "read-only")):
            assert load_json_history_file(path) is None

        assert history_load_pending(path)
        assert write_json_history_file(path, {"value": 2}) is False
        assert path.read_text(encoding="utf-8") == "{not valid json"
        warnings.assert_called_once()

        # The next load moves it aside and the tracker can save again.
        assert load_json_history_file(path) is None
        assert not history_load_pending(path)
        assert len(_corrupt_copies(path)) == 1

    def test_set_aside_reports_the_move(self, tmp_path: Path) -> None:
        """The helper returns whether the file was moved."""
        path = tmp_path / "history.json"
        path.write_text("{}", encoding="utf-8")

        assert set_aside_history_file(path, "a test") is True
        assert set_aside_history_file(path, "a test") is False

        assert history_load_pending(path)
        persistence._PENDING_LOADS.discard(path)

    def test_the_plain_reader_changes_nothing_on_disk(self, tmp_path: Path) -> None:
        """``read_json_history_file`` (backup, debug export) stays read-only."""
        path = tmp_path / "history.json"
        path.write_text("{not valid json", encoding="utf-8")

        assert read_json_history_file(path) is None
        with _unreadable(path):
            assert read_json_history_file(path) is None

        assert path.read_text(encoding="utf-8") == "{not valid json"
        assert _corrupt_copies(path) == []
        assert not history_load_pending(path)


class TestAsyncLoadHistory:
    """The async wrapper hands the file to the tracker's restore."""

    @pytest.mark.asyncio
    async def test_content_reaches_restore(self, tmp_path: Path) -> None:
        """A valid file is passed on unchanged."""
        path = tmp_path / "history.json"
        path.write_text('{"value": 1}', encoding="utf-8")
        seen: list[dict[str, Any]] = []

        await async_load_history(path, seen.append)

        assert seen == [{"value": 1}]

    @pytest.mark.asyncio
    async def test_restore_is_not_called_without_content(self, tmp_path: Path) -> None:
        """A missing file never reaches the tracker."""
        restore = MagicMock()

        await async_load_history(tmp_path / "missing.json", restore)

        restore.assert_not_called()

    @pytest.mark.asyncio
    async def test_content_the_tracker_rejects_is_set_aside(
        self, tmp_path: Path, warnings: MagicMock
    ) -> None:
        """Valid JSON the tracker cannot restore is kept under another name."""
        path = tmp_path / "history.json"
        path.write_text('{"value": "wrong"}', encoding="utf-8")

        await async_load_history(path, MagicMock(side_effect=ValueError("bad value")))

        assert not path.exists()
        (kept,) = _corrupt_copies(path)
        assert kept.read_text(encoding="utf-8") == '{"value": "wrong"}'
        warnings.assert_called_once()

    @pytest.mark.asyncio
    async def test_rejected_content_that_cannot_be_moved_warns_once(
        self, tmp_path: Path, warnings: MagicMock
    ) -> None:
        """Retries of a file that is rejected and cannot be moved stay silent."""
        path = tmp_path / "history.json"
        path.write_text('{"value": "wrong"}', encoding="utf-8")
        restore = MagicMock(side_effect=ValueError("bad value"))

        with patch(f"{_PERSISTENCE}.os.replace", side_effect=OSError(30, "read-only")):
            for _ in range(3):
                await async_load_history(path, restore)
                assert history_load_pending(path)
                assert write_json_history_file(path, {"value": 2}) is False

        warnings.assert_called_once()
        assert path.read_text(encoding="utf-8") == '{"value": "wrong"}'

        # The move works again: the file is kept aside and a save may follow.
        await async_load_history(path, restore)
        assert not history_load_pending(path)
        assert len(_corrupt_copies(path)) == 1

    def test_two_files_set_aside_in_the_same_second_are_both_kept(
        self, tmp_path: Path
    ) -> None:
        """The stamp has microseconds, so a second copy does not replace one."""
        path = tmp_path / "history.json"
        for content in ("first", "second"):
            path.write_text(content, encoding="utf-8")
            assert set_aside_history_file(path, "a test") is True

        assert sorted(kept.read_text() for kept in _corrupt_copies(path)) == [
            "first",
            "second",
        ]


class TestEnsureHistoryLoaded:
    """The retry that runs before a save."""

    @pytest.mark.asyncio
    async def test_a_tracker_without_a_file_is_ready(self) -> None:
        """No history file: nothing to load, and the caller decides the rest."""
        assert await ensure_history_loaded(SavingsTracker()) is True
        assert await ensure_history_loaded(MagicMock()) is True

    @pytest.mark.asyncio
    async def test_a_loaded_tracker_is_not_loaded_again(self, tmp_path: Path) -> None:
        """A tracker whose file loaded keeps its in-memory state."""
        path = tmp_path / "savings.json"
        tracker = SavingsTracker(history_file=str(path))
        assert await tracker.save_history() is True
        tracker.actual_savings = 7.5

        assert await ensure_history_loaded(tracker) is True

        assert tracker.actual_savings == pytest.approx(7.5)

    @pytest.mark.asyncio
    async def test_a_failed_load_is_retried_until_it_succeeds(
        self, tmp_path: Path
    ) -> None:
        """The state on disk is restored as soon as the file can be read."""
        path = tmp_path / "savings.json"
        saved = SavingsTracker(history_file=str(path), actual_savings=12.5)
        assert await saved.save_history() is True

        tracker = SavingsTracker(history_file=str(path))
        with _unreadable(path):
            await tracker.load_history()
            assert await ensure_history_loaded(tracker) is False
            assert tracker.actual_savings == pytest.approx(0.0)

        assert await ensure_history_loaded(tracker) is True
        assert tracker.actual_savings == pytest.approx(12.5)


class TestEveryTracker:
    """All trackers get the behaviour from the shared loader."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("cls", TRACKER_CLASSES, ids=lambda cls: cls.__name__)
    async def test_a_missing_file_starts_empty_without_a_warning(
        self, tmp_path: Path, warnings: MagicMock, cls: type[Any]
    ) -> None:
        """A first start loads nothing and can save right away."""
        path = tmp_path / "history.json"
        tracker = cls(history_file=str(path))

        await tracker.load_history()

        warnings.assert_not_called()
        assert await _save(tracker) is True
        assert path.exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("cls", TRACKER_CLASSES, ids=lambda cls: cls.__name__)
    async def test_an_unreadable_file_is_never_overwritten(
        self, tmp_path: Path, warnings: MagicMock, cls: type[Any]
    ) -> None:
        """The regression of issue #1446: an ``OSError`` cost the history."""
        path = tmp_path / "history.json"
        original = await _seed(cls, path)

        tracker = cls(history_file=str(path))
        with _unreadable(path):
            await tracker.load_history()

        assert await _save(tracker) is False
        assert path.read_bytes() == original
        assert _corrupt_copies(path) == []
        warnings.assert_called_once()

        # The file is readable again: the load is retried, then the save runs.
        assert await ensure_history_loaded(tracker) is True
        assert await _save(tracker) is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("cls", TRACKER_CLASSES, ids=lambda cls: cls.__name__)
    async def test_a_corrupt_file_is_kept_before_a_new_one_is_written(
        self, tmp_path: Path, warnings: MagicMock, cls: type[Any]
    ) -> None:
        """A parse error moves the file aside; the tracker starts empty."""
        path = tmp_path / "history.json"
        path.write_text('{"days": [1, 2', encoding="utf-8")

        tracker = cls(history_file=str(path))
        await tracker.load_history()

        (kept,) = _corrupt_copies(path)
        assert kept.read_text(encoding="utf-8") == '{"days": [1, 2'
        warnings.assert_called_once()
        assert await _save(tracker) is True
        assert json.loads(path.read_text(encoding="utf-8"))
        assert kept.read_text(encoding="utf-8") == '{"days": [1, 2'

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "cls",
        [
            SavingsTracker,
            DailyPlanVsActualTracker,
            FinancialTracker,
            PriceHistoryTracker,
        ],
        ids=lambda cls: cls.__name__,
    )
    async def test_a_tracker_without_a_file_loads_nothing(
        self, tmp_path: Path, cls: type[Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty path is not resolved to the working directory."""
        monkeypatch.chdir(tmp_path)

        await cls().load_history()

        assert list(tmp_path.parent.glob(f"{tmp_path.name}.corrupt-*")) == []
        assert tmp_path.is_dir()


class TestTrackerRestores:
    """Trackers restore real content through the shared loader."""

    @pytest.mark.asyncio
    async def test_savings_content_that_cannot_be_restored_is_set_aside(
        self, tmp_path: Path
    ) -> None:
        """Valid JSON with an unusable total is kept, not overwritten."""
        path = tmp_path / "savings.json"
        content = '{"actual_savings": "a lot", "days": []}'
        path.write_text(content, encoding="utf-8")

        tracker = SavingsTracker(history_file=str(path), baseline_cost=3.0)
        await tracker.load_history()
        assert tracker.baseline_cost == pytest.approx(3.0)
        assert await tracker.save_history() is True

        (kept,) = _corrupt_copies(path)
        assert kept.read_text(encoding="utf-8") == content

    @pytest.mark.asyncio
    async def test_financial_tracker_restores_through_init(
        self, tmp_path: Path
    ) -> None:
        """``init_financial_tracker`` loads the file the tracker wrote."""
        path = tmp_path / ".storage" / "hsem_financial_history.json"
        saved = FinancialTracker(history_file=str(path), import_cost_total=12.5)
        assert await saved.save_history() is True

        tracker = FinancialTracker()
        await init_financial_tracker(tracker, _make_hass(tmp_path))

        assert tracker.import_cost_total == pytest.approx(12.5)
        assert tracker.history_file == str(path)

    @pytest.mark.asyncio
    async def test_financial_tracker_keeps_an_unreadable_file(
        self, tmp_path: Path
    ) -> None:
        """A failed read at setup is retried by the next persist."""
        path = tmp_path / ".storage" / "hsem_financial_history.json"
        saved = FinancialTracker(history_file=str(path), import_cost_total=12.5)
        assert await saved.save_history() is True
        original = path.read_bytes()

        tracker = FinancialTracker()
        with _unreadable(path):
            await init_financial_tracker(tracker, _make_hass(tmp_path))
            assert await tracker.save_history() is False
        assert path.read_bytes() == original

        assert await ensure_history_loaded(tracker) is True
        assert tracker.import_cost_total == pytest.approx(12.5)

    @pytest.mark.asyncio
    async def test_price_history_restores_through_init(self, tmp_path: Path) -> None:
        """``init_price_history_tracker`` loads the file the tracker wrote."""
        path = tmp_path / ".storage" / "hsem_price_history.json"
        saved = PriceHistoryTracker(history_file=str(path))
        saved.source = {"import_entity": "sensor.price"}
        saved._dirty = True
        assert await saved.save_history() is True

        tracker = PriceHistoryTracker()
        await init_price_history_tracker(tracker, _make_hass(tmp_path))

        assert tracker.history_file == str(path)
        assert tracker.source["import_entity"] == "sensor.price"

    @pytest.mark.asyncio
    async def test_daily_record_is_added_to_a_history_loaded_late(
        self, tmp_path: Path
    ) -> None:
        """A day closed while the file was unreadable is not lost."""
        path = tmp_path / "daily.json"
        saved = DailyPlanVsActualTracker(history_file=str(path))
        assert await saved._save_record_to_history(DailyRecord(date="2026-11-19"))

        tracker = DailyPlanVsActualTracker(history_file=str(path))
        with _unreadable(path):
            await tracker.load_history()
            assert not await tracker._save_record_to_history(
                DailyRecord(date="2026-11-20")
            )
        assert [day["date"] for day in json.loads(path.read_text())["days"]] == [
            "2026-11-19"
        ]

        # Readable again at the next rollover: the file's day, the day that
        # only lived in memory, and the new day are all written.
        assert await tracker._save_record_to_history(DailyRecord(date="2026-11-21"))

        assert [day["date"] for day in json.loads(path.read_text())["days"]] == [
            "2026-11-19",
            "2026-11-20",
            "2026-11-21",
        ]

    @pytest.mark.asyncio
    async def test_daily_file_wins_for_a_day_that_is_in_both(
        self, tmp_path: Path
    ) -> None:
        """A late load keeps the file's record of a day over the pending one."""
        path = tmp_path / "daily.json"
        saved = DailyPlanVsActualTracker(history_file=str(path))
        on_disk = DailyRecord(
            date="2026-11-20", actual=DailyMetrics(grid_import_kwh=7.0)
        )
        assert await saved._save_record_to_history(on_disk)

        tracker = DailyPlanVsActualTracker(history_file=str(path))
        tracker.history = [
            DailyRecord(date="2026-11-20", actual=DailyMetrics(grid_import_kwh=1.0)),
            DailyRecord(date="2026-11-21"),
        ]
        await tracker.load_history()

        assert [record.date for record in tracker.history] == [
            "2026-11-20",
            "2026-11-21",
        ]
        assert tracker.history[0].actual.grid_import_kwh == pytest.approx(7.0)

    @pytest.mark.asyncio
    async def test_daily_history_is_retried_every_cycle(self, tmp_path: Path) -> None:
        """The daily tracker saves once a day, so the cycle retries the load."""
        path = tmp_path / "daily.json"
        saved = DailyPlanVsActualTracker(history_file=str(path))
        assert await saved._save_record_to_history(DailyRecord(date="2026-11-19"))

        now = datetime(2026, 11, 21, 10, 0, tzinfo=UTC)
        tracker = DailyPlanVsActualTracker(
            history_file=str(path), today=now.date().isoformat()
        )
        tracker._initialized = True  # type: ignore[attr-defined]  # skip lazy init
        with _unreadable(path):
            await tracker.load_history()
        assert tracker.history == []

        await accumulate_daily_plan_actuals(
            now=now,
            live=LiveState(),
            output=None,
            daily_tracker=tracker,
            daily_plan_last_accumulated=None,
            hass=_make_hass(tmp_path),
        )

        assert [record.date for record in tracker.history] == ["2026-11-19"]

    @pytest.mark.asyncio
    async def test_savings_collected_during_a_failed_load_are_added(
        self, tmp_path: Path
    ) -> None:
        """A late load adds the file to what was collected; totals match days."""
        path = tmp_path / "savings.json"
        saved = SavingsTracker(history_file=str(path), actual_savings=12.5)
        today = saved._today
        saved.daily[today].actual_savings = 2.5
        saved.daily["2026-01-01"] = SavingsDay(date="2026-01-01", actual_savings=10.0)
        assert await saved.save_history() is True

        tracker = SavingsTracker(history_file=str(path))
        with _unreadable(path):
            await tracker.load_history()
            tracker.actual_savings += 1.0
            tracker.daily[today].actual_savings += 1.0
            assert await ensure_history_loaded(tracker) is False

        assert await ensure_history_loaded(tracker) is True

        assert tracker.actual_savings == pytest.approx(13.5)
        assert tracker.daily[today].actual_savings == pytest.approx(3.5)
        assert tracker.daily["2026-01-01"].actual_savings == pytest.approx(10.0)
        assert sum(
            day.actual_savings for day in tracker.daily.values()
        ) == pytest.approx(tracker.actual_savings)


class TestEfficiencyLearnerInfinity:
    """A JSON ``Infinity`` in ``samples`` does not abort the restore."""

    @staticmethod
    def _payload() -> str:
        """Return a learner file whose charge side holds ``Infinity``."""
        good = '{"samples": 5, "measured_kwh": 2.0, "stored_kwh": 1.8}'
        bad = '{"samples": Infinity, "measured_kwh": 2.0, "stored_kwh": 1.8}'
        return (
            f'{{"version": {STORAGE_VERSION},'
            f' "{CHARGE}": {{"total": {good}, "steps": {{"1000": {bad},'
            f' "2000": {good}}}}},'
            f' "{DISCHARGE}": {{"total": {good}, "steps": {{"1000": {good}}}}}}}'
        )

    def test_restore_skips_the_entry_and_reads_the_rest(self) -> None:
        """The invalid step is dropped; everything after it is restored."""
        learner = EfficiencyLearner()

        learner.restore(json.loads(self._payload()))

        assert learner.charge_total == EfficiencyStep(5, 2.0, 1.8)
        assert sorted(learner._steps(CHARGE)) == [2000]
        assert learner.discharge_total == EfficiencyStep(5, 2.0, 1.8)
        assert sorted(learner._steps(DISCHARGE)) == [1000]

    @pytest.mark.asyncio
    async def test_the_file_is_loaded_and_not_set_aside(self, tmp_path: Path) -> None:
        """The learner file loads from disk and stays where it is."""
        path = tmp_path / "efficiency.json"
        path.write_text(self._payload(), encoding="utf-8")

        learner = EfficiencyLearner(history_file=str(path))
        await learner.load_history()

        assert learner.discharge_total.samples == 5
        assert path.exists()
        assert _corrupt_copies(path) == []


class TestPersistAllTrackers:
    """The registry loop retries a failed load before it saves."""

    @staticmethod
    def _coordinator(tracker: SavingsTracker) -> CoordinatorSharedState:
        """Return a stand-in coordinator owning one savings tracker."""
        coordinator = MagicMock()
        coordinator._savings_tracker = tracker
        return cast(CoordinatorSharedState, coordinator)

    @pytest.mark.asyncio
    async def test_an_unreadable_file_is_skipped_without_a_persist_warning(
        self, tmp_path: Path
    ) -> None:
        """The loader already warned once; the save is not attempted."""
        path = tmp_path / "savings.json"
        saved = SavingsTracker(history_file=str(path), actual_savings=12.5)
        assert await saved.save_history() is True
        original = path.read_bytes()

        tracker = SavingsTracker(history_file=str(path))
        with (
            _unreadable(path),
            patch(
                "custom_components.hsem.coordinator_persistence.async_log"
            ) as async_log,
        ):
            await tracker.load_history()
            tracker.actual_savings = 1.0
            await persist_all_trackers(
                self._coordinator(tracker), only=["_savings_tracker"]
            )

        async_log.assert_not_called()
        assert path.read_bytes() == original

    @pytest.mark.asyncio
    async def test_the_history_is_loaded_before_the_next_save(
        self, tmp_path: Path
    ) -> None:
        """Once readable, the file is restored and then written."""
        path = tmp_path / "savings.json"
        saved = SavingsTracker(history_file=str(path), actual_savings=12.5)
        assert await saved.save_history() is True

        tracker = SavingsTracker(history_file=str(path))
        with _unreadable(path):
            await tracker.load_history()

        await persist_all_trackers(
            self._coordinator(tracker), only=["_savings_tracker"]
        )

        assert tracker.actual_savings == pytest.approx(12.5)
        assert json.loads(path.read_text())["actual_savings"] == pytest.approx(12.5)
