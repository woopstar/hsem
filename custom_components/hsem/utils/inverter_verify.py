"""Write-and-verify helper for inverter and battery hardware writes.

Single responsibility: wrap a hardware write with a read-back verification
loop so that HSEM can confirm each setting was accepted by the inverter before
marking the apply cycle as successful.

Design
------
- Write the desired value via a caller-supplied coroutine.
- Wait a configurable settle time so the inverter has time to persist the value.
- Read the current value back via a caller-supplied reader callable.
- Accept the write if the read-back value matches within the specified tolerance.
- Retry up to ``max_retries`` times on mismatch or transient error.
- Return an :class:`ApplyResult` that the caller can log and surface to the
  status sensor.

The helpers in this module are intentionally free of Home Assistant dependencies
so that they can be unit-tested without a running HA instance.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from custom_components.hsem.utils.logger import HSEM_LOGGER as _LOGGER

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

_LOG_FMT = "%s — %s"

#: Default seconds to wait between write and read-back.
#: The inverter is polled every 5-10 s by HA, so the settle time must be
#: long enough for at least one full poll cycle to complete.
DEFAULT_SETTLE_SECONDS: float = 10.0

#: Default maximum number of write+verify attempts.
DEFAULT_MAX_RETRIES: int = 3

#: Absolute tolerance for numeric (float/int) comparisons.
DEFAULT_NUMERIC_TOLERANCE: float = 1.0


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class ApplyStatus(StrEnum):
    """Outcome of a single write-and-verify operation."""

    #: The read-back value matched the desired value within tolerance.
    OK = "ok"

    #: The write was accepted but the read-back timed out or returned None.
    UNVERIFIED = "unverified"

    #: All retries exhausted; the inverter did not accept the value.
    FAILED = "failed"

    #: The write was skipped because the current value already matched.
    SKIPPED = "skipped"


@dataclass
class ApplyResult:
    """Detailed outcome of a :func:`async_write_and_verify` call.

    Attributes:
        entity_id: The HA entity that was written.
        desired: The value that was written.
        actual: The last read-back value (``None`` if unreadable).
        status: Outcome enum.
        attempts: How many write+verify rounds were performed.
        error_message: Human-readable reason for failure (empty on success).
    """

    entity_id: str
    desired: Any
    actual: Any
    status: ApplyStatus
    attempts: int = 0
    error_message: str = ""


@dataclass
class CycleApplySummary:
    """Aggregated results for one full apply cycle (all writes in one coordinator
    tick).

    Attributes:
        results: Individual :class:`ApplyResult` per write operation.
        last_updated: ISO-format timestamp set by the caller after the cycle.
    """

    results: list[ApplyResult] = field(default_factory=list)
    last_updated: str | None = None

    # ------------------------------------------------------------------
    # Derived helpers
    # ------------------------------------------------------------------

    @property
    def overall_status(self) -> ApplyStatus:
        """Worst-case status across all results.

        Priority: FAILED > UNVERIFIED > OK > SKIPPED.
        If the results list is empty, returns ``ApplyStatus.SKIPPED``.
        """
        if not self.results:
            return ApplyStatus.SKIPPED
        priority = (
            ApplyStatus.FAILED,
            ApplyStatus.UNVERIFIED,
            ApplyStatus.OK,
            ApplyStatus.SKIPPED,
        )
        for status in priority:
            if any(r.status == status for r in self.results):
                return status
        return ApplyStatus.SKIPPED

    @property
    def failed_entities(self) -> list[str]:
        """Entity IDs whose last write ultimately failed verification."""
        return [r.entity_id for r in self.results if r.status == ApplyStatus.FAILED]

    @property
    def unverified_entities(self) -> list[str]:
        """Entity IDs whose write could not be verified (reader returned None)."""
        return [r.entity_id for r in self.results if r.status == ApplyStatus.UNVERIFIED]


# ---------------------------------------------------------------------------
# Core write-and-verify primitive
# ---------------------------------------------------------------------------


async def async_write_and_verify(
    entity_id: str,
    desired: Any,
    writer: Callable[[], Awaitable[None]],
    reader: Callable[[], Any],
    *,
    tolerance: float = DEFAULT_NUMERIC_TOLERANCE,
    settle_seconds: float = DEFAULT_SETTLE_SECONDS,
    max_retries: int = DEFAULT_MAX_RETRIES,
    skip_if_equal: bool = True,
) -> ApplyResult:
    """Write *desired* to an inverter entity and verify the value was accepted.

    Args:
        entity_id: HA entity that is written (used only for logging/reporting).
        desired: The value to write.
        writer: Zero-argument coroutine that performs the actual hardware write.
        reader: Zero-argument callable that returns the current entity value
                (may be a regular function or a coroutine).  Returns ``None``
                when the entity is unavailable.
        tolerance: Accepted absolute difference for numeric comparisons.
                   String comparisons use exact equality regardless.
        settle_seconds: Seconds to wait after writing before reading back.
        max_retries: Maximum number of write+verify attempts.
        skip_if_equal: When ``True``, skip the write entirely if the current
                       value already matches *desired* within tolerance.

    Returns:
        :class:`ApplyResult` describing the outcome.
    """
    if max_retries < 1:
        raise ValueError(f"max_retries must be >= 1, got {max_retries}")

    # ------------------------------------------------------------------
    # Pre-flight: read current value
    # ------------------------------------------------------------------
    try:
        current = await reader() if asyncio.iscoroutinefunction(reader) else reader()
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("Could not read current value of %s: %s", entity_id, exc)
        current = None

    if (
        skip_if_equal
        and current is not None
        and _values_match(current, desired, tolerance)
    ):
        return ApplyResult(
            entity_id=entity_id,
            desired=desired,
            actual=current,
            status=ApplyStatus.SKIPPED,
            attempts=0,
        )

    # ------------------------------------------------------------------
    # Retry loop
    # ------------------------------------------------------------------
    last_actual: Any = current
    last_error = ""

    for attempt in range(1, max_retries + 1):
        try:
            await writer()
        except Exception as exc:  # noqa: BLE001
            last_error = f"Write error on attempt {attempt}: {exc}"
            _LOGGER.warning(_LOG_FMT, entity_id, last_error)
            # Wait before retrying even after a write error (device may recover).
            if attempt < max_retries:
                await asyncio.sleep(settle_seconds)
            continue

        # Wait for the inverter to settle before reading back.
        await asyncio.sleep(settle_seconds)

        try:
            readback = (
                await reader() if asyncio.iscoroutinefunction(reader) else reader()
            )
        except Exception as exc:  # noqa: BLE001
            last_error = f"Read-back error on attempt {attempt}: {exc}"
            _LOGGER.warning(_LOG_FMT, entity_id, last_error)
            last_actual = None
            continue

        last_actual = readback

        if readback is None:
            last_error = f"Read-back returned None on attempt {attempt}"
            _LOGGER.warning("%s — %s", entity_id, last_error)
            continue

        if _values_match(readback, desired, tolerance):
            _LOGGER.debug(
                "%s verified after %d attempt(s): desired=%s, actual=%s",
                entity_id,
                attempt,
                desired,
                readback,
            )
            return ApplyResult(
                entity_id=entity_id,
                desired=desired,
                actual=readback,
                status=ApplyStatus.OK,
                attempts=attempt,
            )

        last_error = (
            f"Mismatch on attempt {attempt}: desired={desired}, actual={readback}"
        )
        _LOGGER.warning(_LOG_FMT, entity_id, last_error)

    # ------------------------------------------------------------------
    # All retries exhausted
    # ------------------------------------------------------------------
    final_status = ApplyStatus.UNVERIFIED if last_actual is None else ApplyStatus.FAILED
    _LOGGER.error(
        "%s write-and-verify FAILED after %d attempt(s). Last error: %s",
        entity_id,
        max_retries,
        last_error,
    )
    return ApplyResult(
        entity_id=entity_id,
        desired=desired,
        actual=last_actual,
        status=final_status,
        attempts=max_retries,
        error_message=last_error,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _values_match(actual: Any, desired: Any, tolerance: float) -> bool:
    """Return True when *actual* is close enough to *desired*.

    Numeric types use an absolute tolerance comparison.
    Strings and other types use exact equality.

    Args:
        actual: The read-back value.
        desired: The intended value.
        tolerance: Maximum allowed absolute difference for numeric types.

    Returns:
        True if the values are considered equal within tolerance.
    """
    if isinstance(desired, (int, float)) and isinstance(actual, (int, float)):
        return abs(float(actual) - float(desired)) <= tolerance
    return str(actual).lower().strip() == str(desired).lower().strip()
