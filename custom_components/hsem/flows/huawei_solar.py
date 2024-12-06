import voluptuous as vol
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.misc import (
    async_device_exists,
    async_entity_exists,
    get_config_value,
)


async def get_huawei_solar_step_schema(config_entry):
    """Return the data schema for the 'huawei_solar' step."""
    return vol.Schema(
        {
            vol.Required(
                "hsem_huawei_solar_device_id_inverter_1",
                default=get_config_value(
                    config_entry, "hsem_huawei_solar_device_id_inverter_1"
                ),
            ): selector({"device": {"integration": "huawei_solar"}}),
            vol.Optional(
                "hsem_huawei_solar_device_id_inverter_2",
                default=get_config_value(
                    config_entry, "hsem_huawei_solar_device_id_inverter_2"
                ),
            ): selector({"device": {"integration": "huawei_solar"}}),
            vol.Required(
                "hsem_huawei_solar_device_id_batteries",
                default=get_config_value(
                    config_entry, "hsem_huawei_solar_device_id_batteries"
                ),
            ): selector({"device": {"integration": "huawei_solar"}}),
            vol.Required(
                "hsem_huawei_solar_batteries_working_mode",
                default=get_config_value(
                    config_entry, "hsem_huawei_solar_batteries_working_mode"
                ),
            ): selector({"entity": {"domain": "select"}}),
            vol.Required(
                "hsem_huawei_solar_batteries_state_of_capacity",
                default=get_config_value(
                    config_entry, "hsem_huawei_solar_batteries_state_of_capacity"
                ),
            ): selector({"entity": {"domain": "sensor"}}),
            vol.Required(
                "hsem_huawei_solar_inverter_active_power_control",
                default=get_config_value(
                    config_entry, "hsem_huawei_solar_inverter_active_power_control"
                ),
            ): selector({"entity": {"domain": "sensor"}}),
            vol.Required(
                "hsem_huawei_solar_batteries_maximum_charging_power",
                default=get_config_value(
                    config_entry, "hsem_huawei_solar_batteries_maximum_charging_power"
                ),
            ): selector({"entity": {"domain": "number"}}),
            vol.Required(
                "hsem_huawei_solar_batteries_grid_charge_cutoff_soc",
                default=get_config_value(
                    config_entry, "hsem_huawei_solar_batteries_grid_charge_cutoff_soc"
                ),
            ): selector({"entity": {"domain": "number"}}),
            vol.Required(
                "hsem_huawei_solar_batteries_tou_charging_and_discharging_periods",
                default=get_config_value(
                    config_entry,
                    "hsem_huawei_solar_batteries_tou_charging_and_discharging_periods",
                ),
            ): selector({"entity": {"domain": "sensor"}}),
            vol.Required(
                "hsem_huawei_solar_batteries_rated_capacity",
                default=get_config_value(
                    config_entry, "hsem_huawei_solar_batteries_rated_capacity"
                ),
            ): selector({"entity": {"domain": ["sensor", "input_number"]}}),
            vol.Required(
                "hsem_batteries_conversion_loss",
                default=get_config_value(
                    config_entry, "hsem_batteries_conversion_loss"
                ),
            ): selector(
                {
                    "number": {
                        "min": 0,
                        "max": 50,
                        "step": 1,
                        "unit_of_measurement": "%",
                        "mode": "slider",
                    }
                }
            ),
        }
    )


async def validate_huawei_solar_input(hass, user_input):
    """Validate user input for the 'huawei_solar' step."""
    errors = {}

    required_fields = [
        "hsem_huawei_solar_device_id_inverter_1",
        "hsem_huawei_solar_device_id_batteries",
        "hsem_huawei_solar_batteries_working_mode",
        "hsem_huawei_solar_batteries_state_of_capacity",
        "hsem_huawei_solar_inverter_active_power_control",
        "hsem_huawei_solar_batteries_maximum_charging_power",
        "hsem_huawei_solar_batteries_grid_charge_cutoff_soc",
        "hsem_huawei_solar_batteries_tou_charging_and_discharging_periods",
        "hsem_huawei_solar_batteries_rated_capacity",
        "hsem_batteries_conversion_loss",
    ]

    for field in required_fields:
        if field not in user_input:
            errors[field] = "required"

    if not errors:
        # Tjek entities
        entity_fields = [
            "hsem_huawei_solar_batteries_working_mode",
            "hsem_huawei_solar_batteries_state_of_capacity",
            "hsem_huawei_solar_inverter_active_power_control",
            "hsem_huawei_solar_batteries_maximum_charging_power",
            "hsem_huawei_solar_batteries_grid_charge_cutoff_soc",
            "hsem_huawei_solar_batteries_tou_charging_and_discharging_periods",
            "hsem_huawei_solar_batteries_rated_capacity",
        ]

        for field in entity_fields:
            entity_id = user_input.get(field)
            if entity_id and not await async_entity_exists(hass, entity_id):
                errors[field] = "entity_not_found"

        # Tjek devices
        device_fields = [
            "hsem_huawei_solar_device_id_inverter_1",
            "hsem_huawei_solar_device_id_batteries",
        ]

        for field in device_fields:
            device_id = user_input.get(field)
            if device_id and not await async_device_exists(hass, device_id):
                errors[field] = "device_not_found"

    return errors
