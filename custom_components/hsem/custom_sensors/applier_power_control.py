"""Inverter grid-export power-control writes.

Extracted from ``applier.py`` to satisfy the repository's 30 KB /
1000-line file limit. Pure move: no behaviour change.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from custom_components.hsem.const import (
    GRID_EXPORT_LIMIT_WATT,
)
from custom_components.hsem.custom_sensors.applier_state_readers import (
    _format_power_control_limit,
    _is_power_measurement,
    _is_watt_limit,
    _parse_power_control_limit,
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

    The inverter grid connection point is controlled by *net* export price
    (raw market price minus ``cfg.export_fee_per_kwh`` — the user's retailer
    margin/balancing-fee cost per kWh exported, issue #925) and by the
    user-configured grid export cap:

    - Negative net export price → block all export with a soft watt floor
      (``GRID_EXPORT_LIMIT_WATT``), because exporting then costs money. This
      also fires when the raw market price is positive but fees make the
      real net revenue negative.
    - ``cfg.curtail_pv_below_export_min_price`` is ``True`` and the raw export
      price is below ``export_electricity_min_price`` (even if still
      non-negative) → also block all export with the same watt floor
      (issue #930). This is an opt-in, default-``False`` behavior.
    - Otherwise (non-negative net price, at/above the minimum, or the opt-in
      is disabled) → allow export, but cap it at ``cfg.max_grid_export_power_kw``
      when that value is configured.  A cap of ``0`` or unset is treated as
      unlimited/100 %.  Battery-to-grid export below
      ``export_electricity_min_price`` is gated planner-side by the MILP and
      discharge scheduler, not by throttling the whole connection point.

    This avoids the issue described in #767, where a positive-but-low export
    price caused the applier to write a 100 W connection-point limit that
    blocked surplus PV export once the battery was full. Issue #930 makes
    that same physical block available again as an explicit opt-in for
    installations that want to curtail surplus PV export whenever the price
    drops below their configured minimum.

    Only issues a hardware write when the inverter state actually needs to change.

    Each write is wrapped with :func:`~utils.inverter_verify.async_write_and_verify`
    so that the inverter is polled after the write and the result is verified
    within tolerance.  If any write fails all retries, further writes within this
    cycle are blocked and the failure is recorded in the returned summary.
    Desired and read-back limits are compared as unit-tagged strings
    (``"100w"``, ``"100%"``), so a watt limit is never taken for a
    percentage (issue #1130).

    Without a usable active power control entity (none configured — the EMMA
    case — or one reporting a bare power measurement), nothing can read the
    limit back (issue #1120).  The limit is then written without read-back:
    an accepted write is ``UNVERIFIED``, a write error is still ``FAILED``,
    and an accepted limit is not rewritten until the target changes.

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
    export_fee_per_kwh = cfg.export_fee_per_kwh

    if not isinstance(export_price, (int, float)):
        return summary
    if not isinstance(min_price, (int, float)):
        return summary
    if not isinstance(export_fee_per_kwh, (int, float)):
        export_fee_per_kwh = 0.0

    # Net export price (issue #925): the raw market price minus the user's
    # retailer margin/balancing-fee cost per kWh exported.  A raw price that
    # is positive but net-negative after fees must be treated exactly like a
    # negative raw price below.
    net_export_price = export_price - export_fee_per_kwh

    # Negative net export prices always physically block the whole grid
    # connection point, because exporting then costs money.  For all
    # non-negative net prices we normally keep the connection point open and
    # let the planner gate battery-to-grid export via
    # export_electricity_min_price (issue #767) — unless the user has opted
    # into physically curtailing surplus PV below the minimum price too
    # (issue #930).
    curtail_below_min_price = (
        cfg.curtail_pv_below_export_min_price and export_price < min_price
    )
    if net_export_price < 0.0 or curtail_below_min_price:
        desired = GRID_EXPORT_LIMIT_WATT
        desired_is_watt = True
        if net_export_price < 0.0:
            _LOGGER.debug(
                "Net export price %.4f (raw=%.4f, fee=%.4f) is negative; "
                "blocking all grid export with %d W limit.",
                net_export_price,
                export_price,
                export_fee_per_kwh,
                desired,
            )
        else:
            _LOGGER.debug(
                "Export price %.4f is below export_electricity_min_price %.4f and "
                "hsem_curtail_pv_below_export_min_price is enabled; blocking all "
                "grid export with %d W limit.",
                export_price,
                min_price,
                desired,
            )
    else:
        grid_export_cap_kw = cfg.max_grid_export_power_kw
        if grid_export_cap_kw > 1e-9:
            # Respect the configured DNO/grid export limit as a hard cap.
            desired = int(round(grid_export_cap_kw * 1000.0))
            desired_is_watt = True
            _LOGGER.debug(
                "Net export price %.4f is non-negative; allowing export up to "
                "configured grid limit %d W (%.3f kW).",
                net_export_price,
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
        "Determined export power limit: %s%s (export=%s, net_export=%s, fee=%s, "
        "min=%s, ev1_connected=%s, ev2_connected=%s)",
        desired,
        "W" if desired_is_watt else "%",
        export_price,
        net_export_price,
        export_fee_per_kwh,
        min_price,
        live.ev.is_connected,
        live.ev_second.is_connected,
    )

    feedback_entity = cfg.huawei_solar_inverter_active_power_control
    feedback_state = live.huawei_inverter_active_power_control
    if feedback_entity and _is_power_measurement(feedback_state):
        # A power reading can never confirm a limit — treat it as no feedback.
        _warn_measurement_feedback_once(sensor, feedback_entity, feedback_state)
        feedback_entity = None

    current_pct = _parse_power_control_pct(feedback_state)
    current_is_watt = _is_watt_limit(feedback_state)
    target = (desired, desired_is_watt)
    # Write-and-verify compares desired and read-back with their unit, so
    # 100 W (the export block) never matches 100 % / "Unlimited" (issue #1130).
    desired_limit = _format_power_control_limit(desired, desired_is_watt)
    written_limits = _unverified_export_limits(sensor)

    for inv_id in _export_limit_device_ids(cfg):
        reader_fn: Callable[[], str | None] | None = None
        if feedback_entity:
            # Skip if the inverter already matches the desired state.
            if (
                current_pct is not None
                and current_is_watt == desired_is_watt
                and current_pct == desired
            ):
                continue
            reader_fn = lambda inv=feedback_entity: _parse_power_control_limit(
                sensor.hass.states.get(inv).state
                if inv and sensor.hass.states.get(inv) is not None
                else None
            )
        elif written_limits.get(inv_id) == target:
            # Nothing can read the limit back, so an accepted limit is only
            # rewritten when the target changes (issue #1120).
            continue

        result_label = feedback_entity or f"inverter:{inv_id}"
        if desired_is_watt:
            result = await async_write_and_verify(
                entity_id=result_label,
                desired=desired_limit,
                writer=lambda _id=inv_id, _w=desired: async_set_grid_export_power_watt(  # type: ignore[misc]  # mypy cannot infer lambda types with default parameters
                    sensor, _id, _w
                ),
                reader=reader_fn,
            )
        else:
            result = await async_write_and_verify(
                entity_id=result_label,
                desired=desired_limit,
                writer=lambda _id=inv_id, _pct=desired: (  # type: ignore[misc]  # mypy cannot infer lambda types with default parameters
                    async_set_grid_export_power_pct(sensor, _id, _pct)
                ),
                reader=reader_fn,
            )

        summary.results.append(result)

        if not feedback_entity:
            if result.status == ApplyStatus.UNVERIFIED:
                written_limits[inv_id] = target
            else:
                written_limits.pop(inv_id, None)

        if result.status == ApplyStatus.FAILED:
            if desired_is_watt:
                _warn_emma_p_max_once(sensor, result.error_message)
            mode = "W" if desired_is_watt else "%"
            _LOGGER.debug(
                "Export power %s write FAILED for inverter %s after all retries. "
                "Blocking further writes this cycle.",
                mode,
                inv_id,
            )
            return summary

    return summary


def _export_limit_device_ids(cfg: SensorConfig) -> list[str]:
    """Return the configured device IDs that receive the grid export limit.

    With an EMMA, ``huawei_solar`` accepts these services only for the EMMA
    device, so EMMA users select the EMMA as inverter 1 (issue #1120).
    """
    return [
        device_id
        for device_id in (
            cfg.huawei_solar_device_id_inverter_1,
            cfg.huawei_solar_device_id_inverter_2,
        )
        if device_id is not None
    ]


def _unverified_export_limits(
    sensor: Any,  # NOSONAR -- HA internal type; circular import risk
) -> dict[str, tuple[int, bool]]:
    """Return the limits accepted without read-back, per device.

    Latched on the sensor as ``_unverified_export_limits`` (``(value,
    is_watt)`` keyed by device ID) so an unverifiable limit is written once
    per change instead of every cycle.  It lives in memory only, so the
    limit is written again after a restart.
    """
    written = getattr(sensor, "_unverified_export_limits", None)
    if not isinstance(written, dict):
        written = {}
        sensor._unverified_export_limits = written
    return written


def _warn_measurement_feedback_once(
    sensor: Any,  # NOSONAR -- HA internal type; circular import risk
    entity_id: str,
    state: str | None,
) -> None:
    """Warn once per entity that the feedback entity reports a power reading.

    Latched on the sensor as ``_measurement_feedback_warned`` so the warning
    does not repeat every cycle.
    """
    if getattr(sensor, "_measurement_feedback_warned", None) == entity_id:
        return
    sensor._measurement_feedback_warned = entity_id
    _LOGGER.warning(
        "%s reports %s, a power reading rather than an active power control "
        "state, so grid export limit writes cannot be verified. Clear "
        "hsem_huawei_solar_inverter_active_power_control if your Huawei "
        "Solar setup has no active power control sensor (e.g. EMMA).",
        entity_id,
        state,
    )


def _warn_emma_p_max_once(
    sensor: Any,  # NOSONAR -- HA internal type; circular import risk
    error_message: str,
) -> None:
    """Tell the user to update Huawei Solar when an EMMA watt limit fails.

    Before 2.1.6, ``huawei_solar.set_maximum_feed_grid_power`` validated
    every request against the inverter-only ``P_MAX`` register, so on an EMMA
    it always failed with "Failed to read registers P_max" (issue #1131,
    fixed upstream in wlcrs/huawei_solar#1439).  The write stays ``FAILED``;
    this only explains why.  Latched on the sensor as ``_emma_p_max_warned``
    so it is logged once per session.
    """
    if "p_max" not in error_message.lower():
        return
    if getattr(sensor, "_emma_p_max_warned", False) is True:
        return
    sensor._emma_p_max_warned = True
    _LOGGER.warning(
        "Grid export limit (watts) failed because Huawei Solar could not read "
        "P_max. On an EMMA system this is a Huawei Solar bug fixed in 2.1.6: "
        "update the Huawei Solar integration to 2.1.6 or newer. Until then the "
        "negative-price export block and any hsem_max_grid_export_power_kw cap "
        "fail, and battery writes are skipped in those cycles. Last error: %s",
        error_message,
    )
