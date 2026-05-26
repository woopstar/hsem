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

import re

from custom_components.hsem.const import (
    DEFAULT_HSEM_BATTERIES_WAIT_MODE,
    DEFAULT_HSEM_EV_CHARGER_TOU_MODES,
    DEFAULT_HSEM_TOU_MODES_FORCE_CHARGE,
    GRID_EXPORT_LIMIT_WATT,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.degraded_mode import hardware_writes_allowed
from custom_components.hsem.utils.huawei import (
    async_set_forcible_discharge,
    async_set_grid_export_power_pct,
    async_set_grid_export_power_watt,
    async_set_tou_periods,
)
from custom_components.hsem.utils.inverter_verify import (
    ApplyResult,
    ApplyStatus,
    CycleApplySummary,
    async_write_and_verify,
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
) -> CycleApplySummary:
    """Set the grid-export power percentage on all inverters.

    Decides whether to allow full export (100%) or block export (0%) based on
    the current export price, the minimum price threshold, and EV connection
    state.  Only issues a hardware write when the value differs from the current
    inverter state.

    Each write is wrapped with :func:`~utils.inverter_verify.async_write_and_verify`
    so that the inverter is polled after the write and the result is verified
    within tolerance.  If any write fails all retries, further writes within this
    cycle are blocked and the failure is recorded in the returned summary.

    This function includes its own safety gate as defense-in-depth.  Callers
    (``working_mode_sensor``) are expected to gate writes too, but this
    secondary check ensures no write ever reaches the inverter when
    ``cfg.read_only`` is ``True`` or the degraded mode is ``Error``.

    Args:
        sensor: ``HSEMWorkingModeSensor`` instance for HA access and logging.
        cfg: Current sensor configuration.
        live: Live state snapshot (prices, EV states, inverter control state).

    Returns:
        :class:`CycleApplySummary` with one :class:`ApplyResult` per inverter
        write attempted.  Returns an empty summary immediately when blocked.
    """
    summary = CycleApplySummary()

    # Defense-in-depth: block writes if read_only or degraded mode is Error.
    if cfg.read_only:
        await async_logger(
            sensor,
            "async_apply_inverter_power_control: skipped — read_only=True",
        )
        return summary
    if not hardware_writes_allowed(live.degraded_mode):
        await async_logger(
            sensor,
            f"async_apply_inverter_power_control: skipped — degraded mode: {live.degraded_mode.value}",
            "warning",
        )
        return summary

    export_price = live.export_electricity_price
    min_price = cfg.export_electricity_min_price

    if not isinstance(export_price, (int, float)):
        return summary
    if not isinstance(min_price, (int, float)):
        return summary

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
    current_is_watt = _is_watt_limit(live.huawei_inverter_active_power_control)

    for inv_id in [
        cfg.huawei_solar_device_id_inverter_1,
        cfg.huawei_solar_device_id_inverter_2,
    ]:
        if inv_id is None:
            continue

        inv_entity = cfg.huawei_solar_inverter_active_power_control
        reader_fn = lambda: _parse_power_control_pct(
            sensor.hass.states.get(inv_entity).state
            if inv_entity and sensor.hass.states.get(inv_entity) is not None
            else None
        )

        if export_pct == 0:
            # Block export → set a soft floor at GRID_EXPORT_LIMIT_WATT.
            desired = GRID_EXPORT_LIMIT_WATT
            if current_pct is not None and current_is_watt and current_pct == desired:
                continue  # already at the watt limit

            result = await async_write_and_verify(
                entity_id=inv_entity or f"inverter:{inv_id}",
                desired=desired,
                writer=lambda _id=inv_id, _w=desired: async_set_grid_export_power_watt(
                    sensor, _id, _w
                ),
                reader=reader_fn,
            )
        else:
            # export_pct == 100 — Allow full export.
            if (
                current_pct is not None
                and not current_is_watt
                and current_pct == export_pct
            ):
                continue  # already at unlimited / 100 %

            result = await async_write_and_verify(
                entity_id=inv_entity or f"inverter:{inv_id}",
                desired=export_pct,
                writer=lambda _id=inv_id, _pct=export_pct: (
                    async_set_grid_export_power_pct(sensor, _id, _pct)
                ),
                reader=reader_fn,
            )

        summary.results.append(result)

        if result.status == ApplyStatus.FAILED:
            mode = "W" if export_pct == 0 else "%"
            await async_logger(
                sensor,
                f"Export power {mode} write FAILED for inverter {inv_id} after all retries. "
                f"Blocking further writes this cycle.",
                "error",
            )
            return summary

    return summary


async def async_apply_battery_settings(
    sensor,
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
        await async_logger(
            sensor,
            "async_apply_battery_settings: skipped — read_only=True",
        )
        return summary
    if not hardware_writes_allowed(live.degraded_mode):
        await async_logger(
            sensor,
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
            result = await async_write_and_verify(
                entity_id=discharge_entity or "batteries_maximum_discharging_power",
                desired=max_discharge_power,
                writer=lambda: async_set_number_value(
                    sensor, discharge_entity, max_discharge_power
                ),
                reader=lambda: _read_number_state(sensor, discharge_entity),
            )
            summary.results.append(result)
            if result.status == ApplyStatus.FAILED:
                await async_logger(
                    sensor,
                    f"Max discharge power write FAILED for {discharge_entity}. "
                    "Blocking further battery writes this cycle.",
                    "error",
                )
                return summary

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
            forcible_result = await _async_apply_forcible_discharge(
                sensor, cfg, live, current_required_battery_kwh, max_discharge_power
            )
            if forcible_result is not None:
                summary.results.append(forcible_result)
            return summary

        case Recommendations.BatteriesWaitMode.value:
            tou_modes = DEFAULT_HSEM_BATTERIES_WAIT_MODE
            working_mode = WorkingModes.TimeOfUse.value

        case _:
            # Unrecognised recommendation — nothing to apply.
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
            ev_result = await async_write_and_verify(
                entity_id=discharge_entity or "batteries_maximum_discharging_power",
                desired=ev_max,
                writer=lambda: async_set_number_value(sensor, discharge_entity, ev_max),
                reader=lambda: _read_number_state(sensor, discharge_entity),
            )
            summary.results.append(ev_result)
            if ev_result.status == ApplyStatus.FAILED:
                await async_logger(
                    sensor,
                    f"EV V2H discharge power write FAILED for {discharge_entity}. "
                    "Blocking further battery writes this cycle.",
                    "error",
                )
                return summary

    # Excess PV use in TOU — fed_to_grid for wait/fully-fed modes, charge otherwise.
    # ForceExport maps to WorkingModes.FullyFedToGrid at the hardware level so we
    # check both BatteriesWaitMode and ForceExport recommendations here.
    desired_excess = (
        "fed_to_grid"
        if recommendation
        in (
            Recommendations.BatteriesWaitMode.value,
            Recommendations.ForceExport.value,
        )
        else "charge"
    )
    if live.huawei_batteries_excess_pv_use_in_tou != desired_excess:
        excess_entity = cfg.huawei_solar_batteries_excess_pv_energy_use_in_tou
        excess_result = await async_write_and_verify(
            entity_id=excess_entity or "batteries_excess_pv_energy_use_in_tou",
            desired=desired_excess,
            writer=lambda: async_set_select_option(
                sensor, excess_entity, desired_excess
            ),
            reader=lambda: _read_select_state(sensor, excess_entity),
        )
        summary.results.append(excess_result)
        if excess_result.status == ApplyStatus.FAILED:
            await async_logger(
                sensor,
                f"Excess PV use write FAILED for {excess_entity}. "
                "Blocking further battery writes this cycle.",
                "error",
            )
            return summary

    # TOU periods — no read-back verification (TOU period state is complex JSON;
    # hash comparison is sufficient; single attempt only).
    if working_mode == WorkingModes.TimeOfUse.value and tou_modes:
        if generate_hash(str(tou_modes)) != generate_hash(
            str(live.tou_periods.periods)
        ):
            tou_entity = cfg.huawei_solar_batteries_tou_charging_and_discharging_periods
            result = await async_write_and_verify(
                entity_id=tou_entity or "batteries_tou_periods",
                desired=generate_hash(str(tou_modes)),
                writer=lambda: async_set_tou_periods(
                    sensor, cfg.huawei_solar_device_id_batteries, tou_modes
                ),
                reader=lambda: generate_hash(
                    str(
                        sensor.hass.states.get(tou_entity).state
                        if tou_entity and sensor.hass.states.get(tou_entity) is not None
                        else ""
                    )
                ),
                # TOU periods may take longer to propagate; skip equality check
                # since we always write when the hash differs.
                skip_if_equal=False,
                max_retries=2,
            )
            summary.results.append(result)
            if result.status == ApplyStatus.FAILED:
                await async_logger(
                    sensor,
                    f"TOU period write FAILED for device {cfg.huawei_solar_device_id_batteries}. "
                    "Blocking further battery writes this cycle.",
                    "error",
                )
                return summary

    # Working mode
    if working_mode and live.huawei_batteries_working_mode != working_mode:
        mode_entity = cfg.huawei_solar_batteries_working_mode
        mode_result = await async_write_and_verify(
            entity_id=mode_entity or "batteries_working_mode",
            desired=working_mode,
            writer=lambda: async_set_select_option(sensor, mode_entity, working_mode),
            reader=lambda: _read_select_state(sensor, mode_entity),
        )
        summary.results.append(mode_result)
        if mode_result.status == ApplyStatus.FAILED:
            await async_logger(
                sensor,
                f"Working mode write FAILED for {mode_entity}. "
                "Blocking further battery writes this cycle.",
                "error",
            )
            return summary

    return summary


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


async def _async_apply_forcible_discharge(
    sensor,
    cfg: SensorConfig,
    live: LiveState,
    current_required_kwh: float,
    max_discharge_power: int,
) -> ApplyResult | None:
    """Issue a forcible-discharge command to the battery pack and verify acceptance.

    Returns:
        :class:`ApplyResult` with the outcome, or ``None`` if the preconditions
        were not met and no write was attempted.
    """
    if (
        live.battery_usable_capacity_kwh <= 0
        or current_required_kwh < 0
        or not cfg.huawei_solar_device_id_batteries
    ):
        return None

    target_soc = int((current_required_kwh / live.battery_usable_capacity_kwh) * 100)
    target_soc = max(10, min(100, target_soc))  # clamp 10–100

    bat_soc_entity = cfg.huawei_solar_batteries_state_of_capacity
    device_id = cfg.huawei_solar_device_id_batteries

    result = await async_write_and_verify(
        entity_id=bat_soc_entity or f"battery:{device_id}",
        desired=target_soc,
        writer=lambda: async_set_forcible_discharge(
            sensor,
            device_id,
            target_soc,
            max_discharge_power,
        ),
        reader=lambda: _read_number_state(sensor, bat_soc_entity),
        # Forcible-discharge target SoC may differ from current SoC even after
        # a successful write (the battery is actively discharging), so use a
        # wider tolerance and only 1 retry — the main guard is the write success.
        tolerance=5.0,
        max_retries=1,
    )

    await async_logger(
        sensor,
        f"Excess battery export: Set forcible discharge to {target_soc}% SOC "
        f"at {max_discharge_power}W power. Verify result: {result.status.value}",
    )
    return result


# ---------------------------------------------------------------------------
# Read-back helpers (pure — no side effects)
# ---------------------------------------------------------------------------


def _read_number_state(sensor, entity_id: str | None) -> float | None:
    """Read a number entity state from HA and return it as float, or None.

    Args:
        sensor: HSEM sensor instance with a ``hass`` attribute.
        entity_id: HA entity ID to read.

    Returns:
        Current numeric state, or ``None`` when the entity is unavailable.
    """
    if not entity_id:
        return None
    state = sensor.hass.states.get(entity_id)
    if state is None or state.state in ("unknown", "unavailable", None):
        return None
    try:
        return float(state.state)
    except (ValueError, TypeError):
        return None


def _read_select_state(sensor, entity_id: str | None) -> str | None:
    """Read a select entity state from HA and return it as a string, or None.

    Args:
        sensor: HSEM sensor instance with a ``hass`` attribute.
        entity_id: HA entity ID to read.

    Returns:
        Current option string, or ``None`` when unavailable.
    """
    if not entity_id:
        return None
    state = sensor.hass.states.get(entity_id)
    if state is None or state.state in ("unknown", "unavailable", None):
        return None
    return str(state.state)


def _parse_power_control_pct(state: str | None) -> int | None:
    """Parse the inverter active power control state string into a numeric value.

    Handles both percentage (``"Limited to 80%"`` → 80) and watt-based
    (``"Limited to 100W"`` → 100) formats.  ``"Unlimited"`` returns 100.

    Args:
        state: Raw string from the inverter entity (e.g. ``"Unlimited"``,
               ``"Limited to 80%"``, or ``"Limited to 100W"``).

    Returns:
        Integer value (percentage or watts), or ``None`` if the string
        cannot be parsed.
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
    # Extract the numeric value regardless of surrounding translated text or
    # unit suffix (% or W).  This handles patterns like:
    #   "Limited to 80%"   →  80
    #   "Limited to 100W"  →  100
    #   "Begrenzt auf 80 %"  →  80
    #   "Beperkt tot 80%"  →  80
    match = re.search(r"(-?\d+(?:\.\d+)?)", normalized)
    if match:
        try:
            return int(round(float(match.group(1))))
        except (ValueError, TypeError):
            pass
    return None


def _is_watt_limit(state: str | None) -> bool:
    """Check if the power control state represents a watt-based limit.

    Args:
        state: Raw string from the inverter entity (e.g. ``"Limited to 100W"``
               or ``"Limited to 80%"``).

    Returns:
        ``True`` if the state is a watt-based limit, ``False`` otherwise
        (percentage-based or unlimited).
    """
    if not isinstance(state, str):
        return False
    normalized = state.strip().lower()
    # Unlimited / percentage-based states never contain a watt indicator
    if normalized in (
        "unlimited",
        "ikke begrænset",
        "onbeperkt",
        "unbegrenzt",
        "illimitato",
        "sin límite",
        "không giới hạn",
    ):
        return False
    # Look for a number immediately followed (with optional whitespace) by "w"
    return bool(re.search(r"\d+\s*w", normalized))
