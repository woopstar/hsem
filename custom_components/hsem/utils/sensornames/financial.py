"""Financial sensor unique-ID and entity-ID generators.

Display names for these sensors come from Home Assistant's translation
system (``_attr_translation_key`` + ``translations/*.json``), not from
functions in this module. Provides getter functions for export income,
import cost, and net grid balance sensor unique IDs and entity IDs.
"""

from homeassistant.util import slugify as s

from custom_components.hsem.const import DOMAIN


def get_export_income_unique_id(entry_id: str) -> str:
    """Generate a unique ID for the export income sensor.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.

    Returns:
        str: Unique ID of the export income sensor.

    """
    return f"{DOMAIN}_{entry_id}_export_income_sensor"


def get_export_income_entity_id() -> str:
    """Generate an Entity ID for the export income sensor.

    Returns:
        str: Entity ID of the export income sensor.

    """
    return f"sensor.{s(f'{DOMAIN}_export_income')}"


def get_import_cost_unique_id(entry_id: str) -> str:
    """Generate a unique ID for the import cost sensor.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.

    Returns:
        str: Unique ID of the import cost sensor.

    """
    return f"{DOMAIN}_{entry_id}_import_cost_sensor"


def get_import_cost_entity_id() -> str:
    """Generate an Entity ID for the import cost sensor.

    Returns:
        str: Entity ID of the import cost sensor.

    """
    return f"sensor.{s(f'{DOMAIN}_import_cost')}"


def get_net_grid_balance_unique_id(entry_id: str) -> str:
    """Generate a unique ID for the net grid balance sensor.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.

    Returns:
        str: Unique ID of the net grid balance sensor.

    """
    return f"{DOMAIN}_{entry_id}_net_grid_balance_sensor"


def get_net_grid_balance_entity_id() -> str:
    """Generate an Entity ID for the net grid balance sensor.

    Returns:
        str: Entity ID of the net grid balance sensor.

    """
    return f"sensor.{s(f'{DOMAIN}_net_grid_balance')}"
