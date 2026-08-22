"""Coordinator helper functions extracted from :mod:`coordinator`.

These are pure functions with no dependency on the coordinator class, making
them independently unit-testable.

Extracted to keep :mod:`coordinator` under the 30 KB / 1000-line limit.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from homeassistant.config_entries import ConfigEntry

from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.planner.ev_planner import (
    EVChargingPlan,
    rebuild_ev_plan_from_slots,
)
from custom_components.hsem.utils.datetime_utils import slot_contains, utc_key
from custom_components.hsem.utils.misc import get_config_value
from custom_components.hsem.utils.recommendations import Recommendations
from custom_components.hsem.utils.units import slot_duration_hours

# ---------------------------------------------------------------------------
# Lightweight slot for dynamic floor bridge computation
# ---------------------------------------------------------------------------


@dataclass
class _SimpleSlot:
    """Minimal slot for DynamicDischargeFloor.compute_floor().

    Carries only the fields needed by the bridge computation.
    """

    start: datetime
    end: datetime
    estimated_net_consumption_kwh: float
    batteries_charged_kwh: float
    recommendation: str | None


# ---------------------------------------------------------------------------
# Force-charge-now override helper
# ---------------------------------------------------------------------------


def apply_current_ev_power_override(
    *,
    config_entry: ConfigEntry,
    hourly_recommendations: list[HourlyRecommendation],
    ev_plan: EVChargingPlan | None,
    ev_second_plan: EVChargingPlan | None,
    now: datetime,
    override_primary: bool,
    override_second: bool,
    live: LiveState | None = None,
) -> None:
    """Apply a fuse-safe current EV request with coherent slot accounting."""
    if not override_primary and not override_second:
        return
    slot = next(
        (
            item
            for item in hourly_recommendations
            if slot_contains(item.start, item.end, now)
        ),
        None,
    )
    if slot is None:
        return
    remaining_hours = slot_duration_hours(max(now, slot.start), slot.end)
    if remaining_hours <= 1e-9:
        return

    old_total_ev_kwh = max(float(slot.ev_total_planned_load_kwh), 0.0)
    primary_w = max(float(slot.ev_charger_calculated_power), 0.0)
    second_w = max(float(slot.ev_second_charger_calculated_power), 0.0)
    primary_max_w = max(
        float(
            get_config_value(config_entry, "hsem_ev_planned_load_charger_power_kw")
            or 0.0
        )
        * 1000.0,
        0.0,
    )
    second_max_w = max(
        float(
            get_config_value(
                config_entry, "hsem_ev_second_planned_load_charger_power_kw"
            )
            or 0.0
        )
        * 1000.0,
        0.0,
    )
    if live is not None:
        if override_primary and live.ev.is_connected is False:
            primary_max_w = 0.0
        if override_second and live.ev_second.is_connected is False:
            second_max_w = 0.0

    fuse_amps = max(
        float(get_config_value(config_entry, "hsem_main_fuse_amps") or 0.0), 0.0
    )
    fuse_phases = max(
        int(get_config_value(config_entry, "hsem_main_fuse_phases") or 3), 1
    )
    budget_w = math.inf
    if fuse_amps > 1e-9:
        fixed_site_w = (
            max(float(live.house_consumption_power_w or 0.0), 0.0) if live else 0.0
        )
        if live is not None and bool(
            get_config_value(config_entry, "hsem_house_power_includes_ev_charger_power")
        ):
            fixed_site_w = max(
                fixed_site_w
                - max(float(live.ev.power_w or 0.0), 0.0)
                - max(float(live.ev_second.power_w or 0.0), 0.0),
                0.0,
            )
        budget_w = max(fuse_amps * fuse_phases * 230.0 - fixed_site_w, 0.0)

    if not override_primary:
        budget_w = max(budget_w - primary_w, 0.0)
    else:
        primary_w = min(primary_max_w, budget_w)
        budget_w = max(budget_w - primary_w, 0.0)
    if not override_second:
        budget_w = max(budget_w - second_w, 0.0)
    else:
        second_w = min(second_max_w, budget_w)

    slot.ev_charger_calculated_power = round(primary_w)
    slot.ev_second_charger_calculated_power = round(second_w)
    new_total_ev_kwh = round((primary_w + second_w) * remaining_hours / 1000.0, 3)
    slot.ev_total_planned_load_kwh = new_total_ev_kwh
    base_includes_ev = bool(
        get_config_value(config_entry, "hsem_house_power_includes_ev_charger_power")
    )
    slot.ev_accounted_load_kwh = new_total_ev_kwh if base_includes_ev else 0.0
    slot.ev_planned_load_kwh = 0.0 if base_includes_ev else new_total_ev_kwh
    slot.estimated_net_consumption_kwh = round(
        slot.avg_house_consumption_kwh
        + slot.ev_planned_load_kwh
        - slot.solcast_pv_estimate_kwh,
        3,
    )
    net_grid_kwh = (
        slot.grid_import_kwh
        - slot.grid_export_kwh
        + new_total_ev_kwh
        - old_total_ev_kwh
    )
    slot.grid_import_kwh = round(max(net_grid_kwh, 0.0), 3)
    slot.grid_export_kwh = round(max(-net_grid_kwh, 0.0), 3)
    slot.estimated_cost_currency = round(
        slot.grid_import_kwh * slot.import_price
        - slot.grid_export_kwh * slot.export_price,
        4,
    )
    if primary_w > 1e-9 or second_w > 1e-9:
        slot.recommendation = Recommendations.EVSmartCharging.value

    for plan, is_second, efficiency_key in (
        (ev_plan, False, "hsem_ev_planned_load_charger_efficiency"),
        (ev_second_plan, True, "hsem_ev_second_planned_load_charger_efficiency"),
    ):
        if plan is None:
            continue
        rebuilt = rebuild_ev_plan_from_slots(
            plan,
            hourly_recommendations,
            now,
            float(get_config_value(config_entry, efficiency_key) or 100.0),
            is_second=is_second,
        )
        plan.__dict__.update(rebuilt.__dict__)


def apply_force_charge_now(
    *,
    config_entry: ConfigEntry,
    hourly_recommendations: list[HourlyRecommendation],
    ev_plan: EVChargingPlan | None,
    ev_second_plan: EVChargingPlan | None,
    now: datetime,
    live: LiveState | None = None,
) -> None:
    """Apply the force-charge-now override to the current slot.

    When the user toggles ``hsem_ev_force_charge_now`` (or the second-EV
    equivalent), the current slot's recommendation is overridden to
    ``ev_smart_charging`` and the calculated charger power is set to the
    charger's maximum AC power.

    Crucially, force-charge works **even when smart charging is disabled**.
    The EV planner returns ``smart_charging_disabled`` with zero allocated
    power in that case, so this function also flips the plan state to
    ``charging`` so the plan sensor reflects the forced charge.

    Args:
        config_entry: The HSEM config entry (to read the force-charge switches).
        hourly_recommendations: The list of hourly recommendations to modify.
        ev_plan: The primary EV charging plan (may be ``None``).
        ev_second_plan: The second EV charging plan (may be ``None``).
        now: Current time (timezone-aware), used to locate the current slot.
    """
    force_primary = bool(get_config_value(config_entry, "hsem_ev_force_charge_now"))
    force_second = bool(
        get_config_value(config_entry, "hsem_ev_second_force_charge_now")
    )
    apply_current_ev_power_override(
        config_entry=config_entry,
        hourly_recommendations=hourly_recommendations,
        ev_plan=ev_plan,
        ev_second_plan=ev_second_plan,
        now=now,
        override_primary=force_primary,
        override_second=force_second,
        live=live,
    )


# ---------------------------------------------------------------------------
# Load-forecast fail-closed hold helper
# ---------------------------------------------------------------------------

LOAD_FORECAST_LIVE_DEMAND_THRESHOLD_W = 50.0
LOAD_FORECAST_ZERO_EPSILON_KWH = 1e-9

_LoadSlotSignature = tuple[str, float, float, float, float, float]
LoadForecastSignature = tuple[_LoadSlotSignature, ...]


@dataclass(frozen=True)
class LoadForecastReadiness:
    """Validated future consumption profile and accepted-plan signature."""

    ready: bool
    reason: str | None
    signature: LoadForecastSignature | None


def assess_load_forecast(
    recommendations: Sequence[HourlyRecommendation],
    now: datetime,
    *,
    population_succeeded: bool,
    live_house_demand_w: float | None,
) -> LoadForecastReadiness:
    """Validate future load provenance without rejecting genuine measured zero."""
    if not population_succeeded:
        return LoadForecastReadiness(False, "source_unavailable", None)

    now_utc = utc_key(now)
    future_slots = sorted(
        (rec for rec in recommendations if utc_key(rec.end) > now_utc),
        key=lambda rec: utc_key(rec.start),
    )
    if not future_slots:
        return LoadForecastReadiness(False, "missing_future_slots", None)

    signature_slots: list[_LoadSlotSignature] = []
    weighted_profile_is_zero = True
    for rec in future_slots:
        raw_values = (
            rec.avg_house_consumption_kwh,
            rec.avg_house_consumption_1d_kwh,
            rec.avg_house_consumption_3d_kwh,
            rec.avg_house_consumption_7d_kwh,
            rec.avg_house_consumption_14d_kwh,
        )
        canonical_values: list[float] = []
        for raw_value in raw_values:
            if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
                return LoadForecastReadiness(False, "invalid_future_values", None)
            number = float(raw_value)
            if not math.isfinite(number) or number < 0.0:
                return LoadForecastReadiness(False, "invalid_future_values", None)
            canonical_values.append(round(number, 5))

        weighted, avg_1d, avg_3d, avg_7d, avg_14d = canonical_values
        if weighted > LOAD_FORECAST_ZERO_EPSILON_KWH:
            weighted_profile_is_zero = False
        signature_slots.append(
            (
                utc_key(rec.start).isoformat(),
                weighted,
                avg_1d,
                avg_3d,
                avg_7d,
                avg_14d,
            )
        )

    finite_live_demand_w = (
        float(live_house_demand_w)
        if live_house_demand_w is not None
        and not isinstance(live_house_demand_w, bool)
        and math.isfinite(live_house_demand_w)
        else None
    )
    if (
        weighted_profile_is_zero
        and finite_live_demand_w is not None
        and finite_live_demand_w > LOAD_FORECAST_LIVE_DEMAND_THRESHOLD_W
    ):
        return LoadForecastReadiness(False, "zero_forecast_with_live_demand", None)

    return LoadForecastReadiness(True, None, tuple(signature_slots))


def load_forecast_signatures_match(
    current: LoadForecastSignature | None,
    baseline: LoadForecastSignature | None,
) -> bool:
    """Return whether two load signatures match within the canonical epsilon."""
    if current is None or baseline is None:
        return current is baseline
    if len(current) != len(baseline):
        return False
    for current_slot, baseline_slot in zip(current, baseline, strict=True):
        if current_slot[0] != baseline_slot[0]:
            return False
        if any(
            not math.isclose(
                current_value,
                baseline_value,
                rel_tol=0.0,
                abs_tol=LOAD_FORECAST_ZERO_EPSILON_KWH,
            )
            for current_value, baseline_value in zip(
                current_slot[1:], baseline_slot[1:], strict=True
            )
        ):
            return False
    return True


def set_strict_storage_hold(
    current: HourlyRecommendation,
) -> HourlyRecommendation:
    """Clear plan-derived primary-storage motion and publish explicit hold intent."""
    current.recommendation = Recommendations.BatteriesWaitMode.value
    current.batteries_charged_kwh = 0.0
    current.batteries_discharged_kwh = 0.0
    current.grid_import_kwh = 0.0
    current.grid_export_kwh = 0.0
    current.estimated_cost_currency = 0.0
    current.estimated_net_consumption_kwh = 0.0
    current.ev_planned_load_kwh = 0.0
    current.ev_accounted_load_kwh = 0.0
    current.ev_total_planned_load_kwh = 0.0
    current.ev_charger_calculated_power = 0.0
    current.ev_second_charger_calculated_power = 0.0
    return current


def apply_load_forecast_hold(
    recommendations: list[HourlyRecommendation],
    live: LiveState,
    now: datetime,
    *,
    load_forecast_ready: bool | None = None,
    consumption_ok: bool | None = None,
) -> HourlyRecommendation | None:
    """Publish a strict current-slot hold while the load forecast is unsafe.

    ``consumption_ok`` is retained as a compatibility alias for callers from
    before load readiness became a structured coordinator gate.
    """
    ready = load_forecast_ready if load_forecast_ready is not None else consumption_ok
    if ready is None:
        raise TypeError("load_forecast_ready is required")
    if ready:
        return None
    if str(live.force_working_mode_state).strip().lower() != "auto":
        return None
    current = next(
        (rec for rec in recommendations if slot_contains(rec.start, rec.end, now)),
        None,
    )
    if current is None:
        return None
    return set_strict_storage_hold(current)


def future_consumption_profile_is_nonzero(
    recommendations: list[HourlyRecommendation],
    now: datetime,
) -> bool:
    """Return whether at least one future slot carries a positive load estimate."""
    readiness = assess_load_forecast(
        recommendations,
        now,
        population_succeeded=True,
        live_house_demand_w=None,
    )
    return bool(
        readiness.signature
        and any(
            slot[1] > LOAD_FORECAST_ZERO_EPSILON_KWH for slot in readiness.signature
        )
    )


def live_demand_contradicts_zero_profile(
    recommendations: list[HourlyRecommendation],
    live: LiveState,
    now: datetime,
) -> bool:
    """Return whether live demand disproves an all-zero future load profile."""
    readiness = assess_load_forecast(
        recommendations,
        now,
        population_succeeded=True,
        live_house_demand_w=live.house_consumption_power_w,
    )
    return readiness.reason == "zero_forecast_with_live_demand"
