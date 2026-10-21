"""Current-slot EV overrides applied after the planner and the load hold.

Force charge now, auto-full on a negative price and the minimum-SoC charge
all rewrite the current slot's EV command through
:func:`apply_current_ev_power_override`. :func:`apply_force_charge_overrides`
is the single entry point both the planner phase and the non-planner
load-forecast hold call, *after* the hold, so no user override is wiped by it
(issues #1103, #1323, #1325).

Split out of :mod:`coordinator_helpers` to keep it under the 30 KiB limit.
"""

from __future__ import annotations

import math
from datetime import datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from custom_components.hsem.coordinator_helpers import (
    ev_site_power_budget_w,
    write_ev_slot_commands,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.planner.ev_planner import (
    EVChargingPlan,
    rebuild_ev_plan_from_slots,
)
from custom_components.hsem.utils.conversion import convert_to_float
from custom_components.hsem.utils.datetime_utils import slot_contains
from custom_components.hsem.utils.ev_accounting import normalized_baseline_includes_ev
from custom_components.hsem.utils.logger import async_log
from custom_components.hsem.utils.misc import get_config_value
from custom_components.hsem.utils.phase_power import (
    charger_rated_power_w,
    normalize_ev_phase_topology,
)
from custom_components.hsem.utils.recommendations import Recommendations
from custom_components.hsem.utils.units import slot_duration_hours


def _configured_charger_rated_power_w(config_entry: ConfigEntry, prefix: str) -> float:
    """Return one charger's whole-amp nameplate power from its config.

    ``prefix`` is ``hsem_ev_planned_load`` or ``hsem_ev_second_planned_load``.
    """
    power_kw = max(
        float(get_config_value(config_entry, f"{prefix}_charger_power_kw") or 0.0),
        0.0,
    )
    topology = normalize_ev_phase_topology(
        get_config_value(config_entry, f"{prefix}_charger_phase_topology")
    )
    return charger_rated_power_w(power_kw * 1000.0, topology)


def apply_current_ev_power_override(
    *,
    config_entry: ConfigEntry,
    hourly_recommendations: list[HourlyRecommendation],
    ev_plan: EVChargingPlan | None,
    ev_second_plan: EVChargingPlan | None,
    now: datetime,
    override_primary: bool,
    override_second: bool,
    live: LiveState | None = None,
) -> None:
    """Apply a fuse-safe current EV request with coherent slot accounting."""
    if not override_primary and not override_second:
        return
    slot = next(
        (
            item
            for item in hourly_recommendations
            if slot_contains(item.start, item.end, now)
        ),
        None,
    )
    if slot is None:
        return
    remaining_hours = slot_duration_hours(max(now, slot.start), slot.end)
    if remaining_hours <= 1e-9:
        return

    old_planned_ev_kwh = max(float(slot.ev_planned_load_kwh), 0.0)
    primary_w = max(float(slot.ev_charger_calculated_power), 0.0)
    second_w = max(float(slot.ev_second_charger_calculated_power), 0.0)
    primary_max_w = _configured_charger_rated_power_w(
        config_entry, "hsem_ev_planned_load"
    )
    second_max_w = _configured_charger_rated_power_w(
        config_entry, "hsem_ev_second_planned_load"
    )
    if live is not None:
        if override_primary and live.ev.is_connected is False:
            primary_max_w = 0.0
        if override_second and live.ev_second.is_connected is False:
            second_max_w = 0.0

    budget_w = ev_site_power_budget_w(config_entry, live)

    if not override_primary:
        budget_w = max(budget_w - primary_w, 0.0)
    else:
        primary_w = min(primary_max_w, budget_w)
        budget_w = max(budget_w - primary_w, 0.0)
    if not override_second:
        budget_w = max(budget_w - second_w, 0.0)
    else:
        second_w = min(second_max_w, budget_w)

    write_ev_slot_commands(
        slot,
        primary_w=primary_w,
        second_w=second_w,
        remaining_hours=remaining_hours,
        old_planned_ev_kwh=old_planned_ev_kwh,
        primary_base_load_includes_ev=normalized_baseline_includes_ev(
            raw_house_meter_includes_ev=bool(
                get_config_value(
                    config_entry, "hsem_house_power_includes_ev_charger_power"
                )
            ),
            ev_power_entity=get_config_value(config_entry, "hsem_ev_charger_power"),
        ),
        second_base_load_includes_ev=normalized_baseline_includes_ev(
            raw_house_meter_includes_ev=bool(
                get_config_value(
                    config_entry, "hsem_house_power_includes_ev_charger_power"
                )
            ),
            ev_power_entity=get_config_value(
                config_entry, "hsem_ev_second_charger_power"
            ),
        ),
    )
    if primary_w > 1e-9 or second_w > 1e-9:
        slot.recommendation = Recommendations.EVSmartCharging.value

    for plan, is_second, efficiency_key in (
        (ev_plan, False, "hsem_ev_planned_load_charger_efficiency"),
        (ev_second_plan, True, "hsem_ev_second_planned_load_charger_efficiency"),
    ):
        if plan is None:
            continue
        rebuilt = rebuild_ev_plan_from_slots(
            plan,
            hourly_recommendations,
            now,
            float(get_config_value(config_entry, efficiency_key) or 100.0),
            is_second=is_second,
        )
        plan.__dict__.update(rebuilt.__dict__)


def apply_force_charge_now(
    *,
    config_entry: ConfigEntry,
    hourly_recommendations: list[HourlyRecommendation],
    ev_plan: EVChargingPlan | None,
    ev_second_plan: EVChargingPlan | None,
    now: datetime,
    live: LiveState | None = None,
) -> None:
    """Apply the force-charge-now override to the current slot.

    When the user toggles ``hsem_ev_force_charge_now`` (or the second-EV
    equivalent), the current slot's recommendation is overridden to
    ``ev_smart_charging`` and the calculated charger power is set to the
    charger's maximum AC power.

    Crucially, force-charge works **even when smart charging is disabled**.
    The EV planner returns ``smart_charging_disabled`` with zero allocated
    power in that case, so this function also flips the plan state to
    ``charging`` so the plan sensor reflects the forced charge.

    Args:
        config_entry: The HSEM config entry (to read the force-charge switches).
        hourly_recommendations: The list of hourly recommendations to modify.
        ev_plan: The primary EV charging plan (may be ``None``).
        ev_second_plan: The second EV charging plan (may be ``None``).
        now: Current time (timezone-aware), used to locate the current slot.
    """
    force_primary = bool(get_config_value(config_entry, "hsem_ev_force_charge_now"))
    force_second = bool(
        get_config_value(config_entry, "hsem_ev_second_force_charge_now")
    )
    apply_current_ev_power_override(
        config_entry=config_entry,
        hourly_recommendations=hourly_recommendations,
        ev_plan=ev_plan,
        ev_second_plan=ev_second_plan,
        now=now,
        override_primary=force_primary,
        override_second=force_second,
        live=live,
    )


def reset_force_charge_on_disconnect(
    *,
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    was_connected: bool | None,
    is_connected: bool | None,
    option_key: str,
    ev_label: str,
) -> bool:
    """Clear a force-charge-now option on a connected → disconnected transition.

    ``apply_current_ev_power_override`` already zeroes the forced charger
    power once an EV is disconnected, but leaves the switch itself (and its
    config-entry option) on — so forced full-power charging silently re-arms
    the moment the EV reconnects. This clears the option so the switch
    entity (via its own config-update listener) reflects ``off`` and the
    user must explicitly re-enable it.

    Args:
        hass: The Home Assistant instance, needed to persist the option.
        config_entry: The HSEM config entry backing the force-charge switch.
        was_connected: EV connection state as of the last accepted plan.
        is_connected: EV connection state observed this cycle.
        option_key: The force-charge-now option key to reset
            (``hsem_ev_force_charge_now`` or ``hsem_ev_second_force_charge_now``).
        ev_label: Human-readable EV label, used only in the log message.

    Returns:
        ``True`` if the option was reset, ``False`` for a no-op (no
        disconnect transition, or the option was already ``False``).
    """
    if was_connected is not True or is_connected is not False:
        return False
    if not bool(get_config_value(config_entry, option_key)):
        return False

    hass.config_entries.async_update_entry(
        config_entry,
        options={**config_entry.options, option_key: False},
    )
    async_log(
        "info",
        "[ev] %s disconnected while force-charge-now was on — "
        "automatically resetting %s to off.",
        ev_label,
        option_key,
    )
    return True


def apply_negative_price_auto_full(
    *,
    config_entry: ConfigEntry,
    hourly_recommendations: list[HourlyRecommendation],
    ev_plan: EVChargingPlan | None,
    ev_second_plan: EVChargingPlan | None,
    now: datetime,
    live: LiveState,
) -> None:
    """Charge the primary EV at full power while the import price is <= 0.

    Applies ``hsem_ev_auto_full_negative_price`` (issue #609). The state
    collector stores ``0.0`` for an unavailable import price, so the override
    fires only on a price the sensor actually reported (issue #1325); an
    unavailable reading never commands full power.

    Args:
        config_entry: The HSEM config entry (to read the auto-full switch).
        hourly_recommendations: The list of hourly recommendations to modify.
        ev_plan: The primary EV charging plan (may be ``None``).
        ev_second_plan: The second EV charging plan (may be ``None``).
        now: Current time (timezone-aware), used to locate the current slot.
        live: Live state supplying the import price and its availability.
    """
    if not bool(get_config_value(config_entry, "hsem_ev_auto_full_negative_price")):
        return
    if not live.import_electricity_price_available:
        async_log(
            "debug",
            "[ev] Auto-full skipped: the import price is unavailable.",
        )
        return
    if live.import_electricity_price > 0.0:
        return
    apply_current_ev_power_override(
        config_entry=config_entry,
        hourly_recommendations=hourly_recommendations,
        ev_plan=ev_plan,
        ev_second_plan=ev_second_plan,
        now=now,
        override_primary=True,
        override_second=False,
        live=live,
    )


def _min_soc_charge_due(
    config_entry: ConfigEntry, live: LiveState, *, is_second: bool
) -> bool:
    """Return whether one EV is below its enabled minimum-SoC threshold.

    Never fires on an unknown SoC (issue #988), on an explicitly disconnected
    EV, or without the planned-load feature that supplies the charger
    nameplate the override commands.
    """
    prefix = "hsem_ev_second" if is_second else "hsem_ev"
    if not bool(get_config_value(config_entry, f"{prefix}_min_soc_charge_enabled")):
        return False
    label = "EV2" if is_second else "EV1"
    reason: str | None = None
    soc = live.ev_planning_soc_pct(is_second=is_second)
    threshold = convert_to_float(
        get_config_value(config_entry, f"{prefix}_min_soc_charge_soc")
    )
    if not bool(get_config_value(config_entry, f"{prefix}_planned_load_enabled")):
        reason = "planned load is disabled"
    elif (live.ev_second if is_second else live.ev).is_connected is False:
        reason = "the EV is disconnected"
    elif soc is None or not math.isfinite(soc):
        reason = "the SoC is unknown"
    elif threshold is None:
        reason = "no threshold is set"
    elif soc >= threshold:
        reason = f"SoC {soc:.1f} % is at or above {threshold:.0f} %"
    if reason is not None:
        async_log("debug", "[ev] %s minimum-SoC charge idle: %s.", label, reason)
        return False
    async_log(
        "debug",
        "[ev] %s minimum-SoC charge: SoC %.1f %% is below %.0f %%.",
        label,
        soc,
        threshold,
    )
    return True


def apply_min_soc_charge(
    *,
    config_entry: ConfigEntry,
    hourly_recommendations: list[HourlyRecommendation],
    ev_plan: EVChargingPlan | None,
    ev_second_plan: EVChargingPlan | None,
    now: datetime,
    live: LiveState,
) -> None:
    """Charge each EV at full power while it is below its minimum SoC.

    Applies ``hsem_ev_min_soc_charge_enabled`` / ``hsem_ev_min_soc_charge_soc``
    and the second-EV equivalents (issue #1323), whatever the price, deadline,
    target SoC or smart-charging switch. The override stops on the first
    cycle the SoC (delivered-energy credited, issue #789) reaches the
    threshold, and the normal plan takes over again.

    Args:
        config_entry: The HSEM config entry (to read the options).
        hourly_recommendations: The list of hourly recommendations to modify.
        ev_plan: The primary EV charging plan (may be ``None``).
        ev_second_plan: The second EV charging plan (may be ``None``).
        now: Current time (timezone-aware), used to locate the current slot.
        live: Live state supplying each EV's SoC and connection.
    """
    apply_current_ev_power_override(
        config_entry=config_entry,
        hourly_recommendations=hourly_recommendations,
        ev_plan=ev_plan,
        ev_second_plan=ev_second_plan,
        now=now,
        override_primary=_min_soc_charge_due(config_entry, live, is_second=False),
        override_second=_min_soc_charge_due(config_entry, live, is_second=True),
        live=live,
    )


def apply_force_charge_overrides(
    *,
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    hourly_recommendations: list[HourlyRecommendation],
    ev_plan: EVChargingPlan | None,
    ev_second_plan: EVChargingPlan | None,
    now: datetime,
    live: LiveState,
    was_connected: bool | None,
    was_second_connected: bool | None,
) -> None:
    """Auto-reset force-charge-now on disconnect, then apply the overrides.

    Canonical entry point for both the planner phase and the load-forecast
    hold path. Force charge, auto-full and the minimum-SoC charge are
    explicit user overrides, so they must run *after*
    :func:`apply_load_forecast_hold` (issues #1103, #1323, #1325): the strict
    hold clears primary-storage motion and the planned EV command, and this
    then re-publishes only the overridden EV commands. The home battery stays
    held — its charge/discharge energy remains zero.

    The disconnect reset runs first (issue #900) so a disconnect and reset in
    the same cycle never leaves a stale forced-charge slot. Every override
    sets the same fuse-limited nameplate, so enabling several gives the same
    command as any one of them alone.
    """
    for previous, current, option_key, ev_label in (
        (was_connected, live.ev.is_connected, "hsem_ev_force_charge_now", "EV1"),
        (
            was_second_connected,
            live.ev_second.is_connected,
            "hsem_ev_second_force_charge_now",
            "EV2",
        ),
    ):
        reset_force_charge_on_disconnect(
            hass=hass,
            config_entry=config_entry,
            was_connected=previous,
            is_connected=current,
            option_key=option_key,
            ev_label=ev_label,
        )
    apply_negative_price_auto_full(
        config_entry=config_entry,
        hourly_recommendations=hourly_recommendations,
        ev_plan=ev_plan,
        ev_second_plan=ev_second_plan,
        now=now,
        live=live,
    )
    apply_min_soc_charge(
        config_entry=config_entry,
        hourly_recommendations=hourly_recommendations,
        ev_plan=ev_plan,
        ev_second_plan=ev_second_plan,
        now=now,
        live=live,
    )
    apply_force_charge_now(
        config_entry=config_entry,
        hourly_recommendations=hourly_recommendations,
        ev_plan=ev_plan,
        ev_second_plan=ev_second_plan,
        now=now,
        live=live,
    )
