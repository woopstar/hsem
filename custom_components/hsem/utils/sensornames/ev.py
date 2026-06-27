"""EV-related sensor, switch, number, and time name generators.

Provides getter functions for EV charging, EV optimal charging plan
(primary and second), EV target SoC (primary and second), EV force discharge,
EV smart charging switches (primary and second), EV force charge now switches
(primary and second), and EV deadline time entities (primary and second)
names, unique IDs, and entity IDs.
"""

from homeassistant.util import slugify as s

from custom_components.hsem.const import DOMAIN


# EV Charging Active Sensor
def get_ev_charging_sensor_name() -> str:
    """Return the display name for the EV-charging-active diagnostic sensor."""
    return "EV Charging Active"


def get_ev_charging_sensor_unique_id(entry_id: str) -> str:
    """Return a unique ID for the EV-charging sensor.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_ev_charging_sensor"


def get_ev_charging_sensor_entity_id() -> str:
    """Return the entity_id for the EV-charging sensor."""
    return f"sensor.{s(f'{DOMAIN}_ev_charging_sensor')}"


# EV Optimal Charging Plan Sensor
def get_ev_optimal_charging_plan_sensor_name() -> str:
    """Return the display name for the EV optimal charging plan sensor."""
    return "EV Optimal Charging Plan"


def get_ev_optimal_charging_plan_sensor_unique_id(entry_id: str) -> str:
    """Return a unique ID for the EV optimal charging plan sensor.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_ev_optimal_charging_plan"


def get_ev_optimal_charging_plan_sensor_entity_id() -> str:
    """Return the entity_id for the EV optimal charging plan sensor."""
    return f"sensor.{s(f'{DOMAIN}_ev_optimal_charging_plan')}"


# EV Second Optimal Charging Plan Sensor
def get_ev_second_optimal_charging_plan_sensor_name() -> str:
    """Return the display name for the second EV optimal charging plan sensor."""
    return "EV 2 Optimal Charging Plan"


def get_ev_second_optimal_charging_plan_sensor_unique_id(entry_id: str) -> str:
    """Return a unique ID for the second EV optimal charging plan sensor.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_ev_second_optimal_charging_plan"


def get_ev_second_optimal_charging_plan_sensor_entity_id() -> str:
    """Return the entity_id for the second EV optimal charging plan sensor."""
    return f"sensor.{s(f'{DOMAIN}_ev_second_optimal_charging_plan')}"


# EV Target SoC Number
def get_ev_target_soc_number_key() -> str:
    """Return the entity description key for the EV target SoC number entity."""
    return f"{DOMAIN}_ev_target_soc"


def get_ev_target_soc_number_name() -> str:
    """Return the display name for the EV target SoC number entity."""
    return "EV Target SoC"


def get_ev_target_soc_number_unique_id(entry_id: str) -> str:
    """Return the unique_id for the EV target SoC number entity.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_{get_ev_target_soc_number_key()}_number"


def get_ev_target_soc_number_entity_id() -> str:
    """Return the entity_id for the EV target SoC number entity."""
    return f"number.{s(get_ev_target_soc_number_key())}"


# EV 2 Target SoC Number
def get_ev_second_target_soc_number_key() -> str:
    """Return the entity description key for the EV 2 target SoC number entity."""
    return f"{DOMAIN}_ev_second_target_soc"


def get_ev_second_target_soc_number_name() -> str:
    """Return the display name for the EV 2 target SoC number entity."""
    return "EV 2 Target SoC"


def get_ev_second_target_soc_number_unique_id(entry_id: str) -> str:
    """Return the unique_id for the EV 2 target SoC number entity.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_{get_ev_second_target_soc_number_key()}_number"


def get_ev_second_target_soc_number_entity_id() -> str:
    """Return the entity_id for the EV 2 target SoC number entity."""
    return f"number.{s(get_ev_second_target_soc_number_key())}"


# EV Force Discharge Switch
def get_ev_force_discharge_switch_key() -> str:
    """Return the config-entry key / unique_id basis for the EV-force-discharge switch."""
    return f"{DOMAIN}_ev_charger_force_max_discharge_power"


def get_ev_force_discharge_switch_name() -> str:
    """Return the display name for the EV-force-discharge switch."""
    return "EV Charger Force Max Discharge Power"


def get_ev_force_discharge_switch_unique_id(entry_id: str) -> str:
    """Return the unique_id for the EV-force-discharge switch.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_{get_ev_force_discharge_switch_key()}_switch"


def get_ev_force_discharge_switch_entity_id() -> str:
    """Return the entity_id for the EV-force-discharge switch."""
    return f"switch.{s(get_ev_force_discharge_switch_key())}"


# EV Smart Charging Switch (Primary)
def get_ev_smart_charging_switch_key() -> str:
    """Return the config-entry key for the primary EV smart charging switch."""
    return f"{DOMAIN}_ev_smart_charging"


def get_ev_smart_charging_switch_name() -> str:
    """Return the display name for the primary EV smart charging switch."""
    return "EV Smart Charging"


def get_ev_smart_charging_switch_unique_id(entry_id: str) -> str:
    """Return the unique_id for the primary EV smart charging switch.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_{get_ev_smart_charging_switch_key()}_switch"


def get_ev_smart_charging_switch_entity_id() -> str:
    """Return the entity_id for the primary EV smart charging switch."""
    return f"switch.{s(get_ev_smart_charging_switch_key())}"


# EV Force Charge Now Switch (Primary)
def get_ev_force_charge_now_switch_key() -> str:
    """Return the config-entry key for the primary EV force-charge-now switch."""
    return f"{DOMAIN}_ev_force_charge_now"


def get_ev_force_charge_now_switch_name() -> str:
    """Return the display name for the primary EV force-charge-now switch."""
    return "EV Force Charge Now"


def get_ev_force_charge_now_switch_unique_id(entry_id: str) -> str:
    """Return the unique_id for the primary EV force-charge-now switch.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_{get_ev_force_charge_now_switch_key()}_switch"


def get_ev_force_charge_now_switch_entity_id() -> str:
    """Return the entity_id for the primary EV force-charge-now switch."""
    return f"switch.{s(get_ev_force_charge_now_switch_key())}"


# EV Smart Charging Switch (Second)
def get_ev_second_smart_charging_switch_key() -> str:
    """Return the config-entry key for the second EV smart charging switch."""
    return f"{DOMAIN}_ev_second_smart_charging"


def get_ev_second_smart_charging_switch_name() -> str:
    """Return the display name for the second EV smart charging switch."""
    return "EV 2 Smart Charging"


def get_ev_second_smart_charging_switch_unique_id(entry_id: str) -> str:
    """Return the unique_id for the second EV smart charging switch.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_{get_ev_second_smart_charging_switch_key()}_switch"


def get_ev_second_smart_charging_switch_entity_id() -> str:
    """Return the entity_id for the second EV smart charging switch."""
    return f"switch.{s(get_ev_second_smart_charging_switch_key())}"


# EV Force Charge Now Switch (Second)
def get_ev_second_force_charge_now_switch_key() -> str:
    """Return the config-entry key for the second EV force-charge-now switch."""
    return f"{DOMAIN}_ev_second_force_charge_now"


def get_ev_second_force_charge_now_switch_name() -> str:
    """Return the display name for the second EV force-charge-now switch."""
    return "EV 2 Force Charge Now"


def get_ev_second_force_charge_now_switch_unique_id(entry_id: str) -> str:
    """Return the unique_id for the second EV force-charge-now switch.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_{get_ev_second_force_charge_now_switch_key()}_switch"


def get_ev_second_force_charge_now_switch_entity_id() -> str:
    """Return the entity_id for the second EV force-charge-now switch."""
    return f"switch.{s(get_ev_second_force_charge_now_switch_key())}"


# EV Deadline Time Entities (Primary)
def get_ev_deadline_time_key() -> str:
    """Return the config key for the EV charge deadline time entity."""
    return f"{DOMAIN}_ev_deadline_time"


def get_ev_deadline_time_name() -> str:
    """Return the display name for the EV charge deadline time entity."""
    return "EV Charge Deadline"


def get_ev_deadline_time_unique_id(entry_id: str) -> str:
    """Return the unique ID for the EV charge deadline time entity.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_{get_ev_deadline_time_key()}_time"


def get_ev_deadline_time_entity_id() -> str:
    """Return the Home Assistant entity ID for the EV charge deadline time entity."""
    return f"time.{s(get_ev_deadline_time_key())}"


# EV Deadline Time Entities (Second)
def get_ev_second_deadline_time_key() -> str:
    """Return the config key for the second EV charge deadline time entity."""
    return f"{DOMAIN}_ev_second_deadline_time"


def get_ev_second_deadline_time_name() -> str:
    """Return the display name for the second EV charge deadline time entity."""
    return "EV 2 Charge Deadline"


def get_ev_second_deadline_time_unique_id(entry_id: str) -> str:
    """Return the unique ID for the second EV charge deadline time entity.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_{get_ev_second_deadline_time_key()}_time"


def get_ev_second_deadline_time_entity_id() -> str:
    """Return the Home Assistant entity ID for the second EV charge deadline time entity."""
    return f"time.{s(get_ev_second_deadline_time_key())}"


# EV Auto-Full on Negative Price Switch (issue #609)
def get_ev_auto_full_negative_price_switch_key() -> str:
    """Return the config-entry key for the EV auto-full negative price switch."""
    return f"{DOMAIN}_ev_auto_full_negative_price"


def get_ev_auto_full_negative_price_switch_name() -> str:
    """Return the display name for the EV auto-full negative price switch."""
    return "EV Auto-Full on Negative Price"


def get_ev_auto_full_negative_price_switch_unique_id(entry_id: str) -> str:
    """Return the unique_id for the EV auto-full negative price switch."""
    return f"{DOMAIN}_{entry_id}_{get_ev_auto_full_negative_price_switch_key()}_switch"


def get_ev_auto_full_negative_price_switch_entity_id() -> str:
    """Return the entity_id for the EV auto-full negative price switch."""
    return f"switch.{s(get_ev_auto_full_negative_price_switch_key())}"
