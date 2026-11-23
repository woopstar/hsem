"""Central registry for coordinator tracker persistence (issue #890).

Several coordinator-owned tracker classes each expose their own
``save_history``/``load_history`` pair, persisted ad hoc from inside
separate ``accumulate_*`` functions in :mod:`coordinator_tracking` and
:mod:`coordinator_cycle`. Because more than one of them shares the exact
same method name, ``vulture`` -- which matches unused code by name only,
not by class -- sees one real call site for ``save_history`` and stops
warning about *every* method sharing that name, including ones with zero
callers. That blind spot let ``SavingsTracker.save_history()`` ship fully
implemented and unit-tested but never called from production code
(issue #888): the savings tracker lost all state on every Home Assistant
restart.

This module centralises the actual persistence calls into a single
registry-driven loop so that adding a new tracker without wiring up its
persistence is caught by a reflection test
(``tests/test_coordinator_persistence_registry.py``) instead of relying
on a name-matching static analyser.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Protocol

from custom_components.hsem.coordinator_state import CoordinatorSharedState
from custom_components.hsem.utils.logger import async_log
from custom_components.hsem.utils.persistence import ensure_history_loaded


class Persistable(Protocol):
    """Protocol for tracker classes that persist their own state to disk."""

    history_file: str

    async def load_history(self) -> None:
        """Restore the state from disk."""

    async def save_history(self) -> bool:
        """Persist current state to disk. Return ``True`` on success."""
        ...


def _get_savings_tracker(
    coordinator: CoordinatorSharedState,
) -> Persistable | None:
    return coordinator._savings_tracker


def _get_financial_tracker(
    coordinator: CoordinatorSharedState,
) -> Persistable | None:
    return coordinator._financial_tracker


def _get_energy_tracker(
    coordinator: CoordinatorSharedState,
) -> Persistable | None:
    """Return the energy tracker only once its history file is set."""
    tracker = coordinator._energy_tracker
    if not tracker.history_file:
        return None
    return tracker


def _get_performance_tracker(
    coordinator: CoordinatorSharedState,
) -> Persistable | None:
    """Return the performance tracker only once its history file is set."""
    tracker = coordinator._performance_tracker
    if not tracker.history_file:
        return None
    return tracker


def _get_what_if_history(
    coordinator: CoordinatorSharedState,
) -> Persistable | None:
    """Return the what-if history only once its history file is set."""
    history = coordinator._what_if_history
    if not history.history_file:
        return None
    return history


def _get_prediction_tracker(
    coordinator: CoordinatorSharedState,
) -> Persistable | None:
    """Return the prediction tracker only once its history file is set.

    Mirrors the pre-refactor guard that skipped persistence before
    ``init_prediction_tracker`` had run during coordinator setup.
    """
    tracker = coordinator._prediction_tracker
    if not tracker.history_file:
        return None
    return tracker


def _get_price_history_tracker(
    coordinator: CoordinatorSharedState,
) -> Persistable | None:
    """Return the price history tracker only once its history file is set."""
    tracker = coordinator._price_history_tracker
    if not tracker.history_file:
        return None
    return tracker


def _get_efficiency_learner(
    coordinator: CoordinatorSharedState,
) -> Persistable | None:
    """Return the efficiency learner only once its history file is set."""
    learner = coordinator._efficiency_learner
    if not learner.history_file:
        return None
    return learner


# Every field declared on ``CoordinatorSharedState`` (coordinator_state.py)
# whose type exposes an ``async def save_history(self) -> bool`` method MUST
# have a corresponding entry here, keyed by the exact attribute name.
# Enforced by
# tests/test_coordinator_persistence_registry.py::test_registry_covers_every_persistable_tracker.
TRACKER_REGISTRY: dict[str, Callable[[CoordinatorSharedState], Persistable | None]] = {
    "_savings_tracker": _get_savings_tracker,
    "_financial_tracker": _get_financial_tracker,
    "_energy_tracker": _get_energy_tracker,
    "_performance_tracker": _get_performance_tracker,
    "_prediction_tracker": _get_prediction_tracker,
    "_price_history_tracker": _get_price_history_tracker,
    "_efficiency_learner": _get_efficiency_learner,
    "_what_if_history": _get_what_if_history,
}


async def persist_all_trackers(
    coordinator: CoordinatorSharedState,
    *,
    only: Iterable[str] | None = None,
) -> None:
    """Persist tracker state through the shared registry.

    Called from each point in the coordinator cycle where the
    corresponding ``accumulate_*`` step just ran, replacing the scattered
    per-``accumulate_*`` ``if not await tracker.save_history(): log
    warning`` blocks with one shared loop.

    Args:
        coordinator: The running coordinator instance.
        only: Registry keys to persist on this call, or ``None`` to
            persist every registered tracker. Restricting the call lets
            callers that don't share the same per-cycle cadence as the
            rest (e.g. the prediction tracker only has new data on a
            slot boundary) preserve their existing write cadence.
    """
    names = TRACKER_REGISTRY.keys() if only is None else only
    for name in names:
        tracker = TRACKER_REGISTRY[name](coordinator)
        if tracker is None:
            continue
        # A history file that could not be read is loaded before it is
        # written, and skipped while it still cannot be read (issue #1446).
        if not await ensure_history_loaded(tracker):
            continue
        if not await tracker.save_history():
            label = name.lstrip("_").removesuffix("_tracker")
            async_log("warning", "Failed to persist %s tracker state", label)
