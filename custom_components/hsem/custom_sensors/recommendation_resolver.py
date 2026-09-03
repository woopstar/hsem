"""Recommendation resolver for HSEMWorkingModeSensor.

Single responsibility: apply post-planner adjustments to the **current**
time-slot recommendation based on real-time state that the planner engine
cannot observe (e.g. live EV charging status, remaining battery versus
upcoming scheduled discharge windows).

This module is purely decisional — no I/O, no hardware writes.
"""

from __future__ import annotations

from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.conversion import convert_to_float
from custom_components.hsem.utils.logger import HSEM_LOGGER
from custom_components.hsem.utils.recommendations import Recommendations


def _fmt_live_w(power_w: float | None) -> str:
    """Format a live power reading for log lines (``None`` → ``n/a``)."""
    if power_w is None:
        return "n/a"
    return f"{int(power_w)}W"


def resolve_current_recommendation(
    rec: HourlyRecommendation,
    live: LiveState,
    cfg: SensorConfig,
) -> None:
    """Adjust the current-interval recommendation based on live runtime state.

    The planner engine produces recommendations using static forecasts and
    cannot know, for example, whether a car just plugged in.  This function
    applies the final layer of real-time overrides in priority order:

    1. **Negative import price** → force export, but only when exporting is
       itself economically viable and permitted by the user's configuration.
    2. **Grid charge active** → grid charging takes priority over EV smart charge.
    3. **EV actively charging** → switch to EV smart charging mode.

    The recommendation is modified **in-place** on ``rec``.

    Args:
        rec: The :class:`HourlyRecommendation` for the current time slot.
        live: Live state snapshot at call time.
        cfg: Current sensor configuration (excess-export toggle and export
            price floors).
    """
    if rec is None:
        return

    original_recommendation = rec.recommendation

    # 1. Negative import price → force export.
    #
    # A negative import price alone does not make exporting profitable — the
    # export price can be negative too (issue #732).  Only override when the
    # user has opted into excess battery export AND the live export price is
    # authoritative AND non-negative AND at/above the configured floor
    # (the higher of the general and battery-specific export minimums).
    import_price = convert_to_float(live.import_electricity_price)
    export_price = convert_to_float(live.export_electricity_price)
    export_floor = max(
        cfg.export_electricity_min_price, cfg.batteries_export_min_price, 0.0
    )
    export_is_profitable = (
        live.export_electricity_price_available
        and export_price is not None
        and export_price >= export_floor
    )

    if import_price is not None and import_price < 0:
        if cfg.batteries_enable_excess_export and export_is_profitable:
            rec.recommendation = Recommendations.ForceExport.value
            HSEM_LOGGER.debug(
                "[resolver] negative import price (%.4f) with profitable export "
                "(%.4f >= floor %.4f) → overriding %s to force_export",
                import_price,
                export_price,
                export_floor,
                original_recommendation,
            )
            return
        HSEM_LOGGER.debug(
            "[resolver] negative import price (%.4f) but export not viable "
            "(excess_export_enabled=%s export_price=%s export_available=%s "
            "floor=%.4f) → not forcing export",
            import_price,
            cfg.batteries_enable_excess_export,
            export_price,
            live.export_electricity_price_available,
            export_floor,
        )

    # 2. Grid charging in progress → preserve, do not override
    if rec.recommendation == Recommendations.BatteriesChargeGrid.value:
        HSEM_LOGGER.debug(
            "[resolver] batteries_charge_grid active → keeping recommendation unchanged"
        )
        return

    # 3. Any EV is actively charging AND the planner allocated EV load for
    #    this slot → override with EV smart charging.
    #
    # The planner's ``ev_charger_calculated_power`` is HSEM's *command* to the
    # charger, not a reflection of what the charger is doing.  If the planner
    # set it to 0, that means "stop charging" (e.g. target SoC reached, no
    # surplus PV, expensive grid power).  In that case we must NOT override
    # the recommendation to ``ev_smart_charging`` — the planner's original
    # recommendation (e.g. ``batteries_wait_mode``) should stand.
    #
    # We only override when the planner actually allocated EV load for this
    # slot (``ev_charger_calculated_power > 0`` or ``ev_total_planned_load_kwh > 0``).
    planner_allocated_ev = (
        rec.ev_charger_calculated_power > 1e-9
        or rec.ev_second_charger_calculated_power > 1e-9
        or rec.ev_total_planned_load_kwh > 1e-9
    )
    ev_actively_charging = live.ev.is_charging or live.ev_second.is_charging

    if ev_actively_charging and planner_allocated_ev:
        rec.recommendation = Recommendations.EVSmartCharging.value
        HSEM_LOGGER.debug(
            "[resolver] EV actively charging + planner_allocated_ev=True "
            "(planned_ev_power=%dW planned_ev2_power=%dW ev_total_load=%.3fkWh "
            "live_ev_power=%s live_ev2_power=%s) "
            "→ overriding %s to ev_smart_charging",
            rec.ev_charger_calculated_power,
            rec.ev_second_charger_calculated_power,
            rec.ev_total_planned_load_kwh,
            _fmt_live_w(live.ev.power_w),
            _fmt_live_w(live.ev_second.power_w),
            original_recommendation,
        )
        return

    if ev_actively_charging and not planner_allocated_ev:
        HSEM_LOGGER.debug(
            "[resolver] EV actively charging but planner_allocated_ev=False "
            "(planned_ev_power=%dW planned_ev2_power=%dW ev_total_load=%.3fkWh "
            "live_ev_power=%s live_ev2_power=%s) "
            "→ keeping original recommendation %s",
            rec.ev_charger_calculated_power,
            rec.ev_second_charger_calculated_power,
            rec.ev_total_planned_load_kwh,
            _fmt_live_w(live.ev.power_w),
            _fmt_live_w(live.ev_second.power_w),
            original_recommendation,
        )
