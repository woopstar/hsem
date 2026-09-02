"""Battery charge scheduling for the HSEM planner — pre-charge pass.

Responsible for the schedule-based pre-charge pass
(``apply_charge_schedules`` and ``_apply_grid_charge``).

All functions are pure — no I/O, no Home Assistant imports.  They mutate the
:class:`PlannedSlot` list passed in and return nothing (or a scalar result).
"""

from __future__ import annotations

from datetime import datetime

from custom_components.hsem.const import SOLAR_SURPLUS_CHARGE_THRESHOLD_KWH
from custom_components.hsem.models.battery_schedule_input import BatteryScheduleInput
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.utils.datetime_utils import as_tz
from custom_components.hsem.utils.logger import log_planner
from custom_components.hsem.utils.recommendations import Recommendations
from custom_components.hsem.utils.time_windows import next_window_start_dt

# ---------------------------------------------------------------------------
# Charge scheduling
# ---------------------------------------------------------------------------


def apply_charge_schedules(
    slots: list[PlannedSlot],
    battery_schedules: list[BatteryScheduleInput],
    now: datetime,
    max_charge_per_interval: float,
    *,
    current_kwh: float = 0.0,
    usable_kwh: float = 0.0,
    cycle_cost_per_kwh: float = 0.0,
    recommended_threshold: float = 0.0,
) -> None:
    """Assign charge recommendations to slots before each discharge window.

    Three-priority ordering:

    1. Negative import price (free/paid-to-charge)
    2. Solar surplus (``estimated_net_consumption < SOLAR_SURPLUS_CHARGE_THRESHOLD_KWH``)
    3. Cheapest remaining grid hours (guarded by depreciation threshold + cycle cost)

    Each discharge-window occurrence (calendar day) receives its own
    independent charge budget capped at ``min(needed, usable_kwh)``.
    This correctly accounts for the fact that the battery is discharged
    during the previous window, making room for new charging.  The battery
    never holds more than ``usable_kwh`` at any one time, but it can be
    charged again after being discharged.

    Args:
        slots: Mutable list of planned slots.
        battery_schedules: Schedule configurations.
        now: Timezone-aware current datetime.
        max_charge_per_interval: Maximum energy (kWh) chargeable per slot.
        current_kwh: Current battery energy above the discharge floor (kWh).
            Used to cap total charge across all occurrences.
        usable_kwh: Maximum usable battery capacity (kWh).  Used together
            with *current_kwh* to derive remaining capacity.
        cycle_cost_per_kwh: Additional per-kWh cycle wear cost.
        recommended_threshold: Depreciation-derived price floor passed to
            ``_apply_grid_charge`` to guard profitability.
    """
    if max_charge_per_interval <= 0:
        log_planner(
            "debug",
            "[chg] apply_charge_schedules  skipped — max_charge_per_interval <= 0",
        )
        return

    # Each discharge-window occurrence (calendar day) gets its own
    # independent charge budget because the battery is discharged during
    # the previous window, making room for new charging.  The per-occurrence
    # budget is capped at usable_kwh (or the energy actually needed for that
    # window), not by a global pool shared across all days.
    occurrence_count = 0
    total_charged_all = 0.0
    today = as_tz(now, now.tzinfo).date()
    used_today_current_kwh = False

    log_planner(
        "debug",
        "[chg] apply_charge_schedules  schedules=%d  max_charge/slot=%.3f  "
        "current=%.3f  usable=%.3f",
        len(battery_schedules),
        max_charge_per_interval,
        current_kwh,
        usable_kwh,
    )

    for sched in battery_schedules:
        if not sched.enabled:
            continue

        # Iterate each occurrence of the discharge window independently.
        # Each occurrence needs its own pre-charge budget so day-2 discharge
        # windows get their own cheap-hours charge allocation.
        occurrences: list[tuple[datetime, datetime, float, float]] = getattr(
            sched, "_occurrences", []
        )
        if not occurrences:
            # apply_discharge_schedules (always called first, see engine_core.py)
            # unconditionally sets sched._occurrences for every enabled
            # schedule, but that list is legitimately empty when no future
            # occurrence of the window falls within the planning horizon.
            # This branch covers that case, not a caller skipping the prior
            # pass.
            needed_fb: float = getattr(sched, "_needed_capacity", 0.0)
            avg_price_fb: float = getattr(sched, "_avg_import_price", 0.0)
            if needed_fb > 0:
                occurrences = [
                    (
                        next_window_start_dt(now, sched.start),
                        next_window_start_dt(now, sched.start),
                        needed_fb,
                        avg_price_fb,
                    )
                ]

        for (
            window_start_abs,
            _window_end_abs,
            needed,
            avg_discharge_price,
        ) in occurrences:
            if needed <= 0:
                continue

            # Per-occurrence budget: cap at what's needed for this window
            # or at usable_kwh (the battery's physical capacity), whichever
            # is smaller.  Do NOT share a global pool across days — the
            # battery is discharged between windows, making room for new
            # charging.
            #
            # For windows on today's calendar date, account for the
            # battery's current charge (current_kwh) so we don't plan
            # unnecessary charging when the battery is already full.
            # Windows on future days get the full usable_kwh budget.
            window_date = as_tz(window_start_abs, now.tzinfo).date()
            if window_date == today and not used_today_current_kwh:
                occurrence_budget = (
                    min(needed, max(needed - current_kwh, 0.0), usable_kwh)
                    if usable_kwh > 0
                    else needed
                )
                used_today_current_kwh = True
            else:
                occurrence_budget = (
                    min(needed, usable_kwh) if usable_kwh > 0 else needed
                )

            # Eligible charge slots: future, unassigned, and ending before
            # this specific occurrence's window start. window_start_abs is
            # already a resolved absolute datetime for *this* occurrence
            # (day N of a recurring schedule), so this is a plain datetime
            # comparison — not a candidate for the now-removed
            # interval_ends_before_window_start(interval_end, window_start:
            # time, now) helper, which only resolves the *first* future
            # occurrence relative to `now` and would silently break
            # multi-occurrence (day 2+) budgeting if substituted here.
            eligible = [
                s
                for s in slots
                if as_tz(s.end, now.tzinfo) > now
                and as_tz(s.end, now.tzinfo) <= window_start_abs
                and s.recommendation is None
            ]

            occurrence_count += 1
            charged = 0.0

            # Priority 1: negative import price
            for s in sorted(
                (e for e in eligible if e.price.import_price < 0.0),
                key=lambda x: (x.price.import_price, x.start),
            ):
                if charged >= occurrence_budget:
                    break
                energy = min(max_charge_per_interval, occurrence_budget - charged)
                if energy > 0:
                    s.recommendation = Recommendations.BatteriesChargeGrid.value
                    s.batteries_charged_kwh = round(energy, 3)
                    charged += energy

            # Priority 2: solar surplus
            if charged < occurrence_budget:
                for s in sorted(
                    (
                        e
                        for e in eligible
                        if e.estimated_net_consumption_kwh
                        < SOLAR_SURPLUS_CHARGE_THRESHOLD_KWH
                        and e.recommendation is None
                    ),
                    # NOTE: SOLAR_SURPLUS_CHARGE_THRESHOLD_KWH is negative, so this
                    # selects slots where net consumption is sufficiently negative
                    # (i.e., there is a meaningful solar surplus to charge from).
                    key=lambda x: (x.estimated_net_consumption_kwh, x.start),
                ):
                    if charged >= occurrence_budget:
                        break
                    available_solar = abs(s.estimated_net_consumption_kwh)
                    energy = min(
                        max_charge_per_interval,
                        occurrence_budget - charged,
                        available_solar,
                    )
                    if energy > 0:
                        s.recommendation = Recommendations.BatteriesChargeSolar.value
                        s.batteries_charged_kwh = round(energy, 3)
                        charged += energy

            # Priority 3: cheapest grid hours (depreciation threshold + cycle cost guard)
            if charged < occurrence_budget:
                grid_charged = _apply_grid_charge(
                    eligible,
                    occurrence_budget,
                    charged,
                    max_charge_per_interval,
                    avg_discharge_price,
                    cycle_cost_per_kwh=cycle_cost_per_kwh,
                    recommended_threshold=recommended_threshold,
                )
                charged += grid_charged

            total_charged_all += charged
            log_planner(
                "debug",
                "[chg] apply_charge_schedules  occurrence=%d  budget=%.3f  "
                "needed=%.3f  charged=%.3f  window=%s",
                occurrence_count,
                occurrence_budget,
                needed,
                charged,
                window_start_abs.strftime("%Y-%m-%d %H:%M"),
            )

    log_planner(
        "debug",
        "[chg] apply_charge_schedules DONE  occurrences=%d  total_charged=%.3f",
        occurrence_count,
        total_charged_all,
    )


def _apply_grid_charge(
    eligible: list[PlannedSlot],
    needed: float,
    charged_so_far: float,
    max_charge_per_interval: float,
    avg_discharge_price: float,
    cycle_cost_per_kwh: float = 0.0,
    recommended_threshold: float = 0.0,
) -> float:
    """Apply cheapest-grid-hour charging with depreciation + cycle-cost guard.

    The combined profitability condition is:

        avg_discharge_price − avg_charge_price ≥ recommended_threshold + cycle_cost_per_kwh

    where ``recommended_threshold`` is the depreciation-derived price floor.

    Args:
        eligible: Pre-filtered candidate slots.
        needed: Total energy to charge in kWh.
        charged_so_far: Energy already charged by higher-priority sources.
        max_charge_per_interval: Maximum energy per slot in kWh.
        avg_discharge_price: Average import price during the discharge window.
        cycle_cost_per_kwh: Per-kWh battery wear cost added to the guard.
            Defaults to 0.0 (backwards compatible).
        recommended_threshold: Depreciation + loss derived price floor.

    Returns:
        The total energy (kWh) assigned to grid charging by this call.
    """
    grid_candidates = sorted(
        (e for e in eligible if e.recommendation is None),
        key=lambda x: (x.price.import_price, x.start),
    )

    # First pass: estimate average charge price
    tentative_charged = 0.0
    tentative_count = 0
    tentative_price_sum = 0.0
    for s in grid_candidates:
        if tentative_charged >= needed - charged_so_far:
            break
        available_solar = (
            abs(s.estimated_net_consumption_kwh)
            if s.estimated_net_consumption_kwh < 0
            else 0
        )
        grid_needed = min(
            max_charge_per_interval - available_solar,
            needed - charged_so_far - tentative_charged - available_solar,
        )
        energy = available_solar + grid_needed
        if energy > 0:
            tentative_count += 1
            tentative_price_sum += s.price.import_price
            tentative_charged += energy

    avg_charge_price = (
        tentative_price_sum / tentative_count if tentative_count > 0 else 0.0
    )
    price_diff = avg_discharge_price - avg_charge_price
    # Combined threshold: depreciation-derived price floor + per-kWh wear cost.
    # Both must be covered by the price spread for grid charging to be profitable.
    min_diff = recommended_threshold + cycle_cost_per_kwh

    if abs(min_diff) > 1e-9 and price_diff < min_diff:
        return 0.0  # Price spread does not cover loss + cycle wear cost

    # Second pass: actually assign recommendations
    charged = charged_so_far
    grid_assigned = 0.0
    for s in grid_candidates:
        if charged >= needed:
            break
        available_solar = (
            abs(s.estimated_net_consumption_kwh)
            if s.estimated_net_consumption_kwh < 0
            else 0
        )
        grid_needed = min(
            max_charge_per_interval - available_solar,
            needed - charged - available_solar,
        )
        energy = available_solar + grid_needed
        if energy > 0:
            s.recommendation = Recommendations.BatteriesChargeGrid.value
            s.batteries_charged_kwh = round(energy, 3)
            charged += energy
            grid_assigned += energy

    return grid_assigned
