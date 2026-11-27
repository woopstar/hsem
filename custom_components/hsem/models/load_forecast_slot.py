"""House-load forecast for one planning slot (issue #1490).

A source that forecasts the house load itself (the ML predictor) has a value
of its own for every slot.  The hour-granular
:class:`~custom_components.hsem.models.hourly_consumption_average.HourlyConsumptionAverage`
cannot carry that at sub-hourly slots, so the forecast travels to the planner
in a list of these entries, one per slot, like prices do.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.utils.datetime_utils import slot_position


@dataclass
class LoadForecastSlot:
    """Forecast house load of one planning slot.

    Attributes:
        hour:
            Wall-clock hour of the slot (0-23).
        day_offset:
            Number of whole calendar days from the planning midnight (0 = today,
            1 = tomorrow, …).
        slot_in_day:
            0-based count of real slot steps since the local midnight of the
            slot's day.  The planner keys the entry by
            ``(day_offset, slot_in_day)``, so both occurrences of the DST
            fall-back hour keep their own value.
        load_kwh:
            Forecast house load in kWh for the slot (not for the hour).
    """

    hour: int  # 0-23
    day_offset: int
    slot_in_day: int
    load_kwh: float


def build_load_forecast_slots(
    recommendations: Iterable[HourlyRecommendation],
    planning_midnight: datetime,
    interval_minutes: int,
) -> list[LoadForecastSlot]:
    """Return the forecast of every recommendation slot, keyed for the planner.

    Only for a cycle whose ``avg_house_consumption_kwh`` is a forecast of its
    own (``ConsumptionPopulation.direct_forecast``).

    Args:
        recommendations: The cycle's recommendation slots.
        planning_midnight: Timezone-aware local midnight of the planning day.
        interval_minutes: Slot width in minutes.

    Returns:
        One entry per recommendation slot, in the same order.
    """
    slots: list[LoadForecastSlot] = []
    for rec in recommendations:
        day_offset, slot_in_day = slot_position(
            rec.start, planning_midnight, interval_minutes
        )
        slots.append(
            LoadForecastSlot(
                hour=rec.start.hour,
                day_offset=day_offset,
                slot_in_day=slot_in_day,
                load_kwh=rec.avg_house_consumption_kwh,
            )
        )
    return slots
