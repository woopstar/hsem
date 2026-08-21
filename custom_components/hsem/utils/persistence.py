"""Pure validation helpers for JSON-restored integration state."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any


def finite_float(
    value: Any,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    """Return a bounded finite float, or ``None`` for invalid state."""
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except TypeError, ValueError, OverflowError:
        return None
    if not math.isfinite(parsed):
        return None
    if minimum is not None and parsed < minimum:
        return None
    if maximum is not None and parsed > maximum:
        return None
    return parsed


def aware_datetime_from_iso(value: Any) -> datetime | None:
    """Parse an aware ISO datetime, returning ``None`` when invalid."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except TypeError, ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    try:
        parsed.astimezone(UTC)
    except OverflowError, ValueError:
        return None
    return parsed
