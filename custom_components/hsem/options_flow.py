"""This module defines the configuration flow for the HSEM integration in Home Assistant."""

from typing import Any

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult

from custom_components.hsem.const import NAME
from custom_components.hsem.flows.batteries_excess_export import (
    get_batteries_excess_export_step_schema,
    validate_batteries_excess_export_input,
)
from custom_components.hsem.flows.batteries_schedules import (
    get_batteries_schedules_step_schema,
    validate_batteries_schedules_input,
)
from custom_components.hsem.flows.battery_economics import (
    get_battery_economics_step_schema,
    validate_battery_economics_input,
)
from custom_components.hsem.flows.ev import get_ev_step_schema, validate_ev_step_input
from custom_components.hsem.flows.ev_planned_load import (
    get_ev_planned_load_step_schema,
    validate_ev_planned_load_input,
)
from custom_components.hsem.flows.ev_second import (
    get_ev_second_step_schema,
    validate_ev_second_step_input,
)
from custom_components.hsem.flows.ev_second_planned_load import (
    get_ev_second_planned_load_step_schema,
    validate_ev_second_planned_load_input,
)
from custom_components.hsem.flows.huawei_solar import (
    get_huawei_solar_step_schema,
    validate_huawei_solar_input,
)
from custom_components.hsem.flows.init import (
    get_init_step_schema,
    validate_init_step_input,
)
from custom_components.hsem.flows.months import get_months_schema, validate_months_input
from custom_components.hsem.flows.power import (
    get_power_step_schema,
    validate_power_step_input,
)
from custom_components.hsem.flows.prices import (
    get_prices_step_schema,
    validate_prices_input,
)
from custom_components.hsem.flows.solcast import (
    get_solcast_step_schema,
    validate_solcast_step_input,
)
from custom_components.hsem.flows.weighted_values import (
    get_weighted_values_step_schema,
    validate_weighted_values_input,
)
from custom_components.hsem.utils.misc import convert_months_to_int


class HSEMOptionsFlow(config_entries.OptionsFlow):
    """Options flow for HSEM."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry
        self._user_input: dict[str, Any] = {}

    def update_config_entry_data(self) -> None:
        """Update config_entry.data with the latest configuration values from options."""
        updated_data = {**self._config_entry.data, **self._config_entry.options}
        self.hass.config_entries.async_update_entry(
            self._config_entry, data=updated_data
        )

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}

        if user_input is not None:
            errors = await validate_init_step_input(user_input)
            if not errors:
                self._user_input.update(user_input)
                return await self.async_step_prices()

        data_schema = await get_init_step_schema(self._config_entry)

        return self.async_show_form(
            step_id="init",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_prices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}

        if user_input is not None:
            errors = await validate_prices_input(self.hass, user_input)
            if not errors:
                self._user_input.update(user_input)
                return await self.async_step_months()

        data_schema = await get_prices_step_schema(self._config_entry)

        return self.async_show_form(
            step_id="prices",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_months(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}

        if user_input is not None:
            errors = await validate_months_input(self.hass, user_input)
            if not errors:
                # Convert winter months to integers
                winter_months = convert_months_to_int(
                    user_input.get("hsem_months_winter", [])
                )
                self._user_input.update(user_input)

                # Calculate summer months as the complement of winter months
                all_months = set(range(1, 13))
                summer_months = sorted(list(all_months - set(winter_months)))

                # Update both winter and summer months as integers
                self._user_input["hsem_months_winter"] = winter_months
                self._user_input["hsem_months_summer"] = summer_months

                return await self.async_step_solcast()

        data_schema = await get_months_schema(self._config_entry)

        return self.async_show_form(
            step_id="months",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_solcast(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}

        if user_input is not None:
            errors = await validate_solcast_step_input(self.hass, user_input)
            if not errors:
                self._user_input.update(user_input)
                return await self.async_step_huawei_solar()

        data_schema = await get_solcast_step_schema(self._config_entry)

        return self.async_show_form(
            step_id="solcast",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_huawei_solar(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}

        if user_input is not None:
            errors = await validate_huawei_solar_input(self.hass, user_input)
            if not errors:
                self._user_input.update(user_input)
                return await self.async_step_battery_economics()

        data_schema = await get_huawei_solar_step_schema(self._config_entry)

        return self.async_show_form(
            step_id="huawei_solar",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_battery_economics(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}

        if user_input is not None:
            errors = await validate_battery_economics_input(user_input)
            if not errors:
                self._user_input.update(user_input)
                return await self.async_step_power()

        data_schema = await get_battery_economics_step_schema(self._config_entry)

        return self.async_show_form(
            step_id="battery_economics",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_power(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}

        if user_input is not None:
            errors = await validate_power_step_input(self.hass, user_input)
            if not errors:
                self._user_input.update(user_input)
                return await self.async_step_ev()

        data_schema = await get_power_step_schema(self._config_entry)

        return self.async_show_form(
            step_id="power",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_ev(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}

        if user_input is not None:
            errors = await validate_ev_step_input(self.hass, user_input)
            if not errors:
                self._user_input.update(user_input)

                if bool(self._user_input.get("hsem_ev_second_enabled")):
                    return await self.async_step_ev_second()

                return await self.async_step_ev_planned_load()

        data_schema = await get_ev_step_schema(self._config_entry)

        return self.async_show_form(
            step_id="ev",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_ev_second(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}

        if user_input is not None:
            errors = await validate_ev_second_step_input(self.hass, user_input)
            if not errors:
                self._user_input.update(user_input)
                return await self.async_step_ev_planned_load()

        data_schema = await get_ev_second_step_schema(self._config_entry)

        return self.async_show_form(
            step_id="ev_second",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_ev_planned_load(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}

        if user_input is not None:
            errors = await validate_ev_planned_load_input(self.hass, user_input)
            if not errors:
                self._user_input.update(user_input)
                if bool(self._user_input.get("hsem_ev_second_enabled")):
                    return await self.async_step_ev_second_planned_load()
                return await self.async_step_batteries_schedules()

        data_schema = await get_ev_planned_load_step_schema(self._config_entry)

        return self.async_show_form(
            step_id="ev_planned_load",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_ev_second_planned_load(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}

        if user_input is not None:
            errors = await validate_ev_second_planned_load_input(self.hass, user_input)
            if not errors:
                self._user_input.update(user_input)
                return await self.async_step_batteries_schedules()

        data_schema = await get_ev_second_planned_load_step_schema(self._config_entry)

        return self.async_show_form(
            step_id="ev_second_planned_load",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_batteries_schedules(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}

        if user_input is not None:
            errors = await validate_batteries_schedules_input(user_input)
            if not errors:
                self._user_input.update(user_input)
                return await self.async_step_batteries_excess_export()

        data_schema = await get_batteries_schedules_step_schema(
            self._config_entry, hass=self.hass
        )

        return self.async_show_form(
            step_id="batteries_schedules",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_batteries_excess_export(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}

        if user_input is not None:
            errors = await validate_batteries_excess_export_input(user_input)
            if not errors:
                self._user_input.update(user_input)
                return await self.async_step_weighted_values()

        data_schema = await get_batteries_excess_export_step_schema(
            self._config_entry, hass=self.hass
        )

        return self.async_show_form(
            step_id="batteries_excess_export",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_weighted_values(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}

        if user_input is not None:
            errors = await validate_weighted_values_input(user_input)
            if not errors:
                self._user_input.update(user_input)

                self.update_config_entry_data()

                return self.async_create_entry(
                    title=self._user_input.get("device_name", NAME),
                    data=self._user_input,
                )

        data_schema = await get_weighted_values_step_schema(self._config_entry)

        return self.async_show_form(
            step_id="weighted_values",
            data_schema=data_schema,
            errors=errors,
            last_step=True,
        )
