"""Battery charge and discharge scheduling for the HSEM planner.

Single responsibility: decide *when* to charge and discharge the battery
based on discharge-window schedules, price signals, and seasonal strategy.

All functions are pure — no I/O, no Home Assistant imports.  They mutate the
:class:`PlannedSlot` list passed in and return nothing (or a scalar result).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from custom_components.hsem.const import (
    NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH,
    SOLAR_SURPLUS_CHARGE_THRESHOLD_KWH,
)
from custom_components.hsem.models.planner_inputs import BatteryScheduleInput
from custom_components.hsem.models.planner_outputs import PlannedSlot
from custom_components.hsem.utils.misc import (
    interval_ends_before_window_start,
    next_window_start_dt,
)
from custom_components.hsem.utils.recommendations import Recommendations

# ---------------------------------------------------------------------------
# Discharge schedule detection
# ---------------------------------------------------------------------------


def apply_discharge_schedules(
    slots: list[PlannedSlot],
    battery_schedules: list[BatteryScheduleInput],
    now: datetime,
) -> None:
    """Mark slots inside each enabled discharge window as ``BatteriesDischargeMode``.

    Also populates ``_needed_capacity`` and ``_avg_import_price`` as dynamic
    attributes on each :class:`BatteryScheduleInput` so the charge planner can
    read them without an extra pass.

    Args:
        slots: Mutable list of planned slots.
        battery_schedules: Schedule configurations to evaluate.
        now: Timezone-aware current datetime.
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
            # Cross-midnight discharge window
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

        total_net = sum(
            s.estimated_net_consumption
            for s in slots
            if s.recommendation == Recommendations.BatteriesDischargeMode.value
            and s.start.astimezone(now.tzinfo) >= window_start_abs
            and s.end.astimezone(now.tzinfo) <= window_end_abs
        )
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
    """Return the average import price for slots inside a discharge window."""
    prices = [
        s.import_price
        for s in slots
        if s.start.astimezone(now.tzinfo) >= window_start
        and s.end.astimezone(now.tzinfo) <= window_end
        and s.end.astimezone(now.tzinfo) > now
    ]
    return round(sum(prices) / len(prices), 3) if prices else 0.0


# ---------------------------------------------------------------------------
# Charge scheduling
# ---------------------------------------------------------------------------


def apply_charge_schedules(
    slots: list[PlannedSlot],
    battery_schedules: list[BatteryScheduleInput],
    now: datetime,
    max_charge_per_interval: float,
) -> None:
    """Assign charge recommendations to slots before each discharge window.

    Three-priority ordering (mirrors ``_async_find_best_time_to_charge_battery_schedule``):

    1. Negative import price (free/paid-to-charge)
    2. Solar surplus (``estimated_net_consumption < SOLAR_SURPLUS_CHARGE_THRESHOLD_KWH``)
    3. Cheapest remaining grid hours (guarded by min price difference)

    Args:
        slots: Mutable list of planned slots.
        battery_schedules: Schedule configurations (must have ``_needed_capacity``
            and ``_avg_import_price`` set by :func:`apply_discharge_schedules`).
        now: Timezone-aware current datetime.
        max_charge_per_interval: Maximum energy (kWh) chargeable per slot.
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
                    if e.estimated_net_consumption < SOLAR_SURPLUS_CHARGE_THRESHOLD_KWH
                    and e.recommendation is None
                ),
                # NOTE: SOLAR_SURPLUS_CHARGE_THRESHOLD_KWH is negative, so this
                # selects slots where net consumption is sufficiently negative
                # (i.e., there is a meaningful solar surplus to charge from).
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
            _apply_grid_charge(
                eligible,
                sched,
                needed,
                charged,
                max_charge_per_interval,
                avg_discharge_price,
            )


def _apply_grid_charge(
    eligible: list[PlannedSlot],
    sched: BatteryScheduleInput,
    needed: float,
    charged_so_far: float,
    max_charge_per_interval: float,
    avg_discharge_price: float,
) -> None:
    """Apply cheapest-grid-hour charging with min-price-difference guard.

    Args:
        eligible: Pre-filtered candidate slots.
        sched: The battery schedule being filled.
        needed: Total energy to charge in kWh.
        charged_so_far: Energy already charged by higher-priority sources.
        max_charge_per_interval: Maximum energy per slot in kWh.
        avg_discharge_price: Average import price during the discharge window.
    """
    grid_candidates = sorted(
        (e for e in eligible if e.recommendation is None),
        key=lambda x: (x.import_price, x.start),
    )

    # First pass: estimate average charge price
    tentative_charged = 0.0
    tentative_count = 0
    tentative_price_sum = 0.0
    for s in grid_candidates:
        if tentative_charged >= needed - charged_so_far:
            break
        available_solar = (
            abs(s.estimated_net_consumption) if s.estimated_net_consumption < 0 else 0
        )
        grid_needed = min(
            max_charge_per_interval - available_solar,
            needed - charged_so_far - tentative_charged - available_solar,
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

    if abs(min_diff) > 1e-9 and price_diff < min_diff:
        return  # Price difference does not justify charging

    # Second pass: actually assign recommendations
    charged = charged_so_far
    for s in grid_candidates:
        if charged >= needed:
            break
        available_solar = (
            abs(s.estimated_net_consumption) if s.estimated_net_consumption < 0 else 0
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


# ---------------------------------------------------------------------------
# Excess export
# ---------------------------------------------------------------------------


def calculate_required_battery_until_solar(
    slots: list[PlannedSlot],
    now: datetime,
    usable_capacity: float,
    discharge_buffer_pct: float,
) -> float:
    """Estimate battery capacity needed until the first solar surplus slot.

    Scans forward from *now* and accumulates positive net-consumption until
    a slot with negative net-consumption (solar surplus) is found.

    Args:
        slots: List of planned slots.
        now: Timezone-aware current datetime.
        usable_capacity: Maximum usable battery energy in kWh.
        discharge_buffer_pct: Safety buffer as a percentage of usable capacity.

    Returns:
        Required battery capacity in kWh (including safety buffer).
    """
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


def apply_excess_export(
    slots: list[PlannedSlot],
    now: datetime,
    current_capacity: float,
    required_capacity: float,
    export_price_threshold: float,
    warnings: list[str],
) -> None:
    """Mark high-export-price future slots for forced battery discharge.

    Only triggered when the battery holds more energy than needed until
    the next solar surplus.  Grid-charged batteries require a minimum price
    difference; solar-charged batteries export opportunistically.

    Args:
        slots: Mutable list of planned slots.
        now: Timezone-aware current datetime.
        current_capacity: Current available battery energy in kWh.
        required_capacity: Energy needed until next solar surplus (kWh).
        export_price_threshold: Minimum export-minus-import price delta for
            grid-charged batteries.
        warnings: Mutable list to append diagnostic messages to.
    """
    # battery_discharge_budget_kwh is the kWh the battery can export beyond what is
    # already needed to cover future house load.  Solar surplus in a slot does NOT
    # add to this budget: solar is a separate energy flow and is already accounted for
    # in estimated_net_consumption.  Only positive net consumption (house load > solar)
    # draws down the battery, so we drain the budget by max(net, 0) per slot.
    battery_discharge_budget_kwh = current_capacity - required_capacity
    if battery_discharge_budget_kwh <= 0:
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
        if battery_discharge_budget_kwh <= 0:
            break
        s.recommendation = Recommendations.ForceBatteriesDischarge.value
        warnings.append(
            f"ForceBatteriesDischarge at {s.start.isoformat()}: export={s.export_price}"
        )
        # Only positive net consumption (house load > solar) draws from the battery
        # discharge budget.  Solar-surplus slots (net < 0) contribute 0 drain because
        # the surplus is handled by solar, not by the battery.
        battery_discharge_budget_kwh -= max(s.estimated_net_consumption, 0.0)


# ---------------------------------------------------------------------------
# Seasonal optimization
# ---------------------------------------------------------------------------


def apply_optimization_strategy(
    slots: list[PlannedSlot],
    now: datetime,
    current_capacity: float,
    usable_capacity: float,
    required_capacity: float,
    months_winter: list[int],
    warnings: list[str],
) -> None:
    """Apply seasonal optimization logic to remaining unassigned slots.

    Decision priority per unassigned slot:

    1. Export price > import price → ``ForceExport``
    2. Solar surplus → ``BatteriesChargeSolar`` (until battery full)
    3. Future forced export pending and battery above required → ``BatteriesWaitMode``
    4. Winter month → ``BatteriesWaitMode``
    5. Summer month with solar → ``BatteriesChargeSolar``; else ``BatteriesDischargeMode``

    Args:
        slots: Mutable list of planned slots.
        now: Timezone-aware current datetime.
        current_capacity: Current available battery energy in kWh.
        usable_capacity: Maximum usable battery energy in kWh.
        required_capacity: Energy required until next solar surplus (kWh).
        months_winter: List of month integers (1-12) treated as winter.
        warnings: Mutable list for diagnostic messages (currently unused here).
    """
    current_month = now.month
    months_summer = [m for m in range(1, 13) if m not in months_winter]

    # ForceExport when export > import
    for rec in slots:
        if rec.export_price > rec.import_price and rec.recommendation is None:
            rec.recommendation = Recommendations.ForceExport.value

    # Solar charging until battery full
    batteries_needed_charge = max(usable_capacity - current_capacity, 0.0)
    charged = 0.0

    for rec in sorted(
        (
            s
            for s in slots
            if s.recommendation is None
            and s.start.astimezone(now.tzinfo).date() == now.date()
        ),
        key=lambda x: x.export_price,
    ):
        if charged >= batteries_needed_charge:
            break
        # v5.1.0 threshold: <= NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH
        # (charge even near-zero-consumption slots)
        if rec.estimated_net_consumption <= NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH:
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
            if rec.estimated_net_consumption <= NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH:
                rec.recommendation = Recommendations.BatteriesChargeSolar.value
            else:
                rec.recommendation = Recommendations.BatteriesDischargeMode.value
