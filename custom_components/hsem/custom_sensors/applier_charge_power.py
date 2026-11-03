"""Grid-charge power scaled to the planned energy of the live slot (issue #1364).

A ``batteries_charge_grid`` slot used to run at the rated charging power for
the whole slot whatever the MILP had planned for it.  A planned 0.28 kWh
top-up then put 1.1 kWh into the battery, the next re-solve found a surplus
and discharged it, and the one after that charged again: three hardware
writes and a round-trip loss to land where one slot at the right rate would
have.  The helpers here write ``maximum_charging_power`` so the slot
delivers what was planned, and restore the entity's own maximum on every
other label so PV charging is never left capped.

The stale forcible-charge stop lives here too: ``applier.py`` sits at the
30 KiB file limit.
"""

from __future__ import annotations

import math
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

from custom_components.hsem.custom_sensors.applier_caps import (
    _configured_battery_device_ids,
)
from custom_components.hsem.custom_sensors.applier_state_readers import (
    _read_number_state,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.ha_helpers import async_set_number_value
from custom_components.hsem.utils.huawei import async_stop_forcible_discharge
from custom_components.hsem.utils.inverter_verify import (
    ApplyStatus,
    CycleApplySummary,
    async_write_and_verify,
)
from custom_components.hsem.utils.logger import (
    HSEM_LOGGER as _LOGGER,
    log_latched_warning,
)
from custom_components.hsem.utils.recommendations import Recommendations
from custom_components.hsem.utils.units import (
    is_material_planned_energy_kwh,
    slot_duration_hours,
)


def topup_charge_power_w(
    rec: HourlyRecommendation,
    *,
    rated_w: float,
    min_power_pct: float,
) -> int:
    """Return the charging power to command for the live slot.

    A ``batteries_charge_grid`` slot is charged at the slot-average rate of
    its planned energy, ``planned_kwh / slot_hours``, clamped between the
    configured floor and the rated power.  The slot average is used rather
    than a remaining-time rate on purpose: the MILP re-solves the slot every
    cycle against the live SoC, so a rate that climbed as the slot ran out
    would amplify the integer-percent SoC noise the planned amount already
    carries.  A held label whose current solve plans no energy (window
    hysteresis) lands on the floor, never on the rated power.

    Every other label returns ``rated_w``: the entity's own maximum, so PV
    charging in MaximizeSelfConsumption and the next grid charge start
    uncapped.

    Args:
        rec: The live slot's recommendation.
        rated_w: The number entity's ``max`` attribute, in watts.
        min_power_pct: Floor as a percentage of ``rated_w`` (0–100).

    Returns:
        The power to write, in whole watts.
    """
    rated = int(round(max(rated_w, 0.0)))
    if rec.recommendation != Recommendations.BatteriesChargeGrid.value:
        return rated
    floor_w = int(round(rated * max(min(min_power_pct, 100.0), 0.0) / 100.0))
    planned_kwh = float(rec.batteries_charged_kwh or 0.0)
    if not math.isfinite(planned_kwh) or not is_material_planned_energy_kwh(
        planned_kwh
    ):
        return min(floor_w, rated)
    hours = slot_duration_hours(rec.start, rec.end)
    if hours <= 0.0:
        return rated
    wanted_w = int(round(planned_kwh / hours * 1000.0))
    return max(min(wanted_w, rated), min(floor_w, rated))


async def async_apply_topup_charge_power(
    sensor: Any,  # NOSONAR -- HA internal type; circular import risk
    cfg: SensorConfig,
    live: LiveState,
    rec: HourlyRecommendation,
    summary: CycleApplySummary,
) -> bool:
    """Write the scaled charging power for this cycle when it differs.

    Skipped entirely while ``batteries_topup_min_power_pct`` is 0 (the
    feature is off and HSEM never touches the entity), when the entity is
    not configured, or when its ``max`` attribute is unknown — the rated
    reference is the hardware's own limit, never the commanded state, so a
    previous scaled write cannot become next cycle's ceiling.

    Args:
        sensor: Working-mode sensor for HA access and logging.
        cfg: Current sensor configuration.
        live: Live state snapshot.
        rec: The live slot's recommendation.
        summary: This cycle's apply summary; the write result is appended.

    Returns:
        ``False`` when a write failed and the caller must stop writing this
        cycle, ``True`` otherwise.
    """
    min_pct = float(cfg.batteries_topup_min_power_pct or 0.0)
    if min_pct <= 0.0:
        return True
    entity_id = cfg.huawei_solar_batteries_maximum_charging_power
    if entity_id is None:
        return True
    rated_w = live.huawei_batteries_max_charge_power_limit_w
    log_latched_warning(
        sensor,
        "topup_charge_power",
        rated_w is None or rated_w <= 0,
        "Top-up charge power: the max charging power entity has no usable "
        "max attribute; charging at the commanded power",
    )
    if rated_w is None or rated_w <= 0:
        return True
    desired_w = topup_charge_power_w(rec, rated_w=rated_w, min_power_pct=min_pct)
    current_w = live.huawei_batteries_max_charge_power_w
    if current_w is not None and int(round(current_w)) == desired_w:
        return True
    _eid: str = entity_id  # narrowed for closure
    result = await async_write_and_verify(
        entity_id=_eid,
        desired=desired_w,
        writer=lambda: async_set_number_value(sensor, _eid, desired_w),
        reader=lambda: _read_number_state(sensor, _eid),
    )
    summary.results.append(result)
    _LOGGER.debug(
        "Top-up charge power: %s → %d W (planned=%.3f kWh over %.3f h, "
        "rated=%d W, floor=%.0f%%, rec=%s)",
        "n/a" if current_w is None else f"{int(round(current_w))} W",
        desired_w,
        float(rec.batteries_charged_kwh or 0.0),
        slot_duration_hours(rec.start, rec.end),
        int(round(rated_w)),
        min_pct,
        rec.recommendation,
    )
    if result.status == ApplyStatus.FAILED:
        _LOGGER.error(
            "Max charge power write FAILED for %s; blocking further battery "
            "writes this cycle",
            entity_id,
        )
        return False
    return True


async def async_stop_stale_forcible_charge(
    sensor: Any,  # NOSONAR -- HA internal type; circular import risk
    cfg: SensorConfig,
    live: LiveState,
    recommendation: str | None,
) -> None:
    """Stop a running forcible charge/discharge the new label does not want.

    Moved verbatim from ``applier.py``: when switching away from the two
    forcible modes, any active forcible session is stopped before the new
    working mode is applied.
    """
    if recommendation in (
        Recommendations.ForceBatteriesDischarge.value,
        Recommendations.ForceExport.value,
    ):
        return
    fc_state = live.huawei_batteries_forcible_charge_state or ""
    battery_device_ids = _configured_battery_device_ids(cfg)
    if (
        fc_state
        and fc_state.lower() not in ("stopped", STATE_UNAVAILABLE, STATE_UNKNOWN, "")
        and battery_device_ids
    ):
        for device_id in battery_device_ids:
            await async_stop_forcible_discharge(sensor, device_id)
