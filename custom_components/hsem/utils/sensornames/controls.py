"""Non-EV controls: switches, time entities, and efficiency numbers.

Provides getter functions for read-only switch, extended attributes switch,
verbose logging switch, batteries schedule 1/2/3 switches, schedule 1/2/3
start/end time entities, and battery charge/discharge efficiency numbers.
"""

from homeassistant.util import slugify as s

from custom_components.hsem.const import DOMAIN


# Battery Charge Efficiency Number
def get_charge_efficiency_number_key() -> str:
    """Return the entity description key for the charge efficiency number entity."""
    return f"{DOMAIN}_charge_efficiency"


def get_charge_efficiency_number_name() -> str:
    """Return the display name for the charge efficiency number entity."""
    return "Battery Charge Efficiency"


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


def get_discharge_efficiency_number_name() -> str:
    """Return the display name for the discharge efficiency number entity."""
    return "Battery Discharge Efficiency"


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


def get_read_only_switch_name() -> str:
    """Return the display name for the read-only switch."""
    return "Read Only"


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


def get_extended_attributes_switch_name() -> str:
    """Return the display name for the extended-attributes switch."""
    return "Extended Attributes"


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


def get_verbose_logging_switch_name() -> str:
    """Return the display name for the verbose-logging switch."""
    return "Verbose Logging"


def get_verbose_logging_switch_unique_id(entry_id: str) -> str:
    """Return the unique_id for the verbose-logging switch.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_{get_verbose_logging_switch_key()}_switch"


def get_verbose_logging_switch_entity_id() -> str:
    """Return the entity_id for the verbose-logging switch."""
    return f"switch.{s(get_verbose_logging_switch_key())}"


# Batteries Schedule 1 Switch
def get_batteries_schedule_1_switch_key() -> str:
    """Return the config-entry key / unique_id basis for the batteries-schedule-1 switch."""
    return f"{DOMAIN}_batteries_enable_batteries_schedule_1"


def get_batteries_schedule_1_switch_name() -> str:
    """Return the display name for the batteries-schedule-1 switch."""
    return "Batteries Discharge Schedule 1"


def get_batteries_schedule_1_switch_unique_id(entry_id: str) -> str:
    """Return the unique_id for the batteries-schedule-1 switch.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_{get_batteries_schedule_1_switch_key()}_switch"


def get_batteries_schedule_1_switch_entity_id() -> str:
    """Return the entity_id for the batteries-schedule-1 switch."""
    return f"switch.{s(get_batteries_schedule_1_switch_key())}"


# Batteries Schedule 2 Switch
def get_batteries_schedule_2_switch_key() -> str:
    """Return the config-entry key / unique_id basis for the batteries-schedule-2 switch."""
    return f"{DOMAIN}_batteries_enable_batteries_schedule_2"


def get_batteries_schedule_2_switch_name() -> str:
    """Return the display name for the batteries-schedule-2 switch."""
    return "Batteries Discharge Schedule 2"


def get_batteries_schedule_2_switch_unique_id(entry_id: str) -> str:
    """Return the unique_id for the batteries-schedule-2 switch.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_{get_batteries_schedule_2_switch_key()}_switch"


def get_batteries_schedule_2_switch_entity_id() -> str:
    """Return the entity_id for the batteries-schedule-2 switch."""
    return f"switch.{s(get_batteries_schedule_2_switch_key())}"


# Batteries Schedule 3 Switch
def get_batteries_schedule_3_switch_key() -> str:
    """Return the config-entry key / unique_id basis for the batteries-schedule-3 switch."""
    return f"{DOMAIN}_batteries_enable_batteries_schedule_3"


def get_batteries_schedule_3_switch_name() -> str:
    """Return the display name for the batteries-schedule-3 switch."""
    return "Batteries Discharge Schedule 3"


def get_batteries_schedule_3_switch_unique_id(entry_id: str) -> str:
    """Return the unique_id for the batteries-schedule-3 switch.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_{get_batteries_schedule_3_switch_key()}_switch"


def get_batteries_schedule_3_switch_entity_id() -> str:
    """Return the entity_id for the batteries-schedule-3 switch."""
    return f"switch.{s(get_batteries_schedule_3_switch_key())}"


# Schedule 1 Start Time
def get_schedule_1_start_time_key() -> str:
    """Return the config-entry key / unique_id basis for schedule-1-start time."""
    return f"{DOMAIN}_batteries_enable_batteries_schedule_1_start"


def get_schedule_1_start_time_name() -> str:
    """Return the display name for the schedule-1-start time entity."""
    return "Batteries Discharge Schedule 1 Start"


def get_schedule_1_start_time_unique_id(entry_id: str) -> str:
    """Return the unique_id for the schedule-1-start time entity.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_{get_schedule_1_start_time_key()}_time"


def get_schedule_1_start_time_entity_id() -> str:
    """Return the entity_id for the schedule-1-start time entity."""
    return f"time.{s(get_schedule_1_start_time_key())}"


# Schedule 1 End Time
def get_schedule_1_end_time_key() -> str:
    """Return the config-entry key / unique_id basis for schedule-1-end time."""
    return f"{DOMAIN}_batteries_enable_batteries_schedule_1_end"


def get_schedule_1_end_time_name() -> str:
    """Return the display name for the schedule-1-end time entity."""
    return "Batteries Discharge Schedule 1 End"


def get_schedule_1_end_time_unique_id(entry_id: str) -> str:
    """Return the unique_id for the schedule-1-end time entity.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_{get_schedule_1_end_time_key()}_time"


def get_schedule_1_end_time_entity_id() -> str:
    """Return the entity_id for the schedule-1-end time entity."""
    return f"time.{s(get_schedule_1_end_time_key())}"


# Schedule 2 Start Time
def get_schedule_2_start_time_key() -> str:
    """Return the config-entry key / unique_id basis for schedule-2-start time."""
    return f"{DOMAIN}_batteries_enable_batteries_schedule_2_start"


def get_schedule_2_start_time_name() -> str:
    """Return the display name for the schedule-2-start time entity."""
    return "Batteries Discharge Schedule 2 Start"


def get_schedule_2_start_time_unique_id(entry_id: str) -> str:
    """Return the unique_id for the schedule-2-start time entity.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_{get_schedule_2_start_time_key()}_time"


def get_schedule_2_start_time_entity_id() -> str:
    """Return the entity_id for the schedule-2-start time entity."""
    return f"time.{s(get_schedule_2_start_time_key())}"


# Schedule 2 End Time
def get_schedule_2_end_time_key() -> str:
    """Return the config-entry key / unique_id basis for schedule-2-end time."""
    return f"{DOMAIN}_batteries_enable_batteries_schedule_2_end"


def get_schedule_2_end_time_name() -> str:
    """Return the display name for the schedule-2-end time entity."""
    return "Batteries Discharge Schedule 2 End"


def get_schedule_2_end_time_unique_id(entry_id: str) -> str:
    """Return the unique_id for the schedule-2-end time entity.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_{get_schedule_2_end_time_key()}_time"


def get_schedule_2_end_time_entity_id() -> str:
    """Return the entity_id for the schedule-2-end time entity."""
    return f"time.{s(get_schedule_2_end_time_key())}"


# Schedule 3 Start Time
def get_schedule_3_start_time_key() -> str:
    """Return the config-entry key / unique_id basis for schedule-3-start time."""
    return f"{DOMAIN}_batteries_enable_batteries_schedule_3_start"


def get_schedule_3_start_time_name() -> str:
    """Return the display name for the schedule-3-start time entity."""
    return "Batteries Discharge Schedule 3 Start"


def get_schedule_3_start_time_unique_id(entry_id: str) -> str:
    """Return the unique_id for the schedule-3-start time entity.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_{get_schedule_3_start_time_key()}_time"


def get_schedule_3_start_time_entity_id() -> str:
    """Return the entity_id for the schedule-3-start time entity."""
    return f"time.{s(get_schedule_3_start_time_key())}"


# Schedule 3 End Time
def get_schedule_3_end_time_key() -> str:
    """Return the config-entry key / unique_id basis for schedule-3-end time."""
    return f"{DOMAIN}_batteries_enable_batteries_schedule_3_end"


def get_schedule_3_end_time_name() -> str:
    """Return the display name for the schedule-3-end time entity."""
    return "Batteries Discharge Schedule 3 End"


def get_schedule_3_end_time_unique_id(entry_id: str) -> str:
    """Return the unique_id for the schedule-3-end time entity.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_{get_schedule_3_end_time_key()}_time"


def get_schedule_3_end_time_entity_id() -> str:
    """Return the entity_id for the schedule-3-end time entity."""
    return f"time.{s(get_schedule_3_end_time_key())}"


# Charge rate per temperature bucket number entities (issue #608)

_TEMP_BUCKET_KEYS = [
    "below_0",
    "0_to_5",
    "6_to_15",
    "16_to_21",
    "21_to_35",
    "35_to_50",
    "above_50",
]


def get_charge_rate_number_key(bucket: str) -> str:
    """Return the entity description key for a temperature-bucket charge rate."""
    return f"{DOMAIN}_charge_rate_{bucket}"


def get_charge_rate_number_unique_id(entry_id: str, bucket: str) -> str:
    """Return the unique_id for a temperature-bucket charge rate number entity."""
    return f"{DOMAIN}_{entry_id}_charge_rate_{bucket}"


def get_charge_rate_number_entity_id(bucket: str) -> str:
    """Return the entity_id for a temperature-bucket charge rate number entity."""
    return f"number.{s(f'{DOMAIN}_charge_rate_{bucket}')}"
