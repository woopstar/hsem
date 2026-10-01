"""Price population for the HSEM planner.

Single responsibility: transform raw :class:`PricePoint` inputs into
per-slot :class:`SlotPrice` values on the planned slots, including the
missing-price estimation rule (issue #1002): a slot with no source price
is filled with the same-hour price from the nearest earlier day that has
data, and the gap is always recorded on the shared time-series index.

Split out of ``slot_population.py`` to keep both modules under the 30 KB
file-size limit.  Pure functions — no I/O, no Home Assistant imports.
"""

from __future__ import annotations

import math
from typing import cast

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.price_point import PricePoint
from custom_components.hsem.models.time_series import TimeSeriesIndex
from custom_components.hsem.planner.slot_population import index_by_hour
from custom_components.hsem.utils.logger import log_planner
from custom_components.hsem.utils.prices import SlotPrice


def _estimate_missing_price(
    prices_by_day_hour: dict[tuple[int, int], float],
    day_offset: int,
    hour: int,
) -> float | None:
    """Estimate a missing price from the nearest earlier day with data.

    Spot-market prices are only known for today (and tomorrow after ~13:00
    local time), so slots on uncovered days must not be planned as *free*
    energy (issue #1002).  Walk backwards from ``day_offset - 1`` to day 0
    and return the same-hour price from the nearest day that has one.

    Args:
        prices_by_day_hour: Prices keyed by ``(day_offset, hour)``.
        day_offset: Day offset of the missing slot.
        hour: Wall-clock hour (0-23) of the missing slot.

    Returns:
        The estimated price, or ``None`` when no earlier day has data for
        that hour.
    """
    for d in range(day_offset - 1, -1, -1):
        value = prices_by_day_hour.get((d, hour))
        if value is not None:
            return value
    return None


def populate_prices(
    slots: list[PlannedSlot],
    price_points: list[PricePoint],
    tsi: TimeSeriesIndex | None = None,
) -> None:
    """Write import/export prices into each slot from ``price_points``.

    When a :class:`TimeSeriesIndex` is provided the prices are aligned via
    the shared slot index so that all series use the same time axis.

    Slots with no source price data (e.g. day+1 before the day-ahead
    auction publishes, or any multi-day horizon beyond the data coverage)
    are filled with the same-hour price from the nearest earlier day that
    has data — see :func:`_estimate_missing_price`.  Only when no earlier
    day has data for that hour at all do they fall back to 0.0 for
    backward compatibility.  The gap is always recorded on
    ``tsi.missing_price_slots`` so ``DataQuality`` warnings still fire.

    Args:
        slots: Mutable list of planned slots to update.
        price_points: Per-hour price data.
        tsi: Optional shared time-series index.  When supplied, alignment is
            delegated to :meth:`TimeSeriesIndex.align_hourly_prices` so that
            missing slots are tracked centrally.
    """
    log_planner(
        "debug",
        "[pop] populate_prices  price_points=%d  tsi_provided=%s",
        len(price_points),
        tsi is not None,
    )
    if tsi is not None:
        # Sub-hourly path: when the points carry slot_in_day (issue #720),
        # key by (day_offset, slot_in_day) so quarter-hourly prices land on
        # their own slots instead of being fanned out from one hourly value.
        if any(pp.slot_in_day is not None for pp in price_points):
            imp_by_slot = {
                (pp.day_offset, pp.slot_in_day): pp.import_price
                for pp in price_points
                if pp.slot_in_day is not None
            }
            exp_by_slot = {
                (pp.day_offset, pp.slot_in_day): pp.export_price
                for pp in price_points
                if pp.slot_in_day is not None
            }
            # Hourly fallback for slots the source does not cover (e.g. a
            # 60-min price source feeding 15-min slots).
            imp_by_hour = {
                (pp.day_offset, pp.hour): pp.import_price for pp in price_points
            }
            exp_by_hour = {
                (pp.day_offset, pp.hour): pp.export_price for pp in price_points
            }
            for slot, meta in zip(slots, tsi.slots):
                key = (meta.key.day_offset, meta.key.slot_in_day)
                hour_key = (meta.key.day_offset, meta.hour)
                imp = imp_by_slot.get(key, imp_by_hour.get(hour_key))
                exp = exp_by_slot.get(key, exp_by_hour.get(hour_key))
                if imp is None or exp is None:
                    # Track the gap centrally so DataQuality warnings fire on
                    # this path too (align_hourly_prices is bypassed here).
                    tsi.missing_slots.add(meta.key)
                    tsi.missing_price_slots.add(meta.key)
                    if imp is None:
                        imp = _estimate_missing_price(
                            imp_by_hour, meta.key.day_offset, meta.hour
                        )
                    if exp is None:
                        exp = _estimate_missing_price(
                            exp_by_hour, meta.key.day_offset, meta.hour
                        )
                slot.price = SlotPrice(
                    import_price=0.0 if imp is None else imp,
                    export_price=0.0 if exp is None else exp,
                )
            return

        # Use (day_offset, hour) keys when any entry carries a non-zero
        # day_offset so that tomorrow's prices are not overwritten by today's.
        imp_prices: dict[int, float] | dict[tuple[int, int], float]
        exp_prices: dict[int, float] | dict[tuple[int, int], float]
        if any(pp.day_offset != 0 for pp in price_points):
            imp_prices = {
                (pp.day_offset, pp.hour): pp.import_price for pp in price_points
            }
            exp_prices = {
                (pp.day_offset, pp.hour): pp.export_price for pp in price_points
            }
        else:
            imp_prices = {pp.hour: pp.import_price for pp in price_points}
            exp_prices = {pp.hour: pp.export_price for pp in price_points}
        aligned_imp, aligned_exp = tsi.align_hourly_prices(imp_prices, exp_prices)
        # Estimation needs (day_offset, hour) keys; legacy hour-only keys are
        # cyclical and have no earlier day to estimate from.
        can_estimate = bool(imp_prices) and isinstance(next(iter(imp_prices)), tuple)
        for slot, meta, imp, exp in zip(slots, tsi.slots, aligned_imp, aligned_exp):
            if can_estimate and (math.isnan(imp) or math.isnan(exp)):
                day_prices_imp = cast("dict[tuple[int, int], float]", imp_prices)
                day_prices_exp = cast("dict[tuple[int, int], float]", exp_prices)
                if math.isnan(imp):
                    est_imp = _estimate_missing_price(
                        day_prices_imp, meta.key.day_offset, meta.hour
                    )
                    if est_imp is not None:
                        imp = est_imp
                if math.isnan(exp):
                    est_exp = _estimate_missing_price(
                        day_prices_exp, meta.key.day_offset, meta.hour
                    )
                    if est_exp is not None:
                        exp = est_exp
            # Hours missing on every day (no estimate possible) still fall
            # back to 0.0 — preserve backward-compatible behaviour.
            slot.price = SlotPrice(
                import_price=0.0 if math.isnan(imp) else imp,
                export_price=0.0 if math.isnan(exp) else exp,
            )
        return

    price_by_hour = index_by_hour(price_points)
    for slot in slots:
        pt = price_by_hour.get(slot.start.hour)
        if pt is not None:
            slot.price = SlotPrice(
                import_price=pt.import_price, export_price=pt.export_price
            )
