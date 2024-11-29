"""
This module defines the configuration flow for the HSEM integration in Home Assistant.
"""

import uuid

from homeassistant import config_entries
from homeassistant.core import callback

from custom_components.hsem.const import DOMAIN, NAME
from custom_components.hsem.flows.charge_hours import (
    get_charge_hours_step_schema,
    validate_charge_hours_input,
)
from custom_components.hsem.flows.energidataservice import (
    get_energidataservice_step_schema,
    validate_energidataservice_input,
)
from custom_components.hsem.flows.ev import get_ev_step_schema, validate_ev_step_input
from custom_components.hsem.flows.huawei_solar import (
    get_huawei_solar_step_schema,
    validate_huawei_solar_input,
)
from custom_components.hsem.flows.init import (
    get_init_step_schema,
    validate_init_step_input,
)
from custom_components.hsem.flows.power import (
    get_power_step_schema,
    validate_power_step_input,
)
from custom_components.hsem.flows.solcast import (
    get_solcast_step_schema,
    validate_solcast_step_input,
)
from custom_components.hsem.flows.weighted_values import (
    get_weighted_values_step_schema,
    validate_weighted_values_input,
)
from custom_components.hsem.options_flow import HSEMOptionsFlow


class HSEMConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for HSEM."""

    VERSION = 1
    _user_input = {}

    async def async_step_user(self, user_input=None):
        errors = {}

        # Abort if a config entry with the same unique ID already exists
        self._abort_if_unique_id_configured()

        # Check if there's already an entry for this domain
        existing_entries = self.hass.config_entries.async_entries(DOMAIN)
        if existing_entries:
            errors["base"] = "only_one_entry_allowed"
            return self.async_show_form(step_id="user", errors=errors)

        # If user_input is not None, the user has submitted the form
        if user_input is not None:
            errors = await validate_init_step_input(user_input)
            if not errors:
                self._user_input.update(user_input)
                return await self.async_step_energidataservice()

        data_schema = await get_init_step_schema(None)

        # Show the init form
        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_energidataservice(self, user_input=None):
        errors = {}

        if user_input is not None:
            errors = await validate_energidataservice_input(self.hass, user_input)
            if not errors:
                self._user_input.update(user_input)
                return await self.async_step_power()

        data_schema = await get_energidataservice_step_schema(None)

        return self.async_show_form(
            step_id="energidataservice",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_power(self, user_input=None):
        errors = {}

        if user_input is not None:
            errors = await validate_power_step_input(self.hass, user_input)
            if not errors:
                self._user_input.update(user_input)
                return await self.async_step_solcast()

        data_schema = await get_power_step_schema(None)

        return self.async_show_form(
            step_id="power",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_solcast(self, user_input=None):
        errors = {}

        if user_input is not None:
            errors = await validate_solcast_step_input(self.hass, user_input)
            if not errors:
                self._user_input.update(user_input)
                return await self.async_step_ev()

        # Define the form schema for energy data services step
        data_schema = await get_solcast_step_schema(None)

        return self.async_show_form(
            step_id="solcast",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_ev(self, user_input=None):
        errors = {}

        if user_input is not None:
            errors = await validate_ev_step_input(self.hass, user_input)
            if not errors:
                self._user_input.update(user_input)
                return await self.async_step_weighted_values()

        data_schema = await get_ev_step_schema(None)

        return self.async_show_form(
            step_id="ev",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_weighted_values(self, user_input=None):
        errors = {}

        if user_input is not None:
            errors = await validate_weighted_values_input(user_input)
            if not errors:
                self._user_input.update(user_input)
                return await self.async_step_charge_hours()

        data_schema = await get_weighted_values_step_schema(None)

        return self.async_show_form(
            step_id="weighted_values",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_charge_hours(self, user_input=None):
        errors = {}

        if user_input is not None:
            errors = await validate_charge_hours_input(user_input)
            if not errors:
                self._user_input.update(user_input)
                return await self.async_step_huawei_solar()

        data_schema = await get_charge_hours_step_schema(None)

        return self.async_show_form(
            step_id="charge_hours",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_huawei_solar(self, user_input=None):
        errors = {}

        if user_input is not None:
            errors = await validate_huawei_solar_input(self.hass, user_input)
            if not errors:
                final_data = {**self._user_input, **user_input}

                # Ensure that optional inverter_id is set to an empty string if not provided
                final_data["hsem_huawei_solar_device_id_inverter_2"] = final_data.get(
                    "hsem_huawei_solar_device_id_inverter_2", ""
                )

                # Ensure that optional ev_charger_status is set to an empty string if not provided
                final_data["hsem_ev_charger_status"] = final_data.get(
                    "hsem_ev_charger_status", None
                )
                final_data["hsem_ev_charger_power"] = final_data.get(
                    "hsem_ev_charger_power", None
                )

                # Set unique ID for this config flow based on DOMAIN
                await self.async_set_unique_id(str(uuid.uuid4()))

                return self.async_create_entry(
                    title=final_data.get("device_name", NAME),
                    data=final_data,
                )

        data_schema = await get_huawei_solar_step_schema(None)

        return self.async_show_form(
            step_id="huawei_solar",
            data_schema=data_schema,
            errors=errors,
            last_step=True,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Return the options flow."""
        return HSEMOptionsFlow(config_entry)
