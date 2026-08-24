"""Slot population and live-data injection for the HSEM planner."""

from __future__ import annotations

from datetime import datetime

from custom_components.hsem.models.data_quality import DataQuality
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.time_series import TimeSeriesIndex
from custom_components.hsem.planner.slot_population import (
    populate_consumption,
    populate_prices,
    populate_solcast,
)
from custom_components.hsem.utils.datetime_utils import as_tz
from custom_components.hsem.utils.logger import log_planner


def _parse_now(now_iso: str) -> datetime:
    """Parse a timezone-aware ISO-8601 string."""
    dt = datetime.fromisoformat(now_iso)
    if dt.tzinfo is None:
        raise ValueError(f"now_iso must be timezone-aware, got: {now_iso!r}")
    return dt


def _populate_slots(
    slots: list,
    inp: PlannerInput,
    tsi: TimeSeriesIndex,
    warnings: list[str],
    missing_inputs: list[str],
) -> tuple[DataQuality, list[str], list[str]]:
    """Populate price/PV/consumption data, data-quality diagnostics, confidence decay."""
    log_planner(
        "debug",
        "[core] _populate_slots  slots=%d  interval=%dmin  horizon=%dh",
        len(slots),
        inp.interval_minutes,
        inp.interval_length_hours,
    )
    populate_prices(slots, inp.price_points, tsi)
    populate_solcast(
        slots,
        inp.solcast_slots,
        inp.interval_minutes,
        tsi,
        corrector=inp.solar_corrector,
    )
    populate_consumption(
        slots,
        inp.consumption_averages,
        inp.weight_1d,
        inp.weight_3d,
        inp.weight_7d,
        inp.weight_14d,
        inp.interval_minutes,
        tsi,
    )
    for hour in sorted(tsi.missing_hours()):
        missing_inputs.append(f"hour_{hour:02d}")
    horizon_has_tomorrow = tsi.has_tomorrow_slots()
    horizon_days = tsi.horizon_days
    dq = DataQuality(
        tomorrow_price_missing_hours=sorted(tsi.missing_future_day_price_hours(1)),
        tomorrow_pv_missing_hours=sorted(tsi.missing_future_day_pv_hours(1)),
        day2_price_missing_hours=sorted(tsi.missing_future_day_price_hours(2)),
        day2_pv_missing_hours=sorted(tsi.missing_future_day_pv_hours(2)),
        today_price_missing_hours=sorted(
            {
                m.hour
                for k in tsi.missing_price_slots
                if k.day_offset == 0
                for m in tsi.slots
                if m.key == k
            }
        ),
        today_pv_missing_hours=sorted(
            {
                m.hour
                for k in tsi.missing_pv_slots
                if k.day_offset == 0
                for m in tsi.slots
                if m.key == k
            }
        ),
        horizon_has_tomorrow=horizon_has_tomorrow,
        horizon_days=horizon_days,
    )
    for tm_label, tm_list in [
        ("tomorrow_price_missing_hours", dq.tomorrow_price_missing_hours),
        ("tomorrow_pv_missing_hours", dq.tomorrow_pv_missing_hours),
        ("day2_price_missing_hours", dq.day2_price_missing_hours),
        ("day2_pv_missing_hours", dq.day2_pv_missing_hours),
    ]:
        if tm_list:
            hs = ",".join(f"{h:02d}" for h in tm_list)
            missing_inputs.append(f"{tm_label}:{hs}")
            labels = {
                "tomorrow_price_missing_hours": "price",
                "tomorrow_pv_missing_hours": "PV",
                "day2_price_missing_hours": "price",
                "day2_pv_missing_hours": "PV",
            }
            warnings.append(
                f"{'Day+2' if tm_label.startswith('day2') else 'Tomorrow'} {labels[tm_label]} data missing for {len(tm_list)} hour(s): {hs}."
            )
    _DECAY_BY_DAY: dict[int, float] = {0: 1.00, 1: 0.90, 2: 0.80}
    if horizon_days > 1:
        for slot in slots:
            si = tsi.slot_index_for(slot.start)
            if si is None:
                continue
            do = tsi.slots[si].key.day_offset
            if do > 0:
                slot.solcast_pv_estimate_kwh = round(
                    slot.solcast_pv_estimate_kwh * _DECAY_BY_DAY.get(do, 0.80), 3
                )
        if horizon_days >= 2:
            warnings.append(
                f"Multi-day horizon ({horizon_days} day(s)): confidence decay applied to PV estimates."
            )
    log_planner(
        "debug",
        "[core] _populate_slots DONE  warnings=%d  missing=%d  horizon_days=%d  "
        "has_tomorrow=%s",
        len(warnings),
        len(missing_inputs),
        horizon_days,
        horizon_has_tomorrow,
    )

    return dq, warnings, missing_inputs


def _inject_live_data_into_current_slot(
    slots: list,
    inp: PlannerInput,
    now: datetime,
) -> None:
    """Replace forecast PV and consumption in the current slot with live measurements.

    The current (partially-elapsed) slot's ``solcast_pv_estimate_kwh`` and
    ``avg_house_consumption_kwh`` are overwritten with values derived from
    live power sensors.  This ensures the MILP and all candidates use
    measured (not forecast) data for the slot that is already in progress.

    The live power in Watts is converted to kWh by multiplying by the
    slot's full duration in hours.  This gives the *projected* full-slot
    energy if the current power level were sustained for the entire slot.
    The MILP's energy balance constraint then correctly accounts for the
    remaining portion of the slot.

    Args:
        slots: Fully populated slot list (after ``_populate_slots``).
        inp: Planner input containing live power readings.
        now: Timezone-aware current datetime.
    """
    slot_hours = inp.interval_minutes / 60.0

    for slot in slots:
        s_start = as_tz(slot.start, now.tzinfo)
        s_end = as_tz(slot.end, now.tzinfo)
        if s_start <= now < s_end:
            # Convert live Watts to projected full-slot kWh.
            if inp.live_solar_production_w > 1e-9:
                live_pv_kwh = (inp.live_solar_production_w / 1000.0) * slot_hours
                log_planner(
                    "debug",
                    "[core] _inject_live_data  slot=%s  "
                    "pv: forecast=%.3f → live=%.3f kWh",
                    slot.start.isoformat(),
                    slot.solcast_pv_estimate_kwh,
                    live_pv_kwh,
                )
                slot.solcast_pv_estimate_kwh = round(live_pv_kwh, 3)

            live_house_available = getattr(
                inp, "live_house_consumption_available", None
            )
            if live_house_available is None:
                live_house_available = inp.live_house_consumption_w > 1e-9
            if live_house_available:
                # When house power includes EV charger load, the live reading
                # may include EV power that the battery must not serve.
                #
                # Layer 1: if ev_session_charge_kw is available (EV actively
                # charging with a known power reading), subtract it directly.
                #
                # Layer 2: otherwise, if the live reading is dramatically
                # higher than the forecast (>3×), cap the injection at the
                # forecast value.  A 3× spike is unambiguous EV load — normal
                # house load does not triple between slots (issue #592).
                live_house_w = inp.live_house_consumption_w
                if inp.house_power_includes_ev:
                    ev_ac_w = 0.0
                    if (
                        inp.ev_session_charge_kw is not None
                        and inp.ev_session_charge_kw > 1e-9
                    ):
                        ev_ac_w += inp.ev_session_charge_kw * 1000.0
                    if (
                        inp.ev_second_session_charge_kw is not None
                        and inp.ev_second_session_charge_kw > 1e-9
                    ):
                        ev_ac_w += inp.ev_second_session_charge_kw * 1000.0
                    live_house_w = max(live_house_w - ev_ac_w, 0.0)

                live_load_kwh = (live_house_w / 1000.0) * slot_hours

                # If the live reading (after subtracting known EV power) still
                # exceeds the forecast by >3×, it likely contains unmeasured EV
                # load.  Cap at the forecast to prevent the battery from
                # serving unknown EV demand.  When the forecast is ~0 (e.g. a
                # night slot), the ratio test is degenerate — use an absolute
                # floor instead so a multi-kW EV spike is still capped.
                forecast = slot.avg_house_consumption_kwh
                cap_kwh = max(forecast * 3.0, 0.05)
                if inp.house_power_includes_ev and live_load_kwh > cap_kwh:
                    log_planner(
                        "debug",
                        "[core] _inject_live_data  slot=%s  "
                        "live %.3f kWh > cap %.3f kWh (3× forecast %.3f kWh) — "
                        "capping (probable unmeasured EV load)",
                        slot.start.isoformat(),
                        live_load_kwh,
                        cap_kwh,
                        forecast,
                    )
                    live_load_kwh = forecast if forecast > 1e-9 else cap_kwh

                log_planner(
                    "debug",
                    "[core] _inject_live_data  slot=%s  "
                    "load: forecast=%.3f → live=%.3f kWh",
                    slot.start.isoformat(),
                    slot.avg_house_consumption_kwh,
                    live_load_kwh,
                )
                slot.avg_house_consumption_kwh = round(live_load_kwh, 3)

            # Keep the historical sub-window averages intact.  The EV
            # discharge-cap fallback in ``applier.async_apply_battery_settings``
            # relies on the *minimum* of the 1d/3d/7d/14d windows to recover a
            # clean house baseline when the live reading is unreliable
            # (issue #592).  Overwriting them with the live-injected value
            # would destroy that fallback — the live value can still be
            # inflated by unmeasured EV load when no EV power sensor is
            # configured, so the historical windows are the only clean source.
            break
