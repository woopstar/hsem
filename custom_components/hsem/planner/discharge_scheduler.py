"""Discharge scheduling for the HSEM planner.

Single responsibility: decide *when* to discharge the battery
based on discharge-window schedules, price signals, and seasonal strategy.

All functions are pure — no I/O, no Home Assistant imports.  They mutate the
:class:`PlannedSlot` list passed in and return nothing (or a scalar result).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from custom_components.hsem.const import NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH
from custom_components.hsem.models.planner_inputs import BatteryScheduleInput
from custom_components.hsem.models.planner_outputs import PlannedSlot
from custom_components.hsem.utils.datetime_utils import as_tz
from custom_components.hsem.utils.logger import log_planner
from custom_components.hsem.utils.misc import clamp_efficiency, next_window_start_dt
from custom_components.hsem.utils.recommendations import (
    DISCHARGE_RECS as _DISCHARGE_RECS,
)
from custom_components.hsem.utils.recommendations import Recommendations

_LOGGER = logging.getLogger(__name__)

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
    log_planner(
        "debug",
        "[disch] apply_discharge_schedules  schedules=%d  now=%s",
        len(battery_schedules),
        now.isoformat(),
    )
    for sched in battery_schedules:
        if not sched.enabled:
            continue

        # Determine the last slot end in the planning horizon so we know how
        # many days to cover.  We apply the discharge window once per calendar
        # day that falls within [now, horizon_end].
        future_slots = [s for s in slots if as_tz(s.end, now.tzinfo) > now]
        if not future_slots:
            continue
        horizon_end = as_tz(future_slots[-1].end, now.tzinfo)

        # Collect all occurrences of this schedule window within the horizon.
        # Start from the first upcoming occurrence and advance one day at a time.
        # Each occurrence is stored so apply_charge_schedules can schedule
        # pre-charge independently per window occurrence.
        window_start_abs = next_window_start_dt(now, sched.start)
        occurrences: list[tuple[datetime, datetime, float, float]] = []
        sched_total_net = 0.0

        while window_start_abs < horizon_end:
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
                slot_start = as_tz(slot.start, now.tzinfo)
                slot_end = as_tz(slot.end, now.tzinfo)
                if slot_end <= now:
                    continue
                if slot_start >= window_start_abs and slot_end <= window_end_abs:
                    slot.recommendation = Recommendations.BatteriesDischargeMode.value

            # Capture per-occurrence capacity and avg price.
            #
            # Battery-relevant net consumption excludes EV planned load:
            #   battery_net = avg_house_consumption - pv
            #
            # When base_load_includes_ev=False, estimated_net_consumption includes
            # ev_planned_load_kwh.  The EV draws directly from grid/PV, not from
            # the home battery, so including it in occ_needed would over-inflate
            # the pre-charge target and cause the price-spread guard in
            # _apply_grid_charge to reject otherwise profitable charge slots.
            #
            # ev_accounted_load_kwh is already captured in avg_house_consumption
            # (base_load_includes_ev=True), so no correction is needed for that
            # case — the battery must cover it.
            occ_net = 0.0
            occ_prices: list[float] = []
            for s in slots:
                s_start = as_tz(s.start, now.tzinfo)
                s_end = as_tz(s.end, now.tzinfo)
                if (
                    s.recommendation == Recommendations.BatteriesDischargeMode.value
                    and s_start >= window_start_abs
                    and s_end <= window_end_abs
                ):
                    # Subtract extra EV load (injected, base_load_includes_ev=False)
                    # so the battery only targets house coverage.
                    battery_net = (
                        s.estimated_net_consumption_kwh - s.ev_planned_load_kwh
                    )
                    occ_net += battery_net
                    occ_prices.append(s.price.import_price)

            occ_needed = max(occ_net, 0.0)
            occ_avg_price = (
                round(sum(occ_prices) / len(occ_prices), 3) if occ_prices else 0.0
            )
            # Store: (window_start, window_end, needed_kwh, avg_discharge_price)
            occurrences.append(
                (window_start_abs, window_end_abs, occ_needed, occ_avg_price)
            )
            sched_total_net += occ_net

            # Advance to the same window start on the following calendar day
            window_start_abs += timedelta(days=1)

        # _occurrences: per-day data consumed by apply_charge_schedules
        sched._occurrences = occurrences  # type: ignore[attr-defined]
        # _needed_capacity: aggregate across all occurrences (used by coordinator)
        sched._needed_capacity = max(sched_total_net, 0.0)  # type: ignore[attr-defined]
        # _avg_import_price: average across all occurrences
        all_occ_prices = [avg for _, _, _, avg in occurrences if avg > 0]
        sched._avg_import_price = (  # type: ignore[attr-defined]
            round(sum(all_occ_prices) / len(all_occ_prices), 3)
            if all_occ_prices
            else 0.0
        )


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
        if as_tz(slot.start, now.tzinfo) < now:
            continue
        if slot.estimated_net_consumption_kwh < 0:
            break
        if slot.estimated_net_consumption_kwh > 0:
            required += slot.estimated_net_consumption_kwh

    buffer_kwh = usable_capacity * (discharge_buffer_pct / 100)
    result = round(required + buffer_kwh, 3)
    log_planner(
        "debug",
        "[disch] calculate_required_battery_until_solar  required=%.3f  buffer=%.3f  result=%.3f",
        required,
        buffer_kwh,
        result,
    )
    return result


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
    log_planner(
        "debug",
        "[disch] apply_excess_export  budget=%.3f  current=%.3f  required=%.3f  "
        "price_threshold=%.4f",
        battery_discharge_budget_kwh,
        current_capacity,
        required_capacity,
        export_price_threshold,
    )
    if battery_discharge_budget_kwh <= 0:
        log_planner(
            "debug",
            "[disch] apply_excess_export  skipped — budget <= 0",
        )
        return

    # D16 fix: track actual solar vs grid fractions rather than a coarse flag.
    # We compute the total kWh scheduled to be charged from solar vs from the
    # grid within this planning run.  The solar fraction determines how much of
    # the battery is considered "free" (solar-charged) vs paid-for (grid-charged).
    # When the solar fraction exceeds 50 % of total planned charging we treat
    # the battery as predominantly solar-charged and bypass the price threshold;
    # otherwise the full price-difference guard applies.
    solar_charged_kwh = sum(
        s.batteries_charged_kwh
        for s in slots
        if s.recommendation == Recommendations.BatteriesChargeSolar.value
    )
    grid_charged_kwh = sum(
        s.batteries_charged_kwh
        for s in slots
        if s.recommendation == Recommendations.BatteriesChargeGrid.value
    )
    total_planned_charge_kwh = solar_charged_kwh + grid_charged_kwh
    # battery_is_solar_charged is True only when solar charging is the
    # dominant (> 50 %) planned source, or when no grid charging is planned.
    battery_is_solar_charged = (
        total_planned_charge_kwh < 1e-9
        or solar_charged_kwh / total_planned_charge_kwh > 0.5
    )

    candidates = sorted(
        (
            s
            for s in slots
            if as_tz(s.start, now.tzinfo) >= now
            and s.recommendation is None
            and (
                battery_is_solar_charged
                or (
                    s.price.export_price - s.price.import_price
                    >= export_price_threshold
                )
            )
            and s.price.export_price > 0
        ),
        key=lambda x: x.price.export_price,
        reverse=True,
    )

    for s in candidates:
        if battery_discharge_budget_kwh <= 0:
            break
        s.recommendation = Recommendations.ForceBatteriesDischarge.value
        warnings.append(
            f"ForceBatteriesDischarge at {s.start.isoformat()}: export={s.price.export_price}"
        )
        # Only positive net consumption (house load > solar) draws from the battery
        # discharge budget.  Solar-surplus slots (net < 0) contribute 0 drain because
        # the surplus is handled by solar, not by the battery.
        battery_discharge_budget_kwh -= max(s.estimated_net_consumption_kwh, 0.0)


# ---------------------------------------------------------------------------
# Discharge concentration — avoid wasting battery on cheap slots
# ---------------------------------------------------------------------------


def concentrate_discharge_on_expensive_slots(
    slots: list[PlannedSlot],
    now: datetime,
    current_kwh: float,
    usable_kwh: float,
    max_discharge_per_slot: float | None,
    discharge_efficiency_pct: float = 100.0,
) -> None:
    """Clear cheap discharge slots the battery cannot fully serve.

    ``apply_discharge_schedules`` and ``apply_optimization_strategy`` mark
    *every* slot in a discharge window as ``BatteriesDischargeMode``, but
    the battery can only cover a fraction of them.  Without concentration
    the SoC simulation greedily discharges in the *first* (cheapest) slots
    and runs out before the most expensive ones.

    This function ranks all ``BatteriesDischargeMode`` slots by import price
    (descending) and clears the recommendation on the cheapest slots that
    exceed the battery's discharge capacity, turning them into grid-import
    slots (marked ``BatteriesWaitMode``).  The most expensive slots keep
    their discharge recommendation.

    The estimate is conservative: it assumes the battery starts at full
    and there is no incoming charge between discharge slots.

    Args:
        slots: Mutable list of planned slots.
        now: Timezone-aware current datetime.
        current_kwh: Energy currently stored above the discharge floor (kWh).
        usable_kwh: Maximum usable energy above the discharge floor (kWh).
        max_discharge_per_slot: Maximum energy dischargeable per slot (kWh).
            ``None`` means unlimited (inverter default).
        discharge_efficiency_pct: Discharge-side efficiency (0-100 %).
    """
    log_planner(
        "debug",
        "[disch] concentrate_discharge_on_expensive_slots  usable=%.3f  current=%.3f  "
        "max_discharge=%s",
        usable_kwh,
        current_kwh,
        f"{max_discharge_per_slot:.3f}" if max_discharge_per_slot is not None else "∞",
    )
    discharge_eff = clamp_efficiency(discharge_efficiency_pct)

    # Collect all future discharge slots (both BatteriesDischargeMode and
    # ForceBatteriesDischarge — issue #425 Bug I fix).
    discharge_slots = [
        s
        for s in slots
        if s.recommendation in _DISCHARGE_RECS and as_tz(s.end, now.tzinfo) > now
    ]
    if not discharge_slots:
        return

    # Sort by import price descending — most expensive first
    discharge_slots.sort(key=lambda s: s.price.import_price, reverse=True)

    # Calculate cumulative battery energy needed for each slot (most expensive first)
    # and keep only as many as the battery can serve.
    # Use usable_kwh because the battery will be charged by the scheduler
    # before the discharge window starts — current_kwh is only the starting
    # state and does not reflect the available capacity at discharge time.
    total_battery_kwh = usable_kwh
    keep_set: set[int] = set()
    for s in discharge_slots:
        # Energy the battery must release to cover net_demand
        slot_demand = max(s.estimated_net_consumption_kwh, 0.0)
        battery_needed = slot_demand / discharge_eff if discharge_eff > 1e-9 else 0.0
        # Respect the inverter's per-slot discharge power limit — the SoC
        # simulation caps at max_discharge_per_slot, so the concentration
        # estimate must match to avoid over-counting how many slots fit.
        if max_discharge_per_slot is not None:
            battery_needed = min(battery_needed, max_discharge_per_slot)

        if battery_needed <= total_battery_kwh:
            total_battery_kwh -= battery_needed
            keep_set.add(id(s))
        else:
            # Not enough battery — skip this slot, but keep checking
            # cheaper slots that may have small enough demand to fit.
            continue

    for s in discharge_slots:
        if id(s) not in keep_set:
            _LOGGER.debug(
                "concentrate: clearing discharge at %s\u2192%s  price=%.4f  "
                "(battery can only cover %d of %d slots)",
                s.start.strftime("%d %H:%M"),
                s.end.strftime("%H:%M"),
                s.price.import_price,
                len(keep_set),
                len(discharge_slots),
            )
            # Use BatteriesWaitMode so the fill pass in engine.py does NOT
            # re-mark this as BatteriesDischargeMode.
            s.recommendation = Recommendations.BatteriesWaitMode.value
            s.batteries_charged_kwh = 0.0


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
    export_min_price: float = 0.0,
) -> None:
    """Apply seasonal optimization logic to remaining unassigned slots.

    Decision priority per unassigned slot:

    1. Export price > import price **and** export price \u2265 ``export_min_price``
       \u2192 ``ForceExport``
    2. Solar surplus \u2192 ``BatteriesChargeSolar`` (until battery full)
    3. Future forced export pending and battery above required \u2192 ``BatteriesWaitMode``
    4. Winter month \u2192 ``BatteriesWaitMode``
    5. Summer month with solar \u2192 ``BatteriesChargeSolar``; else ``BatteriesDischargeMode``

    Args:
        slots: Mutable list of planned slots.
        now: Timezone-aware current datetime.
        current_capacity: Current available battery energy in kWh.
        usable_capacity: Maximum usable battery energy in kWh.
        required_capacity: Energy required until next solar surplus (kWh).
        months_winter: List of month integers (1-12) treated as winter.
        warnings: Mutable list for diagnostic messages (currently unused here).
        export_min_price: Minimum export price required to trigger
            ``ForceExport``.  Slots where export price is below this
            threshold are not marked for export even if export > import.
            Defaults to ``0.0`` (any positive export price qualifies).
    """
    log_planner(
        "debug",
        "[disch] apply_optimization_strategy  current=%.3f  usable=%.3f  "
        "required=%.3f  export_min_price=%.4f",
        current_capacity,
        usable_capacity,
        required_capacity,
        export_min_price,
    )
    current_month = now.month
    months_summer = [m for m in range(1, 13) if m not in months_winter]

    # ForceExport when export > import AND export >= export_min_price (A3 fix)
    for rec in slots:
        if (
            rec.price.export_price > rec.price.import_price
            and rec.price.export_price >= export_min_price
            and rec.recommendation is None
        ):
            rec.recommendation = Recommendations.ForceExport.value

    # Solar charging until battery full — across the full planning horizon
    # (not limited to today; a 48-hour window should charge from solar on
    # both day 1 and day 2).
    batteries_needed_charge = max(usable_capacity - current_capacity, 0.0)
    charged = 0.0

    for rec in sorted(
        (
            s
            for s in slots
            if s.recommendation is None and as_tz(s.start, now.tzinfo) >= now
        ),
        key=lambda x: x.price.export_price,
    ):
        if charged >= batteries_needed_charge:
            break
        # v5.1.0 threshold: <= NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH
        # (charge even near-zero-consumption slots)
        if rec.estimated_net_consumption_kwh <= NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH:
            # Per-slot energy: how much this individual slot contributes, capped at
            # what is still needed.  Store the per-slot value so that summing across
            # slots in engine.py / total_charged_energy_kwh() is not double-counted.
            slot_solar = abs(rec.estimated_net_consumption_kwh)
            slot_energy = min(slot_solar, batteries_needed_charge - charged)
            charged += slot_energy
            rec.recommendation = Recommendations.BatteriesChargeSolar.value
            rec.batteries_charged_kwh = round(slot_energy, 3)

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
            if rec.estimated_net_consumption_kwh <= NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH:
                rec.recommendation = Recommendations.BatteriesChargeSolar.value
            else:
                rec.recommendation = Recommendations.BatteriesDischargeMode.value
