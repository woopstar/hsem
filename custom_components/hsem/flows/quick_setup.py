"""Quick setup wizard with entity auto-detection for HSEM.

Provides automatic detection of HSEM-relevant Home Assistant entities
and a pre-filled quick-setup flow that lets users skip the individual
entity-picker steps.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant

# Mapping from auto-detection key to HSEM config key.
_DETECTION_TO_CONFIG: dict[str, str] = {
    "battery_soc": "hsem_huawei_solar_batteries_state_of_capacity",
    "working_mode": "hsem_huawei_solar_batteries_working_mode",
    "max_charge_power": "hsem_huawei_solar_batteries_maximum_charging_power",
    "max_discharge_power": "hsem_huawei_solar_batteries_maximum_discharging_power",
    "eod_soc": "hsem_huawei_solar_batteries_end_of_discharge_soc",
    "rated_capacity": "hsem_huawei_solar_batteries_rated_capacity",
    "active_power_control": "hsem_huawei_solar_inverter_active_power_control",
    "tou_periods": "hsem_huawei_solar_batteries_tou_charging_and_discharging_periods",
    "solcast_today": "hsem_solcast_pv_forecast_forecast_today",
    "solcast_tomorrow": "hsem_solcast_pv_forecast_forecast_tomorrow",
    "import_price": "hsem_import_electricity_price_sensor",
    "export_price": "hsem_export_electricity_price_sensor",
    "house_power": "hsem_house_consumption_power",
    "solar_power": "hsem_solar_production_power",
}

# Entities that must be detected for quick setup to be viable.
CRITICAL_DETECTION_KEYS: frozenset[str] = frozenset(
    {"battery_soc", "working_mode", "solcast_today"}
)


async def auto_detect_entities(hass: HomeAssistant) -> dict[str, str | None]:
    """Auto-detect HSEM-relevant entities and return a mapping of detection key → entity_id.

    Scans all entity IDs in Home Assistant and matches against known keyword
    patterns for Huawei Solar, Solcast, electricity price, and power sensors.

    Args:
        hass: The Home Assistant instance.

    Returns:
        A dict mapping detection keys to discovered entity IDs (or None).
    """
    detected: dict[str, str | None] = {}

    all_states = hass.states.async_all()
    entity_ids = [s.entity_id for s in all_states]

    # Huawei Solar entities
    detected["battery_soc"] = _find_first(
        entity_ids, ["battery_state_of_capacity", "battery_soc"]
    )
    detected["working_mode"] = _find_first(
        entity_ids, ["batteries_working_mode", "work_mode"]
    )
    detected["max_charge_power"] = _find_first(entity_ids, ["maximum_charging_power"])
    detected["max_discharge_power"] = _find_first(
        entity_ids, ["maximum_discharging_power"]
    )
    detected["eod_soc"] = _find_first(entity_ids, ["end_of_discharge_soc"])
    detected["rated_capacity"] = _find_first(entity_ids, ["batteries_rated_capacity"])
    detected["active_power_control"] = _find_first(entity_ids, ["active_power_control"])
    detected["tou_periods"] = _find_first(
        entity_ids, ["tou_charging_and_discharging_periods"]
    )

    # Solcast
    detected["solcast_today"] = _find_first(
        entity_ids, ["solcast_pv_forecast_forecast_today"]
    )
    detected["solcast_tomorrow"] = _find_first(
        entity_ids, ["solcast_pv_forecast_forecast_tomorrow"]
    )

    # Prices
    detected["import_price"] = _find_first(
        entity_ids,
        [
            "energi_data_service",
            "nordpool",
            "tibber",
            "amber",
            "spot_price",
            "electricity_price",
        ],
    )
    detected["export_price"] = detected.get("import_price")

    # Power meters
    detected["house_power"] = _find_first(
        entity_ids, ["house_consumption", "load_power", "forbrug"]
    )
    detected["solar_power"] = _find_first(
        entity_ids,
        ["solar_production", "pv_power", "solcelleproduktion", "power_inverter_input"],
    )

    return detected


def _find_first(entity_ids: list[str], keywords: list[str]) -> str | None:
    """Return the first entity_id containing any of the keywords.

    Args:
        entity_ids: List of HA entity IDs to search.
        keywords: Keywords to look for in entity IDs.

    Returns:
        The first matching entity ID, or None if no match is found.
    """
    for eid in entity_ids:
        for kw in keywords:
            if kw.lower() in eid.lower():
                return eid
    return None
