"""OCPP-related sensor unique-ID and entity-ID generators.

Display names for these sensors come from Home Assistant's translation
system (``_attr_translation_key`` + ``translations/*.json``), not from
functions in this module. Provides getter functions for OCPP charger
status, power, info, and sessions sensor unique IDs and entity IDs.  Each
OCPP server (one per EV) exposes its own set of sensors; ``charger_index``
selects between the primary (``1``) and second (``2``) EV's server.
"""

from homeassistant.util import slugify as s

from custom_components.hsem.const import DOMAIN

# ---------------------------------------------------------------------------
# OCPP Charger Status Sensor
# ---------------------------------------------------------------------------


def _suffix(charger_index: int) -> str:
    """Return the name/entity suffix for a charger index."""
    return "" if charger_index == 1 else "_second"


def get_ocpp_charger_status_sensor_unique_id(
    entry_id: str, charger_index: int = 1
) -> str:
    """Return a unique ID for the OCPP charger status sensor.

    Args:
        entry_id: The config entry ID for uniqueness across entries.
        charger_index: ``1`` for the primary EV server, ``2`` for the second.
    """
    return f"{DOMAIN}_{entry_id}_ocpp_charger_status_sensor{_suffix(charger_index)}"


def get_ocpp_charger_status_sensor_entity_id(charger_index: int = 1) -> str:
    """Return the entity_id for the OCPP charger status sensor."""
    return f"sensor.{s(f'{DOMAIN}_ocpp_charger_status_sensor{_suffix(charger_index)}')}"


# ---------------------------------------------------------------------------
# OCPP Charger Power Sensor
# ---------------------------------------------------------------------------


def get_ocpp_charger_power_sensor_unique_id(
    entry_id: str, charger_index: int = 1
) -> str:
    """Return a unique ID for the OCPP charger power sensor.

    Args:
        entry_id: The config entry ID for uniqueness across entries.
        charger_index: ``1`` for the primary EV server, ``2`` for the second.
    """
    return f"{DOMAIN}_{entry_id}_ocpp_charger_power_sensor{_suffix(charger_index)}"


def get_ocpp_charger_power_sensor_entity_id(charger_index: int = 1) -> str:
    """Return the entity_id for the OCPP charger power sensor."""
    return f"sensor.{s(f'{DOMAIN}_ocpp_charger_power_sensor{_suffix(charger_index)}')}"


# ---------------------------------------------------------------------------
# OCPP Charger Info Sensor
# ---------------------------------------------------------------------------


def get_ocpp_charger_info_sensor_unique_id(
    entry_id: str, charger_index: int = 1
) -> str:
    """Return a unique ID for the OCPP charger info sensor.

    Args:
        entry_id: The config entry ID for uniqueness across entries.
        charger_index: ``1`` for the primary EV server, ``2`` for the second.
    """
    return f"{DOMAIN}_{entry_id}_ocpp_charger_info_sensor{_suffix(charger_index)}"


def get_ocpp_charger_info_sensor_entity_id(charger_index: int = 1) -> str:
    """Return the entity_id for the OCPP charger info sensor."""
    return f"sensor.{s(f'{DOMAIN}_ocpp_charger_info_sensor{_suffix(charger_index)}')}"


# ---------------------------------------------------------------------------
# OCPP Charger Sessions Sensor
# ---------------------------------------------------------------------------


def get_ocpp_charger_sessions_sensor_unique_id(
    entry_id: str, charger_index: int = 1
) -> str:
    """Return a unique ID for the OCPP charger sessions sensor.

    Args:
        entry_id: The config entry ID for uniqueness across entries.
        charger_index: ``1`` for the primary EV server, ``2`` for the second.
    """
    return f"{DOMAIN}_{entry_id}_ocpp_charger_sessions_sensor{_suffix(charger_index)}"


def get_ocpp_charger_sessions_sensor_entity_id(charger_index: int = 1) -> str:
    """Return the entity_id for the OCPP charger sessions sensor."""
    return (
        f"sensor.{s(f'{DOMAIN}_ocpp_charger_sessions_sensor{_suffix(charger_index)}')}"
    )
