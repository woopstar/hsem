"""Generic electricity price config/options flow step for HSEM.

Replaces the former ``energidataservice`` step with a provider-agnostic
prices step that supports:

* Import and export price sensors (required)
* Optional separate forecast sensors (e.g. Amber Electric)
* Configurable update interval (15, 30, or 60 minutes)
* A minimum export price slider

The naming convention uses ``electricity_price`` rather than a specific
provider name so that users of Energi Data Service, Nordpool, Amber Electric,
or any other price source share the same configuration step.
"""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import selector

from custom_components.hsem.utils.config_validator import (
    async_validate_entity_ids,
    merge_errors,
    validate_price,
)
from custom_components.hsem.utils.misc import get_config_value


async def get_prices_step_schema(config_entry: ConfigEntry | None) -> vol.Schema:
    """Return the data schema for the 'prices' step.

    Args:
        config_entry: A Home Assistant ``ConfigEntry`` (may be ``None``
            during initial config flow before the entry is created).

    Returns:
        A ``vol.Schema`` with entity selectors for import/export price
        sensors, optional forecast sensors, a minimum export price
        slider, and an update-interval dropdown.
    """
    return vol.Schema(
        {
            vol.Required(
                "hsem_import_electricity_price_sensor",
                default=get_config_value(
                    config_entry, "hsem_import_electricity_price_sensor"
                ),
            ): selector({"entity": {"domain": "sensor"}}),
            vol.Required(
                "hsem_export_electricity_price_sensor",
                default=get_config_value(
                    config_entry, "hsem_export_electricity_price_sensor"
                ),
            ): selector({"entity": {"domain": "sensor"}}),
            vol.Optional(
                "hsem_import_electricity_price_forecast_sensor",
                default=get_config_value(
                    config_entry,
                    "hsem_import_electricity_price_forecast_sensor",
                ),
            ): selector({"entity": {"domain": "sensor"}}),
            vol.Optional(
                "hsem_export_electricity_price_forecast_sensor",
                default=get_config_value(
                    config_entry,
                    "hsem_export_electricity_price_forecast_sensor",
                ),
            ): selector({"entity": {"domain": "sensor"}}),
            vol.Required(
                "hsem_export_electricity_min_price",
                default=get_config_value(
                    config_entry, "hsem_export_electricity_min_price"
                ),
            ): selector(
                {
                    "number": {
                        "min": -2.00,
                        "max": 2.00,
                        "step": 0.01,
                        "mode": "slider",
                    }
                }
            ),
            vol.Required(
                "hsem_electricity_price_update_interval",
                default=str(
                    get_config_value(
                        config_entry, "hsem_electricity_price_update_interval"
                    )
                ),
            ): selector(
                {
                    "select": {
                        "multiple": False,
                        "translation_key": "update_interval_minutes",
                        "mode": "list",
                        "options": [
                            "15",
                            "30",
                            "60",
                        ],
                    }
                }
            ),
        }
    )


async def validate_prices_input(
    hass: HomeAssistant, user_input: dict
) -> dict[str, str]:
    """Validate user input for the 'prices' step.

    Args:
        hass: Home Assistant instance (used for entity existence checks).
        user_input: Raw user-supplied dict from the config/options form.

    Returns:
        A dict of field → error-key; empty dict when validation passes.
    """
    entity_errors = await async_validate_entity_ids(
        hass,
        user_input,
        required_fields=[
            "hsem_import_electricity_price_sensor",
            "hsem_export_electricity_price_sensor",
        ],
    )
    price_errors = validate_price(
        user_input,
        "hsem_export_electricity_min_price",
        min_price=-2.0,
        max_price=2.0,
        allow_negative=True,
    )
    required_errors: dict[str, str] = {}
    for field in (
        "hsem_export_electricity_min_price",
        "hsem_electricity_price_update_interval",
    ):
        if field not in user_input:
            required_errors[field] = "required"
    return merge_errors(entity_errors, price_errors, required_errors)
