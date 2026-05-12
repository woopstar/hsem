"""Slot population helpers for the HSEM planner.

Single responsibility: transform raw time-series inputs (prices, Solcast PV,
consumption averages) into fully populated :class:`PlannedSlot` objects.

All functions are pure — no I/O, no side effects beyond mutating the slot list
passed in.  No Home Assistant imports.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from custom_components.hsem.const import (
    BASELINE_14D_SHARE,
    BASELINE_7D_SHARE,
    CAP14_DOWN,
    CAP14_UP,
    CAP7_DOWN,
    CAP7_UP,
    CHANGE3_LIMIT_DOWN_FACTOR,
    CHANGE3_LIMIT_UP_FACTOR,
    CHANGE_LIMIT_DOWN_FACTOR,
    CHANGE_LIMIT_UP_FACTOR,
    RELIABILITY_EPS,
    RELIABILITY_SCALE_STRENGTH,
    SPIKE1_RATIO_MAX,
    SPIKE1_RATIO_MIN,
    SPIKE1_REDIST_TO_14D,
    SPIKE1_REDIST_TO_3D,
    SPIKE1_REDIST_TO_7D,
    SPIKE1_REDUCE_FRACTION_MAX,
    SPIKE3_RATIO_MAX,
    SPIKE3_RATIO_MIN,
    SPIKE3_REDIST_TO_14D,
    SPIKE3_REDIST_TO_7D,
    SPIKE3_REDUCE_FRACTION_MAX,
    SPIKE7_RATIO_MAX,
    SPIKE7_RATIO_MIN,
    SPIKE7_REDIST_TO_14D,
    SPIKE7_REDUCE_FRACTION_MAX,
    SPIKE14_RATIO_MAX,
    SPIKE14_RATIO_MIN,
    SPIKE14_REDIST_TO_7D,
    SPIKE14_REDUCE_FRACTION_MAX,
)
from custom_components.hsem.models.planner_inputs import (
    HourlyConsumptionAverage,
    PlannerInput,
    PricePoint,
    SolcastSlot,
)
from custom_components.hsem.models.planner_outputs import PlannedSlot
from custom_components.hsem.utils.recommendations import Recommendations


# ---------------------------------------------------------------------------
# Slot generation
# ---------------------------------------------------------------------------


def build_slots(inp: PlannerInput, now: datetime) -> list[PlannedSlot]:
    """Generate a chronologically ordered list of empty :class:`PlannedSlot` objects.

    Slots start at midnight of *now*'s local calendar day and cover
    ``interval_length_hours`` hours at ``interval_minutes`` resolution.

    Args:
        inp: Planner input containing interval settings.
        now: Timezone-aware current datetime.

    Returns:
        List of empty :class:`PlannedSlot` objects.
    """
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    steps = int((inp.interval_length_hours * 60) / inp.interval_minutes)

    return [
        PlannedSlot(
            start=midnight + timedelta(minutes=i * inp.interval_minutes),
            end=midnight + timedelta(minutes=(i + 1) * inp.interval_minutes),
        )
        for i in range(steps)
    ]


def index_by_hour(items: list, hour_attr: str = "hour") -> dict[int, Any]:
    """Build a dict keyed by the integer *hour* attribute of each item."""
    return {getattr(item, hour_attr): item for item in items}


# ---------------------------------------------------------------------------
# Time-series population
# ---------------------------------------------------------------------------


def populate_prices(slots: list[PlannedSlot], price_points: list[PricePoint]) -> None:
    """Write import/export prices into each slot from ``price_points``.

    Price lookup is by the slot's ``start.hour``. Missing hours default to 0.

    Args:
        slots: Mutable list of planned slots to update.
        price_points: Per-hour price data.
    """
    price_by_hour = index_by_hour(price_points)
    for slot in slots:
        pt = price_by_hour.get(slot.start.hour)
        if pt is not None:
            slot.import_price = pt.import_price
            slot.export_price = pt.export_price


def populate_solcast(
    slots: list[PlannedSlot],
    solcast_slots: list[SolcastSlot],
    interval_minutes: int,
) -> None:
    """Write PV estimates into each slot, scaled to the slot duration.

    Solcast data is provided per *hour*; if the slot duration is shorter
    (e.g. 15 min) the estimate is divided proportionally.

    Args:
        slots: Mutable list of planned slots to update.
        solcast_slots: Per-hour Solcast PV estimate data.
        interval_minutes: Slot width in minutes.
    """
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
) -> None:
    """Compute and write spike-aware weighted consumption into each slot.

    Args:
        slots: Mutable list of planned slots to update.
        averages: Per-hour historical consumption averages.
        w1..w14: Configured integer weights (percent).
        interval_minutes: Slot width in minutes.
    """
    avg_by_hour = index_by_hour(averages)
    scale = 60.0 / interval_minutes

    for slot in slots:
        h = slot.start.hour
        ca: HourlyConsumptionAverage | None = avg_by_hour.get(h)
        if ca is None:
            continue

        hourly_avg = weighted_avg_consumption(
            ca.avg_1d, ca.avg_3d, ca.avg_7d, ca.avg_14d, w1, w3, w7, w14
        )
        slot_avg = round(hourly_avg / scale, 3)
        slot.avg_house_consumption = slot_avg
        slot.avg_house_consumption_1d = round(ca.avg_1d / scale, 3)
        slot.avg_house_consumption_3d = round(ca.avg_3d / scale, 3)
        slot.avg_house_consumption_7d = round(ca.avg_7d / scale, 3)
        slot.avg_house_consumption_14d = round(ca.avg_14d / scale, 3)


def populate_net_consumption(slots: list[PlannedSlot]) -> None:
    """Compute ``estimated_net_consumption = avg_consumption - pv_estimate``.

    Args:
        slots: Mutable list of planned slots to update.
    """
    for slot in slots:
        slot.estimated_net_consumption = round(
            slot.avg_house_consumption - slot.solcast_pv_estimate, 3
        )


def populate_estimated_cost(slots: list[PlannedSlot]) -> None:
    """Compute estimated grid cost per slot.

    Args:
        slots: Mutable list of planned slots to update.
    """
    for slot in slots:
        net = slot.estimated_net_consumption
        if net >= 0:
            slot.estimated_cost = round(net * slot.import_price, 4)
        else:
            slot.estimated_cost = round(net * slot.export_price, 4)


def mark_time_passed(slots: list[PlannedSlot], now: datetime) -> None:
    """Mark past slots as ``TimePassed``.

    Args:
        slots: Mutable list of planned slots to update.
        now: Timezone-aware current datetime.
    """
    for slot in slots:
        if slot.end.astimezone(now.tzinfo) < now:
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
        slot_start = slot.start.astimezone(now.tzinfo)
        slot_end = slot.end.astimezone(now.tzinfo)

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
) -> tuple[float, float]:
    """Return ``(usable_kwh, current_kwh)`` given rated capacity and SoC limits.

    ``usable_kwh`` is the energy available above the end-of-discharge reserve.
    ``current_kwh`` is the energy currently stored above the discharge floor.

    Args:
        rated_kwh: Nameplate capacity in kWh.
        soc_pct: Current state of charge as a percentage (0-100).
        end_of_discharge_soc_pct: Minimum allowed SoC as a percentage (0-100).

    Returns:
        ``(usable_kwh, current_kwh)`` tuple, both non-negative.
    """
    usable = rated_kwh * (1 - end_of_discharge_soc_pct / 100)
    current = rated_kwh * (soc_pct / 100) - rated_kwh * end_of_discharge_soc_pct / 100
    return max(usable, 0.0), max(current, 0.0)


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


def weighted_avg_consumption(
    value_1d: float,
    value_3d: float,
    value_7d: float,
    value_14d: float,
    w1: int,
    w3: int,
    w7: int,
    w14: int,
) -> float:
    """Apply spike-aware dynamic reweighting and return the weighted average.

    A direct port of the per-hour block inside
    ``HSEMWorkingModeSensor._async_calculate_avg_house_consumption``.

    Args:
        value_1d..value_14d: Raw consumption values for each window (kWh/hour).
        w1..w14: Configured integer weights (percent, should sum to 100).

    Returns:
        Weighted average house consumption in kWh/hour.
    """
    w_total_config = w1 + w3 + w7 + w14
    if w_total_config == 0:
        return 0.0

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

    # Spike severities
    ratio1 = (value_1d / value_7d_eff) if value_7d_eff > 0 else 1.0
    ratio3 = (value_3d / value_7d_eff) if value_7d_eff > 0 else 1.0
    ratio7 = (value_7d_eff / value_14d_eff) if value_14d_eff > 0 else 1.0
    ratio14 = (value_14d_eff / value_7d_eff) if value_7d_eff > 0 else 1.0

    sev1 = compute_spike_severity(ratio1, SPIKE1_RATIO_MIN, SPIKE1_RATIO_MAX)
    sev3 = compute_spike_severity(ratio3, SPIKE3_RATIO_MIN, SPIKE3_RATIO_MAX)
    sev7 = compute_spike_severity(ratio7, SPIKE7_RATIO_MIN, SPIKE7_RATIO_MAX)
    sev14 = compute_spike_severity(ratio14, SPIKE14_RATIO_MIN, SPIKE14_RATIO_MAX)

    # Dynamic reweighting
    freed1 = w1 * (SPIKE1_REDUCE_FRACTION_MAX * sev1)
    w1_eff = w1 - freed1
    w3_eff = w3 + freed1 * SPIKE1_REDIST_TO_3D
    w7_eff = w7 + freed1 * SPIKE1_REDIST_TO_7D
    w14_eff = w14 + freed1 * SPIKE1_REDIST_TO_14D

    freed3 = w3_eff * (SPIKE3_REDUCE_FRACTION_MAX * sev3)
    w3_eff -= freed3
    w7_eff += freed3 * SPIKE3_REDIST_TO_7D
    w14_eff += freed3 * SPIKE3_REDIST_TO_14D

    freed7 = w7_eff * (SPIKE7_REDUCE_FRACTION_MAX * sev7)
    w7_eff -= freed7
    w14_eff += freed7 * SPIKE7_REDIST_TO_14D

    freed14 = w14_eff * (SPIKE14_REDUCE_FRACTION_MAX * sev14)
    w14_eff -= freed14
    w7_eff += freed14 * SPIKE14_REDIST_TO_7D

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
        w1_eff, w3_eff, w7_eff, w14_eff = float(w1), float(w3), float(w7), float(w14)

    return round(
        value_1d_eff * (w1_eff / 100)
        + value_3d_eff * (w3_eff / 100)
        + value_7d_eff * (w7_eff / 100)
        + value_14d_eff * (w14_eff / 100),
        3,
    )
