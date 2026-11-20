"""Price population for the HSEM planner.

Single responsibility: transform raw :class:`PricePoint` inputs into
per-slot :class:`SlotPrice` values on the planned slots, including the
missing-price estimation rule (issue #1002): a slot with no source price
is filled from the weekday and time-of-day profile of real prices, shifted
to the level of the published prices, when one is supplied (issue #1434),
otherwise with the price of the same wall-clock time on the nearest earlier
day that has data (the same quarter of the hour when the source is
sub-hourly, issue #1219), and the gap is always recorded on the shared
time-series index.

Split out of ``slot_population.py`` to keep both modules under the 30 KB
file-size limit.  Pure functions — no I/O, no Home Assistant imports.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Hashable, Iterable
from statistics import median
from typing import cast

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.price_point import PricePoint
from custom_components.hsem.models.price_profile import (
    PROFILE_MIN_ANCHOR_MINUTES,
    PricePair,
    PriceProfile,
)
from custom_components.hsem.models.time_series import SlotMeta, TimeSeriesIndex
from custom_components.hsem.planner.slot_population import index_by_hour
from custom_components.hsem.utils.logger import log_planner
from custom_components.hsem.utils.prices import SlotPrice


def _mean_by_key[K: Hashable](pairs: Iterable[tuple[K, float]]) -> dict[K, float]:
    """Return the mean of the values that share a key.

    A key whose values are all the same keeps the first of them, so an hourly
    source repeated on every sub-hourly slot is not disturbed by rounding.
    """
    grouped: dict[K, list[float]] = {}
    for key, value in pairs:
        grouped.setdefault(key, []).append(value)
    return {
        key: values[0]
        if max(values) - min(values) < 1e-12
        else sum(values) / len(values)
        for key, values in grouped.items()
    }


def _estimate_missing_price(
    prices_by_day_hour: dict[tuple[int, int], float],
    day_offset: int,
    hour: int,
    prices_by_day_quarter: dict[tuple[int, int, int], float] | None = None,
    minute: int = 0,
) -> float | None:
    """Estimate a missing price from the nearest earlier day with data.

    Spot-market prices are only known for today (and tomorrow after ~13:00
    local time), so slots on uncovered days must not be planned as *free*
    energy (issue #1002).  Walk backwards from ``day_offset - 1`` to day 0
    and return the price of the same wall-clock time on the nearest day that
    has one: the same quarter of the hour when *prices_by_day_quarter* holds
    it, otherwise the hour's price (issue #1219).

    Args:
        prices_by_day_hour: Prices keyed by ``(day_offset, hour)``.
        day_offset: Day offset of the missing slot.
        hour: Wall-clock hour (0-23) of the missing slot.
        prices_by_day_quarter: Sub-hourly prices keyed by
            ``(day_offset, hour, minute)``, or ``None`` for an hourly source.
        minute: Wall-clock minute (0-59) the missing slot starts at.

    Returns:
        The estimated price, or ``None`` when no earlier day has data for
        that hour.
    """
    for d in range(day_offset - 1, -1, -1):
        if prices_by_day_quarter is not None:
            quarter = prices_by_day_quarter.get((d, hour, minute))
            if quarter is not None:
                return quarter
        value = prices_by_day_hour.get((d, hour))
        if value is not None:
            return value
    return None


#: Estimates one side's price for a slot from an earlier day, or ``None``.
_Estimator = Callable[[SlotMeta], float | None]


def _anchor_offset(
    known: list[float | None],
    profile_side: list[float | None],
    interval_minutes: int,
) -> float | None:
    """Return the shift that moves the profile to the published price level.

    The profile is a median of up to eight weeks, so its absolute level can
    be far from today's.  Its shape is still useful.  The shift is the median
    difference between the published prices and the profile on the slots
    that have both, so the profile is read relative to the prices that are
    known now (issue #1434).

    Args:
        known: Published price per slot, ``None`` where it is missing.
        profile_side: Profile price per slot, ``None`` where it has none.
        interval_minutes: Slot width in minutes.

    Returns:
        The shift to add to a profile price, or ``None`` when fewer than
        :data:`PROFILE_MIN_ANCHOR_MINUTES` of published prices overlap the
        profile.
    """
    differences = [
        price - shape
        for price, shape in zip(known, profile_side)
        if price is not None and shape is not None
    ]
    if len(differences) * interval_minutes < PROFILE_MIN_ANCHOR_MINUTES:
        return None
    return median(differences)


def _fill_side(
    known: list[float | None],
    metas: list[SlotMeta],
    estimate: _Estimator,
    profile_side: list[float | None] | None,
    interval_minutes: int,
) -> list[float]:
    """Return one side's price for every slot, estimating the missing ones.

    Order for a slot without a published price:

    1. The profile shifted to the published level, when enough published
       prices overlap the profile to derive the shift.
    2. The nearest earlier day in the payload (*estimate*).
    3. The profile as recorded, when there is no published level to shift it
       to (a price outage).
    4. With a profile: the median of the published prices, or of the prices
       filled so far, so a slot the profile does not know is not priced
       ``0.0`` next to real prices.
    5. ``0.0``.

    Without a profile only steps 2 and 5 apply, which is the behaviour
    before issue #1434.

    Args:
        known: Published price per slot, ``None`` where it is missing.
        metas: The slots, parallel to *known*.
        estimate: Nearest-earlier-day estimate for a slot.
        profile_side: Profile price per slot, or ``None`` without a profile.
        interval_minutes: Slot width in minutes.

    Returns:
        The price of every slot.
    """
    if all(price is not None for price in known):
        return [cast("float", price) for price in known]
    filled: list[float | None] = list(known)
    offset = (
        _anchor_offset(known, profile_side, interval_minutes)
        if profile_side is not None
        else None
    )
    for index, meta in enumerate(metas):
        if filled[index] is not None:
            continue
        shape = profile_side[index] if profile_side is not None else None
        if offset is not None and shape is not None:
            filled[index] = shape + offset
            continue
        value = estimate(meta)
        filled[index] = shape if value is None else value
    if profile_side is not None and any(price is None for price in filled):
        published = [price for price in known if price is not None]
        level_source = published or [price for price in filled if price is not None]
        if level_source:
            level = median(level_source)
            filled = [level if price is None else price for price in filled]
    return [0.0 if price is None else price for price in filled]


def _write_prices(
    slots: list[PlannedSlot],
    tsi: TimeSeriesIndex,
    imports: list[float | None],
    exports: list[float | None],
    estimate_import: _Estimator,
    estimate_export: _Estimator,
    profile: PriceProfile | None,
) -> None:
    """Fill the missing prices and write a ``SlotPrice`` to every slot.

    Args:
        slots: Planned slots to update, parallel to ``tsi.slots``.
        tsi: Shared time-series index.
        imports: Published import price per slot, ``None`` where missing.
        exports: Published export price per slot, ``None`` where missing.
        estimate_import: Nearest-earlier-day import estimate for a slot.
        estimate_export: Nearest-earlier-day export estimate for a slot.
        profile: Weekday and time-of-day profile of real prices, or ``None``.
    """
    step = tsi.interval_minutes
    import_shape: list[float | None] | None = None
    export_shape: list[float | None] | None = None
    if profile is not None and not profile.is_empty:
        pairs: list[PricePair | None] = [
            profile.lookup(meta.start.weekday(), meta.hour, meta.minute, step)
            for meta in tsi.slots
        ]
        import_shape = [None if pair is None else pair[0] for pair in pairs]
        export_shape = [None if pair is None else pair[1] for pair in pairs]
    import_prices = _fill_side(imports, tsi.slots, estimate_import, import_shape, step)
    export_prices = _fill_side(exports, tsi.slots, estimate_export, export_shape, step)
    for slot, import_price, export_price in zip(slots, import_prices, export_prices):
        slot.price = SlotPrice(import_price=import_price, export_price=export_price)


def populate_prices(
    slots: list[PlannedSlot],
    price_points: list[PricePoint],
    tsi: TimeSeriesIndex | None = None,
    price_profile: PriceProfile | None = None,
) -> None:
    """Write import/export prices into each slot from ``price_points``.

    When a :class:`TimeSeriesIndex` is provided the prices are aligned via
    the shared slot index so that all series use the same time axis.

    Slots with no source price data (e.g. day+1 before the day-ahead
    auction publishes, or any multi-day horizon beyond the data coverage)
    are estimated per side, see :func:`_fill_side`: from *price_profile*
    shifted to the level of the published prices (issue #1434), otherwise
    with the price of the same wall-clock time on the nearest earlier day
    that has data — see :func:`_estimate_missing_price`.  With sub-hourly
    price points that is the same quarter of the hour, and the hour's mean
    when that day lacks the quarter (issue #1219).  Only when nothing gives
    a price do they fall back to 0.0 for backward compatibility.  The gap is
    always recorded on ``tsi.missing_price_slots`` so ``DataQuality``
    warnings still fire.

    A price point may carry one side only (``None`` on the other).  The
    published side is kept and only the other side is estimated.

    Args:
        slots: Mutable list of planned slots to update.
        price_points: Per-hour price data.
        tsi: Optional shared time-series index.  When supplied, alignment is
            delegated to :meth:`TimeSeriesIndex.align_hourly_prices` so that
            missing slots are tracked centrally.
        price_profile: Optional weekday and time-of-day profile of real
            prices.  ``None`` or an empty profile changes nothing.
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
            _populate_slot_prices(slots, price_points, tsi, price_profile)
            return

        # Hour-granular points are priced as a pair: one without both sides
        # is left out and estimated.
        pairs = [
            (pp, pp.import_price, pp.export_price)
            for pp in price_points
            if pp.import_price is not None and pp.export_price is not None
        ]
        # Use (day_offset, hour) keys when any entry carries a non-zero
        # day_offset so that tomorrow's prices are not overwritten by today's.
        imp_prices: dict[int, float] | dict[tuple[int, int], float]
        exp_prices: dict[int, float] | dict[tuple[int, int], float]
        if any(pp.day_offset != 0 for pp, _, _ in pairs):
            imp_prices = {(pp.day_offset, pp.hour): imp for pp, imp, _ in pairs}
            exp_prices = {(pp.day_offset, pp.hour): exp for pp, _, exp in pairs}
        else:
            imp_prices = {pp.hour: imp for pp, imp, _ in pairs}
            exp_prices = {pp.hour: exp for pp, _, exp in pairs}
        aligned_imp, aligned_exp = tsi.align_hourly_prices(imp_prices, exp_prices)
        # Estimation needs (day_offset, hour) keys; legacy hour-only keys are
        # cyclical and have no earlier day to estimate from.
        can_estimate = bool(imp_prices) and isinstance(next(iter(imp_prices)), tuple)
        day_prices_imp = cast("dict[tuple[int, int], float]", imp_prices)
        day_prices_exp = cast("dict[tuple[int, int], float]", exp_prices)
        _write_prices(
            slots,
            tsi,
            [None if math.isnan(value) else value for value in aligned_imp],
            [None if math.isnan(value) else value for value in aligned_exp],
            lambda meta: (
                _estimate_missing_price(day_prices_imp, meta.key.day_offset, meta.hour)
                if can_estimate
                else None
            ),
            lambda meta: (
                _estimate_missing_price(day_prices_exp, meta.key.day_offset, meta.hour)
                if can_estimate
                else None
            ),
            price_profile,
        )
        return

    price_by_hour = index_by_hour(price_points)
    for slot in slots:
        pt = price_by_hour.get(slot.start.hour)
        if pt is not None:
            slot.price = SlotPrice(
                import_price=pt.import_price or 0.0,
                export_price=pt.export_price or 0.0,
            )


def _side_tables(
    price_points: list[PricePoint], side: str, step: int
) -> tuple[
    dict[tuple[int, int], float],
    dict[tuple[int, int], float],
    dict[tuple[int, int, int], float],
]:
    """Index one side of sub-hourly price points by slot, hour and quarter.

    Args:
        price_points: Price points; those without this side are skipped.
        side: ``"import_price"`` or ``"export_price"``.
        step: Slot width in minutes.

    Returns:
        ``(by slot, by hour, by quarter)``.  The hour table is the mean of
        the hour's points, which for an hourly source is its one price.  The
        quarter table is keyed by the wall-clock minute within the hour:
        ``slot_in_day`` counts real steps since local midnight, so on a DST
        day the same index is a different clock time, while the minute is
        not moved by a whole-hour shift.  Both passes of a repeated fall-back
        hour share a key and are averaged (issue #1219).
    """
    valued = [
        (pp, cast("float", getattr(pp, side)))
        for pp in price_points
        if getattr(pp, side) is not None
    ]
    by_slot = {
        (pp.day_offset, pp.slot_in_day): value
        for pp, value in valued
        if pp.slot_in_day is not None
    }
    by_hour = _mean_by_key(((pp.day_offset, pp.hour), value) for pp, value in valued)
    by_quarter = _mean_by_key(
        ((pp.day_offset, pp.hour, pp.slot_in_day * step % 60), value)
        for pp, value in valued
        if pp.slot_in_day is not None
    )
    return by_slot, by_hour, by_quarter


def _populate_slot_prices(
    slots: list[PlannedSlot],
    price_points: list[PricePoint],
    tsi: TimeSeriesIndex,
    price_profile: PriceProfile | None,
) -> None:
    """Price the slots from points that carry ``slot_in_day`` (issue #720).

    A slot the source does not cover takes the hour's value from the same
    day (a 60-minute source feeding 15-minute slots) before it counts as
    missing.  Missing slots are tracked on *tsi* here because
    ``align_hourly_prices`` is bypassed on this path.

    Args:
        slots: Planned slots to update, parallel to ``tsi.slots``.
        price_points: Price points, at least one with ``slot_in_day``.
        tsi: Shared time-series index.
        price_profile: Weekday and time-of-day profile, or ``None``.
    """
    step = tsi.interval_minutes
    imp_by_slot, imp_by_hour, imp_by_quarter = _side_tables(
        price_points, "import_price", step
    )
    exp_by_slot, exp_by_hour, exp_by_quarter = _side_tables(
        price_points, "export_price", step
    )
    imports: list[float | None] = []
    exports: list[float | None] = []
    for meta in tsi.slots:
        key = (meta.key.day_offset, meta.key.slot_in_day)
        hour_key = (meta.key.day_offset, meta.hour)
        imp = imp_by_slot.get(key, imp_by_hour.get(hour_key))
        exp = exp_by_slot.get(key, exp_by_hour.get(hour_key))
        if imp is None or exp is None:
            tsi.missing_slots.add(meta.key)
            tsi.missing_price_slots.add(meta.key)
        imports.append(imp)
        exports.append(exp)
    _write_prices(
        slots,
        tsi,
        imports,
        exports,
        lambda meta: _estimate_missing_price(
            imp_by_hour, meta.key.day_offset, meta.hour, imp_by_quarter, meta.minute
        ),
        lambda meta: _estimate_missing_price(
            exp_by_hour, meta.key.day_offset, meta.hour, exp_by_quarter, meta.minute
        ),
        price_profile,
    )
