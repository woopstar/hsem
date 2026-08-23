"""Discharge-cap and export-decision helpers for the applier.

Extracted from ``applier.py`` to satisfy the repository's 30 KB /
1000-line file limit. Pure move: no behaviour change.
"""

from __future__ import annotations

from typing import Any

from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig


def _fmt_live_power_w(power_w: float | None) -> str:
    """Format a live power reading for log lines (``None`` → ``n/a``)."""
    if power_w is None:
        return "n/a"
    return f"{int(power_w)} W"


def _configured_battery_device_ids(cfg: SensorConfig) -> list[str]:
    """Return configured battery device IDs, preserving order and uniqueness."""
    device_ids: list[str] = []
    for device_id in (
        cfg.huawei_solar_device_id_batteries,
        cfg.huawei_solar_device_id_batteries_2,
    ):
        if device_id and device_id not in device_ids:
            device_ids.append(device_id)
    return device_ids


def _wait_mode_self_consumption_cap_w(
    battery_capacity_kwh: float,
    required_capacity_kwh: float,
    slot_hours: float,
    max_discharge_power_w: int,
) -> int:
    """Return the discharge power cap for wait-mode self-consumption.

    When the battery holds more energy than the planner has reserved for future
    expensive periods, the surplus may be used for normal household
    self-consumption.  The cap is the power required to consume exactly that
    surplus over the slot duration; the inverter will only draw what the house
    actually needs, so the battery will not discharge faster than the surplus
    allows.

    Args:
        battery_capacity_kwh: Current usable battery energy above the discharge
            floor (kWh).
        required_capacity_kwh: Energy the planner has reserved for future use
            (kWh).
        slot_hours: Duration of the current recommendation slot in hours.
        max_discharge_power_w: Maximum discharge power supported by the battery
            pack (W).

    Returns:
        Discharge power cap in watts.  ``0`` when there is no surplus above the
        reserve or the slot duration is invalid.
    """
    surplus_kwh = max(battery_capacity_kwh - required_capacity_kwh, 0.0)
    if surplus_kwh <= 1e-9 or slot_hours <= 1e-9:
        return 0
    cap_w = int(surplus_kwh / slot_hours * 1000.0)
    return min(cap_w, max_discharge_power_w)


def compute_ev_discharge_cap_w(
    *,
    live_net_w: float | None,
    ev_power_available: bool,
    historical_w: int,
    sub_window_ws: list[int],
) -> int:
    """Compute the EV discharge cap in Watts (pure function, unit-testable).

    The cap limits battery discharge to the house-only load while an EV is
    charging, so 100 % of the EV load goes to the grid.

    Selection rules:

    - **Live reading available** (EV power sensor present): the historical
      baseline is the stable reference — the cap **is** the baseline.  The
      live reading (``house_w − ev_w``) must not move the cap in either
      direction: downward it ratchets toward zero when the CT clamp and the
      EV sensor disagree (beta8: 363→40 W staircase), and upward it swings
      with ordinary house noise (cooking, heat pump cycles), slowly
      draining the battery into what is supposed to be a grid-served EV
      session (v6.2.0-beta1: 652→1968→928 W swings emptied the battery
      before the 06:00 scheduled plan).  A short house spike covered from
      the grid costs a few øre; an empty battery at 06:00 costs the whole
      morning peak.
    - **No live reading** (boolean-only EV sensor): fall back to the
      smallest positive sub-window average — the 1d window recalibrates
      fastest after an upgrade or sensor configuration change.
    - **No history at all** (fresh install): trust the live reading.

    Args:
        live_net_w: ``net_consumption_w`` from the live state (EV power
            already subtracted), or ``None``.
        ev_power_available: Whether at least one EV power sensor reported
            a positive reading this cycle.
        historical_w: House baseline in Watts from the current slot's
            weighted average (0 when unavailable).
        sub_window_ws: Sub-window averages (1d/3d/7d/14d/weighted)
            converted to Watts.

    Returns:
        The discharge cap in Watts (≥ 0).
    """
    if live_net_w is not None and ev_power_available:
        if historical_w > 0:
            return historical_w
        return int(max(live_net_w, 0.0))
    best_w = 0
    for w in sub_window_ws:
        if w > 0 and (best_w == 0 or w < best_w):
            best_w = w
    return best_w


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
