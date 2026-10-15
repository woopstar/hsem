"""Stale-cycle policy: when a newer state event may discard an in-flight cycle.

Every registered state change advances the coordinator's update generation.
A cycle that sees the generation move past the value it started with is
normally discarded and rerun against the newer state, so a plan computed
from superseded inputs is never published.

When cycles take longer than the gap between state events, every cycle is
overtaken and discarded, and HSEM stops publishing while still submitting
full replans to the executor (issue #1303). This module bounds that:

- after :data:`MAX_CONSECUTIVE_STALE_DISCARDS` discards in a row, the next
  cycle publishes even if it is overtaken. Its result is only as old as the
  state it collected at its start, and the pending event still triggers a
  rerun right after it; and
- the update loop waits :data:`STALE_RETRY_BACKOFF_SECONDS` before rerunning
  a discarded cycle, so a burst of events coalesces into one rerun instead of
  a back-to-back stream of executor jobs.
"""

from __future__ import annotations

from typing import Any

from custom_components.hsem.utils.logger import async_log

#: Consecutive stale discards after which the next cycle publishes anyway.
MAX_CONSECUTIVE_STALE_DISCARDS = 2

#: Seconds the update loop waits before rerunning a cycle discarded as stale.
STALE_RETRY_BACKOFF_SECONDS = 2.0


def cycle_is_stale(coordinator: Any, captured_generation: int) -> bool:
    """Return whether the in-flight cycle must be discarded as stale.

    Args:
        coordinator: The HSEM coordinator running the cycle.
        captured_generation: The update generation the cycle started in.

    Returns:
        ``True`` when a newer state event arrived during the cycle and fewer
        than :data:`MAX_CONSECUTIVE_STALE_DISCARDS` cycles in a row have
        already been discarded. ``False`` when nothing newer arrived, or when
        the cycle must publish to end a run of discards.
    """
    if getattr(coordinator, "_update_generation", 0) == captured_generation:
        return False
    return (
        getattr(coordinator, "_consecutive_stale_discards", 0)
        < MAX_CONSECUTIVE_STALE_DISCARDS
    )


def record_stale_discard(coordinator: Any, captured_generation: int) -> None:
    """Count a cycle discarded as stale.

    Args:
        coordinator: The HSEM coordinator running the cycle.
        captured_generation: The update generation the cycle started in.
    """
    discards = getattr(coordinator, "_consecutive_stale_discards", 0) + 1
    coordinator._consecutive_stale_discards = discards
    async_log(
        "debug",
        "[coordinator] Discarding stale cycle: update generation advanced "
        "%d→%d (%d/%d in a row).",
        captured_generation,
        getattr(coordinator, "_update_generation", 0),
        discards,
        MAX_CONSECUTIVE_STALE_DISCARDS,
    )


def record_publication(coordinator: Any, captured_generation: int) -> None:
    """Reset the discard count when a cycle publishes.

    Args:
        coordinator: The HSEM coordinator running the cycle.
        captured_generation: The update generation the cycle started in.
    """
    generation = getattr(coordinator, "_update_generation", 0)
    if generation != captured_generation:
        async_log(
            "info",
            "[coordinator] Publishing a cycle overtaken by newer state "
            "(generation %d→%d) after %d stale discards in a row; "
            "rerunning against the newer state next.",
            captured_generation,
            generation,
            getattr(coordinator, "_consecutive_stale_discards", 0),
        )
    coordinator._consecutive_stale_discards = 0
