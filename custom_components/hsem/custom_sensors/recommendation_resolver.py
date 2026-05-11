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
from custom_components.hsem.utils.misc import convert_to_float
from custom_components.hsem.utils.recommendations import Recommendations


def resolve_current_recommendation(
    rec: HourlyRecommendation,
    live: LiveState,
    batteries_schedules_remaining_capacity_needed: float,
) -> None:
    """Adjust the current-interval recommendation based on live runtime state.

    The planner engine produces recommendations using static forecasts and
    cannot know, for example, whether a car just plugged in.  This function
    applies the final layer of real-time overrides in priority order:

    1. **Negative import price** → force export everything to earn money.
    2. **Grid charge active** → grid charging takes priority over EV smart charge.
    3. **EV actively charging** → switch to EV smart charging mode.
    4. **Battery above remaining schedule need** → switch to discharge mode.

    The recommendation is modified **in-place** on ``rec``.

    Args:
        rec: The :class:`HourlyRecommendation` for the current time slot.
        live: Live state snapshot at call time.
        batteries_schedules_remaining_capacity_needed: Total kWh still needed
            by all upcoming discharge-window schedules.
    """
    if rec is None:
        return

    # 1. Negative import price → force export
    if convert_to_float(live.energi_data_service_import_price) < 0:
        rec.recommendation = Recommendations.ForceExport.value
        return

    # 2. Grid charging in progress → preserve, do not override
    if rec.recommendation == Recommendations.BatteriesChargeGrid.value:
        return

    # 3. Any EV is actively charging → override with EV smart charging
    if live.ev.is_charging or live.ev_second.is_charging:
        rec.recommendation = Recommendations.EVSmartCharging.value
        return

    # 4. Battery has enough energy to cover remaining scheduled discharge needs
    if (
        batteries_schedules_remaining_capacity_needed > 0
        and live.battery_current_capacity_kwh
        > batteries_schedules_remaining_capacity_needed
    ):
        rec.recommendation = Recommendations.BatteriesDischargeMode.value
        return
