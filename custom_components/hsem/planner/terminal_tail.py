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

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Any

from custom_components.hsem.models.plan_explanation import PlanExplanation
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.price_profile import (
    LEVEL_SHIFT_MAX_AGE,
    PriceProfile,
    level_shift_applies,
    median_shift,
)
from custom_components.hsem.models.time_series import TimeSeriesIndex
from custom_components.hsem.planner.cost_helpers import (
    TERMINAL_NIGHT_END_HOUR,
    terminal_end_value,
)
from custom_components.hsem.utils.datetime_utils import as_tz
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


@dataclass(frozen=True)
class TerminalPriceTail:
    """The estimated prices behind a plan's terminal end value.

    Attributes:
        start: Start of the tail, which is the end of the horizon.
        end: End of the tail.
        slot_count: Slots the tail holds.  All of them are estimates.
        shifted_slots: Slots priced with the level shift; the others carry
            the profile as recorded.
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
    shifted_slots: int
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
            "shifted_slots": self.shifted_slots,
            "peak_import_price": round(self.peak_import, 4),
            "night_import_price": round(self.night_import, 4),
            "tail_value_per_kwh": round(self.tail_value_per_kwh, 4),
            "last_day_value_per_kwh": round(self.last_day_value_per_kwh, 4),
            "weight": round(self.weight, 3),
            "value_per_kwh": round(self.value_per_kwh, 4),
        }


def _tail_import_prices(
    slots: Sequence[PlannedSlot],
    published: Sequence[bool],
    profile: PriceProfile,
    interval_minutes: int,
    tz: tzinfo | None,
) -> list[tuple[datetime, float, bool]] | None:
    """Return the estimated import price of every tail slot.

    The profile carries the shape.  The level comes from the import prices
    published in the horizon, as for a missing slot inside it (issue #1434),
    or from the last known level shift when the horizon has none (issue
    #1452).  A tail slot that starts 24 hours or more after the last
    published slot ended takes the profile as recorded: two days after the
    last price the shift raised the error (issue #1452).

    Args:
        slots: The horizon's slots with populated prices.
        published: Per slot, whether its price was published by a source.
        profile: Weekday and time-of-day profile of real prices.
        interval_minutes: Slot width in minutes.
        tz: Local zone the weekday and time of day are read in.

    Returns:
        ``(start, import price, shifted)`` per tail slot, or ``None`` when
        the profile lacks a price for any of them.
    """
    step = timedelta(minutes=interval_minutes)
    tail_start = slots[-1].end
    starts = [
        tail_start + index * step
        for index in range(TERMINAL_TAIL_HOURS * 60 // interval_minutes)
    ]
    shapes: list[float] = []
    for start in starts:
        local = as_tz(start, tz)
        pair = profile.lookup(
            local.weekday(), local.hour, local.minute, interval_minutes
        )
        if pair is None:
            return None
        shapes.append(pair[0])

    known: list[float | None] = []
    horizon_shapes: list[float | None] = []
    for slot, is_published in zip(slots, published):
        local = as_tz(slot.start, tz)
        pair = profile.lookup(
            local.weekday(), local.hour, local.minute, interval_minutes
        )
        known.append(slot.price.import_price if is_published else None)
        horizon_shapes.append(None if pair is None else pair[0])
    level = profile.import_level
    shift = median_shift(known, horizon_shapes, interval_minutes)
    if shift is not None:
        last_published = max(
            slot.end for slot, is_published in zip(slots, published) if is_published
        )
        level = (shift, last_published + LEVEL_SHIFT_MAX_AGE)

    prices: list[tuple[datetime, float, bool]] = []
    for start, shape in zip(starts, shapes):
        if level is not None and level_shift_applies(start, level[1]):
            prices.append((start, shape + level[0], True))
        else:
            prices.append((start, shape, False))
    return prices


def terminal_price_tail(
    slots: Sequence[PlannedSlot],
    published: Sequence[bool],
    profile: PriceProfile | None,
    interval_minutes: int,
    tz: tzinfo | None,
    *,
    last_day_value: float | None,
    weight: float,
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
        weight: Share of the tail's value, clamped to 0-1.  ``0`` disables
            the tail.
        top_n: Number of most expensive slots that make up the peak.
        charge_eff: Charge efficiency fraction (0-1).
        discharge_eff: Discharge efficiency fraction (0-1).
        cycle_cost_per_kwh: Battery wear per kWh of throughput.

    Returns:
        The tail and the blended value, or ``None`` when the tail is
        disabled, there is no last-day value, the profile is missing or
        empty, or it lacks a price for a tail slot.  The caller then keeps
        *last_day_value* unchanged.
    """
    share = min(max(weight, 0.0), 1.0)
    if (
        share < 1e-9
        or last_day_value is None
        or profile is None
        or profile.is_empty
        or not slots
    ):
        return None
    prices = _tail_import_prices(slots, published, profile, interval_minutes, tz)
    if prices is None:
        return None
    top = sorted((price for _, price, _ in prices), reverse=True)[: max(top_n, 1)]
    night = [
        price
        for start, price, _ in prices
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
        start=prices[0][0],
        end=prices[-1][0] + timedelta(minutes=interval_minutes),
        slot_count=len(prices),
        shifted_slots=sum(1 for _, _, shifted in prices if shifted),
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
    slots carry a published price from *tsi*.  See
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
    """
    return terminal_price_tail(
        slots,
        [meta.key not in tsi.missing_price_slots for meta in tsi.slots],
        inp.price_profile,
        inp.interval_minutes,
        tz,
        last_day_value=last_day_value,
        weight=inp.terminal_price_tail_weight,
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
