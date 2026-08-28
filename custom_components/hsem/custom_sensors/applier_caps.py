"""Discharge-cap and export-decision helpers for the applier.

Extracted from ``applier.py`` to satisfy the repository's 30 KB /
1000-line file limit. Pure move: no behaviour change.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from custom_components.hsem.models.live_state import EVLiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.units import is_material_planned_energy_kwh

if TYPE_CHECKING:
    from custom_components.hsem.models.hourly_recommendation import (
        HourlyRecommendation,
    )


def _fmt_live_power_w(power_w: float | None) -> str:
    """Format a live power reading for log lines (``None`` → ``n/a``)."""
    if power_w is None:
        return "n/a"
    return f"{int(power_w)} W"


def _configured_battery_device_ids(cfg: SensorConfig) -> list[str]:
    """Return configured battery device IDs, preserving order and uniqueness."""
    device_ids: list[str] = []
    for device_id in (
        cfg.huawei_solar_device_id_batteries,
        cfg.huawei_solar_device_id_batteries_2,
    ):
        if device_id and device_id not in device_ids:
            device_ids.append(device_id)
    return device_ids


def _wait_mode_self_consumption_cap_w(
    battery_capacity_kwh: float,
    required_capacity_kwh: float,
    slot_hours: float,
    max_discharge_power_w: int,
) -> int:
    """Return the discharge power cap for wait-mode self-consumption.

    When the battery holds more energy than the planner has reserved for future
    expensive periods, the surplus may be used for normal household
    self-consumption.  The cap is the power required to consume exactly that
    surplus over the slot duration; the inverter will only draw what the house
    actually needs, so the battery will not discharge faster than the surplus
    allows.

    Args:
        battery_capacity_kwh: Current usable battery energy above the discharge
            floor (kWh).
        required_capacity_kwh: Energy the planner has reserved for future use
            (kWh).
        slot_hours: Duration of the current recommendation slot in hours.
        max_discharge_power_w: Maximum discharge power supported by the battery
            pack (W).

    Returns:
        Discharge power cap in watts.  ``0`` when there is no surplus above the
        reserve or the slot duration is invalid.
    """
    surplus_kwh = max(battery_capacity_kwh - required_capacity_kwh, 0.0)
    if surplus_kwh <= 1e-9 or slot_hours <= 1e-9:
        return 0
    cap_w = int(surplus_kwh / slot_hours * 1000.0)
    return min(cap_w, max_discharge_power_w)


def _is_positive_finite_number(value: object) -> bool:
    """Return whether a value is a finite, positive non-boolean number."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) > 1e-9
    )


def _ev_is_active_or_planned(
    *,
    ev: EVLiveState,
    planned_power_w: object,
) -> bool:
    """Return whether this EV can currently create Huawei discharge demand.

    Broader than "is charging right now": a positive planned command means
    HSEM is about to command this charger, so its discharge permission must
    already be enforced before the hardware catches up (issue #797).
    """
    return (
        ev.is_charging
        or _is_positive_finite_number(ev.power_w)
        or _is_positive_finite_number(planned_power_w)
    )


def _ev_phase_headroom_reservation_w(
    *,
    ev: EVLiveState,
    planned_power_w: object,
) -> int:
    """Return the phase-headroom reservation for an EV that hasn't ramped down yet.

    When an EV is live charging but the planned power is 0 (or significantly
    lower than the live draw), the OCPP anti-flap stop window means the charger
    hasn't stopped yet. The Huawei discharge cap must reserve headroom for the
    still-running EV draw until the stop command verifies.

    This prevents a transient phase-fuse overload when the plan transitions
    from "EV charging" to "battery discharging" (issue #816).

    Args:
        ev: Live EV state (includes ``is_charging`` and ``power_w``).
        planned_power_w: The planner's solved EV charge command for this slot (W).

    Returns:
        Reservation in watts. 0 when the EV is not charging live, or when the
        planned power is >= the live draw (no reservation needed).
    """
    if not ev.is_charging:
        return 0
    live_power_w = ev.power_w
    if not isinstance(live_power_w, (int, float)) or not math.isfinite(live_power_w):
        return 0
    if live_power_w <= 1e-9:
        return 0
    planned_w = (
        float(planned_power_w) if isinstance(planned_power_w, (int, float)) else 0.0
    )
    if not math.isfinite(planned_w):
        planned_w = 0.0
    # Reservation is the live draw minus the planned draw (if positive).
    reservation_w = live_power_w - planned_w
    return max(math.floor(reservation_w + 1e-9), 0) if reservation_w > 1e-9 else 0


def _planned_ev_discharge_cap_w(
    *,
    planned_discharge_kwh: float,
    slot_hours: float,
    max_discharge_power_w: int,
    ev_max_discharge_power_ws: tuple[int, ...],
) -> int:
    """Return the planned Huawei discharge rate bounded by every relevant ceiling.

    Replaces the historical/live-net heuristic (``compute_ev_discharge_cap_w``)
    with a permission-based ceiling (issue #797): once every active/planned EV
    has explicitly opted in via ``force_max_discharge_power``, the cap is the
    planner's own solved discharge rate for this slot, clamped to the
    hardware maximum and to every opted-in EV's configured ceiling — never
    more than what the plan and the user's configuration both allow.
    """
    if (
        not math.isfinite(planned_discharge_kwh)
        or planned_discharge_kwh <= 1e-9
        or not math.isfinite(slot_hours)
        or slot_hours <= 1e-9
        or max_discharge_power_w <= 0
        or not ev_max_discharge_power_ws
    ):
        return 0

    planned_power_w = planned_discharge_kwh / slot_hours * 1000.0
    cap_w = min(
        planned_power_w,
        float(max_discharge_power_w),
        *(float(max(value, 0)) for value in ev_max_discharge_power_ws),
    )
    return max(math.floor(cap_w + 1e-9), 0)


def _primary_battery_hold(rec: HourlyRecommendation) -> bool:
    """Return whether the solved plan explicitly holds the primary battery.

    Upstream persists this as a dedicated ``primary_battery_hold`` field on
    the recommendation (survives display relabelling such as
    ``EVSmartCharging``); that field does not exist here.  Locally it is
    derived instead: a slot where the solved plan scheduled neither charge
    nor discharge for the primary battery — a near-zero
    ``batteries_charged_kwh`` and ``batteries_discharged_kwh`` pair — is the
    same explicit zero-energy decision, and relabelling never touches these
    energy fields, so the derivation survives relabelling too (issue #797).
    """
    return not is_material_planned_energy_kwh(
        rec.batteries_charged_kwh
    ) and not is_material_planned_energy_kwh(rec.batteries_discharged_kwh)


def _held_planned_export_is_authoritative(rec: HourlyRecommendation) -> bool:
    """Return whether a held slot must preserve its solved grid export.

    A MILP idle slot can deliberately export surplus PV while holding
    primary battery energy at zero. The display recommendation may still be
    ``wait`` (or relabelled for a managed EV), so the aggregate grid-export
    flow is the execution authority once the slot is confirmed held.

    Upstream also requires ``price_actionable``/``export_price_available``
    (a price-freshness/authority signal this repository has no equivalent
    for) before trusting the export figure; that extra guard is not applied
    here.  Only the hold + materiality checks — both derivable from data
    already on the recommendation — gate this decision locally.
    """
    if not _primary_battery_hold(rec):
        return False
    try:
        planned_export_kwh = float(rec.grid_export_kwh)
    except TypeError, ValueError:
        return False
    return math.isfinite(planned_export_kwh) and is_material_planned_energy_kwh(
        planned_export_kwh
    )
