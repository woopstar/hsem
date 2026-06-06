"""Dataclass for the result of a day rollover check."""

from __future__ import annotations

from dataclasses import dataclass

from custom_components.hsem.models.daily_record import DailyRecord


@dataclass
class DayRolloverResult:
    """Result of a day rollover check.

    Attributes:
        record: The :class:`DailyRecord` that was persisted.
        saved: Whether the record was successfully written to disk.
    """

    record: DailyRecord
    saved: bool = False
