"""Terminal end value from the price profile past the horizon (issue #1453).

The terminal-SoC term values the energy a plan leaves in the battery at one
end value ``V`` (issue #1138).  ``V`` used to come from the horizon's last
calendar day alone, which stands in for the unknown day after it.  This
module prices that day itself: the 24 hours after the horizon end, the
*tail*, from the weekday and time-of-day profile of real prices
(:mod:`custom_components.hsem.models.price_profile`).

The tail is not part of the plan.  It adds no slot, no decision variable and
no constraint; its only output is a second estimate of ``V``, which the
engine blends with the last-day estimate.  Every tail price is an estimate.
Pure — no I/O, no Home Assistant imports.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Any

from custom_components.hsem.models.plan_explanation import PlanExplanation
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.price_profile import (
    LEVEL_SHIFT_MAX_AGE,
    LevelShift,
    PriceProfile,
    level_shift_applies,
)
from custom_components.hsem.models.time_series import TimeSeriesIndex
from custom_components.hsem.planner.cost_helpers import (
    TERMINAL_NIGHT_END_HOUR,
    terminal_end_value,
)
from custom_components.hsem.utils.datetime_utils import as_tz, utc_key
from custom_components.hsem.utils.logger import log_planner
from custom_components.hsem.utils.misc import clamp_efficiency

#: Hours after the horizon end that the tail prices.  One day holds the night
#: and the peak that energy left at the horizon end meets first.
TERMINAL_TAIL_HOURS = 24

#: Weight of the tail's end value in the blend with the last-day end value
#: when the tail is enabled.  One day after the last published price the
#: shifted profile and the nearest earlier day had the same error on a year
#: of real prices (export MAE 0.249 both, issue #1452), so neither estimate
#: earns more than half.
TERMINAL_TAIL_WEIGHT = 0.5

#: ``constraints`` flag of a plan whose end value used an estimated tail.
TERMINAL_TAIL_CONSTRAINT = "terminal_value_from_estimated_price_tail"


def tail_share(weight: float) -> float:
    """Return *weight* as a share between 0 and 1.

    Args:
        weight: The configured weight of the tail's end value.

    Returns:
        The weight clamped to 0-1; ``0.0`` when it is not a finite number.
    """
    if not math.isfinite(weight):
        return 0.0
    return min(max(weight, 0.0), 1.0)


@dataclass(frozen=True)
class TerminalPriceTail:
    """The estimated prices behind a plan's terminal end value.

    Attributes:
        start: Start of the tail, which is the end of the horizon.
        end: End of the tail.
        slot_count: Slots the tail holds.  All of them are estimates.
        shifted: Whether the tail carries the level shift.  It applies to
            every slot or to none.
        peak_import: Mean of the tail's most expensive import prices.
        night_import: Mean import price of the tail's night slots.
        tail_value_per_kwh: End value derived from the tail alone.
        last_day_value_per_kwh: End value derived from the horizon's last
            calendar day, the value used without a tail.
        weight: Share of the tail's value in ``value_per_kwh``.
        value_per_kwh: The blended end value ``V`` the plan was solved and
            scored with.
    """

    start: datetime
    end: datetime
    slot_count: int
    shifted: bool
    peak_import: float
    night_import: float
    tail_value_per_kwh: float
    last_day_value_per_kwh: float
    weight: float
    value_per_kwh: float

    def as_dict(self) -> dict[str, Any]:
        """Return the tail as a JSON-safe dict for the plan explanation.

        Returns:
            The fields above, rounded, with ``estimated`` always ``True``.
        """
        return {
            "estimated": True,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "slot_count": self.slot_count,
            "shifted": self.shifted,
            "peak_import_price": round(self.peak_import, 4),
            "night_import_price": round(self.night_import, 4),
            "tail_value_per_kwh": round(self.tail_value_per_kwh, 4),
            "last_day_value_per_kwh": round(self.last_day_value_per_kwh, 4),
            "weight": round(self.weight, 3),
            "value_per_kwh": round(self.value_per_kwh, 4),
        }


def _tail_level(
    slots: Sequence[PlannedSlot],
    published: Sequence[bool],
    profile: PriceProfile,
    interval_minutes: int,
    tz: tzinfo | None,
    published_until: datetime | None,
) -> LevelShift | None:
    """Return the level shift for the tail and the time it stops applying.

    The shift is the one the missing-price fill uses: the median difference
    between the priced horizon slots and the profile
    (:meth:`PriceProfile.level_shift`, issue #1434).  It applies for 24 hours
    after the last published price.  Without enough priced slots the last
    known level shift of the profile is used, with its own time limit (issue
    #1452).

    Args:
        slots: The horizon's slots with populated prices.
        published: Per slot, whether its price came from a price source.
        profile: Weekday and time-of-day profile of real prices.
        interval_minutes: Slot width in minutes.
        tz: Local zone the weekday and time of day are read in.
        published_until: End of the last published price, or ``None`` when
            unknown; the end of the last priced slot is then used.

    Returns:
        ``(shift, first start that is too late for it)``, or ``None`` when
        there is no shift to apply.
    """
    priced = [slot for slot, is_priced in zip(slots, published) if is_priced]
    shift = profile.level_shift(
        {slot.start: slot.price.import_price for slot in priced},
        0,
        interval_minutes,
        tz,
    )
    if shift is None:
        return profile.import_level
    anchor = published_until or max(slot.end for slot in priced)
    return shift, anchor + LEVEL_SHIFT_MAX_AGE


def _tail_import_prices(
    slots: Sequence[PlannedSlot],
    published: Sequence[bool],
    profile: PriceProfile,
    interval_minutes: int,
    tz: tzinfo | None,
    published_until: datetime | None,
) -> tuple[list[tuple[datetime, float]], bool] | None:
    """Return the estimated import price of every tail slot.

    The profile carries the shape and one level applies to the whole tail:
    every slot is shifted, or none is.  See :func:`_tail_level`.

    Args:
        slots: The horizon's slots with populated prices.
        published: Per slot, whether its price came from a price source.
        profile: Weekday and time-of-day profile of real prices.
        interval_minutes: Slot width in minutes.
        tz: Local zone the weekday and time of day are read in.
        published_until: End of the last published price, or ``None``.

    Returns:
        ``(start, import price)`` per tail slot and whether they are shifted.
        ``None`` when the profile lacks a price for a tail slot, or when the
        level shift stops applying inside the tail: a tail is never priced
        at two levels.
    """
    step = timedelta(minutes=interval_minutes)
    # Stepped in real time: on a DST day wall-clock steps would skip or
    # repeat an hour.
    tail_start = utc_key(slots[-1].end)
    prices: list[tuple[datetime, float]] = []
    for index in range(TERMINAL_TAIL_HOURS * 60 // interval_minutes):
        start = tail_start + index * step
        local = as_tz(start, tz)
        pair = profile.lookup(
            local.weekday(), local.hour, local.minute, interval_minutes
        )
        if pair is None:
            return None
        prices.append((start, pair[0]))

    level = _tail_level(
        slots, published, profile, interval_minutes, tz, published_until
    )
    if level is None or not level_shift_applies(prices[0][0], level[1]):
        return prices, False
    if not level_shift_applies(prices[-1][0], level[1]):
        return None
    return [(start, price + level[0]) for start, price in prices], True


def terminal_price_tail(
    slots: Sequence[PlannedSlot],
    published: Sequence[bool],
    profile: PriceProfile | None,
    interval_minutes: int,
    tz: tzinfo | None,
    *,
    last_day_value: float | None,
    weight: float,
    published_until: datetime | None = None,
    top_n: int,
    charge_eff: float,
    discharge_eff: float,
    cycle_cost_per_kwh: float,
) -> TerminalPriceTail | None:
    """Blend the last-day end value with one from the estimated price tail.

    The tail's end value uses the formula of the last-day one
    (:func:`~custom_components.hsem.planner.cost_helpers.terminal_end_value`):
    ``peak`` is the mean of the tail's *top_n* import prices and
    ``night_import`` the mean of its slots before
    :data:`~custom_components.hsem.planner.cost_helpers.TERMINAL_NIGHT_END_HOUR`.
    The result is ``(1 − weight) × last_day_value + weight × tail value``.

    Args:
        slots: The horizon's slots with populated prices.
        published: Per slot, whether its price was published by a source.
        profile: Weekday and time-of-day profile of real prices, or ``None``.
        interval_minutes: Slot width in minutes.
        tz: Local zone that defines the weekday and the night window.
        last_day_value: End value from the horizon's last calendar day.
        weight: Share of the tail's value, clamped to 0-1.  ``0`` or a
            value that is not finite disables the tail.
        published_until: End of the last real price the price sensor has
            published, also when that is past the horizon, or ``None`` when
            unknown.  A horizon that ends before it gets no tail: its tail
            would estimate prices that are published.
        top_n: Number of most expensive slots that make up the peak.
        charge_eff: Charge efficiency fraction (0-1).
        discharge_eff: Discharge efficiency fraction (0-1).
        cycle_cost_per_kwh: Battery wear per kWh of throughput.

    Returns:
        The tail and the blended value, or ``None`` when the tail is
        disabled, there is no last-day value, the profile is missing or
        empty or lacks a price for a tail slot, published prices reach past
        the horizon, or the level shift stops applying inside the tail.  The
        caller then keeps *last_day_value* unchanged.
    """
    share = tail_share(weight)
    if (
        share < 1e-9
        or last_day_value is None
        or profile is None
        or profile.is_empty
        or not slots
        or (published_until is not None and slots[-1].end < published_until)
    ):
        return None
    estimate = _tail_import_prices(
        slots, published, profile, interval_minutes, tz, published_until
    )
    if estimate is None:
        return None
    prices, shifted = estimate
    top = sorted((price for _, price in prices), reverse=True)[: max(top_n, 1)]
    night = [
        price
        for start, price in prices
        if as_tz(start, tz).hour < TERMINAL_NIGHT_END_HOUR
    ]
    peak_import = sum(top) / len(top)
    # 24 hours of tail always hold slots of the night window.
    night_import = sum(night) / len(night)
    tail_value = terminal_end_value(
        peak_import=peak_import,
        night_import=night_import,
        charge_eff=charge_eff,
        discharge_eff=discharge_eff,
        cycle_cost_per_kwh=cycle_cost_per_kwh,
    )
    value = (1.0 - share) * last_day_value + share * tail_value
    log_planner(
        "debug",
        "[cost] terminal_price_tail  start=%s  peak=%.4f  night=%.4f  "
        "tail_value=%.4f  last_day_value=%.4f  weight=%.2f  value=%.4f",
        prices[0][0].isoformat(),
        peak_import,
        night_import,
        tail_value,
        last_day_value,
        share,
        value,
    )
    return TerminalPriceTail(
        start=as_tz(prices[0][0], tz),
        end=as_tz(prices[-1][0] + timedelta(minutes=interval_minutes), tz),
        slot_count=len(prices),
        shifted=shifted,
        peak_import=peak_import,
        night_import=night_import,
        tail_value_per_kwh=tail_value,
        last_day_value_per_kwh=last_day_value,
        weight=share,
        value_per_kwh=value,
    )


def resolve_terminal_price_tail(
    slots: Sequence[PlannedSlot],
    tsi: TimeSeriesIndex,
    inp: PlannerInput,
    tz: tzinfo | None,
    last_day_value: float | None,
    top_n: int,
    cycle_cost_per_kwh: float,
) -> TerminalPriceTail | None:
    """Return the price tail of one planner run, or ``None`` without one.

    Reads the weight, the profile and the efficiencies from *inp* and which
    slots carry a price from a source from *tsi*.  With the tail off, which
    is the default, it returns at once and reads nothing else.  See
    :func:`terminal_price_tail`.

    Args:
        slots: The horizon's slots with populated prices, parallel to
            ``tsi.slots``.
        tsi: The run's time-series index, after price population.
        inp: The planner input.
        tz: Local zone of the run.
        last_day_value: End value from the horizon's last calendar day.
        top_n: Number of most expensive slots that make up the peak.
        cycle_cost_per_kwh: Battery wear per kWh of throughput.

    Returns:
        The tail and the blended end value, or ``None`` when the tail is
        off or cannot be priced.
    """
    if tail_share(inp.terminal_price_tail_weight) < 1e-9:
        return None
    return terminal_price_tail(
        slots,
        [meta.key not in tsi.missing_price_slots for meta in tsi.slots],
        inp.price_profile,
        inp.interval_minutes,
        tz,
        last_day_value=last_day_value,
        weight=inp.terminal_price_tail_weight,
        published_until=inp.price_published_until,
        top_n=top_n,
        charge_eff=clamp_efficiency(inp.battery_charge_efficiency_pct),
        discharge_eff=clamp_efficiency(inp.battery_discharge_efficiency_pct),
        cycle_cost_per_kwh=cycle_cost_per_kwh,
    )


def flag_terminal_price_tail(
    explanation: PlanExplanation, tail: TerminalPriceTail | None
) -> None:
    """Mark in the explanation that the end value used estimated prices.

    Args:
        explanation: The explanation to update in place.
        tail: The run's price tail, or ``None`` when it had none.
    """
    if tail is None:
        return
    explanation.terminal_price_tail = tail.as_dict()
    explanation.constraints.append(TERMINAL_TAIL_CONSTRAINT)
