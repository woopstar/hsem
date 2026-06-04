"""Config flow step for energy entities and ML consumption settings.

Combines daily plan-vs-actual energy meter configuration with the ML
consumption prediction toggle.  When ML is enabled, the energy entities
(grid import / export) are reused as data sources for the ridge regression
predictor.  An optional temperature sensor improves weather-driven load
predictions.
"""

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.misc import get_config_value


async def get_energy_and_ml_step_schema(  # NOSONAR
    config_entry: ConfigEntry | None,
) -> vol.Schema:
    """Return the data schema for the 'energy_and_ml' step.

    Includes energy meter entities for plan-vs-actual tracking AND
    the ML consumption prediction settings.
    """
    return vol.Schema(
        {
            # --- Energy meter entities (daily plan-vs-actual + ML data source) ---
            vol.Optional(
                "hsem_grid_import_energy_entity",
                default=get_config_value(
                    config_entry, "hsem_grid_import_energy_entity"
                ),
            ): selector({"entity": {"domain": "sensor"}}),
            vol.Optional(
                "hsem_grid_export_energy_entity",
                default=get_config_value(
                    config_entry, "hsem_grid_export_energy_entity"
                ),
            ): selector({"entity": {"domain": "sensor"}}),
            vol.Optional(
                "hsem_pv_energy_entity",
                default=get_config_value(config_entry, "hsem_pv_energy_entity"),
            ): selector({"entity": {"domain": "sensor"}}),
            # --- ML consumption prediction ---
            vol.Required(
                "hsem_ml_consumption_enabled",
                default=bool(
                    get_config_value(config_entry, "hsem_ml_consumption_enabled")
                ),
            ): selector({"boolean": {}}),
            vol.Optional(
                "hsem_ml_consumption_energy_entity",
                default=get_config_value(
                    config_entry, "hsem_ml_consumption_energy_entity"
                ),
            ): selector({"entity": {"domain": "sensor"}}),
            vol.Required(
                "hsem_ml_consumption_history_days",
                default=get_config_value(
                    config_entry, "hsem_ml_consumption_history_days"
                ),
            ): selector(
                {
                    "number": {
                        "min": 7,
                        "max": 90,
                        "step": 1,
                        "unit_of_measurement": UnitOfTime.DAYS,
                        "mode": "slider",
                    }
                }
            ),
            vol.Required(
                "hsem_ml_consumption_net_consumption",
                default=bool(
                    get_config_value(
                        config_entry, "hsem_ml_consumption_net_consumption"
                    )
                ),
            ): selector({"boolean": {}}),
            vol.Optional(
                "hsem_ml_consumption_temperature_entity",
                default=get_config_value(
                    config_entry, "hsem_ml_consumption_temperature_entity"
                ),
            ): selector({"entity": {"domain": "sensor"}}),
        }
    )


async def validate_energy_and_ml_input(  # NOSONAR
    _hass: HomeAssistant,
    _user_input: dict[str, Any],
) -> dict[str, str]:
    """Validate user input for the 'energy_and_ml' step.

    All fields are optional — no hard validation beyond schema.
    """
    return {}
