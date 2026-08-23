"""Inverter grid-export power-control writes.

Extracted from ``applier.py`` to satisfy the repository's 30 KB /
1000-line file limit. Pure move: no behaviour change.
"""

from __future__ import annotations

from typing import Any

from custom_components.hsem.const import (
    GRID_EXPORT_LIMIT_WATT,
)
from custom_components.hsem.custom_sensors.applier_state_readers import (
    _is_watt_limit,
    _parse_power_control_pct,
)
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.degraded_mode import hardware_writes_allowed
from custom_components.hsem.utils.huawei import (
    async_set_grid_export_power_pct,
    async_set_grid_export_power_watt,
)
from custom_components.hsem.utils.inverter_verify import (
    ApplyStatus,
    CycleApplySummary,
    async_write_and_verify,
)
from custom_components.hsem.utils.logger import HSEM_LOGGER as _LOGGER


async def async_apply_inverter_power_control(
    sensor: Any,  # NOSONAR -- HA internal type; circular import risk
    cfg: SensorConfig,
    live: LiveState,
) -> CycleApplySummary:
    """Set the grid-export power limit on all inverters.

    The inverter grid connection point is controlled by price and by the
    user-configured grid export cap:

    - Negative export price → block all export with a soft watt floor
      (``GRID_EXPORT_LIMIT_WATT``), because exporting then costs money.
    - Non-negative export price → allow export, but cap it at
      ``cfg.max_grid_export_power_kw`` when that value is configured.  A cap of
      ``0`` or unset is treated as unlimited/100 %.  Battery-to-grid export
      below ``export_electricity_min_price`` is gated planner-side by the
      MILP and discharge scheduler, not by throttling the whole connection point.

    This avoids the issue described in #767, where a positive-but-low export
    price caused the applier to write a 100 W connection-point limit that
    blocked surplus PV export once the battery was full.

    Only issues a hardware write when the inverter state actually needs to change.

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
        _LOGGER.debug("async_apply_inverter_power_control: skipped — read_only=True")
        return summary
    if not hardware_writes_allowed(live.degraded_mode):
        _LOGGER.debug(
            "async_apply_inverter_power_control: skipped — degraded mode: %s",
            live.degraded_mode.value,
        )
        return summary

    export_price = live.export_electricity_price
    min_price = cfg.export_electricity_min_price

    if not isinstance(export_price, (int, float)):
        return summary
    if not isinstance(min_price, (int, float)):
        return summary

    # Negative export prices are the only case where we physically block the
    # whole grid connection point.  When exporting costs money we must not
    # allow any export, including surplus PV.  For all non-negative prices we
    # keep the connection point open and let the planner gate battery-to-grid
    # export via export_electricity_min_price (issue #767).
    if export_price < 0.0:
        desired = GRID_EXPORT_LIMIT_WATT
        desired_is_watt = True
        _LOGGER.debug(
            "Export price %.4f is negative; blocking all grid export with %d W limit.",
            export_price,
            desired,
        )
    else:
        grid_export_cap_kw = cfg.max_grid_export_power_kw
        if grid_export_cap_kw > 1e-9:
            # Respect the configured DNO/grid export limit as a hard cap.
            desired = int(round(grid_export_cap_kw * 1000.0))
            desired_is_watt = True
            _LOGGER.debug(
                "Export price %.4f is non-negative; allowing export up to "
                "configured grid limit %d W (%.3f kW).",
                export_price,
                desired,
                grid_export_cap_kw,
            )
        else:
            # No cap configured → unlimited/100 % export.
            desired = 100
            desired_is_watt = False
            if export_price < min_price:
                _LOGGER.debug(
                    "Export price %.4f is below export_electricity_min_price %.4f; "
                    "leaving grid feed-in limit unlimited so PV surplus can export. "
                    "Battery-to-grid export is gated by the planner.",
                    export_price,
                    min_price,
                )

    _LOGGER.debug(
        "Determined export power limit: %s%s (export=%s, min=%s, "
        "ev1_connected=%s, ev2_connected=%s)",
        desired,
        "W" if desired_is_watt else "%",
        export_price,
        min_price,
        live.ev.is_connected,
        live.ev_second.is_connected,
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

        # Skip if the inverter already matches the desired state.
        if (
            current_pct is not None
            and current_is_watt == desired_is_watt
            and current_pct == desired
        ):
            continue

        if desired_is_watt:
            result = await async_write_and_verify(
                entity_id=inv_entity or f"inverter:{inv_id}",
                desired=desired,
                writer=lambda _id=inv_id, _w=desired: async_set_grid_export_power_watt(  # type: ignore[misc]  # mypy cannot infer lambda types with default parameters
                    sensor, _id, _w
                ),
                reader=reader_fn,
            )
        else:
            result = await async_write_and_verify(
                entity_id=inv_entity or f"inverter:{inv_id}",
                desired=desired,
                writer=lambda _id=inv_id, _pct=desired: (  # type: ignore[misc]  # mypy cannot infer lambda types with default parameters
                    async_set_grid_export_power_pct(sensor, _id, _pct)
                ),
                reader=reader_fn,
            )

        summary.results.append(result)

        if result.status == ApplyStatus.FAILED:
            mode = "W" if desired_is_watt else "%"
            _LOGGER.debug(
                "Export power %s write FAILED for inverter %s after all retries. "
                "Blocking further writes this cycle.",
                mode,
                inv_id,
            )
            return summary

    return summary
