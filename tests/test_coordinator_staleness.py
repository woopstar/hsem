"""Stale-cycle policy: bounded discards and a backoff before reruns (#1303).

The update-loop tests drive the **real**
``HSEMDataUpdateCoordinator._async_handle_update`` against a minimal stub
``self``, like ``test_coordinator_failed_retry.py``.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from custom_components.hsem.coordinator import HSEMDataUpdateCoordinator
from custom_components.hsem.coordinator_staleness import (
    MAX_CONSECUTIVE_STALE_DISCARDS,
    STALE_RETRY_BACKOFF_SECONDS,
    cycle_is_stale,
    record_publication,
    record_stale_discard,
)


class _Coordinator:
    """Just the counters the policy reads and writes."""

    def __init__(self, *, generation: int = 0, discards: int = 0) -> None:
        self._update_generation = generation
        self._consecutive_stale_discards = discards


class TestCycleIsStale:
    """When an overtaken cycle is discarded."""

    def test_a_cycle_nothing_overtook_is_not_stale(self) -> None:
        assert cycle_is_stale(_Coordinator(generation=4), 4) is False

    def test_an_overtaken_cycle_is_stale(self) -> None:
        assert cycle_is_stale(_Coordinator(generation=5), 4) is True

    def test_an_overtaken_cycle_publishes_after_the_discard_limit(self) -> None:
        coordinator = _Coordinator(
            generation=5, discards=MAX_CONSECUTIVE_STALE_DISCARDS
        )
        assert cycle_is_stale(coordinator, 4) is False

    def test_a_bare_coordinator_defaults_to_no_discards(self) -> None:
        assert cycle_is_stale(object(), 0) is False


class TestCounters:
    """Discards accumulate until a cycle publishes."""

    def test_discards_count_up_and_a_publication_resets_them(self) -> None:
        coordinator = _Coordinator(generation=3)

        record_stale_discard(coordinator, 1)
        record_stale_discard(coordinator, 2)
        assert coordinator._consecutive_stale_discards == 2

        record_publication(coordinator, 2)
        assert coordinator._consecutive_stale_discards == 0

    def test_publishing_an_overtaken_cycle_is_logged(self) -> None:
        coordinator = _Coordinator(
            generation=3, discards=MAX_CONSECUTIVE_STALE_DISCARDS
        )
        with patch("custom_components.hsem.coordinator_staleness.async_log") as log:
            record_publication(coordinator, 2)
        log.assert_called_once()
        assert log.call_args.args[0] == "info"

    def test_an_up_to_date_publication_is_not_logged(self) -> None:
        with patch("custom_components.hsem.coordinator_staleness.async_log") as log:
            record_publication(_Coordinator(generation=2), 2)
        log.assert_not_called()


class _LoopHarness:
    """Minimal stand-in exposing only what ``_async_handle_update`` touches."""

    def __init__(self, outcomes: list[str]) -> None:
        """Script the cycles.

        Args:
            outcomes: One entry per cycle. ``"stale"`` is overtaken and
                discarded, ``"forced"`` is overtaken but publishes, and
                ``"quiet"`` publishes with nothing newer pending.
        """
        self._update_lock = asyncio.Lock()
        self._update_generation = 0
        self._event_update_pending = False
        self._consecutive_stale_discards = 0
        self._outcomes = list(outcomes)
        self.cycles = 0

    async def _async_run_update_cycle(self) -> None:
        self.cycles += 1
        outcome = self._outcomes.pop(0)
        if outcome == "quiet":
            self._consecutive_stale_discards = 0
            return
        # A state event lands while this cycle runs.
        self._update_generation += 1
        self._event_update_pending = True
        if outcome == "stale":
            self._consecutive_stale_discards += 1
        else:
            self._consecutive_stale_discards = 0

    async def run(self) -> None:
        await HSEMDataUpdateCoordinator._async_handle_update(self, None)  # type: ignore[arg-type]


async def _run_recording_backoffs(harness: _LoopHarness) -> list[Any]:
    backoffs: list[Any] = []

    async def record(delay: Any) -> None:
        backoffs.append(delay)

    with patch("custom_components.hsem.coordinator.asyncio.sleep", side_effect=record):
        await harness.run()
    return backoffs


@pytest.mark.asyncio
async def test_a_discarded_cycle_waits_for_the_backoff_before_its_rerun() -> None:
    harness = _LoopHarness(["stale", "stale", "forced", "quiet"])

    backoffs = await _run_recording_backoffs(harness)

    assert harness.cycles == 4
    # Only the two discards back off; the forced publication reruns at once.
    assert backoffs == [STALE_RETRY_BACKOFF_SECONDS] * 2
    assert not harness._update_lock.locked()


@pytest.mark.asyncio
async def test_a_quiet_cycle_never_backs_off() -> None:
    harness = _LoopHarness(["quiet"])

    backoffs = await _run_recording_backoffs(harness)

    assert harness.cycles == 1
    assert backoffs == []
