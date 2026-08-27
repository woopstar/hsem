"""Config flow step for power sensor selection.

Allows the user to select the Home Assistant entities for house
consumption power and solar production power, the main fuse rating,
and the optional live per-phase grid-charge safety limiter (issue #831):
three Huawei power-meter phase sensors plus the enable toggle. The
limiter's own grid-charge-maximum-power write entity is configured in the
``huawei_solar`` step alongside the other Huawei number entities.
"""

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfElectricCurrent, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.config_validator import async_validate_entity_ids
from custom_components.hsem.utils.misc import get_config_value


async def get_power_step_schema(
    config_entry: ConfigEntry | None,
) -> vol.Schema:  # NOSONAR
    """Return the data schema for the 'power' step."""
    return vol.Schema(
        {
            vol.Required(
                "hsem_house_consumption_power",
                default=get_config_value(config_entry, "hsem_house_consumption_power"),
            ): selector({"entity": {"domain": "sensor"}}),
            vol.Required(
                "hsem_solar_production_power",
                default=get_config_value(config_entry, "hsem_solar_production_power"),
            ): selector({"entity": {"domain": "sensor"}}),
            vol.Optional(
                "hsem_main_fuse_amps",
                default=get_config_value(config_entry, "hsem_main_fuse_amps"),
            ): selector(
                {
                    "number": {
                        "min": 0,
                        "max": 125,
                        "step": 1,
                        "mode": "slider",
                        "unit_of_measurement": UnitOfElectricCurrent.AMPERE,
                    }
                }
            ),
            vol.Optional(
                "hsem_main_fuse_phases",
                default=get_config_value(config_entry, "hsem_main_fuse_phases"),
            ): selector(
                {
                    "number": {
                        "min": 1,
                        "max": 3,
                        "step": 2,
                        "mode": "box",
                    }
                }
            ),
            vol.Optional(
                "hsem_max_grid_export_power_kw",
                default=get_config_value(config_entry, "hsem_max_grid_export_power_kw"),
            ): selector(
                {
                    "number": {
                        "min": 0,
                        "max": 100,
                        "step": 0.1,
                        "mode": "box",
                        "unit_of_measurement": UnitOfPower.KILO_WATT,
                    }
                }
            ),
            vol.Optional(
                "hsem_phase_aware_charging_enabled",
                default=get_config_value(
                    config_entry, "hsem_phase_aware_charging_enabled"
                ),
            ): selector({"boolean": {}}),
            vol.Optional(
                "hsem_huawei_solar_power_meter_phase_a_active_power",
                default=get_config_value(
                    config_entry, "hsem_huawei_solar_power_meter_phase_a_active_power"
                ),
            ): selector({"entity": {"domain": "sensor"}}),
            vol.Optional(
                "hsem_huawei_solar_power_meter_phase_b_active_power",
                default=get_config_value(
                    config_entry, "hsem_huawei_solar_power_meter_phase_b_active_power"
                ),
            ): selector({"entity": {"domain": "sensor"}}),
            vol.Optional(
                "hsem_huawei_solar_power_meter_phase_c_active_power",
                default=get_config_value(
                    config_entry, "hsem_huawei_solar_power_meter_phase_c_active_power"
                ),
            ): selector({"entity": {"domain": "sensor"}}),
        }
    )


async def validate_power_step_input(
    hass: HomeAssistant, user_input: dict
) -> dict[str, str]:
    """Validate user input for the 'power' step."""
    return await async_validate_entity_ids(
        hass,
        user_input,
        required_fields=[
            "hsem_house_consumption_power",
            "hsem_solar_production_power",
        ],
        optional_fields=[
            "hsem_huawei_solar_power_meter_phase_a_active_power",
            "hsem_huawei_solar_power_meter_phase_b_active_power",
            "hsem_huawei_solar_power_meter_phase_c_active_power",
        ],
    )
