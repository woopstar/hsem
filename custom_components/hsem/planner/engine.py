"""Pure-Python HSEM planner engine.

This module re-implements the core scheduling logic from
:class:`~custom_components.hsem.custom_sensors.working_mode_sensor.HSEMWorkingModeSensor`
as a stateless function that accepts a
:class:`~custom_components.hsem.models.planner_inputs.PlannerInput` and returns a
:class:`~custom_components.hsem.models.planner_outputs.PlannerOutput`.

**No Home Assistant types are imported here.**  This makes the engine
directly testable with plain ``pytest`` without a running HA instance.

Design notes
------------
- All business logic is ported from the sensor class as closely as
  possible, so that tests against this engine are also valid regression
  tests for the sensor.
- The engine is intentionally *synchronous*.  The sensor's async wrappers
  exist only because HA's event loop requires them; the underlying
  calculations are CPU-bound and need no I/O.
- ``warnings`` emitted during planning are collected and returned in
  :attr:`PlannerOutput.warnings` so tests can assert on diagnostic
  messages without capturing log output.
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
    BatteryScheduleInput,
    HourlyConsumptionAverage,
    PlannerInput,
    PricePoint,
    SolcastSlot,
)
from custom_components.hsem.models.planner_outputs import (
    ChargeWindow,
    DischargeWindow,
    PlannedSlot,
    PlannerOutput,
)
from custom_components.hsem.utils.misc import (
    calculate_recommended_threshold,
    interval_ends_before_window_start,
    next_window_start_dt,
)
from custom_components.hsem.utils.recommendations import Recommendations

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_CHARGE_RECOMMENDATIONS = frozenset(
    {
        Recommendations.BatteriesChargeGrid.value,
        Recommendations.BatteriesChargeSolar.value,
    }
)
_DISCHARGE_RECOMMENDATIONS = frozenset(
    {
        Recommendations.BatteriesDischargeMode.value,
        Recommendations.ForceBatteriesDischarge.value,
    }
)


def _parse_now(now_iso: str) -> datetime:
    """Parse a timezone-aware ISO-8601 string into a ``datetime``.

    Raises:
        ValueError: If the string cannot be parsed or is not timezone-aware.
    """
    dt = datetime.fromisoformat(now_iso)
    if dt.tzinfo is None:
        raise ValueError(f"now_iso must be timezone-aware, got: {now_iso!r}")
    return dt


def _build_slots(inp: PlannerInput, now: datetime) -> list[PlannedSlot]:
    """Generate a chronologically ordered list of empty :class:`PlannedSlot` objects.

    Mirrors ``HSEMWorkingModeSensor._generate_recommendation_intervals``:
    slots start at midnight of *now*'s local calendar day and cover
    ``interval_length_hours`` hours at ``interval_minutes`` resolution.
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


def _index_by_hour(items: list, hour_attr: str = "hour") -> dict[int, Any]:
    """Build a dict keyed by the integer *hour* attribute of each item."""
    return {getattr(item, hour_attr): item for item in items}


def _populate_prices(slots: list[PlannedSlot], price_points: list[PricePoint]) -> None:
    """Write import/export prices into each slot from ``price_points``.

    Price lookup is by the slot's ``start.hour``.  Missing hours default to 0.
    """
    price_by_hour = _index_by_hour(price_points)
    for slot in slots:
        pt = price_by_hour.get(slot.start.hour)
        if pt is not None:
            slot.import_price = pt.import_price
            slot.export_price = pt.export_price


def _populate_solcast(
    slots: list[PlannedSlot], solcast_slots: list[SolcastSlot], interval_minutes: int
) -> None:
    """Write PV estimates into each slot, scaled to the slot duration.

    Solcast data is provided per *hour*; if the slot duration is shorter
    (e.g. 15 min) the estimate is divided proportionally.
    """
    solcast_by_hour = _index_by_hour(solcast_slots)
    scale = 60.0 / interval_minutes  # e.g. 4 for 15-min slots

    for slot in slots:
        sc = solcast_by_hour.get(slot.start.hour)
        slot.solcast_pv_estimate = round(sc.pv_estimate / scale, 3) if sc else 0.0


def _compute_spike_severity(ratio: float, ratio_min: float, ratio_max: float) -> float:
    """Return a severity value in [0, 1] for spike detection."""
    if ratio <= ratio_min:
        return 0.0
    if ratio >= ratio_max:
        return 1.0
    return (ratio - ratio_min) / (ratio_max - ratio_min)


def _weighted_avg_consumption(
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

    This is a direct port of the per-hour block inside
    ``HSEMWorkingModeSensor._async_calculate_avg_house_consumption``.

    Returns:
        Weighted average house consumption (in the same unit as the inputs,
        typically kWh/hour).
    """
    w_total_config = w1 + w3 + w7 + w14
    if w_total_config == 0:
        return 0.0

    # --- Mild capping between 7d and 14d ---
    value_7d_eff = max(CAP7_DOWN * value_14d, min(value_7d, CAP7_UP * value_14d))
    value_14d_eff = max(
        CAP14_DOWN * value_7d_eff, min(value_14d, CAP14_UP * value_7d_eff)
    )

    # --- Baseline and capping for 1d/3d ---
    baseline = BASELINE_7D_SHARE * value_7d_eff + BASELINE_14D_SHARE * value_14d_eff

    value_1d_eff = max(
        baseline * CHANGE_LIMIT_DOWN_FACTOR,
        min(value_1d, baseline * CHANGE_LIMIT_UP_FACTOR),
    )
    value_3d_eff = max(
        baseline * CHANGE3_LIMIT_DOWN_FACTOR,
        min(value_3d, baseline * CHANGE3_LIMIT_UP_FACTOR),
    )

    # --- Spike severities ---
    ratio1 = (value_1d / value_7d_eff) if value_7d_eff > 0 else 1.0
    ratio3 = (value_3d / value_7d_eff) if value_7d_eff > 0 else 1.0
    ratio7 = (value_7d_eff / value_14d_eff) if value_14d_eff > 0 else 1.0
    ratio14 = (value_14d_eff / value_7d_eff) if value_7d_eff > 0 else 1.0

    sev1 = _compute_spike_severity(ratio1, SPIKE1_RATIO_MIN, SPIKE1_RATIO_MAX)
    sev3 = _compute_spike_severity(ratio3, SPIKE3_RATIO_MIN, SPIKE3_RATIO_MAX)
    sev7 = _compute_spike_severity(ratio7, SPIKE7_RATIO_MIN, SPIKE7_RATIO_MAX)
    sev14 = _compute_spike_severity(ratio14, SPIKE14_RATIO_MIN, SPIKE14_RATIO_MAX)

    # --- Dynamic reweighting ---
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

    # --- Reliability scaling ---
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


def _populate_consumption(
    slots: list[PlannedSlot],
    averages: list[HourlyConsumptionAverage],
    w1: int,
    w3: int,
    w7: int,
    w14: int,
    interval_minutes: int,
) -> None:
    """Compute and write spike-aware weighted consumption into each slot."""
    avg_by_hour = _index_by_hour(averages)
    scale = 60.0 / interval_minutes  # slots per hour

    for slot in slots:
        h = slot.start.hour
        ca: HourlyConsumptionAverage | None = avg_by_hour.get(h)
        if ca is None:
            continue

        hourly_avg = _weighted_avg_consumption(
            ca.avg_1d, ca.avg_3d, ca.avg_7d, ca.avg_14d, w1, w3, w7, w14
        )
        slot_avg = round(hourly_avg / scale, 3)
        slot.avg_house_consumption = slot_avg
        slot.avg_house_consumption_1d = round(ca.avg_1d / scale, 3)
        slot.avg_house_consumption_3d = round(ca.avg_3d / scale, 3)
        slot.avg_house_consumption_7d = round(ca.avg_7d / scale, 3)
        slot.avg_house_consumption_14d = round(ca.avg_14d / scale, 3)


def _populate_net_consumption(slots: list[PlannedSlot]) -> None:
    """Compute ``estimated_net_consumption = avg_consumption - pv_estimate``."""
    for slot in slots:
        slot.estimated_net_consumption = round(
            slot.avg_house_consumption - slot.solcast_pv_estimate, 3
        )


def _populate_estimated_cost(slots: list[PlannedSlot]) -> None:
    """Compute estimated grid cost per slot."""
    for slot in slots:
        net = slot.estimated_net_consumption
        if net >= 0:
            slot.estimated_cost = round(net * slot.import_price, 4)
        else:
            slot.estimated_cost = round(net * slot.export_price, 4)


def _mark_time_passed(slots: list[PlannedSlot], now: datetime) -> None:
    """Mark past slots as ``TimePassed``."""
    for slot in slots:
        if slot.end.astimezone(now.tzinfo) < now:
            slot.recommendation = Recommendations.TimePassed.value


def _usable_capacity(
    rated_kwh: float,
    soc_pct: float,
    end_of_discharge_soc_pct: float,
) -> float:
    """Return current usable energy (kWh) given rated capacity and SoC limits.

    ``usable`` is the energy available above the end-of-discharge reserve.
    ``current`` is the energy currently stored above the end-of-discharge floor.
    """
    usable = rated_kwh * (1 - end_of_discharge_soc_pct / 100)
    current = rated_kwh * (soc_pct / 100) - (rated_kwh * end_of_discharge_soc_pct / 100)
    return max(usable, 0.0), max(current, 0.0)


def _populate_battery_capacity(
    slots: list[PlannedSlot],
    now: datetime,
    current_capacity: float,
    usable_capacity: float,
) -> None:
    """Forward-simulate battery SoC through all slots.

    Mirrors ``_async_calculate_estimated_batteries_capacity``.
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
            # Past slot – keep zero
            cap = 0.0

        cap = min(cap, usable_capacity)
        slot.estimated_battery_capacity = round(cap, 3)
        previous_capacity = cap

    for slot in slots:
        if slot.estimated_battery_capacity > 0 and usable_capacity > 0:
            slot.estimated_battery_soc = round(
                slot.estimated_battery_capacity / usable_capacity * 100, 2
            )


def _apply_discharge_schedules(
    slots: list[PlannedSlot],
    battery_schedules: list[BatteryScheduleInput],
    now: datetime,
) -> None:
    """Mark slots inside each enabled discharge window.

    Mirrors ``_async_calculate_batteries_schedules``.
    """
    for sched in battery_schedules:
        if not sched.enabled:
            continue

        window_start_abs = next_window_start_dt(now, sched.start)
        if sched.end > sched.start:
            window_end_abs = datetime.combine(
                window_start_abs.date(), sched.end
            ).replace(tzinfo=now.tzinfo)
        else:
            window_end_abs = datetime.combine(
                (window_start_abs + timedelta(days=1)).date(), sched.end
            ).replace(tzinfo=now.tzinfo)

        for slot in slots:
            slot_start = slot.start.astimezone(now.tzinfo)
            slot_end = slot.end.astimezone(now.tzinfo)

            if slot_end <= now:
                continue
            if slot_start >= window_start_abs and slot_end <= window_end_abs:
                slot.recommendation = Recommendations.BatteriesDischargeMode.value

        # Back-fill needed_batteries_capacity on the schedule object so that
        # the charge planner can use it.
        total_net = sum(
            s.estimated_net_consumption
            for s in slots
            if s.recommendation == Recommendations.BatteriesDischargeMode.value
            and s.start.astimezone(now.tzinfo) >= window_start_abs
            and s.end.astimezone(now.tzinfo) <= window_end_abs
        )
        # Store on the input object as a side-effect so charge planner can read it
        sched._needed_capacity = max(total_net, 0.0)  # type: ignore[attr-defined]
        sched._avg_import_price = _avg_price_in_window(  # type: ignore[attr-defined]
            slots, window_start_abs, window_end_abs, now
        )


def _avg_price_in_window(
    slots: list[PlannedSlot],
    window_start: datetime,
    window_end: datetime,
    now: datetime,
) -> float:
    prices = [
        s.import_price
        for s in slots
        if s.start.astimezone(now.tzinfo) >= window_start
        and s.end.astimezone(now.tzinfo) <= window_end
        and s.end.astimezone(now.tzinfo) > now
    ]
    return round(sum(prices) / len(prices), 3) if prices else 0.0


def _apply_charge_schedules(
    slots: list[PlannedSlot],
    battery_schedules: list[BatteryScheduleInput],
    now: datetime,
    max_charge_per_interval: float,
) -> None:
    """Assign charge recommendations to slots before each discharge window.

    Mirrors ``_async_find_best_time_to_charge_battery_schedule`` with its
    three-priority ordering:
    1. Negative import price (free/paid-to-charge)
    2. Solar surplus (``estimated_net_consumption < -0.2``)
    3. Cheapest remaining grid hours (guarded by min price difference)
    """
    if max_charge_per_interval <= 0:
        return

    for sched in battery_schedules:
        if not sched.enabled:
            continue

        needed: float = getattr(sched, "_needed_capacity", 0.0)
        avg_discharge_price: float = getattr(sched, "_avg_import_price", 0.0)

        if needed <= 0:
            continue

        # Slots that end before the charge window starts and are not yet assigned
        eligible = [
            s
            for s in slots
            if s.end.astimezone(now.tzinfo) > now
            and interval_ends_before_window_start(
                s.end.astimezone(now.tzinfo), sched.start, now
            )
            and s.recommendation is None
        ]

        charged = 0.0

        # Priority 1: negative import price
        for s in sorted(
            (e for e in eligible if e.import_price < 0.0),
            key=lambda x: (x.import_price, x.start),
        ):
            if charged >= needed:
                break
            energy = min(max_charge_per_interval, needed - charged)
            if energy > 0:
                s.recommendation = Recommendations.BatteriesChargeGrid.value
                s.batteries_charged = round(energy, 3)
                charged += energy

        # Priority 2: solar surplus
        if charged < needed:
            for s in sorted(
                (
                    e
                    for e in eligible
                    if e.estimated_net_consumption < -0.2 and e.recommendation is None
                ),
                key=lambda x: (x.estimated_net_consumption, x.start),
            ):
                if charged >= needed:
                    break
                available_solar = abs(s.estimated_net_consumption)
                energy = min(max_charge_per_interval, needed - charged, available_solar)
                if energy > 0:
                    s.recommendation = Recommendations.BatteriesChargeSolar.value
                    s.batteries_charged = round(energy, 3)
                    charged += energy

        # Priority 3: cheapest grid hours (min price difference guard)
        if charged < needed:
            grid_candidates = sorted(
                (e for e in eligible if e.recommendation is None),
                key=lambda x: (x.import_price, x.start),
            )
            # First pass: calculate average charge price
            tentative_charged = 0.0
            tentative_count = 0
            tentative_price_sum = 0.0
            for s in grid_candidates:
                if tentative_charged >= needed:
                    break
                available_solar = (
                    abs(s.estimated_net_consumption)
                    if s.estimated_net_consumption < 0
                    else 0
                )
                grid_needed = min(
                    max_charge_per_interval - available_solar,
                    needed - tentative_charged - available_solar,
                )
                energy = available_solar + grid_needed
                if energy > 0:
                    tentative_count += 1
                    tentative_price_sum += s.import_price
                    tentative_charged += energy

            avg_charge_price = (
                tentative_price_sum / tentative_count if tentative_count > 0 else 0.0
            )
            price_diff = avg_discharge_price - avg_charge_price

            min_diff = sched.min_price_difference
            if min_diff == 0 or price_diff >= min_diff:
                for s in grid_candidates:
                    if charged >= needed:
                        break
                    available_solar = (
                        abs(s.estimated_net_consumption)
                        if s.estimated_net_consumption < 0
                        else 0
                    )
                    grid_needed = min(
                        max_charge_per_interval - available_solar,
                        needed - charged - available_solar,
                    )
                    energy = available_solar + grid_needed
                    if energy > 0:
                        s.recommendation = Recommendations.BatteriesChargeGrid.value
                        s.batteries_charged = round(energy, 3)
                        charged += energy


def _calculate_required_battery_until_solar(
    slots: list[PlannedSlot],
    now: datetime,
    usable_capacity: float,
    discharge_buffer_pct: float,
) -> float:
    """Estimate battery capacity needed until the first solar surplus slot."""
    required = 0.0
    for slot in slots:
        if slot.start.astimezone(now.tzinfo) < now:
            continue
        if slot.estimated_net_consumption < 0:
            break
        if slot.estimated_net_consumption > 0:
            required += slot.estimated_net_consumption

    buffer_kwh = usable_capacity * (discharge_buffer_pct / 100)
    return round(required + buffer_kwh, 3)


def _apply_excess_export(
    slots: list[PlannedSlot],
    now: datetime,
    current_capacity: float,
    usable_capacity: float,
    required_capacity: float,
    export_price_threshold: float,
    warnings: list[str],
) -> None:
    """Mark high-export-price future slots for forced battery discharge.

    Mirrors ``_async_apply_excess_battery_export``.
    """
    excess = current_capacity - required_capacity
    if excess <= 0:
        return

    battery_is_solar_charged = any(
        s.recommendation == Recommendations.BatteriesChargeSolar.value for s in slots
    )

    candidates = sorted(
        (
            s
            for s in slots
            if s.start.astimezone(now.tzinfo) >= now
            and s.recommendation is None
            and (
                battery_is_solar_charged
                or (s.export_price - s.import_price >= export_price_threshold)
            )
            and s.export_price > 0
        ),
        key=lambda x: x.export_price,
        reverse=True,
    )

    for s in candidates:
        if excess <= 0:
            break
        s.recommendation = Recommendations.ForceBatteriesDischarge.value
        warnings.append(
            f"ForceBatteriesDischarge at {s.start.isoformat()}: export={s.export_price}"
        )
        excess -= s.estimated_net_consumption


def _apply_optimization_strategy(
    slots: list[PlannedSlot],
    now: datetime,
    current_capacity: float,
    usable_capacity: float,
    required_capacity: float,
    months_winter: list[int],
    warnings: list[str],
) -> None:
    """Apply seasonal optimization logic to remaining unassigned slots.

    Mirrors ``_async_optimization_strategy``.
    """
    current_month = now.month
    months_summer = [m for m in range(1, 13) if m not in months_winter]

    # ForceExport when export price > import price
    for rec in slots:
        if rec.export_price > rec.import_price and rec.recommendation is None:
            rec.recommendation = Recommendations.ForceExport.value

    # Solar charging pass
    batteries_needed_charge = usable_capacity - current_capacity
    if batteries_needed_charge < 0:
        batteries_needed_charge = 0.0

    charged = 0.0
    sorted_by_export = sorted(
        (
            s
            for s in slots
            if s.recommendation is None
            and s.start.astimezone(now.tzinfo).date() == now.date()
        ),
        key=lambda x: x.export_price,
    )

    for rec in sorted_by_export:
        if charged >= batteries_needed_charge:
            break
        if rec.estimated_net_consumption <= -0.1:
            charged += abs(rec.estimated_net_consumption)
            rec.recommendation = Recommendations.BatteriesChargeSolar.value
            rec.batteries_charged = round(charged, 3)

    # Seasonal fill for remaining unassigned slots
    for rec in slots:
        if rec.recommendation is not None:
            continue

        has_future_forced_export = any(
            r.recommendation == Recommendations.ForceBatteriesDischarge.value
            and r.start > rec.start
            for r in slots
        )
        if has_future_forced_export and current_capacity > required_capacity:
            rec.recommendation = Recommendations.BatteriesWaitMode.value
            continue

        if current_month in months_winter:
            rec.recommendation = Recommendations.BatteriesWaitMode.value
        elif current_month in months_summer:
            if rec.estimated_net_consumption <= 0.1:
                rec.recommendation = Recommendations.BatteriesChargeSolar.value
            else:
                rec.recommendation = Recommendations.BatteriesDischargeMode.value


def _derive_windows(
    slots: list[PlannedSlot],
) -> tuple[list[ChargeWindow], list[DischargeWindow]]:
    """Derive contiguous charge and discharge windows from the slot list."""
    charge_windows: list[ChargeWindow] = []
    discharge_windows: list[DischargeWindow] = []

    def _flush_charge(group: list[PlannedSlot]) -> None:
        if not group:
            return
        total_e = round(sum(s.batteries_charged for s in group), 3)
        prices = [s.import_price for s in group]
        avg_p = round(sum(prices) / len(prices), 4)
        charge_windows.append(
            ChargeWindow(
                start=group[0].start,
                end=group[-1].end,
                total_energy_kwh=total_e,
                avg_import_price=avg_p,
                recommendation=group[0].recommendation or "",
            )
        )

    def _flush_discharge(group: list[PlannedSlot]) -> None:
        if not group:
            return
        prices = [s.import_price for s in group]
        avg_p = round(sum(prices) / len(prices), 4)
        discharge_windows.append(
            DischargeWindow(
                start=group[0].start,
                end=group[-1].end,
                avg_import_price=avg_p,
                recommendation=group[0].recommendation or "",
            )
        )

    current_charge_group: list[PlannedSlot] = []
    current_discharge_group: list[PlannedSlot] = []

    for slot in slots:
        rec = slot.recommendation or ""

        if rec in _CHARGE_RECOMMENDATIONS:
            if current_discharge_group:
                _flush_discharge(current_discharge_group)
                current_discharge_group = []
            current_charge_group.append(slot)
        elif rec in _DISCHARGE_RECOMMENDATIONS:
            if current_charge_group:
                _flush_charge(current_charge_group)
                current_charge_group = []
            current_discharge_group.append(slot)
        else:
            if current_charge_group:
                _flush_charge(current_charge_group)
                current_charge_group = []
            if current_discharge_group:
                _flush_discharge(current_discharge_group)
                current_discharge_group = []

    if current_charge_group:
        _flush_charge(current_charge_group)
    if current_discharge_group:
        _flush_discharge(current_discharge_group)

    return charge_windows, discharge_windows


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_planner(inp: PlannerInput) -> PlannerOutput:
    """Execute the HSEM planner and return a :class:`PlannerOutput`.

    This function is the pure-Python equivalent of
    ``HSEMWorkingModeSensor._async_run_update_cycle`` stripped of all
    Home Assistant I/O.  It is safe to call from any synchronous context.

    Args:
        inp: Fully populated :class:`PlannerInput`.

    Returns:
        A :class:`PlannerOutput` containing per-slot decisions, charge /
        discharge windows, and diagnostic information.

    Raises:
        ValueError: If ``inp.now_iso`` is not a timezone-aware ISO-8601
            string or if ``interval_minutes`` is ≤ 0.
    """
    warnings: list[str] = []
    missing_inputs: list[str] = []

    # ------------------------------------------------------------------ parse now
    now = _parse_now(inp.now_iso)

    # ------------------------------------------------------------------ battery state
    usable_capacity, current_capacity = _usable_capacity(
        inp.battery_rated_capacity_kwh,
        inp.battery_soc_pct,
        inp.battery_end_of_discharge_soc_pct,
    )

    if inp.battery_rated_capacity_kwh <= 0:
        warnings.append(
            "battery_rated_capacity_kwh is zero or negative; battery simulation disabled."
        )
        usable_capacity = 0.0
        current_capacity = 0.0

    # Validate weights
    weight_sum = inp.weight_1d + inp.weight_3d + inp.weight_7d + inp.weight_14d
    if weight_sum != 100:
        warnings.append(
            f"Consumption weights sum to {weight_sum}, not 100. "
            "Results may not be meaningful."
        )

    # ------------------------------------------------------------------ slots
    slots = _build_slots(inp, now)

    if not slots:
        warnings.append(
            "No slots generated; check interval_minutes and interval_length_hours."
        )
        return PlannerOutput(
            missing_inputs=missing_inputs,
            warnings=warnings,
        )

    # ------------------------------------------------------------------ populate time-series
    _populate_prices(slots, inp.price_points)
    _populate_solcast(slots, inp.solcast_slots, inp.interval_minutes)
    _populate_consumption(
        slots,
        inp.consumption_averages,
        inp.weight_1d,
        inp.weight_3d,
        inp.weight_7d,
        inp.weight_14d,
        inp.interval_minutes,
    )
    _populate_net_consumption(slots)
    _populate_estimated_cost(slots)

    # ------------------------------------------------------------------ recommended threshold
    recommended_threshold = calculate_recommended_threshold(
        inp.battery_purchase_price,
        inp.battery_expected_cycles,
        usable_capacity,
        inp.battery_conversion_loss_pct,
    )
    if recommended_threshold > 0:
        warnings.append(
            f"Recommended price threshold: {recommended_threshold:.4f} "
            f"(depreciation + conversion loss)."
        )

    # ------------------------------------------------------------------ time-passed
    _mark_time_passed(slots, now)

    # ------------------------------------------------------------------ discharge schedules
    _apply_discharge_schedules(slots, inp.battery_schedules, now)

    # ------------------------------------------------------------------ max charge per interval
    conversion_loss_factor = 1 - (inp.battery_conversion_loss_pct / 100)
    max_charge_per_hour = (
        inp.battery_max_charge_power_w / 1000
    ) * conversion_loss_factor
    max_charge_per_interval = max_charge_per_hour / (60 / inp.interval_minutes)

    # ------------------------------------------------------------------ charge schedules
    _apply_charge_schedules(slots, inp.battery_schedules, now, max_charge_per_interval)

    # ------------------------------------------------------------------ battery capacity forward-sim
    _populate_battery_capacity(slots, now, current_capacity, usable_capacity)

    # ------------------------------------------------------------------ required capacity for excess export
    required_capacity = _calculate_required_battery_until_solar(
        slots, now, usable_capacity, inp.excess_export_discharge_buffer_pct
    )

    # ------------------------------------------------------------------ excess export
    if inp.excess_export_enabled:
        _apply_excess_export(
            slots,
            now,
            current_capacity,
            usable_capacity,
            required_capacity,
            inp.excess_export_price_threshold,
            warnings,
        )

    # ------------------------------------------------------------------ optimization strategy
    _apply_optimization_strategy(
        slots,
        now,
        current_capacity,
        usable_capacity,
        required_capacity,
        inp.months_winter,
        warnings,
    )

    # ------------------------------------------------------------------ re-run battery sim with charges
    _populate_battery_capacity(slots, now, current_capacity, usable_capacity)

    # ------------------------------------------------------------------ current recommendation
    current_recommendation: str | None = None
    for slot in slots:
        slot_start = slot.start.astimezone(now.tzinfo)
        slot_end = slot.end.astimezone(now.tzinfo)
        if slot_start <= now < slot_end:
            current_recommendation = slot.recommendation
            break

    # ------------------------------------------------------------------ final SoC
    future_slots = [s for s in slots if s.end.astimezone(now.tzinfo) > now]
    battery_soc_at_end = future_slots[-1].estimated_battery_soc if future_slots else 0.0

    # ------------------------------------------------------------------ windows
    charge_windows, discharge_windows = _derive_windows(slots)

    return PlannerOutput(
        slots=slots,
        charge_windows=charge_windows,
        discharge_windows=discharge_windows,
        current_recommendation=current_recommendation,
        battery_soc_at_end=battery_soc_at_end,
        required_capacity_kwh=required_capacity,
        missing_inputs=missing_inputs,
        warnings=warnings,
    )
