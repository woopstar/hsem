import logging

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import selector

from .const import (
    DEFAULT_HSEM_ENERGI_DATA_SERVICE_EXPORT,
    DEFAULT_HSEM_ENERGI_DATA_SERVICE_IMPORT,
    DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_STATE_OF_CAPACITY,
    DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_WORKING_MODE,
    DEFAULT_HSEM_HUAWEI_SOLAR_INVERTER_ACTIVE_POWER_CONTROL,
    DOMAIN,
    NAME,
)

_LOGGER = logging.getLogger(__name__)


class HSEMConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for HSEM."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        self._errors = {}

        # Check if there's already an entry for this domain
        existing_entries = self.hass.config_entries.async_entries(DOMAIN)
        if existing_entries:
            self._errors["base"] = "only_one_entry_allowed"
            return self.async_show_form(step_id="user", errors=self._errors)

        # If user_input is not None, the user has submitted the form
        if user_input is not None:
            # Validate device_name
            if not user_input.get("device_name"):
                self._errors["device_name"] = "required"
            else:
                # Save initial user input and move to the energidataservice step
                self._user_input = user_input
                return await self.async_step_energidataservice()

        # Define the form schema for the first step
        data_schema = vol.Schema(
            {
                vol.Optional("device_name", default=NAME): str,
            }
        )

        # Show the init form
        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=self._errors,
            last_step=False,
        )

    async def async_step_energidataservice(self, user_input=None):
        """Handle the step for energy data services."""
        self._errors = {}

        if user_input is not None:
            # Validate input_sensor and other necessary fields
            if not user_input.get("hsem_energi_data_service_import"):
                self._errors["hsem_energi_data_service_import"] = "required"
            elif not user_input.get("hsem_energi_data_service_export"):
                self._errors["hsem_energi_data_service_export"] = "required"
            else:
                # Save energidata input and move to the next step (working mode)
                self._user_input.update(user_input)
                return await self.async_step_huawei_solar()

        # Define the form schema for energy data services step
        data_schema = vol.Schema(
            {
                vol.Required(
                    "hsem_energi_data_service_import",
                    default=DEFAULT_HSEM_ENERGI_DATA_SERVICE_IMPORT,
                ): selector({"entity": {"domain": "sensor"}}),
                vol.Required(
                    "hsem_energi_data_service_export",
                    default=DEFAULT_HSEM_ENERGI_DATA_SERVICE_EXPORT,
                ): selector({"entity": {"domain": "sensor"}}),
            }
        )

        # Show the form to the user for energy data services
        return self.async_show_form(
            step_id="energidataservice",
            data_schema=data_schema,
            errors=self._errors,
            last_step=False,
        )

    async def async_step_huawei_solar(self, user_input=None):
        """Handle the step for working mode."""
        self._errors = {}

        if user_input is not None:
            # Validate the working mode
            if not user_input.get("hsem_huawei_solar_batteries_working_mode"):
                self._errors["hsem_huawei_solar_batteries_working_mode"] = "required"
            elif not user_input.get("hsem_huawei_solar_batteries_state_of_capacity"):
                self._errors["hsem_huawei_solar_batteries_state_of_capacity"] = (
                    "required"
                )
            elif not user_input.get("hsem_huawei_solar_inverter_active_power_control"):
                self._errors["hsem_huawei_solar_inverter_active_power_control"] = (
                    "required"
                )
            else:
                # Combine user inputs and create the entry
                final_data = {**self._user_input, **user_input}
                return self.async_create_entry(
                    title=final_data.get("device_name", NAME),
                    data=final_data,
                )

        # Define the form schema for working mode step
        data_schema = vol.Schema(
            {
                vol.Required("hsem_huawei_solar_device_id_inverter_1"): selector(
                    {"device": {"integration": "huawei_solar"}}
                ),
                vol.Optional(
                    "hsem_huawei_solar_device_id_inverter_2", default=None
                ): selector({"device": {"integration": "huawei_solar"}}),
                vol.Required("hsem_huawei_solar_device_id_batteries"): selector(
                    {"device": {"integration": "huawei_solar"}}
                ),
                vol.Required(
                    "hsem_huawei_solar_batteries_working_mode",
                    default=DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_WORKING_MODE,
                ): selector({"entity": {"domain": "select"}}),
                vol.Required(
                    "hsem_huawei_solar_batteries_state_of_capacity",
                    default=DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_STATE_OF_CAPACITY,
                ): selector({"entity": {"domain": "sensor"}}),
                vol.Required(
                    "hsem_huawei_solar_inverter_active_power_control",
                    default=DEFAULT_HSEM_HUAWEI_SOLAR_INVERTER_ACTIVE_POWER_CONTROL,
                ): selector({"entity": {"domain": "sensor"}}),
            }
        )

        # Show the form to the user for working mode
        return self.async_show_form(
            step_id="huawei_solar", data_schema=data_schema, errors=self._errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Return the options flow."""
        return HSEMOptionsFlow(config_entry)


class HSEMOptionsFlow(config_entries.OptionsFlow):
    """Options flow for HSEM."""

    def __init__(self, config_entry):
        """Initialize options flow."""
        self.config_entry = config_entry
        self._user_input = {}

    async def async_step_init(self, user_input=None):
        """Handle the initial options step."""
        self._errors = {}

        if user_input is not None:
            if not user_input.get("device_name"):
                self._errors["device_name"] = "required"
            else:
                # Save user input and move to the energy data services step
                self._user_input.update(user_input)
                return await self.async_step_energidataservice()

        # Define the form schema for the first step
        data_schema = vol.Schema(
            {
                vol.Optional(
                    "device_name",
                    default=self.config_entry.options.get("device_name", NAME),
                ): str,
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=data_schema,
            errors=self._errors,
            last_step=False,
        )

    async def async_step_energidataservice(self, user_input=None):
        """Handle the step for energy data services."""
        self._errors = {}

        if user_input is not None:
            if not user_input.get("hsem_energi_data_service_import"):
                self._errors["hsem_energi_data_service_import"] = "required"
            elif not user_input.get("hsem_energi_data_service_export"):
                self._errors["hsem_energi_data_service_export"] = "required"
            else:
                # Save energidata input and move to the next step (working mode)
                self._user_input.update(user_input)
                return await self.async_step_huawei_solar()

        # Define the form schema for energy data services step
        data_schema = vol.Schema(
            {
                vol.Required(
                    "hsem_energi_data_service_import",
                    default=self.config_entry.options.get(
                        "hsem_energi_data_service_import",
                        DEFAULT_HSEM_ENERGI_DATA_SERVICE_IMPORT,
                    ),
                ): selector({"entity": {"domain": "sensor"}}),
                vol.Required(
                    "hsem_energi_data_service_export",
                    default=self.config_entry.options.get(
                        "hsem_energi_data_service_export",
                        DEFAULT_HSEM_ENERGI_DATA_SERVICE_EXPORT,
                    ),
                ): selector({"entity": {"domain": "sensor"}}),
            }
        )

        return self.async_show_form(
            step_id="energidataservice",
            data_schema=data_schema,
            errors=self._errors,
            last_step=False,
        )

    async def async_step_huawei_solar(self, user_input=None):
        """Handle the step for huawei_solar."""
        self._errors = {}

        if user_input is not None:
            if not user_input.get("hsem_huawei_solar_batteries_working_mode"):
                self._errors["hsem_huawei_solar_batteries_working_mode"] = "required"
            elif not user_input.get("hsem_huawei_solar_batteries_state_of_capacity"):
                self._errors["hsem_huawei_solar_batteries_state_of_capacity"] = (
                    "required"
                )
            elif not user_input.get("hsem_huawei_solar_inverter_active_power_control"):
                self._errors["hsem_huawei_solar_inverter_active_power_control"] = (
                    "required"
                )
            else:
                # Combine user inputs and create the entry
                final_data = {**self._user_input, **user_input}
                return self.async_create_entry(
                    title=final_data.get("device_name", NAME),
                    data=final_data,
                )

        # Define the form schema for working mode step
        data_schema = vol.Schema(
            {
                vol.Required(
                    "hsem_huawei_solar_device_id_inverter_1",
                    default=self.config_entry.options.get(
                        "hsem_huawei_solar_device_id_inverter_1"
                    ),
                ): selector({"device": {"integration": "huawei_solar"}}),
                vol.Optional(
                    "hsem_huawei_solar_device_id_inverter_2",
                    default=self.config_entry.options.get(
                        "hsem_huawei_solar_device_id_inverter_2", None
                    ),
                ): selector({"device": {"integration": "huawei_solar"}}),
                vol.Required(
                    "hsem_huawei_solar_device_id_batteries",
                    default=self.config_entry.options.get(
                        "hsem_huawei_solar_device_id_batteries"
                    ),
                ): selector({"device": {"integration": "huawei_solar"}}),
                vol.Required(
                    "hsem_huawei_solar_batteries_working_mode",
                    default=self.config_entry.options.get(
                        "hsem_huawei_solar_batteries_working_mode",
                        DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_WORKING_MODE,
                    ),
                ): selector({"entity": {"domain": "select"}}),
                vol.Required(
                    "hsem_huawei_solar_batteries_state_of_capacity",
                    default=self.config_entry.options.get(
                        "hsem_huawei_solar_batteries_state_of_capacity",
                        DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_STATE_OF_CAPACITY,
                    ),
                ): selector({"entity": {"domain": "sensor"}}),
                vol.Required(
                    "hsem_huawei_solar_inverter_active_power_control",
                    default=self.config_entry.options.get(
                        "hsem_huawei_solar_inverter_active_power_control",
                        DEFAULT_HSEM_HUAWEI_SOLAR_INVERTER_ACTIVE_POWER_CONTROL,
                    ),
                ): selector({"entity": {"domain": "sensor"}}),
            }
        )

        return self.async_show_form(
            step_id="huawei_solar", data_schema=data_schema, errors=self._errors
        )
