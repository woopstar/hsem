"""Coordinator tracking functions extracted from :mod:`coordinator`.

Contains the forecast-vs-actual, daily plan-vs-actual, financial, and savings
tracking logic.  These are extracted as free functions that take the coordinator
state they need as parameters, making them independently unit-testable.

Extracted to keep :mod:`coordinator` under the 30 KB / 1000-line limit.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_change

from custom_components.hsem.models.daily_metrics import DailyMetrics
from custom_components.hsem.models.daily_plan_vs_actual_tracker import (
    DailyPlanVsActualTracker,
)
from custom_components.hsem.models.financial_tracker import FinancialTracker
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.models.savings_tracker import SavingsTracker
from custom_components.hsem.utils.datetime_utils import as_tz
from custom_components.hsem.utils.forecast_tracker import (
    ForecastTracker,
    compute_accumulated_energy,
)
from custom_components.hsem.utils.logger import async_log
from custom_components.hsem.utils.prediction_tracker import (
    PredictionTracker,
    _action_label,
)
from custom_components.hsem.utils.recommendations import CHARGE_RECS
from custom_components.hsem.utils.solar_corrector import SolarForecastCorrector

# ---------------------------------------------------------------------------
# Forecast-vs-actual accumulation (issue #373)
# ---------------------------------------------------------------------------


def accumulate_forecast_actuals(
    *,
    now: datetime,
    live: LiveState,
    hourly_recommendations: list[HourlyRecommendation],
    forecast_tracker: ForecastTracker,
    last_accumulation_ts: datetime | None,
    solar_corrector: SolarForecastCorrector,
    solar_corrector_processed: set[datetime],
    prediction_tracker: PredictionTracker,
    last_planner_output: PlannerOutput | None,
) -> tuple[datetime | None, bool]:
    """Accumulate actual PV and load energy into the current slot.

    Called every coordinator cycle to accumulate energy from instantaneous
    power readings.  Uses the elapsed time since the last accumulation to
    convert power (W) to energy (kWh).

    Args:
        now: Current time (timezone-aware).
        live: The live HA entity state snapshot.
        hourly_recommendations: Current hourly recommendations.
        forecast_tracker: The forecast-vs-actual tracker instance.
        last_accumulation_ts: Timestamp of the previous accumulation.
        solar_corrector: Solar forecast accuracy auto-corrector.
        solar_corrector_processed: Set of slot start times already fed to the
            solar corrector.
        prediction_tracker: Prediction accuracy tracker.
        last_planner_output: Most recent planner output, or None.

    Returns:
        The new ``last_accumulation_ts`` value (``now``).
    """
    # Compute elapsed seconds since last accumulation.
    if last_accumulation_ts is not None:
        elapsed = (now - last_accumulation_ts).total_seconds()
    else:
        elapsed = 0.0

    new_last_ts = now
    prediction_record_added = False

    if elapsed <= 0:
        return new_last_ts, prediction_record_added

    # Find the current slot's record.
    if not hourly_recommendations:
        return new_last_ts, prediction_record_added

    # Find the slot whose time range contains 'now'.
    current_slot = None
    for rec in hourly_recommendations:
        if as_tz(rec.start, now.tzinfo) <= now < as_tz(rec.end, now.tzinfo):
            current_slot = rec
            break

    if current_slot is None:
        return new_last_ts, prediction_record_added

    # Get or create the tracker record for this slot.
    tracker_rec = forecast_tracker.get_or_create_record(
        current_slot.start, current_slot.end
    )

    # Accumulate PV energy.
    pv_power_w = live.solar_production_power_w or 0.0
    pv_energy = compute_accumulated_energy(pv_power_w, elapsed)
    tracker_rec.accumulate_pv(pv_energy)

    # Accumulate load energy.
    load_power_w = live.house_consumption_power_w or 0.0
    load_energy = compute_accumulated_energy(load_power_w, elapsed)
    tracker_rec.accumulate_load(load_energy)

    # Finalise any slots whose end time has passed.
    forecast_tracker.finalise_past_records(now)

    # -------------------------------------------------------------------
    # Solar forecast auto-correction (issue #602)
    # -------------------------------------------------------------------
    # Feed every newly-finalised forecast tracker record into the solar
    # corrector so it can learn per-hour accuracy factors and update the
    # intra-hour residual buffer.
    for frec in forecast_tracker.records:
        if not frec.finalised:
            continue
        if frec.start in solar_corrector_processed:
            continue

        solar_corrector.update_hour(
            frec.start.hour, frec.forecast_pv_kwh, frec.actual_pv_kwh
        )
        solar_corrector.update_residual(frec.forecast_pv_kwh, frec.actual_pv_kwh)
        solar_corrector_processed.add(frec.start)

    # -------------------------------------------------------------------
    # Prediction accuracy scorecard (issue #601)
    # -------------------------------------------------------------------
    # Feed completed slots into the prediction accuracy tracker so the
    # sensor can report SoC MAE, solar MAPE, and action mix.
    if last_planner_output is not None:
        for frec in forecast_tracker.records:
            if not frec.finalised:
                continue
            # Find the matching planner slot for this forecast record.
            planner_slot = None
            for slot in last_planner_output.slots:
                if slot.start == frec.start:
                    planner_slot = slot
                    break
            if planner_slot is None:
                continue
            prediction_record_added = prediction_tracker.add_record(
                predicted_soc=planner_slot.estimated_battery_soc_pct,
                actual_soc=live.huawei_batteries_soc_pct or 0.0,
                predicted_pv=planner_slot.solcast_pv_estimate_kwh,
                actual_pv=frec.actual_pv_kwh,
                predicted_load=planner_slot.avg_house_consumption_kwh,
                actual_load=frec.actual_load_kwh,
                action=_action_label(planner_slot.recommendation),
                slot_start=frec.start,
            )

    return new_last_ts, prediction_record_added


async def init_prediction_tracker(
    tracker: PredictionTracker,
    hass: HomeAssistant,
) -> None:
    """Restore the bounded prediction scorecard history."""
    if tracker.history_file:
        return
    tracker.history_file = str(
        Path(hass.config.config_dir) / ".storage" / "hsem_prediction_history.json"
    )
    await tracker.load_history()


def register_forecasts_from_planner(
    output: PlannerOutput,
    forecast_tracker: ForecastTracker,
) -> None:
    """Register PV and load forecasts from planner output into the tracker.

    This is called after the planner runs successfully.  Forecast values
    are only set if the tracker record exists and is not yet finalised.

    Args:
        output: The :class:`~planner.engine.PlannerOutput` returned by the
            planner engine.
        forecast_tracker: The forecast-vs-actual tracker instance.
    """
    for slot in output.slots:
        pv_forecast = getattr(slot, "solcast_pv_estimate_kwh", 0.0)
        load_forecast = getattr(slot, "avg_house_consumption_kwh", 0.0)

        forecast_tracker.set_forecasts(
            start=slot.start,
            pv_kwh=pv_forecast,
            load_kwh=load_forecast,
        )


# ---------------------------------------------------------------------------
# Daily plan-vs-actual accumulation (issue #540)
# ---------------------------------------------------------------------------


async def accumulate_daily_plan_actuals(
    *,
    now: datetime,
    live: LiveState,
    output: PlannerOutput,
    daily_tracker: DailyPlanVsActualTracker,
    daily_plan_last_accumulated: datetime | None,
    hass: HomeAssistant,
) -> datetime | None:
    """Accumulate plan and actual values into the daily tracker.

    Plan side: sum planned import/export/cycle/PV from planner slots
    whose end time has passed.

    Actual side: use cumulative energy meter readings from live state,
    falling back to SoC-based cycle tracking when meters are unavailable.

    Args:
        now: Current datetime (timezone-aware).
        live: Live HA entity state snapshot.
        output: Planner output with slot-level decisions.
        daily_tracker: The daily plan-vs-actual tracker instance.
        daily_plan_last_accumulated: Marker from the previous accumulation.
        hass: Home Assistant instance for midnight timer registration.

    Returns:
        The new ``daily_plan_last_accumulated`` marker.
    """
    await _init_daily_tracker(daily_tracker, hass)
    tracker = daily_tracker

    # Check and handle day rollover first.
    await tracker.check_day_rollover(now)

    # ---- Plan accumulation ----
    # Accumulate plan values for the current in-progress slot (and any
    # completed slots that may have been missed).  The current slot's
    # plan values are captured before the SoC simulation zeroes them
    # on the next planner run.
    new_marker = _accumulate_plan_for_slots(
        tracker,
        output.slots,
        now,
        daily_plan_last_accumulated,
    )

    # ---- Actual accumulation ----
    # Use cumulative energy meter readings when available.
    # Battery cycle tracking uses SoC delta converted to kWh via rated capacity.
    soc_pct = live.huawei_batteries_soc_pct
    rated_cap_kwh = (live.huawei_batteries_rated_capacity_wh or 0.0) / 1000.0
    tracker.accumulate_actual(
        grid_import_energy_kwh=live.grid_import_energy_kwh,
        grid_export_energy_kwh=live.grid_export_energy_kwh,
        pv_energy_kwh=live.pv_energy_kwh,
        soc_pct=soc_pct,
        rated_capacity_kwh=rated_cap_kwh,
        import_price=live.import_electricity_price,
        export_price=live.export_electricity_price,
    )

    return new_marker


# ---------------------------------------------------------------------------
# Financial tracker accumulation (issue #599)
# ---------------------------------------------------------------------------


async def init_financial_tracker(
    financial_tracker: FinancialTracker,
    hass: HomeAssistant,
) -> None:
    """Lazily initialise the financial tracker.

    Called once on the first access.  Loads the JSON history file.
    Failures are logged and leave the tracker with an empty history
    file path so the sensors show 'no data' rather than crashing the
    coordinator.
    """
    if getattr(financial_tracker, "_initialized", False):
        return

    try:
        config_dir = hass.config.config_dir
        financial_tracker.history_file = str(
            Path(config_dir) / ".storage" / "hsem_financial_history.json"
        )
        await _load_financial_tracker(financial_tracker)
        financial_tracker._initialized = True  # type: ignore[attr-defined]
    except Exception:
        async_log(
            "error",
            "Failed to initialise financial tracker "
            "(financial sensors will be unavailable)",
        )
        financial_tracker._initialized = True  # type: ignore[attr-defined]


async def _load_financial_tracker(tracker: FinancialTracker) -> None:
    """Load financial tracker state from the JSON persistence file."""
    path = Path(tracker.history_file)
    if not path.exists():
        return
    try:
        data = await asyncio.to_thread(FinancialTracker._read_history_file, path)
        if data is not None:
            loaded = FinancialTracker.from_dict(data)
            # Copy loaded state into the existing tracker instance.
            tracker.import_cost_total = loaded.import_cost_total
            tracker.export_income_total = loaded.export_income_total
            tracker._today_start_import_cost = loaded._today_start_import_cost
            tracker._today_start_export_income = loaded._today_start_export_income
            tracker.today = loaded.today
            tracker._last_import_energy_kwh = loaded._last_import_energy_kwh
            tracker._last_export_energy_kwh = loaded._last_export_energy_kwh
            tracker._last_import_sample_at = loaded._last_import_sample_at
            tracker._last_export_sample_at = loaded._last_export_sample_at
            tracker._last_import_price = loaded._last_import_price
            tracker._last_export_price = loaded._last_export_price
            tracker.daily_log = loaded.daily_log
    except Exception:
        async_log("error", "Failed to load financial tracker history")


async def persist_financial_tracker(tracker: FinancialTracker) -> bool:
    """Persist financial tracker state to disk atomically."""
    if not tracker.history_file:
        return False
    data = tracker.as_dict()
    path = Path(tracker.history_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    return await asyncio.to_thread(FinancialTracker._write_history_file, data, path)


async def accumulate_financials(
    *,
    now: datetime,
    live: LiveState,
    financial_tracker: FinancialTracker,
    hass: HomeAssistant,
    update_interval_minutes: int,
) -> None:
    """Accumulate import cost and export income into the financial tracker.

    Called each coordinator cycle after plan-vs-actual accumulation.
    Handles day rollover (snapshotting yesterday's totals) before
    accumulating the live cost deltas from the energy meters.

    Args:
        now: Current datetime (timezone-aware).
        live: Live HA entity state snapshot.
        financial_tracker: The financial tracker instance.
        hass: Home Assistant instance.
    """
    await init_financial_tracker(financial_tracker, hass)
    tracker = financial_tracker

    had_baseline = (
        tracker._last_import_energy_kwh is not None
        or tracker._last_export_energy_kwh is not None
    )
    contiguous = tracker.accumulate(
        grid_import_energy_kwh=live.grid_import_energy_kwh,
        grid_export_energy_kwh=live.grid_export_energy_kwh,
        import_price=live.import_electricity_price,
        export_price=live.export_electricity_price,
        sample_time=now,
        max_gap_seconds=2.0 * max(float(update_interval_minutes) * 60.0, 60.0),
    )
    if not contiguous and had_baseline:
        async_log(
            "warning",
            "Financial meter baseline was stale; skipped replaying the gap.",
        )

    # Price the interval ending at midnight before rolling to the new day.
    tracker.check_day_rollover(now)

    if not await persist_financial_tracker(tracker):
        async_log("warning", "Failed to persist financial tracker state")


# ---------------------------------------------------------------------------
# Savings tracker accumulation (issue #604)
# ---------------------------------------------------------------------------


def _compute_daily_avg_import_price(output: PlannerOutput) -> float:
    """Compute the average import price for today from planner slots."""
    today_str = date.today().isoformat()
    prices: list[float] = []
    for slot in output.slots:
        slot_date = slot.start.strftime("%Y-%m-%d")
        if slot_date == today_str:
            p = getattr(slot, "import_price", None)
            if p is not None and p > 0:
                prices.append(float(p))
    if not prices:
        return 0.0
    return sum(prices) / len(prices)


async def accumulate_savings(
    *,
    now: datetime,
    live: LiveState,
    output: PlannerOutput,
    savings_tracker: SavingsTracker,
    daily_tracker: DailyPlanVsActualTracker,
    hourly_recommendation: HourlyRecommendation | None,
    hass: HomeAssistant,
) -> None:
    """Accumulate savings data for the current cycle.

    Computes export revenue delta, charge savings delta, and baseline
    cost delta from the daily tracker and planner output.

    Args:
        now: Current datetime (timezone-aware).
        live: Live HA entity state snapshot.
        output: Planner output with slot-level decisions.
        savings_tracker: The savings tracker instance.
        daily_tracker: The daily plan-vs-actual tracker instance.
        hourly_recommendation: The current slot's recommendation.
        hass: Home Assistant instance.
    """
    await _init_savings_tracker(savings_tracker, hass)
    st = savings_tracker
    dt = daily_tracker

    # Check day rollover first.
    today_str = now.date().isoformat()
    st.check_day_rollover(today_str)

    # ---- Compute per-cycle deltas from the daily tracker ----
    current_export_rev = dt.actual.grid_export_rev
    current_import_cost = dt.actual.grid_import_cost

    export_rev_delta = 0.0
    if st._last_export_rev is not None:
        export_rev_delta = max(0.0, current_export_rev - st._last_export_rev)
    st._last_export_rev = current_export_rev

    import_cost_delta = 0.0
    if st._last_import_cost is not None:
        import_cost_delta = max(0.0, current_import_cost - st._last_import_cost)
    st._last_import_cost = current_import_cost

    # ---- Charge savings: money saved by charging cheap now ----
    charge_savings_delta = 0.0
    import_price = live.import_electricity_price

    # Compute average daily import price from planner slots for today.
    avg_import_price = _compute_daily_avg_import_price(output)

    # Check if the current recommendation is a charge action.
    if (
        hourly_recommendation is not None
        and hourly_recommendation.recommendation in CHARGE_RECS
        and import_price < avg_import_price
        and avg_import_price > 0
    ):
        charge_kwh = hourly_recommendation.batteries_charged_kwh or 0.0
        if abs(charge_kwh) > 1e-9:
            charge_savings_delta = charge_kwh * (avg_import_price - import_price)

    # ---- Baseline cost: what passive mode would cost this cycle ----
    baseline_cost_delta = import_cost_delta

    # ---- Determine if the master switch is on ----
    switch_on = live.force_working_mode_state == "auto"

    st.accumulate(
        export_revenue_delta=export_rev_delta,
        charge_savings_delta=charge_savings_delta,
        baseline_cost_delta=baseline_cost_delta,
        switch_on=switch_on,
    )


async def _init_savings_tracker(
    tracker: SavingsTracker,
    hass: HomeAssistant,
) -> None:
    """Lazily initialise the savings tracker."""
    if getattr(tracker, "_initialized", False):
        return

    try:
        config_dir = hass.config.config_dir
        tracker.history_file = str(
            Path(config_dir) / ".storage" / "hsem_savings_history.json"
        )
        await tracker.load_history()
        tracker._initialized = True  # type: ignore[attr-defined]
    except Exception:
        async_log(
            "error",
            "Failed to initialise savings tracker (savings sensor will be unavailable)",
        )
        tracker._initialized = True  # type: ignore[attr-defined]


async def _init_daily_tracker(
    tracker: DailyPlanVsActualTracker,
    hass: HomeAssistant,
) -> None:
    """Lazily initialise the daily plan-vs-actual tracker.

    Called once on the first access.  Registers the midnight timer
    and loads the history file.  Failures are logged and leave the
    tracker with an empty history file path so the sensor shows
    'no data' rather than crashing the coordinator.
    """
    if getattr(tracker, "_initialized", False):
        return

    try:
        config_dir = hass.config.config_dir
        tracker.history_file = str(
            Path(config_dir) / ".storage" / "hsem_daily_history.json"
        )
        await tracker.load_history()

        tracker._midnight_unsub = async_track_time_change(  # type: ignore[attr-defined]
            hass,
            lambda _now: _async_handle_midnight(tracker, hass),
            hour=0,
            minute=0,
            second=0,
        )
        tracker._initialized = True  # type: ignore[attr-defined]
    except Exception:
        async_log(
            "error",
            "Failed to initialise daily tracker (plan-vs-actual "
            "sensor will be unavailable)",
        )
        tracker._initialized = True  # type: ignore[attr-defined]


async def _async_handle_midnight(
    tracker: DailyPlanVsActualTracker,
    hass: HomeAssistant,
) -> None:
    """Handle the midnight timer — persist the day's record and reset.

    This is called by the HA time-change listener at 00:00:00 local time.
    Saves yesterday's record, resets accumulators, and updates today's date
    so the next update cycle does not double-save.
    """
    if tracker.history_file:
        today_record = tracker._build_today_record()
        saved = await tracker._save_record_to_history(today_record)
        if saved:
            async_log(
                "info",
                "Daily plan-vs-actual record saved for %s",
                tracker.today,
            )
        else:
            async_log(
                "warning",
                "Failed to save daily plan-vs-actual record for %s",
                tracker.today,
            )

        # Reset accumulators for the new day so check_day_rollover()
        # does not double-save on the next cycle.
        tracker.today = date.today().isoformat()
        tracker.actual = DailyMetrics()
        tracker.plan = DailyMetrics()
        tracker.last_soc_pct = None
        tracker._last_import_energy_kwh = None
        tracker._last_export_energy_kwh = None
        tracker._last_pv_energy_kwh = None


# ---------------------------------------------------------------------------
# Module-level helpers for daily plan-vs-actual accumulation
# ---------------------------------------------------------------------------


def _accumulate_plan_for_slots(
    tracker: DailyPlanVsActualTracker,
    slots: list,
    now: datetime,
    last_accumulated: datetime | None,
) -> datetime | None:
    """Accumulate plan values for the current in-progress slot.

    Accumulates the FULL plan value for each slot exactly once, on the
    first cycle where the slot is the current in-progress slot
    (``start <= now < end``).  This captures the plan as it was when
    the slot started, before the SoC simulation zeroes the plan fields
    for past slots on subsequent planner runs.

    Completed past slots are also handled as a safety net for slots
    that may become past between cycles (e.g. after a coordinator
    restart).

    Returns:
        The accumulation marker (start of the current slot if it was
        just accumulated, or the last_accumulated value unchanged).
    """
    for slot in slots:
        slot_start = as_tz(slot.start, now.tzinfo) if hasattr(slot, "start") else None
        slot_end = as_tz(slot.end, now.tzinfo) if hasattr(slot, "end") else None

        # Current in-progress slot: accumulate full plan on first encounter.
        if (
            slot_start is not None
            and slot_end is not None
            and slot_start <= now < slot_end
        ):
            if last_accumulated is None or last_accumulated < slot_start:
                _add_slot_to_tracker(tracker, slot, fraction=1.0)
                return slot_start  # Mark this slot as accumulated
            return last_accumulated  # Already accumulated this slot

        # Safety net: completed past slots that may not have been
        # accumulated yet.  Only active after the first cycle (when
        # last_accumulated is not None) to avoid inflating plan values
        # with stale zeroed fields from past slots on startup.
        if last_accumulated is not None and slot_end is not None and slot_end <= now:
            # Use slot_start in the skip-check because last_accumulated
            # is now a slot-start marker (set by the current-slot branch).
            if slot_start is not None and slot_start <= last_accumulated:
                continue
            _add_slot_to_tracker(tracker, slot, fraction=1.0)

    # If no current slot was found, return the end of the last completed
    # slot as the marker (prevents re-accumulation of past slots).
    return _last_completed_slot_end(slots, now) or last_accumulated


def _add_slot_to_tracker(
    tracker: DailyPlanVsActualTracker,
    slot: object,
    fraction: float = 1.0,
) -> None:
    """Add a single slot's plan values to the tracker, scaled by *fraction*."""
    gi = (getattr(slot, "grid_import_kwh", 0.0) or 0.0) * fraction
    ge = (getattr(slot, "grid_export_kwh", 0.0) or 0.0) * fraction
    chg = (getattr(slot, "batteries_charged_kwh", 0.0) or 0.0) * fraction
    dis = (getattr(slot, "batteries_discharged_kwh", 0.0) or 0.0) * fraction
    pv = (getattr(slot, "solcast_pv_estimate_kwh", 0.0) or 0.0) * fraction
    slot_price = getattr(slot, "price", None)
    import_price = slot_price.import_price if slot_price is not None else 0.0
    export_price = slot_price.export_price if slot_price is not None else 0.0
    cycle_kwh = abs(chg) + abs(dis)
    tracker.accumulate_plan(
        grid_import_kwh=gi,
        grid_export_kwh=ge,
        cycle_kwh=cycle_kwh,
        pv_kwh=pv,
        import_price=import_price,
        export_price=export_price,
    )


def _last_completed_slot_end(slots: list, now: datetime) -> datetime | None:
    """Return the end time of the most recent completed slot, or None."""
    last_end: datetime | None = None
    for slot in slots:
        slot_end = as_tz(slot.end, now.tzinfo) if hasattr(slot, "end") else None
        if slot_end is not None and slot_end <= now:
            if last_end is None or slot_end > last_end:
                last_end = slot_end
    return last_end
