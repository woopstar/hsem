"""Config flow step for daily plan-vs-actual energy meter entities.

Allows the user to optionally map cumulative energy meter entities for
grid import, grid export, and PV production.  When these are configured,
the daily plan-vs-actual sensor uses the meter readings for actual values.
When not configured, it falls back to Riemann-sum estimates from
instantaneous power sensors.
"""

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.misc import get_config_value


async def get_daily_tracking_step_schema(  # NOSONAR
    config_entry: ConfigEntry | None,
) -> vol.Schema:
    """Return the data schema for the 'daily_tracking' step.

    All fields are optional — the sensor works with partial data.
    """
    return vol.Schema(
        {
            vol.Optional(
                "hsem_grid_import_energy_entity",
                default=get_config_value(
                    config_entry, "hsem_grid_import_energy_entity"
                ),
            ): selector(
                {
                    "entity": {
                        "domain": "sensor",
                    }
                }
            ),
            vol.Optional(
                "hsem_grid_export_energy_entity",
                default=get_config_value(
                    config_entry, "hsem_grid_export_energy_entity"
                ),
            ): selector(
                {
                    "entity": {
                        "domain": "sensor",
                    }
                }
            ),
            vol.Optional(
                "hsem_pv_energy_entity",
                default=get_config_value(config_entry, "hsem_pv_energy_entity"),
            ): selector(
                {
                    "entity": {
                        "domain": "sensor",
                    }
                }
            ),
        }
    )


async def validate_daily_tracking_input(  # NOSONAR
    _hass: HomeAssistant,
    _user_input: dict[str, Any],
) -> dict[str, str]:
    """Validate user input for the 'daily_tracking' step.

    Since all fields are optional, no validation is performed beyond the
    schema itself.
    """
    return {}
