"""Switch entity description and ID map for the HSEM integration.

Defines :class:`HSEMSwitchEntityDescription` and the
``build_switch_id_map()`` function that maps config-entry keys to
(unique_id, entity_id) tuples.
"""

from dataclasses import dataclass

from homeassistant.components.switch import SwitchEntityDescription

from custom_components.hsem.utils.sensornames import (
    get_batteries_schedule_1_switch_entity_id,
    get_batteries_schedule_1_switch_key,
    get_batteries_schedule_1_switch_unique_id,
    get_batteries_schedule_2_switch_entity_id,
    get_batteries_schedule_2_switch_key,
    get_batteries_schedule_2_switch_unique_id,
    get_batteries_schedule_3_switch_entity_id,
    get_batteries_schedule_3_switch_key,
    get_batteries_schedule_3_switch_unique_id,
    get_ev_force_charge_now_switch_entity_id,
    get_ev_force_charge_now_switch_key,
    get_ev_force_charge_now_switch_unique_id,
    get_ev_force_discharge_switch_entity_id,
    get_ev_force_discharge_switch_key,
    get_ev_force_discharge_switch_unique_id,
    get_ev_second_force_charge_now_switch_entity_id,
    get_ev_second_force_charge_now_switch_key,
    get_ev_second_force_charge_now_switch_unique_id,
    get_ev_second_smart_charging_switch_entity_id,
    get_ev_second_smart_charging_switch_key,
    get_ev_second_smart_charging_switch_unique_id,
    get_ev_smart_charging_switch_entity_id,
    get_ev_smart_charging_switch_key,
    get_ev_smart_charging_switch_unique_id,
    get_extended_attributes_switch_entity_id,
    get_extended_attributes_switch_key,
    get_extended_attributes_switch_unique_id,
    get_ml_consumption_switch_entity_id,
    get_ml_consumption_switch_key,
    get_ml_consumption_switch_unique_id,
    get_ml_sequential_switch_entity_id,
    get_ml_sequential_switch_key,
    get_ml_sequential_switch_unique_id,
    get_read_only_switch_entity_id,
    get_read_only_switch_key,
    get_read_only_switch_unique_id,
    get_verbose_logging_switch_entity_id,
    get_verbose_logging_switch_key,
    get_verbose_logging_switch_unique_id,
)


def build_switch_id_map(entry_id: str) -> dict[str, tuple[str, str]]:
    """Build the switch ID map for a given config entry.

    Args:
        entry_id: The config entry ID for uniqueness across entries.

    Returns:
        A dict mapping config-entry keys to (unique_id, entity_id) tuples.
    """
    return {
        get_read_only_switch_key(): (
            get_read_only_switch_unique_id(entry_id),
            get_read_only_switch_entity_id(),
        ),
        get_extended_attributes_switch_key(): (
            get_extended_attributes_switch_unique_id(entry_id),
            get_extended_attributes_switch_entity_id(),
        ),
        get_verbose_logging_switch_key(): (
            get_verbose_logging_switch_unique_id(entry_id),
            get_verbose_logging_switch_entity_id(),
        ),
        get_batteries_schedule_1_switch_key(): (
            get_batteries_schedule_1_switch_unique_id(entry_id),
            get_batteries_schedule_1_switch_entity_id(),
        ),
        get_batteries_schedule_2_switch_key(): (
            get_batteries_schedule_2_switch_unique_id(entry_id),
            get_batteries_schedule_2_switch_entity_id(),
        ),
        get_batteries_schedule_3_switch_key(): (
            get_batteries_schedule_3_switch_unique_id(entry_id),
            get_batteries_schedule_3_switch_entity_id(),
        ),
        get_ev_force_discharge_switch_key(): (
            get_ev_force_discharge_switch_unique_id(entry_id),
            get_ev_force_discharge_switch_entity_id(),
        ),
        get_ev_smart_charging_switch_key(): (
            get_ev_smart_charging_switch_unique_id(entry_id),
            get_ev_smart_charging_switch_entity_id(),
        ),
        get_ev_force_charge_now_switch_key(): (
            get_ev_force_charge_now_switch_unique_id(entry_id),
            get_ev_force_charge_now_switch_entity_id(),
        ),
        get_ev_second_smart_charging_switch_key(): (
            get_ev_second_smart_charging_switch_unique_id(entry_id),
            get_ev_second_smart_charging_switch_entity_id(),
        ),
        get_ev_second_force_charge_now_switch_key(): (
            get_ev_second_force_charge_now_switch_unique_id(entry_id),
            get_ev_second_force_charge_now_switch_entity_id(),
        ),
        get_ml_consumption_switch_key(): (
            get_ml_consumption_switch_unique_id(entry_id),
            get_ml_consumption_switch_entity_id(),
        ),
        get_ml_sequential_switch_key(): (
            get_ml_sequential_switch_unique_id(entry_id),
            get_ml_sequential_switch_entity_id(),
        ),
    }


@dataclass(frozen=True)
class HSEMSwitchEntityDescription(SwitchEntityDescription):
    """Extended entity description that adds a human-readable description field.

    Attributes
    ----------
    description:
        Short human-readable description of the switch's purpose, exposed as
        an entity attribute for dashboard display.
    """

    description: str = ""
