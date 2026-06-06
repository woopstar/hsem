"""ML-related switch name generators.

Provides getter functions for the ML consumption switch and ML sequential
prediction switch names, unique IDs, and entity IDs.
"""

from homeassistant.util import slugify as s

from custom_components.hsem.const import DOMAIN


# ML Consumption Switch
def get_ml_consumption_switch_key() -> str:
    """Return the config-entry key for the ML consumption switch."""
    return "hsem_ml_consumption_enabled"


def get_ml_consumption_switch_unique_id(entry_id: str) -> str:
    """Return the unique_id for the ML consumption switch."""
    return f"{DOMAIN}_{entry_id}_ml_consumption_switch"


def get_ml_consumption_switch_entity_id() -> str:
    """Return the entity_id for the ML consumption switch."""
    return f"switch.{s(get_ml_consumption_switch_key())}"


# ML Sequential Prediction Switch
def get_ml_sequential_switch_key() -> str:
    """Return the config-entry key for the ML sequential prediction switch."""
    return "hsem_ml_consumption_sequential"


def get_ml_sequential_switch_unique_id(entry_id: str) -> str:
    """Return the unique_id for the ML sequential prediction switch."""
    return f"{DOMAIN}_{entry_id}_ml_sequential_switch"


def get_ml_sequential_switch_entity_id() -> str:
    """Return the entity_id for the ML sequential prediction switch."""
    return f"switch.{s(get_ml_sequential_switch_key())}"
