"""Config flow step for power sensor selection.

Allows the user to select the Home Assistant entities for house
consumption power and solar production power, the main fuse rating,
and the optional live per-phase grid-charge safety limiter (issue #831):
three per-phase power or current sensors (issue #1119), three optional
per-phase voltage sensors, plus the enable toggle. The limiter's own
grid-charge-maximum-power write entity is configured in the
``huawei_solar`` step alongside the other Huawei number entities.
"""

from typing import Any

import voluptuous as vol

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfElectricCurrent, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.config_validator import async_validate_entity_ids
from custom_components.hsem.utils.misc import get_config_value

# A main fuse trips on current, so each live phase field accepts a power or a
# current sensor (issue #1119); anything else could not be compared with the
# fuse rating and would be treated as unavailable at runtime.
_PHASE_READING_SELECTOR: dict[str, Any] = {
    "entity": {
        "domain": "sensor",
        "device_class": [SensorDeviceClass.POWER, SensorDeviceClass.CURRENT],
    }
}
_PHASE_VOLTAGE_SELECTOR: dict[str, Any] = {
    "entity": {"domain": "sensor", "device_class": SensorDeviceClass.VOLTAGE}
}
_PHASE_READING_FIELDS = tuple(
    f"hsem_huawei_solar_power_meter_phase_{phase}_active_power" for phase in "abc"
)
_PHASE_VOLTAGE_FIELDS = tuple(
    f"hsem_huawei_solar_power_meter_phase_{phase}_voltage" for phase in "abc"
)


def _optional_entity_fields(
    config_entry: ConfigEntry | None,
    keys: tuple[str, ...],
    selector_config: dict[str, Any],
) -> dict[vol.Marker, Any]:
    """Return optional entity-picker fields sharing one selector config."""
    return {
        vol.Optional(key, default=get_config_value(config_entry, key)): selector(
            selector_config
        )
        for key in keys
    }


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
            **_optional_entity_fields(
                config_entry, _PHASE_READING_FIELDS, _PHASE_READING_SELECTOR
            ),
            **_optional_entity_fields(
                config_entry, _PHASE_VOLTAGE_FIELDS, _PHASE_VOLTAGE_SELECTOR
            ),
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
        optional_fields=[*_PHASE_READING_FIELDS, *_PHASE_VOLTAGE_FIELDS],
    )
