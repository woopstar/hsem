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
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

from custom_components.hsem.const import (
    DEFAULT_HSEM_BATTERIES_WAIT_MODE,
    DEFAULT_HSEM_EV_CHARGER_TOU_MODES,
    DEFAULT_HSEM_TOU_MODES_FORCE_CHARGE,
    GRID_EXPORT_LIMIT_WATT,
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
    async_set_forcible_discharge,
    async_set_grid_export_power_pct,
    async_set_grid_export_power_watt,
    async_set_tou_periods,
    async_stop_forcible_discharge,
)
from custom_components.hsem.utils.inverter_verify import (
    ApplyResult,
    ApplyStatus,
    CycleApplySummary,
    async_write_and_verify,
)
from custom_components.hsem.utils.logger import async_logger
from custom_components.hsem.utils.misc import (
    generate_hash,
    get_max_discharge_power,
)
from custom_components.hsem.utils.recommendations import Recommendations
from custom_components.hsem.utils.workingmodes import WorkingModes


def _should_force_export_for_ev(
    ev: Any,
    ev_cfg: Any,
    live: LiveState,
) -> bool:
    """Return True if the EV needs charging and export should be forced."""
    if not ev.is_connected:
        return False
    if (
        isinstance(ev.soc_pct, (int, float))
        and isinstance(ev.soc_target_pct, (int, float))
        and ev.soc_pct < ev.soc_target_pct
    ):
        return True
    if (
        isinstance(ev.soc_pct, (int, float))
        and ev_cfg.allow_charge_past_target_soc
        and ev.soc_pct < 100
        and live.huawei_batteries_soc_pct is not None
        and live.huawei_batteries_soc_pct >= 99.0
    ):
        return True
    return False


async def async_apply_inverter_power_control(
    sensor: Any,  # NOSONAR -- HA internal type; circular import risk
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
    if export_pct == 0 and _should_force_export_for_ev(live.ev, cfg.ev, live):
        export_pct = 100
    if export_pct == 0 and _should_force_export_for_ev(
        live.ev_second, cfg.ev_second, live
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
        reader_fn = lambda inv=inv_entity: _parse_power_control_pct(
            sensor.hass.states.get(inv).state
            if inv and sensor.hass.states.get(inv) is not None
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
                writer=lambda _id=inv_id, _w=desired: async_set_grid_export_power_watt(  # type: ignore[misc]  # mypy cannot infer lambda types with default parameters
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
                writer=lambda _id=inv_id, _pct=export_pct: (  # type: ignore[misc]  # mypy cannot infer lambda types with default parameters
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
            if discharge_entity is None:
                await async_logger(
                    sensor,
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
                await async_logger(
                    sensor,
                    f"Max discharge power write FAILED for {discharge_entity}. "
                    "Blocking further battery writes this cycle.",
                    "error",
                )
                return summary

    recommendation = rec.recommendation

    # If we're switching away from force discharge, explicitly stop any
    # active forcible charge/discharge before applying the new mode.
    if recommendation not in (
        Recommendations.ForceBatteriesDischarge.value,
        Recommendations.ForceExport.value,
    ):
        fc_state = live.huawei_batteries_forcible_charge_state or ""
        if (
            fc_state
            and fc_state.lower()
            not in (
                "stopped",
                STATE_UNAVAILABLE,
                STATE_UNKNOWN,
                "",
            )
            and cfg.huawei_solar_device_id_batteries is not None
        ):
            await async_stop_forcible_discharge(
                sensor, cfg.huawei_solar_device_id_batteries
            )

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
            if discharge_entity is None:
                await async_logger(
                    sensor,
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
        if excess_entity is None:
            await async_logger(
                sensor,
                "Excess PV use entity not configured; skipping write.",
                "warning",
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
            await async_logger(
                sensor,
                f"Excess PV use write FAILED for {excess_entity}. "
                "Blocking further battery writes this cycle.",
                "error",
            )
            return summary

    # TOU periods — no read-back verification (TOU period state is complex JSON;
    # hash comparison is sufficient; single attempt only).
    if (
        working_mode == WorkingModes.TimeOfUse.value
        and tou_modes
        and generate_hash(str(tou_modes))
        != generate_hash(str(live.tou_periods.periods))
    ):
        tou_entity = cfg.huawei_solar_batteries_tou_charging_and_discharging_periods
        battery_device_id = cfg.huawei_solar_device_id_batteries
        if tou_entity is None or battery_device_id is None:
            await async_logger(
                sensor,
                "TOU entity or battery device ID not configured; skipping write.",
                "warning",
            )
            return summary
        result = await async_write_and_verify(
            entity_id=tou_entity,
            desired=generate_hash(str(tou_modes)),
            writer=lambda: async_set_tou_periods(sensor, battery_device_id, tou_modes),
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
        if mode_entity is None:
            await async_logger(
                sensor,
                "Working mode entity not configured; skipping write.",
                "warning",
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
    sensor: Any,  # NOSONAR -- HA internal type; circular import risk
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

    target_soc = int(
        live.huawei_batteries_end_of_discharge_soc_pct
    )  # discharge to floor
    target_soc = max(5, min(100, target_soc))  # clamp 5-100 for safety

    bat_fc_entity = cfg.huawei_solar_batteries_forcible_charge
    device_id = cfg.huawei_solar_device_id_batteries

    def _read_fc_accepted() -> float | None:
        """Return 1.0 if forcible charge state is active (not stopped/empty),
        None otherwise.  The forcible_charge sensor reports a string like
        'Discharging at 5000W until 5.0%' when active, or 'Stopped' when idle."""
        if not bat_fc_entity:
            return None
        state = sensor.hass.states.get(bat_fc_entity)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE, ""):
            return None
        if state.state.lower() == "stopped":
            return None
        return 1.0

    result = await async_write_and_verify(
        entity_id=bat_fc_entity or f"forcible:{device_id}",
        desired=1.0,
        writer=lambda: async_set_forcible_discharge(
            sensor,
            device_id,
            target_soc,
            max_discharge_power,
        ),
        reader=_read_fc_accepted,
        # The forcible_charge sensor changes state immediately when the
        # command is accepted — no need for wide tolerance or retries.
        tolerance=0.0,
        max_retries=3,
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


def _read_number_state(
    sensor: Any, entity_id: str | None
) -> float | None:  # NOSONAR -- HA internal type; circular import risk
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
    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE, None):
        return None
    try:
        return float(state.state)
    except ValueError, TypeError:
        return None


def _read_select_state(
    sensor: Any, entity_id: str | None
) -> str | None:  # NOSONAR -- HA internal type; circular import risk
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
    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE, None):
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
        except ValueError, TypeError:
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
    # Single quantifier avoids polynomial backtracking from stacked greedy quantifiers
    return bool(re.search(r"\d[\d\s]*w", normalized))
