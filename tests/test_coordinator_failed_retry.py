"""Regression: a failed cycle must not strand a pending newer-state flag.

``_async_handle_update`` clears ``_event_update_pending`` at the top of each
iteration and re-runs while it is set again. Before this fix, an exception from
``_async_run_update_cycle`` escaped the loop entirely — so an event that arrived
*during* the failed cycle left the flag ``True`` with nothing scheduled to act
on it, and the newer state was never planned against until some later unrelated
event happened to arrive.

These tests drive the **real** ``HSEMDataUpdateCoordinator._async_handle_update``
against a minimal stub ``self``, rather than a reimplementation of its logic, so
they cannot drift away from production behaviour.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from custom_components.hsem.coordinator import (
    _MAX_FAILED_UPDATE_RETRIES,
    HSEMDataUpdateCoordinator,
)


class _RetryHarness:
    """Minimal stand-in exposing only what ``_async_handle_update`` touches."""

    def __init__(
        self,
        *,
        failures: int,
        pending_during_cycle: bool,
        pending_cycles: int | None = None,
    ) -> None:
        """Configure the simulated cycle.

        Args:
            failures: How many leading cycles raise before one succeeds.
            pending_during_cycle: Whether a state event lands mid-cycle.
            pending_cycles: Limit the mid-cycle event to the first N cycles.
                ``None`` means every cycle, which never terminates on success.
        """
        self._update_lock = asyncio.Lock()
        self._update_generation = 0
        self._event_update_pending = False
        self._failures_remaining = failures
        self._pending_during_cycle = pending_during_cycle
        self._pending_cycles = pending_cycles
        self.cycles = 0

    async def _async_run_update_cycle(self) -> None:
        self.cycles += 1
        if self._pending_during_cycle and (
            self._pending_cycles is None or self.cycles <= self._pending_cycles
        ):
            # Simulate a state event landing while this cycle runs.
            self._event_update_pending = True
        if self._failures_remaining > 0:
            self._failures_remaining -= 1
            raise RuntimeError("simulated cycle failure")

    async def run(self, event: Any = None) -> None:
        await HSEMDataUpdateCoordinator._async_handle_update(self, event)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_failed_cycle_retries_when_newer_state_is_pending() -> None:
    """One failure with a pending event is retried instead of stranding it."""
    # Cycle 1 raises with a newer event pending; cycle 2 is the retry and
    # succeeds with nothing further pending, so the loop exits cleanly.
    harness = _RetryHarness(
        failures=1,
        pending_during_cycle=True,
        pending_cycles=1,
    )

    await harness.run()

    assert harness.cycles == 2, (
        "the failed cycle must be retried while newer state is pending; "
        f"{harness.cycles} cycle(s) ran"
    )
    assert not harness._event_update_pending
    assert not harness._update_lock.locked()


@pytest.mark.asyncio
async def test_failure_without_pending_state_propagates() -> None:
    """With nothing newer pending, the error still surfaces to Home Assistant."""
    harness = _RetryHarness(failures=1, pending_during_cycle=False)
    with pytest.raises(RuntimeError, match="simulated cycle failure"):
        await harness.run()
    assert harness.cycles == 1, "must not retry when no newer state is pending"


@pytest.mark.asyncio
async def test_persistent_failure_is_bounded_and_reraises() -> None:
    """A permanently failing cycle cannot hot-spin the event loop forever."""
    harness = _RetryHarness(failures=10_000, pending_during_cycle=True)
    with pytest.raises(RuntimeError, match="simulated cycle failure"):
        await harness.run()
    assert harness.cycles == _MAX_FAILED_UPDATE_RETRIES + 1, (
        "retries must be bounded: expected "
        f"{_MAX_FAILED_UPDATE_RETRIES + 1} cycles, got {harness.cycles}"
    )
    assert not harness._update_lock.locked(), "the update lock must be released"


@pytest.mark.asyncio
async def test_successful_cycle_resets_the_retry_budget() -> None:
    """A success between failures restores the full retry allowance."""
    harness = _RetryHarness(failures=0, pending_during_cycle=False)
    await harness.run()
    assert harness.cycles == 1
    assert not harness._event_update_pending
