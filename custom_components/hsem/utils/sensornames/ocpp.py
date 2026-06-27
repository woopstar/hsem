"""OCPP-related sensor name generators.

Provides getter functions for OCPP charger status, power, info, and
sessions sensor names, unique IDs, and entity IDs.
"""

from homeassistant.util import slugify as s

from custom_components.hsem.const import DOMAIN

# ---------------------------------------------------------------------------
# OCPP Charger Status Sensor
# ---------------------------------------------------------------------------


def get_ocpp_charger_status_sensor_name() -> str:
    """Return the display name for the OCPP charger status sensor."""
    return "OCPP Charger Status"


def get_ocpp_charger_status_sensor_unique_id(entry_id: str) -> str:
    """Return a unique ID for the OCPP charger status sensor.

    Args:
        entry_id: The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_ocpp_charger_status_sensor"


def get_ocpp_charger_status_sensor_entity_id() -> str:
    """Return the entity_id for the OCPP charger status sensor."""
    return f"sensor.{s(f'{DOMAIN}_ocpp_charger_status_sensor')}"


# ---------------------------------------------------------------------------
# OCPP Charger Power Sensor
# ---------------------------------------------------------------------------


def get_ocpp_charger_power_sensor_name() -> str:
    """Return the display name for the OCPP charger power sensor."""
    return "OCPP Charger Power"


def get_ocpp_charger_power_sensor_unique_id(entry_id: str) -> str:
    """Return a unique ID for the OCPP charger power sensor.

    Args:
        entry_id: The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_ocpp_charger_power_sensor"


def get_ocpp_charger_power_sensor_entity_id() -> str:
    """Return the entity_id for the OCPP charger power sensor."""
    return f"sensor.{s(f'{DOMAIN}_ocpp_charger_power_sensor')}"


# ---------------------------------------------------------------------------
# OCPP Charger Info Sensor
# ---------------------------------------------------------------------------


def get_ocpp_charger_info_sensor_name() -> str:
    """Return the display name for the OCPP charger info sensor."""
    return "OCPP Charger Info"


def get_ocpp_charger_info_sensor_unique_id(entry_id: str) -> str:
    """Return a unique ID for the OCPP charger info sensor.

    Args:
        entry_id: The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_ocpp_charger_info_sensor"


def get_ocpp_charger_info_sensor_entity_id() -> str:
    """Return the entity_id for the OCPP charger info sensor."""
    return f"sensor.{s(f'{DOMAIN}_ocpp_charger_info_sensor')}"


# ---------------------------------------------------------------------------
# OCPP Charger Sessions Sensor
# ---------------------------------------------------------------------------


def get_ocpp_charger_sessions_sensor_name() -> str:
    """Return the display name for the OCPP charger sessions sensor."""
    return "OCPP Charger Sessions"


def get_ocpp_charger_sessions_sensor_unique_id(entry_id: str) -> str:
    """Return a unique ID for the OCPP charger sessions sensor.

    Args:
        entry_id: The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_ocpp_charger_sessions_sensor"


def get_ocpp_charger_sessions_sensor_entity_id() -> str:
    """Return the entity_id for the OCPP charger sessions sensor."""
    return f"sensor.{s(f'{DOMAIN}_ocpp_charger_sessions_sensor')}"
