"""Applier for HSEMWorkingModeSensor.

Single responsibility: translate the current :class:`HourlyRecommendation` and
:class:`LiveState` into hardware write calls on the Huawei Solar inverter and
battery pack.

This is the **only** module in the sensor pipeline that is allowed to call
``async_set_*`` hardware functions.  All decision logic lives in the planner
engine or the recommendation resolver; this module only executes the resulting
action plan.
"""

from __future__ import annotations

import re

from custom_components.hsem.const import (
    DEFAULT_HSEM_BATTERIES_WAIT_MODE,
    DEFAULT_HSEM_EV_CHARGER_TOU_MODES,
    DEFAULT_HSEM_TOU_MODES_FORCE_CHARGE,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.huawei import (
    async_set_forcible_discharge,
    async_set_grid_export_power_pct,
    async_set_tou_periods,
)
from custom_components.hsem.utils.logger import async_logger
from custom_components.hsem.utils.misc import (
    async_set_number_value,
    async_set_select_option,
    convert_to_int,
    generate_hash,
    get_max_discharge_power,
)
from custom_components.hsem.utils.recommendations import Recommendations
from custom_components.hsem.utils.workingmodes import WorkingModes


async def async_apply_inverter_power_control(
    sensor,
    cfg: SensorConfig,
    live: LiveState,
) -> None:
    """Set the grid-export power percentage on all inverters.

    Decides whether to allow full export (100%) or block export (0%) based on
    the current export price, the minimum price threshold, and EV connection
    state.  Only issues a hardware write when the value differs from the current
    inverter state.

    Args:
        sensor: ``HSEMWorkingModeSensor`` instance for HA access and logging.
        cfg: Current sensor configuration.
        live: Live state snapshot (prices, EV states, inverter control state).
    """
    export_price = live.energi_data_service_export_price
    min_price = cfg.energi_data_service_export_min_price

    if not isinstance(export_price, (int, float)):
        return
    if not isinstance(min_price, (int, float)):
        return

    export_pct = 100 if export_price >= min_price else 0

    # Allow export if EV is connected and needs charging
    if export_pct == 0:
        ev1_cfg = cfg.ev
        ev1 = live.ev
        if ev1.is_connected:
            if (
                isinstance(ev1.soc_pct, (int, float))
                and isinstance(ev1.soc_target_pct, (int, float))
                and ev1.soc_pct < ev1.soc_target_pct
            ):
                export_pct = 100
            elif (
                isinstance(ev1.soc_pct, (int, float))
                and ev1_cfg.allow_charge_past_target_soc
                and ev1.soc_pct < 100
            ):
                export_pct = 100

        ev2_cfg = cfg.ev_second
        ev2 = live.ev_second
        if ev2.is_connected:
            if (
                isinstance(ev2.soc_pct, (int, float))
                and isinstance(ev2.soc_target_pct, (int, float))
                and ev2.soc_pct < ev2.soc_target_pct
            ):
                export_pct = 100
            elif (
                isinstance(ev2.soc_pct, (int, float))
                and ev2_cfg.allow_charge_past_target_soc
                and ev2.soc_pct < 100
            ):
                export_pct = 100

    await async_logger(
        sensor,
        f"Determined export power percentage: {export_pct}% "
        f"(export={export_price}, min={min_price}, "
        f"ev1_connected={live.ev.is_connected}, ev2_connected={live.ev_second.is_connected})",
    )

    current_pct = _parse_power_control_pct(live.huawei_inverter_active_power_control)
    if current_pct is not None and current_pct != export_pct:
        for inv_id in [
            cfg.huawei_solar_device_id_inverter_1,
            cfg.huawei_solar_device_id_inverter_2,
        ]:
            if inv_id is not None:
                await async_set_grid_export_power_pct(sensor, inv_id, export_pct)


async def async_apply_battery_settings(
    sensor,
    cfg: SensorConfig,
    live: LiveState,
    rec: HourlyRecommendation,
    current_required_battery_kwh: float,
) -> None:
    """Apply the working mode, TOU periods, and discharge power to the battery pack.

    Translates the ``rec.recommendation`` string into the correct Huawei Solar
    API calls.  Only issues writes when the hardware state actually needs to
    change (idempotent guard on each write).

    Args:
        sensor: ``HSEMWorkingModeSensor`` instance for HA access and logging.
        cfg: Current sensor configuration.
        live: Live state snapshot.
        rec: The current-interval recommendation.
        current_required_battery_kwh: Remaining energy required until end of day
            (used when computing forcible-discharge target SoC).
    """
    tou_modes = None
    working_mode = None

    max_discharge_power = get_max_discharge_power(
        convert_to_int(live.huawei_batteries_rated_capacity_wh)
    )

    # Set maximum discharging power unless EV is charging
    if not live.ev.is_charging and not live.ev_second.is_charging:
        if live.huawei_batteries_max_discharge_power_w != max_discharge_power:
            await async_set_number_value(
                sensor,
                cfg.huawei_solar_batteries_maximum_discharging_power,
                max_discharge_power,
            )

    recommendation = rec.recommendation

    match recommendation:
        case Recommendations.ForceExport.value:
            working_mode = WorkingModes.FullyFedToGrid.value

        case Recommendations.BatteriesChargeGrid.value:
            tou_modes = DEFAULT_HSEM_TOU_MODES_FORCE_CHARGE
            working_mode = WorkingModes.TimeOfUse.value

        case Recommendations.EVSmartCharging.value:
            if (
                live.ev.force_max_discharge_power
                or live.ev_second.force_max_discharge_power
            ):
                working_mode = WorkingModes.MaximizeSelfConsumption.value
            else:
                tou_modes = DEFAULT_HSEM_EV_CHARGER_TOU_MODES
                working_mode = WorkingModes.TimeOfUse.value

        case Recommendations.BatteriesDischargeMode.value:
            working_mode = WorkingModes.MaximizeSelfConsumption.value

        case Recommendations.BatteriesChargeSolar.value:
            working_mode = WorkingModes.MaximizeSelfConsumption.value

        case Recommendations.ForceBatteriesDischarge.value:
            await _async_apply_forcible_discharge(
                sensor, cfg, live, current_required_battery_kwh, max_discharge_power
            )
            return

        case Recommendations.BatteriesWaitMode.value:
            tou_modes = DEFAULT_HSEM_BATTERIES_WAIT_MODE
            working_mode = WorkingModes.TimeOfUse.value

        case _:
            return

    # Override discharge power when EV uses V2H
    if recommendation == Recommendations.EVSmartCharging.value and (
        live.ev.force_max_discharge_power or live.ev_second.force_max_discharge_power
    ):
        ev_max = max(
            live.ev.max_discharge_power_w,
            live.ev_second.max_discharge_power_w,
        )
        if live.huawei_batteries_max_discharge_power_w != ev_max:
            await async_set_number_value(
                sensor,
                cfg.huawei_solar_batteries_maximum_discharging_power,
                ev_max,
            )

    # Excess PV use in TOU
    desired_excess = (
        "fed_to_grid"
        if recommendation
        in (Recommendations.BatteriesWaitMode.value, WorkingModes.FullyFedToGrid.value)
        else "charge"
    )
    if live.huawei_batteries_excess_pv_use_in_tou != desired_excess:
        await async_set_select_option(
            sensor,
            cfg.huawei_solar_batteries_excess_pv_energy_use_in_tou,
            desired_excess,
        )

    # TOU periods
    if working_mode == WorkingModes.TimeOfUse.value and tou_modes:
        if generate_hash(str(tou_modes)) != generate_hash(
            str(live.tou_periods.periods)
        ):
            await async_set_tou_periods(
                sensor, cfg.huawei_solar_device_id_batteries, tou_modes
            )

    # Working mode
    if working_mode and live.huawei_batteries_working_mode != working_mode:
        await async_set_select_option(
            sensor,
            cfg.huawei_solar_batteries_working_mode,
            working_mode,
        )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


async def _async_apply_forcible_discharge(
    sensor,
    cfg: SensorConfig,
    live: LiveState,
    current_required_kwh: float,
    max_discharge_power: int,
) -> None:
    """Issue a forcible-discharge command to the battery pack."""
    if (
        live.battery_usable_capacity_kwh <= 0
        or current_required_kwh < 0
        or not cfg.huawei_solar_device_id_batteries
    ):
        return

    target_soc = int((current_required_kwh / live.battery_usable_capacity_kwh) * 100)
    target_soc = max(10, min(100, target_soc))  # clamp 10–100

    await async_set_forcible_discharge(
        sensor,
        cfg.huawei_solar_device_id_batteries,
        target_soc,
        max_discharge_power,
    )
    await async_logger(
        sensor,
        f"Excess battery export: Set forcible discharge to {target_soc}% SOC "
        f"at {max_discharge_power}W power.",
    )


def _parse_power_control_pct(state: str | None) -> int | None:
    """Parse the inverter active power control state string into a percentage.

    Args:
        state: Raw string from the inverter entity (e.g. ``"Unlimited"`` or
               ``"Limited to 80%"``).

    Returns:
        Integer percentage, or ``None`` if the string cannot be parsed.
    """
    if not isinstance(state, str):
        return None
    normalized = state.strip().lower()
    # Accept any locale-independent representation of "unlimited" / no cap.
    if normalized in (
        "unlimited",
        "ikke begrænset",
        "onbeperkt",
        "unbegrenzt",
        "illimitato",
        "sin límite",
        "không giới hạn",
    ):
        return 100
    # Extract numeric percentage regardless of surrounding translated text.
    # Strategy: strip everything that is not a digit, dot, or minus sign,
    # then parse the remaining number.  This handles patterns like:
    #   "Limited to 80%"  →  80
    #   "Begrenzt auf 80 %"  →  80
    #   "Beperkt tot 80%"  →  80
    if "%" in normalized:
        # Extract the numeric value regardless of surrounding translated text
        match = re.search(r"(-?\d+(?:\.\d+)?)", normalized)
        if match:
            try:
                return int(round(float(match.group(1))))
            except (ValueError, TypeError):
                pass
    return None
