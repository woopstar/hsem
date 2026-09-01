"""Non-EV controls: switches, time entities, and efficiency numbers.

Provides getter functions for read-only switch, extended attributes switch,
verbose logging switch, dynamic-discharge-floor switch, and battery
charge/discharge efficiency numbers.
"""

from homeassistant.util import slugify as s

from custom_components.hsem.const import DOMAIN


# Battery Charge Efficiency Number
def get_charge_efficiency_number_key() -> str:
    """Return the entity description key for the charge efficiency number entity."""
    return f"{DOMAIN}_charge_efficiency"


def get_charge_efficiency_number_unique_id(entry_id: str) -> str:
    """Return the unique_id for the charge efficiency number entity.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_battery_charge_efficiency"


def get_charge_efficiency_number_entity_id() -> str:
    """Return the entity_id for the charge efficiency number entity."""
    return f"number.{s(f'{DOMAIN}_battery_charge_efficiency')}"


# Battery Discharge Efficiency Number
def get_discharge_efficiency_number_key() -> str:
    """Return the entity description key for the discharge efficiency number entity."""
    return f"{DOMAIN}_discharge_efficiency"


def get_discharge_efficiency_number_unique_id(entry_id: str) -> str:
    """Return the unique_id for the discharge efficiency number entity.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_battery_discharge_efficiency"


def get_discharge_efficiency_number_entity_id() -> str:
    """Return the entity_id for the discharge efficiency number entity."""
    return f"number.{s(f'{DOMAIN}_battery_discharge_efficiency')}"


# Read-Only Switch
def get_read_only_switch_key() -> str:
    """Return the config-entry key / unique_id basis for the read-only switch."""
    return f"{DOMAIN}_read_only"


def get_read_only_switch_unique_id(entry_id: str) -> str:
    """Return the unique_id for the read-only switch.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_{get_read_only_switch_key()}_switch"


def get_read_only_switch_entity_id() -> str:
    """Return the entity_id for the read-only switch."""
    return f"switch.{s(get_read_only_switch_key())}"


# Extended Attributes Switch
def get_extended_attributes_switch_key() -> str:
    """Return the config-entry key / unique_id basis for the extended-attributes switch."""
    return f"{DOMAIN}_extended_attributes"


def get_extended_attributes_switch_unique_id(entry_id: str) -> str:
    """Return the unique_id for the extended-attributes switch.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_{get_extended_attributes_switch_key()}_switch"


def get_extended_attributes_switch_entity_id() -> str:
    """Return the entity_id for the extended-attributes switch."""
    return f"switch.{s(get_extended_attributes_switch_key())}"


# Verbose Logging Switch
def get_verbose_logging_switch_key() -> str:
    """Return the config-entry key / unique_id basis for the verbose-logging switch."""
    return f"{DOMAIN}_verbose_logging"


def get_verbose_logging_switch_unique_id(entry_id: str) -> str:
    """Return the unique_id for the verbose-logging switch.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_{get_verbose_logging_switch_key()}_switch"


def get_verbose_logging_switch_entity_id() -> str:
    """Return the entity_id for the verbose-logging switch."""
    return f"switch.{s(get_verbose_logging_switch_key())}"


# Dynamic Discharge Floor Switch
def get_dynamic_discharge_floor_switch_key() -> str:
    """Return the config-entry key for the dynamic-discharge-floor switch."""
    return f"{DOMAIN}_dynamic_discharge_floor"


def get_dynamic_discharge_floor_switch_unique_id(entry_id: str) -> str:
    """Return the unique_id for the dynamic-discharge-floor switch.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_{get_dynamic_discharge_floor_switch_key()}_switch"


def get_dynamic_discharge_floor_switch_entity_id() -> str:
    """Return the entity_id for the dynamic-discharge-floor switch."""
    return f"switch.{s(get_dynamic_discharge_floor_switch_key())}"
