"""Monthly energy sensor unique-ID and entity-ID generators (issue #1435).

Display names for these sensors come from Home Assistant's translation
system (``_attr_translation_key`` + ``translations/*.json``), not from
functions in this module.  Every sensor shares the ``monthly`` prefix so the
four sort next to each other.
"""

from homeassistant.util import slugify as s

from custom_components.hsem.const import DOMAIN


def get_monthly_self_sufficiency_unique_id(entry_id: str) -> str:
    """Generate a unique ID for the monthly self-sufficiency sensor.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.

    Returns:
        str: Unique ID of the monthly self-sufficiency sensor.

    """
    return f"{DOMAIN}_{entry_id}_monthly_self_sufficiency_sensor"


def get_monthly_self_sufficiency_entity_id() -> str:
    """Generate an Entity ID for the monthly self-sufficiency sensor.

    Returns:
        str: Entity ID of the monthly self-sufficiency sensor.

    """
    return f"sensor.{s(f'{DOMAIN}_monthly_self_sufficiency')}"


def get_monthly_battery_cycles_unique_id(entry_id: str) -> str:
    """Generate a unique ID for the monthly battery-cycles sensor.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.

    Returns:
        str: Unique ID of the monthly battery-cycles sensor.

    """
    return f"{DOMAIN}_{entry_id}_monthly_battery_cycles_sensor"


def get_monthly_battery_cycles_entity_id() -> str:
    """Generate an Entity ID for the monthly battery-cycles sensor.

    Returns:
        str: Entity ID of the monthly battery-cycles sensor.

    """
    return f"sensor.{s(f'{DOMAIN}_monthly_battery_cycles')}"


def get_monthly_battery_wear_cost_unique_id(entry_id: str) -> str:
    """Generate a unique ID for the monthly battery-wear-cost sensor.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.

    Returns:
        str: Unique ID of the monthly battery-wear-cost sensor.

    """
    return f"{DOMAIN}_{entry_id}_monthly_battery_wear_cost_sensor"


def get_monthly_battery_wear_cost_entity_id() -> str:
    """Generate an Entity ID for the monthly battery-wear-cost sensor.

    Returns:
        str: Entity ID of the monthly battery-wear-cost sensor.

    """
    return f"sensor.{s(f'{DOMAIN}_monthly_battery_wear_cost')}"


def get_monthly_round_trip_loss_unique_id(entry_id: str) -> str:
    """Generate a unique ID for the monthly round-trip-loss sensor.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.

    Returns:
        str: Unique ID of the monthly round-trip-loss sensor.

    """
    return f"{DOMAIN}_{entry_id}_monthly_round_trip_loss_sensor"


def get_monthly_round_trip_loss_entity_id() -> str:
    """Generate an Entity ID for the monthly round-trip-loss sensor.

    Returns:
        str: Entity ID of the monthly round-trip-loss sensor.

    """
    return f"sensor.{s(f'{DOMAIN}_monthly_round_trip_loss')}"
