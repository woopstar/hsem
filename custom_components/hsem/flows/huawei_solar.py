"""Config flow step for Huawei Solar device and entity selections.

This step contains only the Huawei-specific device and entity selectors.
Battery economics (purchase price, expected cycles, cycle cost, efficiencies)
have been moved to the separate ``battery_economics`` step.
"""

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.config_validator import (
    async_validate_device_ids,
    async_validate_entity_ids,
    merge_errors,
)
from custom_components.hsem.utils.misc import get_config_value


async def get_huawei_solar_step_schema(
    config_entry: ConfigEntry | None,
) -> vol.Schema:  # NOSONAR -- async required by HA config/options flow framework
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
            vol.Optional(
                "hsem_huawei_solar_batteries_maximum_discharging_power",
                default=get_config_value(
                    config_entry,
                    "hsem_huawei_solar_batteries_maximum_discharging_power",
                ),
            ): selector({"entity": {"domain": "number"}}),
            vol.Required(
                "hsem_huawei_solar_batteries_grid_charge_cutoff_soc",
                default=get_config_value(
                    config_entry, "hsem_huawei_solar_batteries_grid_charge_cutoff_soc"
                ),
            ): selector({"entity": {"domain": "number"}}),
            vol.Required(
                "hsem_huawei_solar_batteries_charging_cutoff_capacity",
                default=get_config_value(
                    config_entry,
                    "hsem_huawei_solar_batteries_charging_cutoff_capacity",
                ),
            ): selector({"entity": {"domain": "number"}}),
            vol.Required(
                "hsem_huawei_solar_batteries_end_of_discharge_soc",
                default=get_config_value(
                    config_entry, "hsem_huawei_solar_batteries_end_of_discharge_soc"
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
                "hsem_huawei_solar_batteries_forcible_charge",
                default=get_config_value(
                    config_entry, "hsem_huawei_solar_batteries_forcible_charge"
                ),
            ): selector({"entity": {"domain": "sensor"}}),
            vol.Required(
                "hsem_huawei_solar_batteries_excess_pv_energy_use_in_tou",
                default=get_config_value(
                    config_entry,
                    "hsem_huawei_solar_batteries_excess_pv_energy_use_in_tou",
                ),
            ): selector({"entity": {"domain": "select"}}),
        }
    )


async def validate_huawei_solar_input(
    hass: HomeAssistant, user_input: dict
) -> dict[str, str]:
    """Validate user input for the 'huawei_solar' step."""
    # --- entity existence ---
    entity_errors = await async_validate_entity_ids(
        hass,
        user_input,
        required_fields=[
            "hsem_huawei_solar_batteries_working_mode",
            "hsem_huawei_solar_batteries_state_of_capacity",
            "hsem_huawei_solar_inverter_active_power_control",
            "hsem_huawei_solar_batteries_maximum_charging_power",
            "hsem_huawei_solar_batteries_grid_charge_cutoff_soc",
            "hsem_huawei_solar_batteries_charging_cutoff_capacity",
            "hsem_huawei_solar_batteries_tou_charging_and_discharging_periods",
            "hsem_huawei_solar_batteries_rated_capacity",
            "hsem_huawei_solar_batteries_excess_pv_energy_use_in_tou",
            "hsem_huawei_solar_batteries_end_of_discharge_soc",
        ],
        optional_fields=[
            "hsem_huawei_solar_batteries_maximum_discharging_power",
        ],
    )

    # --- device existence ---
    device_errors = await async_validate_device_ids(
        hass,
        user_input,
        required_fields=["hsem_huawei_solar_device_id_inverter_1"],
        optional_fields=[
            "hsem_huawei_solar_device_id_inverter_2",
            "hsem_huawei_solar_device_id_batteries",
        ],
    )

    return merge_errors(entity_errors, device_errors)
