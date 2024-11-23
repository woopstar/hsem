"""
This module defines the configuration flow for the HSEM integration in Home Assistant.

Classes:
    HSEMOptionsFlow: Handles the options flow for the HSEM integration.

Functions:
    async_step_user: Handles the initial step of the configuration flow.
    async_step_energidataservice: Handles the step for energy data services configuration.
    async_step_huawei_solar: Handles the step for Huawei solar configuration.
    async_step_power: Handles the step for power sensors configuration.
    async_step_solcast: Handles the step for Solcast PV forecast configuration.
    async_step_misc: Handles the step for miscellaneous configuration.
    async_get_options_flow: Returns the options flow for the HSEM integration.

Attributes:
    DOMAIN: The domain of the HSEM integration.
    NAME: The name of the HSEM integration.
    DEFAULT_HSEM_*: Default values for various configuration parameters.
"""


from homeassistant import config_entries

from custom_components.hsem.const import (
    NAME,
)

from custom_components.hsem.flows.init import get_init_step_schema, validate_init_step_input
from custom_components.hsem.flows.ev import get_ev_step_schema, validate_ev_step_input
from custom_components.hsem.flows.energidataservice import get_energidataservice_step_schema, validate_energidataservice_input
from custom_components.hsem.flows.power import get_power_step_schema, validate_power_step_input
from custom_components.hsem.flows.solcast import get_solcast_step_schema, validate_solcast_step_input
from custom_components.hsem.flows.weighted_values import get_weighted_values_step_schema, validate_weighted_values_input
from custom_components.hsem.flows.charge_hours import get_charge_hours_step_schema, validate_charge_hours_input
from custom_components.hsem.flows.huawei_solar import get_huawei_solar_step_schema, validate_huawei_solar_input

class HSEMOptionsFlow(config_entries.OptionsFlow):
    """Options flow for HSEM."""

    def __init__(self, config_entry):
        self.config_entry = config_entry
        self._user_input = {}

    def update_config_entry_data(self):
        """Update config_entry.data with the latest configuration values from options."""
        updated_data = {**self.config_entry.data, **self.config_entry.options}
        self.hass.config_entries.async_update_entry(
            self.config_entry, data=updated_data
        )

    async def async_step_init(self, user_input=None):
        errors = {}

        if user_input is not None:
            errors = validate_init_step_input(user_input)
            if not errors:
                self._user_input.update(user_input)
                return await self.async_step_energidataservice()

        data_schema = get_init_step_schema(self.config_entry)

        return self.async_show_form(
            step_id="init",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_energidataservice(self, user_input=None):
        errors = {}

        if user_input is not None:
            errors = validate_energidataservice_input(user_input)
            if not errors:
                self._user_input.update(user_input)
                return await self.async_step_power()

        data_schema = get_energidataservice_step_schema(self.config_entry)

        return self.async_show_form(
            step_id="energidataservice",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_power(self, user_input=None):
        errors = {}

        if user_input is not None:
            errors = validate_power_step_input(user_input)
            if not errors:
                self._user_input.update(user_input)
                return await self.async_step_solcast()

        data_schema = get_power_step_schema(self.config_entry)

        return self.async_show_form(
            step_id="power",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_solcast(self, user_input=None):
        errors = {}

        if user_input is not None:
            errors = validate_solcast_step_input(user_input)
            if not errors:
                self._user_input.update(user_input)
                return await self.async_step_ev()

        # Define the form schema for energy data services step
        data_schema = get_solcast_step_schema(self.config_entry)

        return self.async_show_form(
            step_id="solcast",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_ev(self, user_input=None):
        errors = {}

        if user_input is not None:
            errors = validate_ev_step_input(user_input)
            if not errors:
                self._user_input.update(user_input)
                return await self.async_step_weighted_values()

        data_schema = get_ev_step_schema(self.config_entry)

        return self.async_show_form(
            step_id="ev",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_weighted_values(self, user_input=None):
        errors = {}

        if user_input is not None:
            errors = validate_weighted_values_input(user_input)
            if not errors:
                self._user_input.update(user_input)
                return await self.async_step_charge_hours()

        data_schema = get_weighted_values_step_schema(self.config_entry)

        return self.async_show_form(
            step_id="weighted_values",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_charge_hours(self, user_input=None):
        errors = {}

        if user_input is not None:
            errors = validate_charge_hours_input(user_input)
            if not errors:
                self._user_input.update(user_input)
                return await self.async_step_huawei_solar()

        data_schema = get_charge_hours_step_schema(self.config_entry)

        return self.async_show_form(
            step_id="charge_hours",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_huawei_solar(self, user_input=None):
        errors = {}

        if user_input is not None:
            errors = validate_huawei_solar_input(user_input)
            if not errors:
                final_data = {**self._user_input, **user_input}
                self.update_config_entry_data()
                return self.async_create_entry(
                    title=final_data.get("device_name", NAME),
                    data=final_data,
                )

        data_schema = get_huawei_solar_step_schema(self.config_entry)

        return self.async_show_form(
            step_id="huawei_solar",
            data_schema=data_schema,
            errors=errors,
            last_step=True,
        )
