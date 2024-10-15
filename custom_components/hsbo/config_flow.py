import logging

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import selector

from .const import (
    DEFAULT_HSBO_ENERGI_DATA_SERVICE_IMPORT,
    DEFAULT_HSBO_ENERGI_DATA_SERVICE_EXPORT,
    DOMAIN,
    NAME,
)

_LOGGER = logging.getLogger(__name__)


class HSBOConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Huawei Solar Battery Optimization."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        self._errors = {}

        # If user_input is not None, the user has submitted the form
        if user_input is not None:
            # Validate input_sensor and other necessary fields
            if not user_input.get("hsbo_energi_data_service_import"):
                self._errors["hsbo_energi_data_service_import"] = "required"
            elif not user_input.get("hsbo_energi_data_service_export"):
                self._errors["hsbo_energi_data_service_export"] = "required"
            else:
                # Create the configuration with device_name as title
                return self.async_create_entry(
                    title=user_input.get("device_name", NAME),
                    data=user_input,
                )

        # Define the form schema
        data_schema = vol.Schema(
            {
                vol.Optional("device_name", default=NAME): str,
                vol.Required("hsbo_energi_data_service_import", default=DEFAULT_HSBO_ENERGI_DATA_SERVICE_IMPORT): selector(
                    {"entity": {"domain": "sensor"}}
                ),
                vol.Required("hsbo_energi_data_service_export", default=DEFAULT_HSBO_ENERGI_DATA_SERVICE_EXPORT): selector(
                    {"entity": {"domain": "sensor"}}
                )
            }
        )

        # Show the form to the user
        return self.async_show_form(
            step_id="user", data_schema=data_schema, errors=self._errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Return the options flow."""
        return HSBOOptionsFlow(config_entry)


class HSBOOptionsFlow(config_entries.OptionsFlow):
    """Options flow for Huawei Solar Battery Optimization."""

    def __init__(self, config_entry):
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Handle options step."""

        if user_input is not None:
            if not user_input.get("hsbo_energi_data_service_import"):
                self._errors["hsbo_energi_data_service_import"] = "required"
            elif not user_input.get("hsbo_energi_data_service_export"):
                self._errors["hsbo_energi_data_service_export"] = "required"
            else:
                # Update the device name in options flow
                return self.async_create_entry(
                    title=user_input.get("device_name", self.config_entry.title),
                    data=user_input,
                )

        # Use default values from options and translations
        data_schema = vol.Schema(
            {
                vol.Optional(
                    "device_name",
                    default=self.config_entry.options.get("device_name", NAME),
                ): str,
                vol.Required("hsbo_energi_data_service_import", default=DEFAULT_HSBO_ENERGI_DATA_SERVICE_IMPORT): selector(
                    {"entity": {"domain": "sensor"}}
                ),
                vol.Required("hsbo_energi_data_service_export", default=DEFAULT_HSBO_ENERGI_DATA_SERVICE_EXPORT): selector(
                    {"entity": {"domain": "sensor"}}
                ),

            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=data_schema,
        )
