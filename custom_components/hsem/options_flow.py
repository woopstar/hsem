"""
This module defines the configuration flow for the HSEM integration in Home Assistant.
"""

from homeassistant import config_entries

from custom_components.hsem.const import NAME
from custom_components.hsem.flows.batteries_schedule import (
    get_batteries_schedule_step_schema,
    validate_batteries_schedule_input,
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


class HSEMOptionsFlow(config_entries.OptionsFlow):
    """Options flow for HSEM."""

    def __init__(self, config_entry):
        self._config_entry = config_entry
        self._user_input = {}

    def update_config_entry_data(self):
        """Update config_entry.data with the latest configuration values from options."""
        updated_data = {**self._config_entry.data, **self._config_entry.options}
        self.hass.config_entries.async_update_entry(
            self._config_entry, data=updated_data
        )

    async def async_step_init(self, user_input=None):
        errors = {}

        if user_input is not None:
            errors = await validate_init_step_input(user_input)
            if not errors:
                self._user_input.update(user_input)
                return await self.async_step_energidataservice()

        data_schema = await get_init_step_schema(self._config_entry)

        return self.async_show_form(
            step_id="init",
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

        data_schema = await get_energidataservice_step_schema(self._config_entry)

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

        data_schema = await get_power_step_schema(self._config_entry)

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
        data_schema = await get_solcast_step_schema(self._config_entry)

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

        data_schema = await get_ev_step_schema(self._config_entry)

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
                return await self.async_step_batteries_schedule()

        data_schema = await get_weighted_values_step_schema(self._config_entry)

        return self.async_show_form(
            step_id="weighted_values",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_batteries_schedule(self, user_input=None):
        errors = {}

        if user_input is not None:
            errors = await validate_batteries_schedule_input(user_input)
            if not errors:
                self._user_input.update(user_input)
                return await self.async_step_huawei_solar()

        data_schema = await get_batteries_schedule_step_schema(self._config_entry)

        return self.async_show_form(
            step_id="batteries_schedule",
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
                self.update_config_entry_data()
                return self.async_create_entry(
                    title=final_data.get("device_name", NAME),
                    data=final_data,
                )

        data_schema = await get_huawei_solar_step_schema(self._config_entry)

        return self.async_show_form(
            step_id="huawei_solar",
            data_schema=data_schema,
            errors=errors,
            last_step=True,
        )
