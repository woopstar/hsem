"""Live per-phase Huawei grid-charge safety limiter (issue #831).

Translates a planned battery grid-charge into a phase-safe live command.
This is intentionally a runtime correction on top of the horizon MILP: the
MILP's phase-fuse constraint (``planner/milp/_phase_fuse.py``) uses a
forecast at solve time, while this module uses the newest live phase-meter
snapshot immediately before the hardware write, so it protects against
appliance changes since the plan was solved.

Huawei-only — this repository has no secondary/PowMr inverter.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.logger import HSEM_LOGGER as _LOGGER
from custom_components.hsem.utils.phase_power import (
    PhaseChargeLimits,
    compute_phase_charge_limits,
    phase_powers_valid,
)
from custom_components.hsem.utils.recommendations import Recommendations
from custom_components.hsem.utils.units import slot_duration_hours


@dataclass(frozen=True)
class PhaseAwareChargeCommands:
    """Hardware commands after applying live per-phase headroom.

    ``primary_grid_charge_power_w`` is ``None`` when the limiter did not run
    (feature disabled or this slot is not a grid-charge slot) — the caller
    should leave the grid-charge-maximum-power entity untouched in that
    case. A finite value (including ``0.0``) means the limiter did run and
    the caller must write exactly that value.
    """

    recommendation: HourlyRecommendation
    primary_grid_charge_power_w: float | None = None
    limits: PhaseChargeLimits | None = None


def _blocked_commands(
    rec: HourlyRecommendation, reason: str
) -> PhaseAwareChargeCommands:
    """Return a fail-closed 0 W command with a logged reason.

    Used whenever required live telemetry is missing or invalid, or a
    verified downward transition has timed out. Failing closed (rather
    than leaving a stale positive cap in place) is the safe default: a
    charge that cannot be safety-checked must not proceed at its
    last-known rate.

    Args:
        rec: The recommendation being applied.
        reason: Logged explanation of which telemetry was unusable.

    Returns:
        Commands with the grid-charge cap pinned to 0 W.
    """
    _LOGGER.warning("Phase-aware charge blocked: %s", reason)
    return PhaseAwareChargeCommands(recommendation=rec, primary_grid_charge_power_w=0.0)


def build_phase_aware_charge_commands(
    cfg: SensorConfig,
    live: LiveState,
    rec: HourlyRecommendation,
    *,
    primary_grid_charge_transition_reference_w: float | None = None,
    primary_grid_charge_transition_timed_out: bool = False,
) -> PhaseAwareChargeCommands:
    """Return a Huawei grid-charge command that never plans above the fuse.

    Returns a command with ``primary_grid_charge_power_w=None`` (no
    override) unless phase-aware charging is enabled *and* this slot is a
    grid-charge slot. Otherwise, returns a finite Watts value: either the
    live-safe charge cap, or ``0.0`` when required telemetry is missing or
    a verified downward transition has not settled within its deadline.

    Args:
        cfg: Current sensor configuration.
        live: Live state snapshot (phase meters, battery power, fuse config).
        rec: The current-interval recommendation.
        primary_grid_charge_transition_reference_w: While a verified
            downward cap change is still taking effect, the caller passes
            the pre-write (higher) cap here. The live battery-power
            reading may lag behind a just-verified lower command — using
            the greater of the two as the subtracted battery contribution
            prevents a stale-low reading from manufacturing phase headroom
            that does not physically exist yet. ``None`` when no
            transition is in flight for this slot.
        primary_grid_charge_transition_timed_out: ``True`` when the
            caller's verified-transition deadline (see
            ``working_mode_sensor._PrimaryGridChargeTransition``) expired
            without the live telemetry settling to the new target. Forces
            a fail-closed 0 W result for the remainder of this slot.

    Returns:
        :class:`PhaseAwareChargeCommands` describing what to write, if
        anything.
    """
    grid_charging = rec.recommendation == Recommendations.BatteriesChargeGrid.value

    if not cfg.phase_aware_charging_enabled or not grid_charging:
        return PhaseAwareChargeCommands(recommendation=rec)

    if primary_grid_charge_transition_timed_out:
        return _blocked_commands(
            rec,
            "Huawei grid-charge telemetry did not settle after a verified cap change",
        )

    if cfg.main_fuse_phases != 3 or cfg.main_fuse_amps <= 0:
        return _blocked_commands(
            rec, "main-fuse configuration is not a valid three-phase supply"
        )

    if not phase_powers_valid(live.grid_phase_power_w):
        return _blocked_commands(
            rec, "one or more grid phase-power readings are unavailable"
        )

    battery_power_w = live.huawei_batteries_charge_discharge_power_w
    if battery_power_w is None or not math.isfinite(battery_power_w):
        return _blocked_commands(
            rec,
            "Huawei battery charge/discharge power is unavailable, so "
            "per-phase headroom cannot be computed",
        )

    effective_battery_power_w = battery_power_w
    if (
        primary_grid_charge_transition_reference_w is not None
        and math.isfinite(primary_grid_charge_transition_reference_w)
        and primary_grid_charge_transition_reference_w > effective_battery_power_w
    ):
        # A verified lower cap may take effect before the battery-power
        # sensor's own poll reflects it. Keep subtracting the old physical
        # draw so this cycle cannot manufacture headroom the hardware has
        # not physically released yet.
        effective_battery_power_w = primary_grid_charge_transition_reference_w

    slot_hours = slot_duration_hours(rec.start, rec.end)
    desired_charge_power_w = 0.0
    if slot_hours > 1e-9:
        desired_charge_power_w = (
            max(rec.batteries_charged_kwh, 0.0) * 1000.0 / slot_hours
        )
        if live.huawei_batteries_max_charge_power_w is not None:
            desired_charge_power_w = min(
                desired_charge_power_w,
                max(live.huawei_batteries_max_charge_power_w, 0.0),
            )

    limits = compute_phase_charge_limits(
        measured_phase_power_w=live.grid_phase_power_w,
        fuse_amps=float(cfg.main_fuse_amps),
        desired_charge_power_w=desired_charge_power_w,
        battery_actual_power_w=effective_battery_power_w,
        charge_efficiency_pct=cfg.batteries_charge_efficiency,
        discharge_efficiency_pct=cfg.batteries_discharge_efficiency,
    )
    return PhaseAwareChargeCommands(
        recommendation=rec,
        primary_grid_charge_power_w=limits.primary_charge_power_w,
        limits=limits,
    )
