"""Pure-Python HSEM planner engine.

Single responsibility: orchestrate the planning pipeline and return a
:class:`PlannerOutput`.

The heavy lifting is fully delegated:

- :mod:`slot_population` — slot generation, time-series population, battery
  capacity simulation, and the spike-aware consumption weighting algorithm
- :mod:`charge_scheduler` — discharge window detection, charge scheduling,
  excess export, and seasonal optimization

**No Home Assistant types are imported here.**  This makes the engine
directly testable with plain ``pytest`` without a running HA instance.

Design notes
------------
- The engine is intentionally *synchronous*.  The sensor's async wrappers
  exist only because HA's event loop requires them; the underlying
  calculations are CPU-bound and need no I/O.
- ``warnings`` emitted during planning are collected and returned in
  :attr:`PlannerOutput.warnings` so tests can assert on diagnostic
  messages without capturing log output.
"""

from __future__ import annotations

from datetime import datetime

from custom_components.hsem.models.planner_inputs import PlannerInput
from custom_components.hsem.models.planner_outputs import (
    ChargeWindow,
    DischargeWindow,
    PlannedSlot,
    PlannerOutput,
)
from custom_components.hsem.planner.charge_scheduler import (
    apply_charge_schedules,
    apply_discharge_schedules,
    apply_excess_export,
    apply_optimization_strategy,
    calculate_required_battery_until_solar,
)
from custom_components.hsem.planner.slot_population import (
    build_slots,
    build_time_series_index,
    mark_time_passed,
    populate_battery_capacity,
    populate_consumption,
    populate_estimated_cost,
    populate_net_consumption,
    populate_prices,
    populate_solcast,
    usable_capacity,
)
from custom_components.hsem.utils.misc import calculate_recommended_threshold
from custom_components.hsem.utils.recommendations import Recommendations

# Sets used by _derive_windows — kept here as they reference Recommendations
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

    # Parse now
    now = _parse_now(inp.now_iso)

    # Battery state
    usable_kwh, current_kwh = usable_capacity(
        inp.battery_rated_capacity_kwh,
        inp.battery_soc_pct,
        inp.battery_end_of_discharge_soc_pct,
    )

    if inp.battery_rated_capacity_kwh <= 0:
        warnings.append(
            "battery_rated_capacity_kwh is zero or negative; battery simulation disabled."
        )
        usable_kwh = 0.0
        current_kwh = 0.0

    # Validate weights
    weight_sum = inp.weight_1d + inp.weight_3d + inp.weight_7d + inp.weight_14d
    if weight_sum != 100:
        warnings.append(
            f"Consumption weights sum to {weight_sum}, not 100. "
            "Results may not be meaningful."
        )

    # Build shared time-series index — single source of truth for all slot boundaries
    tsi = build_time_series_index(inp, now)

    # Generate slots (boundaries come from TSI for DST-safe consistency)
    slots = build_slots(inp, now)
    if not slots:
        warnings.append(
            "No slots generated; check interval_minutes and interval_length_hours."
        )
        return PlannerOutput(missing_inputs=missing_inputs, warnings=warnings)

    # Populate time-series — all series aligned to the shared TSI axis
    populate_prices(slots, inp.price_points, tsi)
    populate_solcast(slots, inp.solcast_slots, inp.interval_minutes, tsi)
    populate_consumption(
        slots,
        inp.consumption_averages,
        inp.weight_1d,
        inp.weight_3d,
        inp.weight_7d,
        inp.weight_14d,
        inp.interval_minutes,
        tsi,
    )

    # Surface any hours where input data was absent
    for hour in sorted(tsi.missing_hours()):
        missing_inputs.append(f"hour_{hour:02d}")
    populate_net_consumption(slots)
    populate_estimated_cost(slots)

    # Depreciation threshold diagnostic
    recommended_threshold = calculate_recommended_threshold(
        inp.battery_purchase_price,
        inp.battery_expected_cycles,
        usable_kwh,
        inp.battery_conversion_loss_pct,
    )
    if recommended_threshold > 0:
        warnings.append(
            f"Recommended price threshold: {recommended_threshold:.4f} "
            f"(depreciation + conversion loss)."
        )

    # Mark past slots
    mark_time_passed(slots, now)

    # Discharge schedule detection
    apply_discharge_schedules(slots, inp.battery_schedules, now)

    # Charge scheduling
    conversion_loss_factor = 1 - (inp.battery_conversion_loss_pct / 100)
    max_charge_per_hour = (
        inp.battery_max_charge_power_w / 1000
    ) * conversion_loss_factor
    max_charge_per_interval = max_charge_per_hour / (60 / inp.interval_minutes)

    apply_charge_schedules(slots, inp.battery_schedules, now, max_charge_per_interval)

    # Battery capacity forward simulation (first pass)
    populate_battery_capacity(slots, now, current_kwh, usable_kwh)

    # Required capacity until solar surplus
    required_capacity = calculate_required_battery_until_solar(
        slots, now, usable_kwh, inp.excess_export_discharge_buffer_pct
    )

    # Excess export
    if inp.excess_export_enabled:
        apply_excess_export(
            slots,
            now,
            current_kwh,
            required_capacity,
            inp.excess_export_price_threshold,
            warnings,
        )

    # Seasonal optimization
    apply_optimization_strategy(
        slots,
        now,
        current_kwh,
        usable_kwh,
        required_capacity,
        inp.months_winter,
        warnings,
    )

    # Battery capacity forward simulation (second pass — after charges assigned)
    populate_battery_capacity(slots, now, current_kwh, usable_kwh)

    # Current recommendation
    current_recommendation: str | None = None
    for slot in slots:
        if slot.start.astimezone(now.tzinfo) <= now < slot.end.astimezone(now.tzinfo):
            current_recommendation = slot.recommendation
            break

    # Final SoC
    future_slots = [s for s in slots if s.end.astimezone(now.tzinfo) > now]
    battery_soc_at_end = future_slots[-1].estimated_battery_soc if future_slots else 0.0

    # Derive contiguous charge/discharge windows
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
        time_series_index=tsi,
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _parse_now(now_iso: str) -> datetime:
    """Parse a timezone-aware ISO-8601 string into a ``datetime``.

    Args:
        now_iso: ISO-8601 string with timezone offset.

    Raises:
        ValueError: If the string cannot be parsed or is not timezone-aware.
    """
    dt = datetime.fromisoformat(now_iso)
    if dt.tzinfo is None:
        raise ValueError(f"now_iso must be timezone-aware, got: {now_iso!r}")
    return dt


def _derive_windows(
    slots: list[PlannedSlot],
) -> tuple[list[ChargeWindow], list[DischargeWindow]]:
    """Derive contiguous charge and discharge windows from the slot list.

    Args:
        slots: Ordered list of planned slots.

    Returns:
        Tuple of ``(charge_windows, discharge_windows)``.
    """
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
