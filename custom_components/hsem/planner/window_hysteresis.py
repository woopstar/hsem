"""Window-level hysteresis — prevent rapid recommendation toggles."""

from __future__ import annotations

from datetime import datetime

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.utils.datetime_utils import as_tz
from custom_components.hsem.utils.logger import log_planner
from custom_components.hsem.utils.recommendations import (
    CHARGE_RECS as _CHARGE_RECS,
    DISCHARGE_RECS as _DISCHARGE_RECS,
)

# ---------------------------------------------------------------------------
# Window-level hysteresis — prevent rapid recommendation toggles
# ---------------------------------------------------------------------------


def apply_window_hysteresis(
    slots: list[PlannedSlot],
    now: datetime,
    *,
    window_hysteresis_minutes: int,
    previous_current_recommendation: str | None,
    previous_current_slot_start: datetime | None,
) -> tuple[str | None, datetime | None]:
    """Apply minimum hold time before allowing any recommendation change.

    Prevents rapid toggling between recommendations (both cross-category
    charge↔discharge flips and within-category flips such as
    ``batteries_charge_solar`` ↔ ``ev_smart_charging``) by enforcing a
    minimum hold time.  If the current slot's recommendation would change
    and the previous recommendation has been in place for less than
    ``window_hysteresis_minutes``, the previous recommendation is kept.

    Neutral recommendations (``batteries_wait_mode``, ``time_passed``,
    ``missing_input_entities``, ``None``) are never held — only actionable
    recommendations are subject to the hold timer.

    Args:
        slots:
            Ordered list of planned slots (mutated in place).
        now:
            Timezone-aware current datetime.
        window_hysteresis_minutes:
            Minimum hold time in minutes.  0 disables the feature entirely.
        previous_current_recommendation:
            Recommendation that was active on the current slot during the
            previous planner run.  ``None`` on first run.
        previous_current_slot_start:
            Start time of the slot that carried
            ``previous_current_recommendation``.  ``None`` on first run.

    Returns:
        A ``(updated_recommendation, current_slot_start)`` tuple.
        ``updated_recommendation`` is the (possibly held) recommendation
        for the current slot, and ``current_slot_start`` is the start time
        of the current slot (for persisting across cycles).
    """
    if window_hysteresis_minutes <= 0:
        # Feature disabled — find and return current recommendation unchanged
        for s in slots:
            if as_tz(s.start, now.tzinfo) <= now < as_tz(s.end, now.tzinfo):
                return s.recommendation, s.start
        return None, None

    # Find the current slot
    current_slot: PlannedSlot | None = None
    for s in slots:
        if as_tz(s.start, now.tzinfo) <= now < as_tz(s.end, now.tzinfo):
            current_slot = s
            break

    if current_slot is None:
        return None, None

    new_rec = current_slot.recommendation
    new_start = current_slot.start

    # No previous state — first run, no hysteresis to apply
    if previous_current_recommendation is None or previous_current_slot_start is None:
        return new_rec, new_start

    # Neutral recommendations never trigger a hold — if the new or previous
    # recommendation is neutral, allow the transition immediately.
    new_category = _rec_category(new_rec)
    prev_category = _rec_category(previous_current_recommendation)
    if new_category == "neutral" or prev_category == "neutral":
        return new_rec, new_start

    # If the recommendation hasn't changed at all, no hold needed
    if new_rec == previous_current_recommendation:
        return new_rec, new_start

    # Recommendation changed — check hold time
    elapsed_minutes = (now - previous_current_slot_start).total_seconds() / 60.0
    if elapsed_minutes < window_hysteresis_minutes:
        # Hold the previous recommendation
        log_planner(
            "debug",
            "[window_hysteresis] Holding previous recommendation '%s' on current "
            "slot (elapsed=%.1f min < hold=%d min). New '%s' suppressed.",
            previous_current_recommendation,
            elapsed_minutes,
            window_hysteresis_minutes,
            new_rec,
        )
        current_slot.recommendation = previous_current_recommendation
        return previous_current_recommendation, previous_current_slot_start

    # Enough time has passed — allow the switch
    log_planner(
        "debug",
        "[window_hysteresis] Allowing transition '%s' → '%s' on current slot "
        "(elapsed=%.1f min >= hold=%d min).",
        previous_current_recommendation,
        new_rec,
        elapsed_minutes,
        window_hysteresis_minutes,
    )
    return new_rec, new_start


def _rec_category(rec: str | None) -> str:
    """Classify a recommendation into a category.

    Returns ``"charge"``, ``"discharge"``, or ``"neutral"``.
    """
    if rec in _CHARGE_RECS:
        return "charge"
    if rec in _DISCHARGE_RECS:
        return "discharge"
    return "neutral"
