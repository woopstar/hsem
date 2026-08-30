"""Config-entry schema migrations for the HSEM config flow.

Extracted from ``config_flow.py`` to satisfy the repository's 30 KB /
1000-line file limit. Pure move: the migration constants and functions keep
their exact behaviour and are re-exported from ``config_flow``.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from custom_components.hsem.const import DOMAIN
from custom_components.hsem.utils.misc import convert_months_to_int

_LOGGER = logging.getLogger(__name__)


_V1_TO_V2_KEY_RENAMES: dict[str, str] = {
    "hsem_energi_data_service_export_min_price": "hsem_export_electricity_min_price",
    "hsem_energi_data_service_update_interval": "hsem_electricity_price_update_interval",
    "hsem_energi_data_service_export": "hsem_export_electricity_price_sensor",
    "hsem_energi_data_service_import": "hsem_import_electricity_price_sensor",
}

# Keys that existed in v1 but have no equivalent in v2 (removed).
_V1_DEPRECATED_KEYS: frozenset[str] = frozenset(
    {
        "hsem_batteries_enable_batteries_schedule_1_min_price_difference",
        "hsem_batteries_enable_batteries_schedule_2_min_price_difference",
        "hsem_batteries_enable_batteries_schedule_3_min_price_difference",
        "hsem_batteries_conversion_loss",
    }
)

_CHARGE_RATE_BUCKETS: tuple[str, ...] = (
    "below_0",
    "0_to_5",
    "6_to_15",
    "16_to_21",
    "21_to_35",
    "35_to_50",
    "above_50",
)

# The temperature learner never received a battery-temperature entity and
# therefore always fell back to the configured inverter limit. Remove both its
# persisted sample blob and the seven manual override values during v3 migration.
_V3_DEPRECATED_KEYS: frozenset[str] = frozenset(
    {"hsem_charge_rate_learned_rates"}
    | {f"hsem_charge_rate_override_{bucket}" for bucket in _CHARGE_RATE_BUCKETS}
)

# New keys introduced in v2 that did not exist in v1.  When
# migrating a v1 entry these are backfilled with their defaults.
_V2_NEW_KEY_DEFAULTS: dict[str, Any] = {
    # Battery economics
    "hsem_batteries_charge_efficiency": 98,
    "hsem_batteries_discharge_efficiency": 98,
    "hsem_batteries_purchase_price": 0.0,
    "hsem_batteries_expected_cycles": 6000,
    "hsem_batteries_cycle_cost": 0.0,
    "hsem_batteries_capacity_loss_pct": 30,
    # Excess export
    "hsem_batteries_enable_excess_export": False,
    "hsem_batteries_excess_export_discharge_buffer": 10,
    # Wait mode behaviour
    "hsem_batteries_wait_mode_behavior": "strict",
    # Energy price forecast sensors (optional — None = not configured)
    "hsem_import_electricity_price_forecast_sensor": None,
    "hsem_export_electricity_price_forecast_sensor": None,
    # EV smart charging flags
    "hsem_ev_target_soc": 80,
    "hsem_ev_deadline_time": "07:00",
    "hsem_ev_smart_charging": False,
    "hsem_ev_force_charge_now": False,
    "hsem_ev_second_target_soc": 80,
    "hsem_ev_second_deadline_time": "07:00",
    "hsem_ev_second_smart_charging": False,
    "hsem_ev_second_force_charge_now": False,
    # EV planned load (disabled by default)
    "hsem_ev_planned_load_enabled": False,
    "hsem_ev_planned_load_battery_capacity_kwh": 0.0,
    "hsem_ev_planned_load_charger_power_kw": 0.0,
    "hsem_ev_planned_load_charger_efficiency": 100,
    "hsem_ev_planned_load_charger_min_power_w": 1380,
    "hsem_ev_planned_load_deadline_safety_margin_pct": 0,
    "hsem_ev_planned_load_command_deadband_a": 3,
    "hsem_ev_planned_load_stub_floor_minutes": 2,
    "hsem_ev_second_planned_load_enabled": False,
    "hsem_ev_second_planned_load_battery_capacity_kwh": 0.0,
    "hsem_ev_second_planned_load_charger_power_kw": 0.0,
    "hsem_ev_second_planned_load_charger_efficiency": 100,
    "hsem_ev_second_planned_load_charger_min_power_w": 1380,
    "hsem_ev_second_planned_load_deadline_safety_margin_pct": 0,
    "hsem_ev_second_planned_load_command_deadband_a": 3,
    "hsem_ev_second_planned_load_stub_floor_minutes": 2,
    # Planner hysteresis
    "hsem_planner_hysteresis_enabled": True,
    "hsem_planner_hysteresis_absolute": 0.0,
    "hsem_planner_hysteresis_percentage": 5.0,
    "hsem_planner_window_hysteresis_minutes": 10,
    # Huawei Solar additions in v2
    "hsem_huawei_solar_batteries_charging_cutoff_capacity": (
        "number.batteries_end_of_charge_soc"
    ),
    "hsem_huawei_solar_batteries_forcible_charge": ("sensor.batteries_forcible_charge"),
}


def _migrate_v1_to_v2(data: dict[str, Any]) -> dict[str, Any]:
    """Convert a v1 (v5.1.0 era) config data dict to v2.

    Idempotent — safe to call on data that has already been partially migrated.
    """
    migrated = dict(data)

    # 1. Rename old keys to new keys (only if the new key is absent).
    for old_key, new_key in _V1_TO_V2_KEY_RENAMES.items():
        if old_key in migrated and new_key not in migrated:
            migrated[new_key] = migrated.pop(old_key)

    # 2. Drop deprecated keys that have no v2 equivalent.
    for key in _V1_DEPRECATED_KEYS:
        migrated.pop(key, None)

    # 3. Backfill new keys with their defaults (only if absent).
    for key, default in _V2_NEW_KEY_DEFAULTS.items():
        if key not in migrated:
            migrated[key] = default

    # 4. Convert month lists from strings to ints (v5.1.0 stored them as
    #    strings; v6.0.0 expects integers).
    for month_key in ("hsem_months_summer", "hsem_months_winter"):
        raw = migrated.get(month_key, [])
        if raw and isinstance(raw[0], str):
            migrated[month_key] = convert_months_to_int(raw)

    return migrated


def _migrate_v2_to_v3(values: dict[str, Any]) -> dict[str, Any]:
    """Remove persisted values for the retired charge-rate learner."""
    return {
        key: value for key, value in values.items() if key not in _V3_DEPRECATED_KEYS
    }


@callback
def _remove_v3_charge_rate_registry_entries(hass: HomeAssistant, entry_id: str) -> None:
    """Remove registry rows for the seven retired charge-rate number entities."""
    registry = er.async_get(hass)
    retired_unique_ids = {
        f"{DOMAIN}_{entry_id}_charge_rate_{bucket}" for bucket in _CHARGE_RATE_BUCKETS
    } | {f"{DOMAIN}_charge_rate_{bucket}" for bucket in _CHARGE_RATE_BUCKETS}
    for entity_entry in list(er.async_entries_for_config_entry(registry, entry_id)):
        if (
            entity_entry.platform == DOMAIN
            and entity_entry.domain == "number"
            and entity_entry.unique_id in retired_unique_ids
        ):
            registry.async_remove(entity_entry.entity_id)
