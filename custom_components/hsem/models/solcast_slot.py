"""Dataclass for a forecast PV production estimate for a single time slot."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SolcastSlot:
    """Forecast PV production estimate for one hour or one planner slot.

    This docstring is the one place that defines the unit of the PV forecast
    on its way to the planner: ``pv_estimate`` is the **average PV power in
    kW** over the entry's period, as the Solcast integration publishes it.
    The energy of a planner slot is ``pv_estimate × slot duration in hours``
    (:meth:`~custom_components.hsem.models.time_series.TimeSeriesIndex.align_hourly_pv`
    and
    :meth:`~custom_components.hsem.models.time_series.TimeSeriesIndex.align_slot_pv`).

    Attributes:
        hour:
            0-based calendar hour (0-23).
        pv_estimate:
            Average PV power in kW over the entry's period.  For an
            hour-granular entry that number is also the hour's energy in kWh.
        day_offset:
            Number of whole calendar days from the planning midnight (0 = today,
            1 = tomorrow, …).  Defaults to 0 for backward compatibility with
            callers that only pass 24 single-day entries.
        slot_in_day:
            Optional 0-based index of the planner slot within its calendar
            day (0-95 for 15-min slots, 0-47 for 30-min).  ``None`` (default)
            means the entry is hour-granular and is split evenly over the
            hour's slots.  When set, the planner keys the entry by
            ``(day_offset, slot_in_day)`` so a half-hourly or quarter-hourly
            forecast reaches the plan at its own resolution instead of being
            flattened to one value per hour (issue #1191).
    """

    hour: int  # 0-23
    pv_estimate: float = 0.0
    day_offset: int = 0
    slot_in_day: int | None = None
