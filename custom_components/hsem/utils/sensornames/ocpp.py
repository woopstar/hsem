"""OCPP-related sensor name generators.

Provides getter functions for OCPP charger status, power, info, and
sessions sensor names, unique IDs, and entity IDs.  Each OCPP server
(one per EV) exposes its own set of sensors; ``charger_index`` selects
between the primary (``1``) and second (``2``) EV's server.
"""

from homeassistant.util import slugify as s

from custom_components.hsem.const import DOMAIN

# ---------------------------------------------------------------------------
# OCPP Charger Status Sensor
# ---------------------------------------------------------------------------


def _suffix(charger_index: int) -> str:
    """Return the name/entity suffix for a charger index."""
    return "" if charger_index == 1 else "_second"


def get_ocpp_charger_status_sensor_name() -> str:
    """Return the display name for the OCPP charger status sensor.

    Identical for both chargers — the EV Primary / EV Secondary device
    (issue #875) disambiguates them, so the entity name carries neither
    the redundant "OCPP" integration prefix nor a "Second"/"2" marker
    (e.g. rendered as "EV Secondary Charger Status" via
    ``_attr_has_entity_name``).
    """
    return "Charger Status"


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


def get_ocpp_charger_power_sensor_name() -> str:
    """Return the display name for the OCPP charger power sensor.

    Identical for both chargers — the EV Primary / EV Secondary device
    (issue #875) disambiguates them, so the entity name carries neither
    the redundant "OCPP" integration prefix nor a "Second"/"2" marker.
    """
    return "Charger Power"


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


def get_ocpp_charger_info_sensor_name() -> str:
    """Return the display name for the OCPP charger info sensor.

    Identical for both chargers — the EV Primary / EV Secondary device
    (issue #875) disambiguates them, so the entity name carries neither
    the redundant "OCPP" integration prefix nor a "Second"/"2" marker.
    """
    return "Charger Info"


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


def get_ocpp_charger_sessions_sensor_name() -> str:
    """Return the display name for the OCPP charger sessions sensor.

    Identical for both chargers — the EV Primary / EV Secondary device
    (issue #875) disambiguates them, so the entity name carries neither
    the redundant "OCPP" integration prefix nor a "Second"/"2" marker.
    """
    return "Charger Sessions"


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
