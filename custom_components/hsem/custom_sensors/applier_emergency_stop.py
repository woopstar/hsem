"""Error-mode emergency stop for an HSEM-owned Huawei grid charge (issue #840).

``DegradedMode.Error`` blocks every ordinary hardware write because critical
telemetry (battery SoC, house load, etc.) is missing and the planner cannot
safely decide anything new. But if HSEM had already armed a Huawei
grid-charge (TOU force-charge periods + a positive grid-charge-maximum-power
cap) before that telemetry loss, the blanket block leaves the charge running
uncontrolled at its last commanded rate — the one thing worth doing safely
in Error mode is turning that *specific*, HSEM-owned charge off.

This module provides exactly one narrowly-scoped exception: a downward-only
write of the grid-charge-maximum-power number to 0 W, gated on:

- the live per-phase safety limiter being enabled (``cfg.phase_aware_charging_enabled``)
  — the emergency path only ever touches the entity that feature already
  manages;
- HSEM provably owning the armed charge, derived from the accepted plan's own
  recommendation history (never inferred from hardware state alone) and
  latched across cycles so a failed stop is retried, never abandoned;
- live telemetry not already proving the charge is stopped.

An externally or manually armed TOU/force-charge schedule is never claimed:
if HSEM's own recommendation history never marked this slot as
``batteries_charge_grid``, ownership is never latched and the emergency stop
is skipped.

Huawei-only — this repository has no secondary/PowMr inverter.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from custom_components.hsem.const import DEFAULT_HSEM_TOU_MODES_FORCE_CHARGE
from custom_components.hsem.custom_sensors.applier_state_readers import (
    _read_number_state,
)
from custom_components.hsem.utils.ha_helpers import async_set_number_value
from custom_components.hsem.utils.inverter_verify import (
    ApplyResult,
    ApplyStatus,
    CycleApplySummary,
    async_write_and_verify,
)
from custom_components.hsem.utils.logger import HSEM_LOGGER as _LOGGER
from custom_components.hsem.utils.recommendations import Recommendations
from custom_components.hsem.utils.workingmodes import WorkingModes

if TYPE_CHECKING:
    from custom_components.hsem.models.hourly_recommendation import (
        HourlyRecommendation,
    )
    from custom_components.hsem.models.live_state import LiveState
    from custom_components.hsem.models.sensor_config import SensorConfig


def primary_grid_charge_is_known_disarmed(live: LiveState) -> bool:
    """Return whether live hardware positively proves grid charge is stopped.

    A verified 0 W cap, a working mode other than ``TimeOfUse``, or TOU
    periods that no longer match the force-charge schedule are all
    independent proof the charge is not running, regardless of what HSEM's
    own ownership latch says.
    """
    current_limit_w = live.huawei_batteries_grid_charge_max_power_w
    if (
        isinstance(current_limit_w, int | float)
        and not isinstance(current_limit_w, bool)
        and math.isfinite(current_limit_w)
        and current_limit_w <= 0.0
    ):
        return True

    working_mode = live.huawei_batteries_working_mode
    if isinstance(working_mode, str) and working_mode != WorkingModes.TimeOfUse.value:
        return True

    return (
        working_mode == WorkingModes.TimeOfUse.value
        and isinstance(live.tou_periods.raw_state, str)
        and list(live.tou_periods.periods) != list(DEFAULT_HSEM_TOU_MODES_FORCE_CHARGE)
    )


def huawei_grid_charge_emergency_needed(
    cfg: SensorConfig,
    live: LiveState,
    rec: HourlyRecommendation | None,
    ownership_latched: bool,
) -> bool:
    """Return whether HSEM-owned Huawei charging must be pinned to 0 W.

    Ownership is ``True`` when either the *current* accepted recommendation
    is ``batteries_charge_grid`` or a previous cycle already latched
    ownership (e.g. a prior emergency write failed and must be retried). A
    charge armed by anything other than HSEM's own plan history is never
    claimed.
    """
    hsem_owned = (
        rec is not None
        and rec.recommendation == Recommendations.BatteriesChargeGrid.value
    ) or ownership_latched
    return (
        cfg.phase_aware_charging_enabled
        and hsem_owned
        and not primary_grid_charge_is_known_disarmed(live)
    )


def summary_verifies_zero_grid_charge(
    cfg: SensorConfig,
    summary: CycleApplySummary,
) -> bool:
    """Return whether this cycle verified the Huawei charge ceiling at 0 W."""
    entity_id = cfg.huawei_solar_batteries_grid_charge_maximum_power
    return entity_id is not None and any(
        result.entity_id == entity_id
        and result.status in {ApplyStatus.OK, ApplyStatus.SKIPPED}
        and isinstance(result.desired, int | float)
        and not isinstance(result.desired, bool)
        and math.isfinite(float(result.desired))
        and float(result.desired) <= 1.0
        and isinstance(result.actual, int | float)
        and not isinstance(result.actual, bool)
        and math.isfinite(float(result.actual))
        and float(result.actual) <= 1.0
        for result in summary.results
    )


async def async_emergency_disable_grid_charge(
    sensor: Any,  # NOSONAR -- HA internal type; circular import risk
    cfg: SensorConfig,
    live: LiveState,
) -> CycleApplySummary:
    """Force the Huawei grid-charge ceiling to 0 W after critical telemetry loss.

    The caller (``huawei_grid_charge_emergency_needed``) must already have
    proven HSEM ownership of the armed charge before calling this. This
    helper remains downward-only: it never writes a value other than 0, and
    it verifies the write via the same write-and-verify primitive used by
    every other applier write.

    Args:
        sensor: ``HSEMWorkingModeSensor`` instance for HA access and logging.
        cfg: Current sensor configuration.
        live: Live state snapshot.

    Returns:
        :class:`CycleApplySummary` with zero or one :class:`ApplyResult`.
        Empty when read-only, when the feature is disabled, or when the live
        cap already reads at or below 0 W (nothing to do).
    """
    summary = CycleApplySummary()
    if cfg.read_only or not cfg.phase_aware_charging_enabled:
        return summary

    current_limit_w = live.huawei_batteries_grid_charge_max_power_w
    if (
        isinstance(current_limit_w, int | float)
        and not isinstance(current_limit_w, bool)
        and math.isfinite(current_limit_w)
        and current_limit_w <= 0.0
    ):
        return summary

    entity_id = cfg.huawei_solar_batteries_grid_charge_maximum_power
    if entity_id is None:
        summary.results.append(
            ApplyResult(
                entity_id="number:grid_charge_maximum_power",
                desired=0.0,
                actual=current_limit_w,
                status=ApplyStatus.FAILED,
                attempts=0,
                error_message=(
                    "Cannot emergency-disable grid charging: maximum-power "
                    "entity is not configured"
                ),
            )
        )
        return summary

    result = await async_write_and_verify(
        entity_id=entity_id,
        desired=0.0,
        writer=lambda: async_set_number_value(sensor, entity_id, 0.0),
        reader=lambda: _read_number_state(sensor, entity_id),
    )
    summary.results.append(result)
    _LOGGER.warning(
        "Emergency phase-aware grid-charge cap set to 0 W after critical "
        "telemetry loss (%s)",
        result.status.value,
    )
    return summary


class GridChargeEmergencyStopMixin:
    """Error-mode emergency-stop orchestration (issue #840).

    Mixed into :class:`~custom_sensors.working_mode_sensor.HSEMWorkingModeSensor`.
    Owns exactly one piece of state, ``_primary_grid_charge_owned``, latched
    across coordinator cycles.
    """

    def _release_primary_grid_charge_ownership_if_safe(
        self,
        cfg: SensorConfig,
        live: LiveState,
    ) -> None:
        """Release ownership once telemetry independently proves it is safe.

        Runs every cycle regardless of read-only/degraded-mode, so ownership
        is released the moment it is verified safe — not only when degraded
        mode happens to clear. Disabling the phase-aware charging feature
        also relinquishes ownership outright, since the emergency path only
        ever exists to protect that feature's own managed entity.
        """
        if (
            not cfg.phase_aware_charging_enabled
            or primary_grid_charge_is_known_disarmed(live)
        ):
            self._primary_grid_charge_owned = False

    async def _async_run_error_mode_emergency_stop(
        self,
        sensor: Any,
        cfg: SensorConfig,
        live: LiveState,
        rec: HourlyRecommendation | None,
    ) -> CycleApplySummary:
        """Run the narrow downward-only Error-mode exception, if needed.

        Error mode blocks every ordinary write, but an HSEM-owned Huawei
        grid charge must still be stoppable when critical telemetry
        disappears mid-charge. Ownership is derived only from the accepted
        plan's own recommendation history, never from hardware state alone,
        and is latched so a failed stop is retried next cycle instead of
        abandoned.

        Returns:
            :class:`CycleApplySummary`, empty when no emergency stop is
            needed this cycle.
        """
        summary = CycleApplySummary()
        if not huawei_grid_charge_emergency_needed(
            cfg, live, rec, self._primary_grid_charge_owned
        ):
            return summary
        self._primary_grid_charge_owned = True
        summary = await async_emergency_disable_grid_charge(sensor, cfg, live)
        if summary_verifies_zero_grid_charge(cfg, summary):
            self._primary_grid_charge_owned = False
        return summary
