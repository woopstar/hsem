"""Derived battery-capacity and net-consumption computations.

Extracted from ``state_collector.py`` to satisfy the repository's 30 KB /
1000-line file limit. Pure move: no behaviour change.
"""

from __future__ import annotations

from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig

# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _compute_battery_capacities(state: LiveState) -> None:
    """Fill ``battery_usable_capacity_kwh``, ``battery_current_capacity_kwh``,
    and ``battery_rated_capacity_min_kwh`` from the raw entity readings.

    This is the extracted logic from
    ``_async_calculate_remaining_battery_capacity`` in the sensor.
    """
    rated_wh = state.huawei_batteries_rated_capacity_wh
    soc_pct = state.huawei_batteries_soc_pct

    if not isinstance(rated_wh, (int, float)) or not isinstance(soc_pct, (int, float)):
        return

    rated_kwh = rated_wh / 1000.0
    eod_soc = state.huawei_batteries_end_of_discharge_soc_pct or 5.0
    # Respect the max-SoC ceiling from the charging cutoff entity; default to 100 %
    # (no upper restriction) when the entity is unavailable.
    max_soc = state.huawei_batteries_charging_cutoff_capacity_pct or 100.0
    effective_max_soc = min(max(max_soc, eod_soc), 100.0)
    reserve_kwh = rated_kwh * (eod_soc / 100.0)
    max_kwh = rated_kwh * (effective_max_soc / 100.0)
    usable_kwh = max(max_kwh - reserve_kwh, 0.0)
    current_kwh = (soc_pct / 100.0) * rated_kwh
    available_kwh = max(current_kwh - reserve_kwh, 0.0)

    state.battery_rated_capacity_min_kwh = round(reserve_kwh, 3)
    state.battery_usable_capacity_kwh = round(usable_kwh, 2)
    state.battery_current_capacity_kwh = round(available_kwh, 2)
    # BMS-reported energy remaining — the total kWh stored in the battery
    # (including reserve).  Used by CapacityLearner for capacity auto-detection.
    state.bms_kwh_remaining = round(current_kwh, 3)


def _compute_net_consumption(state: LiveState, cfg: SensorConfig) -> None:
    """Compute ``net_consumption_w`` and ``net_consumption_with_ev_w``.

    Extracted from ``_async_calculate_net_consumption`` in the sensor.
    """
    house_w = state.house_consumption_power_w
    solar_w = state.solar_production_power_w

    if not isinstance(house_w, (int, float)) or not isinstance(solar_w, (int, float)):
        state.net_consumption_w = 0.0
        return

    ev_w = (state.ev.power_w or 0.0) + (state.ev_second.power_w or 0.0)

    if cfg.house_power_includes_ev_charger_power:
        state.net_consumption_with_ev_w = round(house_w - solar_w, 3)
        state.net_consumption_w = round(house_w - solar_w - ev_w, 3)
    else:
        state.net_consumption_with_ev_w = round(house_w - solar_w + ev_w, 3)
        state.net_consumption_w = round(house_w - solar_w, 3)
