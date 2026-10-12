"""Financial sensor unique-ID and entity-ID generators.

Display names for these sensors come from Home Assistant's translation
system (``_attr_translation_key`` + ``translations/*.json``), not from
functions in this module. Provides getter functions for the export income,
import cost, net grid balance and performance sensor unique IDs and entity IDs.
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


# Performance (KPI) sensors, issue #1182. Every one shares the ``performance``
# prefix so the KPIs of later stages sort next to the first ones.


def get_performance_savings_capture_unique_id(entry_id: str) -> str:
    """Generate a unique ID for the performance savings-capture sensor.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.

    Returns:
        str: Unique ID of the savings-capture sensor.

    """
    return f"{DOMAIN}_{entry_id}_performance_savings_capture_sensor"


def get_performance_savings_capture_entity_id() -> str:
    """Generate an Entity ID for the performance savings-capture sensor.

    Returns:
        str: Entity ID of the savings-capture sensor.

    """
    return f"sensor.{s(f'{DOMAIN}_performance_savings_capture')}"


def get_performance_actual_benefit_unique_id(entry_id: str) -> str:
    """Generate a unique ID for the performance actual-benefit sensor.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.

    Returns:
        str: Unique ID of the actual-benefit sensor.

    """
    return f"{DOMAIN}_{entry_id}_performance_actual_benefit_sensor"


def get_performance_actual_benefit_entity_id() -> str:
    """Generate an Entity ID for the performance actual-benefit sensor.

    Returns:
        str: Entity ID of the actual-benefit sensor.

    """
    return f"sensor.{s(f'{DOMAIN}_performance_actual_benefit')}"


def get_performance_missed_opportunity_unique_id(entry_id: str) -> str:
    """Generate a unique ID for the performance missed-opportunity sensor.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.

    Returns:
        str: Unique ID of the missed-opportunity sensor.

    """
    return f"{DOMAIN}_{entry_id}_performance_missed_opportunity_sensor"


def get_performance_missed_opportunity_entity_id() -> str:
    """Generate an Entity ID for the performance missed-opportunity sensor.

    Returns:
        str: Entity ID of the missed-opportunity sensor.

    """
    return f"sensor.{s(f'{DOMAIN}_performance_missed_opportunity')}"
