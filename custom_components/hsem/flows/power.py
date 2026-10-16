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

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfElectricCurrent, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.config_validator import async_validate_entity_ids
from custom_components.hsem.utils.house_load import (
    HOUSE_LOAD_INVERTER_KEYS,
    HOUSE_LOAD_METER_KEY,
    house_load_inputs,
)
from custom_components.hsem.utils.misc import get_config_value, optional_entity_id
from custom_components.hsem.utils.schema import vol

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
_POWER_SELECTOR: dict[str, Any] = {
    "entity": {"domain": "sensor", "device_class": SensorDeviceClass.POWER}
}
# Inputs of the derived House Load Power sensor (issue #1310).
_HOUSE_LOAD_FIELDS = (*HOUSE_LOAD_INVERTER_KEYS, HOUSE_LOAD_METER_KEY)


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
            # Optional since issue #1310: without it, HSEM derives the house
            # load from the inverter and power meter inputs below.
            vol.Optional(
                "hsem_house_consumption_power",
                default=get_config_value(config_entry, "hsem_house_consumption_power"),
            ): selector({"entity": {"domain": "sensor"}}),
            vol.Required(
                "hsem_solar_production_power",
                default=get_config_value(config_entry, "hsem_solar_production_power"),
            ): selector({"entity": {"domain": "sensor"}}),
            **_optional_entity_fields(
                config_entry, _HOUSE_LOAD_FIELDS, _POWER_SELECTOR
            ),
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
    """Validate user input for the 'power' step.

    The house consumption sensor may be left empty when an inverter and the
    power meter are given, from which HSEM derives the house load (issue
    #1310).  The second inverter needs the first.
    """
    errors = await async_validate_entity_ids(
        hass,
        user_input,
        required_fields=["hsem_solar_production_power"],
        optional_fields=[
            "hsem_house_consumption_power",
            *_HOUSE_LOAD_FIELDS,
            *_PHASE_READING_FIELDS,
            *_PHASE_VOLTAGE_FIELDS,
        ],
    )
    inverter, inverter_2 = (
        optional_entity_id(user_input.get(key)) for key in HOUSE_LOAD_INVERTER_KEYS
    )
    derived = house_load_inputs(
        inverter,
        inverter_2,
        optional_entity_id(user_input.get(HOUSE_LOAD_METER_KEY)),
    )
    if inverter_2 and not inverter:
        errors.setdefault(HOUSE_LOAD_INVERTER_KEYS[1], "house_load_needs_inverter_1")
    if not optional_entity_id(user_input.get("hsem_house_consumption_power")) and (
        derived is None
    ):
        errors.setdefault("hsem_house_consumption_power", "house_power_required")
    return errors
