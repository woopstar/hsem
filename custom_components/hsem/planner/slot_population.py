"""Slot population helpers for the HSEM planner.

Single responsibility: transform raw time-series inputs (prices, Solcast PV,
consumption averages) into fully populated :class:`PlannedSlot` objects.

All functions are pure — no I/O, no side effects beyond mutating the slot list
passed in.  No Home Assistant imports.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from custom_components.hsem.const import (
    BASELINE_7D_SHARE,
    BASELINE_14D_SHARE,
    CAP7_DOWN,
    CAP7_UP,
    CAP14_DOWN,
    CAP14_UP,
    CHANGE3_LIMIT_DOWN_FACTOR,
    CHANGE3_LIMIT_UP_FACTOR,
    CHANGE_LIMIT_DOWN_FACTOR,
    CHANGE_LIMIT_UP_FACTOR,
    IQR_OUTLIER_MULTIPLIER,
    RELIABILITY_EPS,
    RELIABILITY_SCALE_STRENGTH,
)
from custom_components.hsem.models.planner_inputs import (
    HourlyConsumptionAverage,
    PlannerInput,
    PricePoint,
    SolcastSlot,
)
from custom_components.hsem.models.planner_outputs import PlannedSlot
from custom_components.hsem.models.time_series import TimeSeriesIndex
from custom_components.hsem.utils.datetime_utils import as_tz
from custom_components.hsem.utils.prices import SlotPrice
from custom_components.hsem.utils.recommendations import Recommendations

# ---------------------------------------------------------------------------
# Slot generation
# ---------------------------------------------------------------------------


def build_time_series_index(inp: PlannerInput, now: datetime) -> TimeSeriesIndex:
    """Build the shared :class:`TimeSeriesIndex` for a planning run.

    The index is the single source of truth for slot boundaries.  All
    populate functions should derive their slot positions from the index
    rather than computing ``start.hour`` independently.

    Args:
        inp: Planner input containing interval and horizon settings.
        now: Timezone-aware current datetime.

    Returns:
        A fully constructed :class:`TimeSeriesIndex`.
    """
    return TimeSeriesIndex.from_now(
        now,
        interval_minutes=inp.interval_minutes,
        horizon_hours=inp.interval_length_hours,
    )


def build_slots(inp: PlannerInput, now: datetime) -> list[PlannedSlot]:
    """Generate a chronologically ordered list of empty :class:`PlannedSlot` objects.

    Slot boundaries are derived from a :class:`TimeSeriesIndex` so that
    every slot is DST-safe and consistent with the shared time axis.

    Args:
        inp: Planner input containing interval settings.
        now: Timezone-aware current datetime.

    Returns:
        List of empty :class:`PlannedSlot` objects.
    """
    tsi = build_time_series_index(inp, now)
    return [PlannedSlot(start=meta.start, end=meta.end) for meta in tsi]


def index_by_hour(items: list, hour_attr: str = "hour") -> dict[int, Any]:
    """Build a dict keyed by the integer *hour* attribute of each item."""
    return {getattr(item, hour_attr): item for item in items}


# ---------------------------------------------------------------------------
# Time-series population
# ---------------------------------------------------------------------------


def populate_prices(
    slots: list[PlannedSlot],
    price_points: list[PricePoint],
    tsi: TimeSeriesIndex | None = None,
) -> None:
    """Write import/export prices into each slot from ``price_points``.

    When a :class:`TimeSeriesIndex` is provided the prices are aligned via
    the shared slot index so that all series use the same time axis.  Missing
    hours (``NaN`` sentinel) default to 0 to preserve backward-compatible
    behaviour.

    Args:
        slots: Mutable list of planned slots to update.
        price_points: Per-hour price data.
        tsi: Optional shared time-series index.  When supplied, alignment is
            delegated to :meth:`TimeSeriesIndex.align_hourly_prices` so that
            missing slots are tracked centrally.
    """
    if tsi is not None:
        # Use (day_offset, hour) keys when any entry carries a non-zero
        # day_offset so that tomorrow's prices are not overwritten by today's.
        if any(pp.day_offset != 0 for pp in price_points):
            imp_prices: dict[tuple[int, int], float] = {
                (pp.day_offset, pp.hour): pp.import_price for pp in price_points
            }
            exp_prices: dict[tuple[int, int], float] = {
                (pp.day_offset, pp.hour): pp.export_price for pp in price_points
            }
        else:
            imp_prices = {pp.hour: pp.import_price for pp in price_points}  # type: ignore[assignment]
            exp_prices = {pp.hour: pp.export_price for pp in price_points}  # type: ignore[assignment]
        aligned_imp, aligned_exp = tsi.align_hourly_prices(imp_prices, exp_prices)
        for slot, imp, exp in zip(slots, aligned_imp, aligned_exp):
            # Missing hours (NaN sentinel) fall back to 0.0 — preserve
            # backward-compatible behaviour for downstream consumers.
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


def populate_solcast(
    slots: list[PlannedSlot],
    solcast_slots: list[SolcastSlot],
    interval_minutes: int,
    tsi: TimeSeriesIndex | None = None,
) -> None:
    """Write PV estimates into each slot, scaled to the slot duration.

    Solcast data is provided per *hour*; if the slot duration is shorter
    (e.g. 15 min) the estimate is divided proportionally.

    When a :class:`TimeSeriesIndex` is provided the PV series is aligned via
    the shared slot index and missing slots are tracked centrally.

    Args:
        slots: Mutable list of planned slots to update.
        solcast_slots: Per-hour Solcast PV estimate data.
        interval_minutes: Slot width in minutes.
        tsi: Optional shared time-series index.
    """
    if tsi is not None:
        # Use (day_offset, hour) keys when any entry carries a non-zero
        # day_offset so that tomorrow's PV forecast is not shadowed by today's.
        if any(sc.day_offset != 0 for sc in solcast_slots):
            pv_by_hour: dict[tuple[int, int], float] = {
                (sc.day_offset, sc.hour): sc.pv_estimate for sc in solcast_slots
            }
        else:
            pv_by_hour = {sc.hour: sc.pv_estimate for sc in solcast_slots}  # type: ignore[assignment]
        aligned = tsi.align_hourly_pv(pv_by_hour)
        for slot, val in zip(slots, aligned):
            slot.solcast_pv_estimate = 0.0 if math.isnan(val) else round(val, 3)
        return

    solcast_by_hour = index_by_hour(solcast_slots)
    scale = 60.0 / interval_minutes  # e.g. 4 for 15-min slots

    for slot in slots:
        sc = solcast_by_hour.get(slot.start.hour)
        slot.solcast_pv_estimate = round(sc.pv_estimate / scale, 3) if sc else 0.0


def populate_consumption(
    slots: list[PlannedSlot],
    averages: list[HourlyConsumptionAverage],
    w1: int,
    w3: int,
    w7: int,
    w14: int,
    interval_minutes: int,
    tsi: TimeSeriesIndex | None = None,
) -> None:
    """Compute and write spike-aware weighted consumption into each slot.

    When a :class:`TimeSeriesIndex` is provided each sub-series (1d, 3d, 7d,
    14d) is individually aligned via the shared slot axis so that missing
    hours are tracked centrally rather than silently defaulted to zero.

    Args:
        slots: Mutable list of planned slots to update.
        averages: Per-hour historical consumption averages.
        w1..w14: Configured integer weights (percent).
        interval_minutes: Slot width in minutes.
        tsi: Optional shared time-series index.
    """
    if tsi is not None:
        # Use (day_offset, hour) keys when any entry carries a non-zero
        # day_offset so that tomorrow's consumption forecast is not overwritten
        # by today's cyclical averages.
        if any(ca.day_offset != 0 for ca in averages):
            avg_1d: dict[tuple[int, int], float] = {
                (ca.day_offset, ca.hour): ca.avg_1d for ca in averages
            }
            avg_3d: dict[tuple[int, int], float] = {
                (ca.day_offset, ca.hour): ca.avg_3d for ca in averages
            }
            avg_7d: dict[tuple[int, int], float] = {
                (ca.day_offset, ca.hour): ca.avg_7d for ca in averages
            }
            avg_14d: dict[tuple[int, int], float] = {
                (ca.day_offset, ca.hour): ca.avg_14d for ca in averages
            }
        else:
            avg_1d = {ca.hour: ca.avg_1d for ca in averages}  # type: ignore[assignment]
            avg_3d = {ca.hour: ca.avg_3d for ca in averages}  # type: ignore[assignment]
            avg_7d = {ca.hour: ca.avg_7d for ca in averages}  # type: ignore[assignment]
            avg_14d = {ca.hour: ca.avg_14d for ca in averages}  # type: ignore[assignment]
        aligned_1d = tsi.align_hourly_load(avg_1d)
        aligned_3d = tsi.align_hourly_load(avg_3d)
        aligned_7d = tsi.align_hourly_load(avg_7d)
        aligned_14d = tsi.align_hourly_load(avg_14d)
        for i, (slot, v1, v3, v7, v14) in enumerate(
            zip(slots, aligned_1d, aligned_3d, aligned_7d, aligned_14d)
        ):
            if any(math.isnan(v) for v in (v1, v3, v7, v14)):
                continue  # missing data — leave defaults
            # Reverse the slot_fraction scaling: TSI already applied it;
            # weighted_avg_consumption expects hourly values, so undo scaling.
            sf = tsi.slots[i].slot_fraction
            if abs(sf) < 1e-9:
                continue
            h1 = v1 / sf
            h3 = v3 / sf
            h7 = v7 / sf
            h14 = v14 / sf
            hourly_avg, _ = weighted_avg_consumption(h1, h3, h7, h14, w1, w3, w7, w14)
            slot.avg_house_consumption = round(hourly_avg * sf, 3)
            slot.avg_house_consumption_1d = round(v1, 3)
            slot.avg_house_consumption_3d = round(v3, 3)
            slot.avg_house_consumption_7d = round(v7, 3)
            slot.avg_house_consumption_14d = round(v14, 3)
        return

    avg_by_hour = index_by_hour(averages)
    scale = 60.0 / interval_minutes

    for slot in slots:
        h = slot.start.hour
        ca: HourlyConsumptionAverage | None = avg_by_hour.get(h)
        if ca is None:
            continue

        hourly_avg, _ = weighted_avg_consumption(
            ca.avg_1d, ca.avg_3d, ca.avg_7d, ca.avg_14d, w1, w3, w7, w14
        )
        slot_avg = round(hourly_avg / scale, 3)
        slot.avg_house_consumption = slot_avg
        slot.avg_house_consumption_1d = round(ca.avg_1d / scale, 3)
        slot.avg_house_consumption_3d = round(ca.avg_3d / scale, 3)
        slot.avg_house_consumption_7d = round(ca.avg_7d / scale, 3)
        slot.avg_house_consumption_14d = round(ca.avg_14d / scale, 3)


def populate_net_consumption(slots: list[PlannedSlot]) -> None:
    """Compute effective net consumption per slot.

    Formula::

        effective_net_load_kwh
            = avg_house_consumption + ev_planned_load_kwh - solcast_pv_estimate

    The ``ev_planned_load_kwh`` field is already populated when EV planned
    load integration is active (and ``base_load_includes_ev`` is False).
    When EV integration is disabled ``ev_planned_load_kwh`` defaults to 0.0
    so the formula degrades to the original ``avg_consumption - pv_estimate``.

    Args:
        slots: Mutable list of planned slots to update.
    """
    for slot in slots:
        slot.estimated_net_consumption = round(
            slot.avg_house_consumption
            + slot.ev_planned_load_kwh
            - slot.solcast_pv_estimate,
            3,
        )


def populate_estimated_cost(slots: list[PlannedSlot]) -> None:
    """Compute estimated grid cost per slot.

    Args:
        slots: Mutable list of planned slots to update.
    """
    for slot in slots:
        net = slot.estimated_net_consumption
        if net >= 0:
            slot.estimated_cost = round(net * slot.price.import_price, 4)
        else:
            slot.estimated_cost = round(net * slot.price.export_price, 4)


def mark_time_passed(slots: list[PlannedSlot], now: datetime) -> None:
    """Mark past slots as ``TimePassed``.

    Args:
        slots: Mutable list of planned slots to update.
        now: Timezone-aware current datetime.
    """
    for slot in slots:
        if as_tz(slot.end, now.tzinfo) < now:
            slot.recommendation = Recommendations.TimePassed.value


def populate_battery_capacity(
    slots: list[PlannedSlot],
    now: datetime,
    current_capacity: float,
    usable_capacity: float,
) -> None:
    """Forward-simulate battery SoC through all slots.

    Args:
        slots: Mutable list of planned slots to update.
        now: Timezone-aware current datetime.
        current_capacity: Currently available battery energy in kWh.
        usable_capacity: Maximum usable battery energy in kWh.
    """
    previous_capacity = 0.0

    for slot in slots:
        slot_start = as_tz(slot.start, now.tzinfo)
        slot_end = as_tz(slot.end, now.tzinfo)

        if slot_start <= now < slot_end:
            cap = max(
                current_capacity
                - slot.estimated_net_consumption
                + slot.batteries_charged,
                0.0,
            )
        elif slot_start >= now:
            cap = max(
                previous_capacity
                - slot.estimated_net_consumption
                + slot.batteries_charged,
                0.0,
            )
        else:
            cap = 0.0

        cap = min(cap, usable_capacity)
        slot.estimated_battery_capacity = round(cap, 3)
        previous_capacity = cap

    for slot in slots:
        if slot.estimated_battery_capacity > 0 and usable_capacity > 0:
            slot.estimated_battery_soc = round(
                slot.estimated_battery_capacity / usable_capacity * 100, 2
            )


def usable_capacity(
    rated_kwh: float,
    soc_pct: float,
    end_of_discharge_soc_pct: float,
    max_soc_pct: float = 100.0,
) -> tuple[float, float]:
    """Return ``(usable_kwh, current_kwh)`` given rated capacity and SoC limits.

    ``usable_kwh`` is the energy available in the range
    ``[end_of_discharge_soc, max_soc]``.
    ``current_kwh`` is the energy currently stored above the discharge floor,
    clamped to ``usable_kwh``.

    Args:
        rated_kwh: Nameplate capacity in kWh.
        soc_pct: Current state of charge as a percentage (0-100).
        end_of_discharge_soc_pct: Minimum allowed SoC as a percentage (0-100).
        max_soc_pct: Maximum allowed SoC as a percentage (0-100).  Defaults to
            100 % (no restriction beyond nameplate capacity).

    Returns:
        ``(usable_kwh, current_kwh)`` tuple, both non-negative.
    """
    effective_max_soc = min(max(max_soc_pct, end_of_discharge_soc_pct), 100.0)
    usable = rated_kwh * (effective_max_soc - end_of_discharge_soc_pct) / 100
    current = rated_kwh * (soc_pct / 100) - rated_kwh * end_of_discharge_soc_pct / 100
    return max(usable, 0.0), min(max(current, 0.0), max(usable, 0.0))


# ---------------------------------------------------------------------------
# Consumption weighting (pure arithmetic, no I/O)
# ---------------------------------------------------------------------------


def compute_spike_severity(ratio: float, ratio_min: float, ratio_max: float) -> float:
    """Return a severity value in [0, 1] for spike detection.

    Args:
        ratio: Observed ratio between two consumption windows.
        ratio_min: Lower threshold below which severity is 0.
        ratio_max: Upper threshold above which severity is 1.

    Returns:
        Float in [0, 1].
    """
    if ratio <= ratio_min:
        return 0.0
    if ratio >= ratio_max:
        return 1.0
    return (ratio - ratio_min) / (ratio_max - ratio_min)


def detect_outliers_iqr(
    values: list[float],
    multiplier: float = IQR_OUTLIER_MULTIPLIER,
) -> list[bool]:
    """Return a boolean mask flagging outlier values via median-ratio detection.

    With only 4 data points (1d, 3d, 7d, 14d), the classic IQR Tukey fence
    produces wide bounds that rarely flag anything.  Instead we use a
    median-ratio approach: a value is an outlier when its ratio to the
    median of all 4 values exceeds ``multiplier`` (for upward outliers) or
    falls below ``1/multiplier`` (for downward outliers).

    This detects both upward spikes (e.g. 10.0 vs 1.0) and downward anomalies
    (e.g. 0.188 vs 0.708) while allowing gradual trends (e.g. 2.0, 1.9, 1.8, 1.0).

    When all values are identical (median = 0), no value is flagged.

    Args:
        values: List of 4 float values (typically 4: 1d, 3d, 7d, 14d).
        multiplier: Ratio threshold.  Defaults to
            :data:`IQR_OUTLIER_MULTIPLIER` (1.5).

    Returns:
        List of booleans the same length as *values*, where ``True`` means
        the corresponding value is an outlier.
    """
    n = len(values)
    if n < 4:
        return [False] * n

    sorted_vals = sorted(values)
    # Median of 4 values = average of the two middle values
    median = (sorted_vals[1] + sorted_vals[2]) / 2.0

    if abs(median) < 1e-12:
        return [False] * n  # all near-zero — no outliers

    upper_ratio = multiplier
    lower_ratio = 1.0 / multiplier

    return [v / median > upper_ratio or v / median < lower_ratio for v in values]


def weighted_avg_consumption(
    value_1d: float,
    value_3d: float,
    value_7d: float,
    value_14d: float,
    w1: int,
    w3: int,
    w7: int,
    w14: int,
) -> tuple[float, list[bool]]:
    """Apply IQR-based outlier-aware dynamic reweighting and return the weighted average.

    Replaces the old ratio-based spike detection (issue #301).  Outliers are
    detected via the IQR (Tukey fence) method across the 4 consumption windows.
    When a window is flagged as an outlier, its weight is redistributed to the
    non-outlier windows proportionally.  Outliers are tracked and returned
    so callers can log or display them.

    The mild capping between 7d/14d and the baseline capping for 1d/3d are
    retained as a safety net.  The reliability scaling is also retained.

    Args:
        value_1d..value_14d: Raw consumption values for each window (kWh/hour).
        w1..w14: Configured integer weights (percent, should sum to 100).

    Returns:
        ``(weighted_average, outlier_mask)`` where *weighted_average* is the
        final weighted average in kWh/hour and *outlier_mask* is a list of 4
        booleans ``[is_1d_outlier, is_3d_outlier, is_7d_outlier, is_14d_outlier]``.
    """
    w_total_config = w1 + w3 + w7 + w14
    if w_total_config == 0:
        return 0.0, [False, False, False, False]

    # IQR outlier detection on the RAW values (before capping) so that
    # extreme spikes are detected even when capping would hide them.
    raw = [value_1d, value_3d, value_7d, value_14d]
    outlier_mask = detect_outliers_iqr(raw)

    # Mild capping between 7d and 14d
    value_7d_eff = max(CAP7_DOWN * value_14d, min(value_7d, CAP7_UP * value_14d))
    value_14d_eff = max(
        CAP14_DOWN * value_7d_eff, min(value_14d, CAP14_UP * value_7d_eff)
    )

    # Baseline capping for 1d/3d
    baseline = BASELINE_7D_SHARE * value_7d_eff + BASELINE_14D_SHARE * value_14d_eff
    value_1d_eff = max(
        baseline * CHANGE_LIMIT_DOWN_FACTOR,
        min(value_1d, baseline * CHANGE_LIMIT_UP_FACTOR),
    )
    value_3d_eff = max(
        baseline * CHANGE3_LIMIT_DOWN_FACTOR,
        min(value_3d, baseline * CHANGE3_LIMIT_UP_FACTOR),
    )

    # Redistribute weight from outlier windows to non-outlier windows.
    weights = [float(w1), float(w3), float(w7), float(w14)]
    non_outlier_weight = sum(
        w for w, is_out in zip(weights, outlier_mask) if not is_out
    )

    if non_outlier_weight > 1e-9 and any(outlier_mask):
        scale = w_total_config / non_outlier_weight
        w1_eff = weights[0] * scale if not outlier_mask[0] else 0.0
        w3_eff = weights[1] * scale if not outlier_mask[1] else 0.0
        w7_eff = weights[2] * scale if not outlier_mask[2] else 0.0
        w14_eff = weights[3] * scale if not outlier_mask[3] else 0.0
    else:
        w1_eff, w3_eff, w7_eff, w14_eff = weights

    # Reliability scaling
    def _rel(diff: float) -> float:
        raw = 1.0 / (RELIABILITY_EPS + abs(diff))
        return 1.0 + (raw - 1.0) * RELIABILITY_SCALE_STRENGTH

    w1_eff *= _rel(value_1d_eff - value_7d_eff)
    w3_eff *= _rel(value_3d_eff - value_7d_eff)
    w7_eff *= _rel(value_7d_eff - value_14d_eff)
    w14_eff *= _rel(value_14d_eff - value_7d_eff)

    w_sum_eff = w1_eff + w3_eff + w7_eff + w14_eff
    if w_sum_eff > 0:
        scale_back = w_total_config / w_sum_eff
        w1_eff *= scale_back
        w3_eff *= scale_back
        w7_eff *= scale_back
        w14_eff *= scale_back
    else:
        w1_eff, w3_eff, w7_eff, w14_eff = (
            float(w1),
            float(w3),
            float(w7),
            float(w14),
        )

    result = round(
        value_1d_eff * (w1_eff / 100)
        + value_3d_eff * (w3_eff / 100)
        + value_7d_eff * (w7_eff / 100)
        + value_14d_eff * (w14_eff / 100),
        3,
    )
    return result, outlier_mask
