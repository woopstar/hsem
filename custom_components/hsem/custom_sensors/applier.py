"""Applier for HSEMWorkingModeSensor.

Single responsibility: translate the current :class:`HourlyRecommendation` and
:class:`LiveState` into hardware write calls on the Huawei Solar inverter and
battery pack.

This is the **only** module in the sensor pipeline that is allowed to call
``async_set_*`` hardware functions.  All decision logic lives in the planner
engine or the recommendation resolver; this module only executes the resulting
action plan.

Write-and-verify
----------------
Every hardware write is wrapped with :func:`~utils.inverter_verify.async_write_and_verify`:

1. Write the desired value via a Huawei Solar service call.
2. Wait :data:`~utils.inverter_verify.DEFAULT_SETTLE_SECONDS` for the inverter
   to persist the new value.
3. Read the entity state back from HA.
4. Accept if the read-back value matches within
   :data:`~utils.inverter_verify.DEFAULT_NUMERIC_TOLERANCE`.
5. Retry up to :data:`~utils.inverter_verify.DEFAULT_MAX_RETRIES` times on
   mismatch or transient read/write error.
6. After all retries, mark the result ``FAILED`` and **block further writes for
   this cycle** (the caller gates subsequent writes on the summary status).

Each top-level apply function returns a :class:`~utils.inverter_verify.CycleApplySummary`
that the :class:`~custom_sensors.applier_status_sensor.HSEMApplierStatusSensor` surfaces
to Home Assistant.
"""

from __future__ import annotations

from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

from custom_components.hsem.const import (
    DEFAULT_HSEM_BATTERIES_WAIT_MODE,
    DEFAULT_HSEM_EV_CHARGER_TOU_MODES,
    DEFAULT_HSEM_TOU_MODES_FORCE_CHARGE,
)
from custom_components.hsem.custom_sensors.applier_caps import (  # noqa: F401
    _configured_battery_device_ids,
    _fmt_live_power_w,
    _should_force_export_for_ev,
    _wait_mode_self_consumption_cap_w,
    compute_ev_discharge_cap_w,
)
from custom_components.hsem.custom_sensors.applier_forcible_discharge import (  # noqa: F401
    _async_apply_forcible_discharge,
)
from custom_components.hsem.custom_sensors.applier_power_control import (  # noqa: F401
    async_apply_inverter_power_control,
)
from custom_components.hsem.custom_sensors.applier_state_readers import (  # noqa: F401
    _is_watt_limit,
    _parse_power_control_pct,
    _read_number_state,
    _read_select_state,
    _read_tou_periods,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.conversion import convert_to_int
from custom_components.hsem.utils.degraded_mode import hardware_writes_allowed
from custom_components.hsem.utils.ha_helpers import (
    async_set_number_value,
    async_set_select_option,
)
from custom_components.hsem.utils.huawei import (
    async_set_tou_periods,
    async_stop_forcible_discharge,
)
from custom_components.hsem.utils.inverter_verify import (
    ApplyStatus,
    CycleApplySummary,
    async_write_and_verify,
)
from custom_components.hsem.utils.logger import HSEM_LOGGER as _LOGGER
from custom_components.hsem.utils.misc import (
    generate_hash,
    get_max_discharge_power,
)
from custom_components.hsem.utils.recommendations import Recommendations
from custom_components.hsem.utils.units import slot_duration_hours
from custom_components.hsem.utils.workingmodes import WorkingModes


async def async_apply_battery_settings(
    sensor: Any,  # NOSONAR -- HA internal type; circular import risk
    cfg: SensorConfig,
    live: LiveState,
    rec: HourlyRecommendation,
    current_required_battery_kwh: float,
) -> CycleApplySummary:
    """Apply the working mode, TOU periods, and discharge power to the battery pack.

    Translates the ``rec.recommendation`` string into the correct Huawei Solar
    API calls.  Only issues writes when the hardware state actually needs to
    change (idempotent guard on each write).

    Each write is wrapped with :func:`~utils.inverter_verify.async_write_and_verify`
    so that the value is polled back from HA after the write and verified within
    tolerance.  If a write fails all retries it is recorded in the returned
    summary and further writes are blocked for this cycle.

    Args:
        sensor: ``HSEMWorkingModeSensor`` instance for HA access and logging.
        cfg: Current sensor configuration.
        live: Live state snapshot.
        rec: The current-interval recommendation.
        current_required_battery_kwh: Remaining energy required until end of day
            (used when computing forcible-discharge target SoC).

    Returns:
        :class:`CycleApplySummary` with one :class:`ApplyResult` per write
        attempted.  Returns an empty summary immediately when blocked.
    """
    summary = CycleApplySummary()

    # Defense-in-depth: block writes if read_only or degraded mode is Error.
    if cfg.read_only:
        _LOGGER.debug("async_apply_battery_settings: skipped — read_only=True")
        return summary
    if not hardware_writes_allowed(live.degraded_mode):
        _LOGGER.debug(
            f"async_apply_battery_settings: skipped — degraded mode: {live.degraded_mode.value}",
            "warning",
        )
        return summary

    tou_modes = None
    working_mode = None

    _rated_capacity = convert_to_int(live.huawei_batteries_rated_capacity_wh)
    max_discharge_power = get_max_discharge_power(
        _rated_capacity if _rated_capacity is not None else 0
    )

    # Set maximum discharging power unless EV is charging
    if not live.ev.is_charging and not live.ev_second.is_charging:
        if live.huawei_batteries_max_discharge_power_w != max_discharge_power:
            discharge_entity = cfg.huawei_solar_batteries_maximum_discharging_power
            if discharge_entity is None:
                _LOGGER.debug(
                    "Max discharge power entity not configured; skipping write.",
                    "warning",
                )
                return summary
            _de: str = discharge_entity  # narrowed for closure
            result = await async_write_and_verify(
                entity_id=_de,
                desired=max_discharge_power,
                writer=lambda: async_set_number_value(sensor, _de, max_discharge_power),
                reader=lambda: _read_number_state(sensor, _de),
            )
            summary.results.append(result)
            if result.status == ApplyStatus.FAILED:
                _LOGGER.debug(
                    f"Max discharge power write FAILED for {discharge_entity}. "
                    "Blocking further battery writes this cycle.",
                    "error",
                )
                return summary

    recommendation = rec.recommendation

    # When an EV is actively charging and we are NOT in a forced-discharge
    # or forced-export recommendation, cap the battery discharge power to
    # prevent the Huawei inverter from physically discharging into the EV.
    #
    # The inverter's CT clamp sees the full EV load as demand and will
    # offset it from the battery unless the discharge power is explicitly
    # restricted.  The cap is the house-only consumption — the battery
    # still covers normal house load while 100 % of the EV load goes to
    # the grid.  This applies identically to HSEM-planned and unplanned
    # ("ghost") EV charging (issue #592).
    ev_active = live.any_ev_charging
    if ev_active and recommendation not in (
        Recommendations.ForceBatteriesDischarge.value,
        Recommendations.ForceExport.value,
    ):
        discharge_entity = cfg.huawei_solar_batteries_maximum_discharging_power
        if discharge_entity is not None:
            # Determine whether HSEM actually planned EV charging.
            # Fields come from the planner output; they are 0 when the
            # planner decided NOT to charge the EV in this slot.
            hsem_planned_ev = (
                rec.ev_charger_calculated_power > 1e-9
                or rec.ev_second_charger_calculated_power > 1e-9
                or rec.ev_total_planned_load_kwh > 1e-9
            )

            # Cap to the house-only consumption using live data when
            # available (already has EV power subtracted — unaffected
            # by polluted v5 upgrade history).  Falls back to historical
            # average.  Applies to both HSEM-planned and unplanned EV
            # charging — the cap limits to house-only load in both cases.
            slot_hours = slot_duration_hours(rec.start, rec.end)
            historical_w = (
                int(rec.avg_house_consumption_kwh / slot_hours * 1000.0)
                if slot_hours > 1e-9 and rec.avg_house_consumption_kwh > 1e-9
                else 0
            )
            live_net_w = live.net_consumption_w
            # Only trust net_consumption_w if the EV power sensor actually
            # provided a value.  When ev.power_w is 0/None but is_charging
            # is True (e.g. boolean-only sensor, or sensor unavailable),
            # net_consumption_w == house_w (no EV subtraction happened).
            ev_power_available = (
                live.ev.power_w is not None and live.ev.power_w > 1e-9
            ) or (live.ev_second.power_w is not None and live.ev_second.power_w > 1e-9)
            sub_window_ws = [
                int(sw / slot_hours * 1000.0)
                for sw in (
                    rec.avg_house_consumption_1d_kwh,
                    rec.avg_house_consumption_3d_kwh,
                    rec.avg_house_consumption_7d_kwh,
                    rec.avg_house_consumption_14d_kwh,
                    rec.avg_house_consumption_kwh,
                )
                if sw > 1e-9 and slot_hours > 1e-9
            ]
            cap_w = compute_ev_discharge_cap_w(
                live_net_w=live_net_w,
                ev_power_available=ev_power_available,
                historical_w=historical_w,
                sub_window_ws=sub_window_ws,
            )
            cap_reason = "EV active" if hsem_planned_ev else "EV active (unplanned)"

            # SoC guard (issue #592, v6.2.0-beta1): never let the EV cap
            # drain the battery below the energy the planner has reserved
            # for upcoming scheduled discharge windows.  When the remaining
            # usable energy is at or below the required reserve, force the
            # cap to 0 — the battery is preserved for its schedule and the
            # house load (like the EV) is served from the grid until the
            # battery recovers above the reserve.
            if (
                cap_w > 0
                and current_required_battery_kwh > 1e-9
                and live.battery_current_capacity_kwh > 1e-9
                and live.battery_current_capacity_kwh <= current_required_battery_kwh
            ):
                _LOGGER.debug(
                    "%s — battery reserve reached (%.2f kWh left, %.2f kWh "
                    "reserved for scheduled plans) — forcing EV discharge "
                    "cap to 0 W to protect the schedule",
                    cap_reason,
                    live.battery_current_capacity_kwh,
                    current_required_battery_kwh,
                )
                cap_w = 0

            if live.huawei_batteries_max_discharge_power_w != cap_w:
                _de3: str = discharge_entity  # narrowed for closure
                ev_discharge_result = await async_write_and_verify(
                    entity_id=_de3,
                    desired=cap_w,
                    writer=lambda: async_set_number_value(sensor, _de3, cap_w),
                    reader=lambda: _read_number_state(sensor, _de3),
                )
                summary.results.append(ev_discharge_result)
                _LOGGER.debug(
                    "%s — capped max discharge power to %d W "
                    "(planned_ev_power=%dW planned_ev2_power=%dW "
                    "ev_total_load=%.3fkWh live_ev_power=%s live_ev2_power=%s "
                    "house_avg=%.3f kWh/slot)",
                    cap_reason,
                    cap_w,
                    rec.ev_charger_calculated_power,
                    rec.ev_second_charger_calculated_power,
                    rec.ev_total_planned_load_kwh,
                    _fmt_live_power_w(live.ev.power_w),
                    _fmt_live_power_w(live.ev_second.power_w),
                    rec.avg_house_consumption_kwh,
                )
                if ev_discharge_result.status == ApplyStatus.FAILED:
                    _LOGGER.debug(
                        f"EV discharge cap write FAILED for {discharge_entity}.",
                        "error",
                    )
                    return summary

    # If we're switching away from force discharge, explicitly stop any
    # active forcible charge/discharge before applying the new mode.
    if recommendation not in (
        Recommendations.ForceBatteriesDischarge.value,
        Recommendations.ForceExport.value,
    ):
        fc_state = live.huawei_batteries_forcible_charge_state or ""
        battery_device_ids = _configured_battery_device_ids(cfg)
        if (
            fc_state
            and fc_state.lower()
            not in (
                "stopped",
                STATE_UNAVAILABLE,
                STATE_UNKNOWN,
                "",
            )
            and battery_device_ids
        ):
            for device_id in battery_device_ids:
                await async_stop_forcible_discharge(sensor, device_id)

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
            forcible_results = await _async_apply_forcible_discharge(
                sensor, cfg, live, current_required_battery_kwh, max_discharge_power
            )
            if forcible_results:
                summary.results.extend(forcible_results)
            return summary

        case Recommendations.BatteriesWaitMode.value:
            # Strict wait keeps the battery idle in TOU mode.  Self-consumption
            # with reserve switches to MaximizeSelfConsumption so the house can
            # use surplus battery energy above the planner's required reserve.
            if cfg.batteries_wait_mode_behavior == "self_consumption_with_reserve":
                surplus = (
                    live.battery_current_capacity_kwh - current_required_battery_kwh
                )
                if surplus > 1e-9:
                    working_mode = WorkingModes.MaximizeSelfConsumption.value
                else:
                    tou_modes = DEFAULT_HSEM_BATTERIES_WAIT_MODE
                    working_mode = WorkingModes.TimeOfUse.value
            else:
                tou_modes = DEFAULT_HSEM_BATTERIES_WAIT_MODE
                working_mode = WorkingModes.TimeOfUse.value

        case _:
            # Unrecognised recommendation — nothing to apply.
            return summary

    # Wait mode self-consumption: cap discharge power so only surplus energy
    # above the planner's required reserve can be used.  This preserves the
    # reserve for future scheduled discharge windows while still allowing the
    # house to cover normal self-consumption from the surplus.
    wait_mode_self_consumption = (
        recommendation == Recommendations.BatteriesWaitMode.value
        and cfg.batteries_wait_mode_behavior == "self_consumption_with_reserve"
        and working_mode == WorkingModes.MaximizeSelfConsumption.value
        and not ev_active
    )
    if wait_mode_self_consumption:
        slot_hours = slot_duration_hours(rec.start, rec.end)
        surplus = max(
            live.battery_current_capacity_kwh - current_required_battery_kwh, 0.0
        )
        cap_w = _wait_mode_self_consumption_cap_w(
            battery_capacity_kwh=live.battery_current_capacity_kwh,
            required_capacity_kwh=current_required_battery_kwh,
            slot_hours=slot_hours,
            max_discharge_power_w=max_discharge_power,
        )
        if live.huawei_batteries_max_discharge_power_w != cap_w:
            discharge_entity = cfg.huawei_solar_batteries_maximum_discharging_power
            if discharge_entity is None:
                _LOGGER.debug(
                    "Wait mode self-consumption discharge power entity not configured; "
                    "skipping write.",
                    "warning",
                )
                return summary
            _de_wait: str = discharge_entity  # narrowed for closure
            wait_cap_result = await async_write_and_verify(
                entity_id=_de_wait,
                desired=cap_w,
                writer=lambda: async_set_number_value(sensor, _de_wait, cap_w),
                reader=lambda: _read_number_state(sensor, _de_wait),
            )
            summary.results.append(wait_cap_result)
            _LOGGER.debug(
                "Wait mode self-consumption — capped max discharge power to %d W "
                "(capacity=%.2f kWh, required=%.2f kWh, surplus=%.2f kWh, "
                "slot_hours=%.3f)",
                cap_w,
                live.battery_current_capacity_kwh,
                current_required_battery_kwh,
                surplus,
                slot_hours,
            )
            if wait_cap_result.status == ApplyStatus.FAILED:
                _LOGGER.debug(
                    "Wait mode self-consumption discharge cap write FAILED for %s. "
                    "Blocking further battery writes this cycle.",
                    discharge_entity,
                    "error",
                )
                return summary

    # Override discharge power when EV uses V2H
    if recommendation == Recommendations.EVSmartCharging.value and (
        live.ev.force_max_discharge_power or live.ev_second.force_max_discharge_power
    ):
        ev_max = max(
            live.ev.max_discharge_power_w,
            live.ev_second.max_discharge_power_w,
        )
        if live.huawei_batteries_max_discharge_power_w != ev_max:
            discharge_entity = cfg.huawei_solar_batteries_maximum_discharging_power
            if discharge_entity is None:
                _LOGGER.debug(
                    "EV V2H discharge power entity not configured; skipping write.",
                    "warning",
                )
                return summary
            _de2: str = discharge_entity  # narrowed for closure
            ev_result = await async_write_and_verify(
                entity_id=_de2,
                desired=ev_max,
                writer=lambda: async_set_number_value(sensor, _de2, ev_max),
                reader=lambda: _read_number_state(sensor, _de2),
            )
            summary.results.append(ev_result)
            if ev_result.status == ApplyStatus.FAILED:
                _LOGGER.debug(
                    f"EV V2H discharge power write FAILED for {discharge_entity}. "
                    "Blocking further battery writes this cycle.",
                    "error",
                )
                return summary

    # Excess PV use in TOU — fed_to_grid for strict wait/fully-fed modes, charge
    # otherwise.  Wait-mode self-consumption keeps excess PV in the battery so
    # the surplus above the reserve can be used for household self-consumption.
    # ForceExport maps to WorkingModes.FullyFedToGrid at the hardware level so we
    # check both BatteriesWaitMode and ForceExport recommendations here.
    desired_excess = (
        "charge"
        if wait_mode_self_consumption
        else (
            "fed_to_grid"
            if recommendation
            in (
                Recommendations.BatteriesWaitMode.value,
                Recommendations.ForceExport.value,
            )
            else "charge"
        )
    )
    if live.huawei_batteries_excess_pv_use_in_tou != desired_excess:
        excess_entity = cfg.huawei_solar_batteries_excess_pv_energy_use_in_tou
        if excess_entity is None:
            _LOGGER.debug(
                "Excess PV use entity not configured; skipping write.", "warning"
            )
            return summary
        _ee: str = excess_entity  # narrowed for closure
        excess_result = await async_write_and_verify(
            entity_id=_ee,
            desired=desired_excess,
            writer=lambda: async_set_select_option(sensor, _ee, desired_excess),
            reader=lambda: _read_select_state(sensor, _ee),
        )
        summary.results.append(excess_result)
        if excess_result.status == ApplyStatus.FAILED:
            _LOGGER.debug(
                f"Excess PV use write FAILED for {excess_entity}. "
                "Blocking further battery writes this cycle.",
                "error",
            )
            return summary

    # TOU periods — verified against the entity's live ``Period N`` attributes
    # (see :func:`_read_tou_periods`).  The gate below uses the pre-write
    # LiveState snapshot only to decide *whether* a write is needed;
    # verification must re-read HA, because that snapshot by definition still
    # holds the old schedule.
    if (
        working_mode == WorkingModes.TimeOfUse.value
        and tou_modes
        and generate_hash(str(tou_modes))
        != generate_hash(str(live.tou_periods.periods))
    ):
        tou_entity = cfg.huawei_solar_batteries_tou_charging_and_discharging_periods
        battery_device_ids = _configured_battery_device_ids(cfg)
        if tou_entity is None or not battery_device_ids:
            _LOGGER.debug(
                "TOU entity or battery device ID not configured; skipping write.",
                "warning",
            )
            return summary
        _te: str = tou_entity  # narrowed for closure
        for battery_device_id in battery_device_ids:

            async def _write_tou(
                _dev: str = battery_device_id,
            ) -> None:
                await async_set_tou_periods(sensor, _dev, tou_modes)

            result = await async_write_and_verify(
                entity_id=f"{_te}:{battery_device_id}",
                desired=list(tou_modes),
                writer=_write_tou,
                reader=lambda: _read_tou_periods(sensor, _te),
                # The LiveState gate above already established a difference, so the
                # first attempt must write rather than short-circuit on a stale read.
                skip_if_equal=False,
                max_retries=2,
            )
            summary.results.append(result)
            if result.status == ApplyStatus.FAILED:
                _LOGGER.debug(
                    "TOU period write FAILED for device %s. Blocking further "
                    "battery writes this cycle.",
                    battery_device_id,
                )
                return summary

    # Working mode
    if working_mode and live.huawei_batteries_working_mode != working_mode:
        mode_entity = cfg.huawei_solar_batteries_working_mode
        if mode_entity is None:
            _LOGGER.debug(
                "Working mode entity not configured; skipping write.", "warning"
            )
            return summary
        _me: str = mode_entity  # narrowed for closure
        mode_result = await async_write_and_verify(
            entity_id=_me,
            desired=working_mode,
            writer=lambda: async_set_select_option(sensor, _me, working_mode),
            reader=lambda: _read_select_state(sensor, _me),
        )
        summary.results.append(mode_result)
        if mode_result.status == ApplyStatus.FAILED:
            _LOGGER.debug(
                f"Working mode write FAILED for {mode_entity}. "
                "Blocking further battery writes this cycle.",
                "error",
            )
            return summary

    return summary
