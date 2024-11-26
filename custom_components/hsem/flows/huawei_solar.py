import voluptuous as vol
from homeassistant.helpers.selector import selector

from custom_components.hsem.const import (
    DEFAULT_HSEM_BATTERY_CONVERSION_LOSS,
    DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_GRID_CHARGE_CUTOFF_SOC,
    DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_MAXIMUM_CHARGING_POWER,
    DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_RATED_CAPACITY,
    DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_STATE_OF_CAPACITY,
    DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_TOU_CHARGING_AND_DISCHARGING_PERIODS,
    DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_WORKING_MODE,
    DEFAULT_HSEM_HUAWEI_SOLAR_INVERTER_ACTIVE_POWER_CONTROL,
)
from custom_components.hsem.utils.misc import get_config_value


async def get_huawei_solar_step_schema(config_entry):
    """Return the data schema for the 'huawei_solar' step."""
    return vol.Schema(
        {
            vol.Required(
                "hsem_huawei_solar_device_id_inverter_1",
                default=get_config_value(
                    config_entry, "hsem_huawei_solar_device_id_inverter_1", ""
                ),
            ): selector({"device": {"integration": "huawei_solar"}}),
            vol.Optional(
                "hsem_huawei_solar_device_id_inverter_2",
                default=get_config_value(
                    config_entry, "hsem_huawei_solar_device_id_inverter_2", ""
                ),
            ): selector({"device": {"integration": "huawei_solar"}}),
            vol.Required(
                "hsem_huawei_solar_device_id_batteries",
                default=get_config_value(
                    config_entry, "hsem_huawei_solar_device_id_batteries", ""
                ),
            ): selector({"device": {"integration": "huawei_solar"}}),
            vol.Required(
                "hsem_huawei_solar_batteries_working_mode",
                default=get_config_value(
                    config_entry,
                    "hsem_huawei_solar_batteries_working_mode",
                    DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_WORKING_MODE,
                ),
            ): selector({"entity": {"domain": "select"}}),
            vol.Required(
                "hsem_huawei_solar_batteries_state_of_capacity",
                default=get_config_value(
                    config_entry,
                    "hsem_huawei_solar_batteries_state_of_capacity",
                    DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_STATE_OF_CAPACITY,
                ),
            ): selector({"entity": {"domain": "sensor"}}),
            vol.Required(
                "hsem_huawei_solar_inverter_active_power_control",
                default=get_config_value(
                    config_entry,
                    "hsem_huawei_solar_inverter_active_power_control",
                    DEFAULT_HSEM_HUAWEI_SOLAR_INVERTER_ACTIVE_POWER_CONTROL,
                ),
            ): selector({"entity": {"domain": "sensor"}}),
            vol.Required(
                "hsem_huawei_solar_batteries_maximum_charging_power",
                default=get_config_value(
                    config_entry,
                    "hsem_huawei_solar_batteries_maximum_charging_power",
                    DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_MAXIMUM_CHARGING_POWER,
                ),
            ): selector({"entity": {"domain": "number"}}),
            vol.Required(
                "hsem_huawei_solar_batteries_grid_charge_cutoff_soc",
                default=get_config_value(
                    config_entry,
                    "hsem_huawei_solar_batteries_grid_charge_cutoff_soc",
                    DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_GRID_CHARGE_CUTOFF_SOC,
                ),
            ): selector({"entity": {"domain": "number"}}),
            vol.Required(
                "hsem_huawei_solar_batteries_tou_charging_and_discharging_periods",
                default=get_config_value(
                    config_entry,
                    "hsem_huawei_solar_batteries_tou_charging_and_discharging_periods",
                    DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_TOU_CHARGING_AND_DISCHARGING_PERIODS,
                ),
            ): selector({"entity": {"domain": "sensor"}}),
            vol.Required(
                "hsem_huawei_solar_batteries_rated_capacity",
                default=get_config_value(
                    config_entry,
                    "hsem_huawei_solar_batteries_rated_capacity",
                    DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_RATED_CAPACITY,
                ),
            ): selector({"entity": {"domain": ["sensor", "input_number"]}}),
            vol.Required(
                "hsem_battery_conversion_loss",
                default=get_config_value(
                    config_entry,
                    "hsem_battery_conversion_loss",
                    DEFAULT_HSEM_BATTERY_CONVERSION_LOSS,
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


async def validate_huawei_solar_input(user_input):
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
        "hsem_battery_conversion_loss",
    ]

    for field in required_fields:
        if field not in user_input:
            errors[field] = "required"

    return errors
