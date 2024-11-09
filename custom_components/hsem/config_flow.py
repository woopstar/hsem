import logging

"""
This module defines the configuration flow for the HSEM integration in Home Assistant.

Classes:
    HSEMConfigFlow: Handles the configuration flow for the HSEM integration.
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
    _LOGGER: Logger instance for the module.
    DOMAIN: The domain of the HSEM integration.
    NAME: The name of the HSEM integration.
    DEFAULT_HSEM_*: Default values for various configuration parameters.
"""

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import selector

from .const import (
    DEFAULT_HSEM_BATTERY_CONVERSION_LOSS,
    DEFAULT_HSEM_BATTERY_MAX_CAPACITY,
    DEFAULT_HSEM_ENERGI_DATA_SERVICE_EXPORT,
    DEFAULT_HSEM_ENERGI_DATA_SERVICE_IMPORT,
    DEFAULT_HSEM_EV_CHARGER_POWER,
    DEFAULT_HSEM_EV_CHARGER_STATUS,
    DEFAULT_HSEM_HOUSE_CONSUMPTION_POWER,
    DEFAULT_HSEM_HOUSE_POWER_INCLUDES_EV_CHARGER_POWER,
    DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_MAXIMUM_CHARGING_POWER,
    DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_STATE_OF_CAPACITY,
    DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_WORKING_MODE,
    DEFAULT_HSEM_HUAWEI_SOLAR_INVERTER_ACTIVE_POWER_CONTROL,
    DEFAULT_HSEM_MORNING_ENERGY_NEED,
    DEFAULT_HSEM_SOLAR_PRODUCTION_POWER,
    DEFAULT_HSEM_SOLCAST_PV_FORECAST_FORECAST_TODAY,
    DEFAULT_HSEM_SOLCAST_PV_FORECAST_FORECAST_TOMORROW,
    DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_GRID_CHARGE_CUTOFF_SOC,
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
                vol.Required("device_name", default=NAME): str,
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
                return await self.async_step_power()

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
            if not user_input.get("hsem_huawei_solar_device_id_inverter_1"):
                self._errors["hsem_huawei_solar_device_id_inverter_1"] = "required"
            elif not user_input.get("hsem_huawei_solar_batteries_working_mode"):
                self._errors["hsem_huawei_solar_batteries_working_mode"] = "required"
            elif not user_input.get("hsem_huawei_solar_batteries_state_of_capacity"):
                self._errors["hsem_huawei_solar_batteries_state_of_capacity"] = (
                    "required"
                )
            elif not user_input.get("hsem_huawei_solar_inverter_active_power_control"):
                self._errors["hsem_huawei_solar_inverter_active_power_control"] = (
                    "required"
                )
            elif not user_input.get(
                "hsem_huawei_solar_batteries_maximum_charging_power"
            ):
                self._errors["hsem_huawei_solar_batteries_maximum_charging_power"] = (
                    "required"
                )
            elif not user_input.get("hsem_battery_max_capacity"):
                self._errors["hsem_battery_max_capacity"] = "required"
            elif not user_input.get("hsem_battery_conversion_loss"):
                self._errors["hsem_battery_conversion_loss"] = "required"
            elif not user_input.get("hsem_huawei_solar_batteries_grid_charge_cutoff_soc"):
                self._errors["hsem_huawei_solar_batteries_grid_charge_cutoff_soc"] = (
                    "required"
                )
            else:
                # Combine user inputs and create the entry
                final_data = {**self._user_input, **user_input}

                # Ensure that optional inverter_id is set to an empty string if not provided
                final_data["hsem_huawei_solar_device_id_inverter_2"] = final_data.get(
                    "hsem_huawei_solar_device_id_inverter_2", ""
                )

                # Ensure that optional ev_charger_status is set to an empty string if not provided
                final_data["hsem_ev_charger_status"] = final_data.get(
                    "hsem_ev_charger_status", ""
                )

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
                    "hsem_huawei_solar_device_id_inverter_2", default=""
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
                vol.Required(
                    "hsem_huawei_solar_batteries_maximum_charging_power",
                    default=DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_MAXIMUM_CHARGING_POWER,
                ): selector({"entity": {"domain": "number"}}),
                vol.Required(
                    "hsem_huawei_solar_batteries_grid_charge_cutoff_soc",
                    default=DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_GRID_CHARGE_CUTOFF_SOC,
                ): selector({"entity": {"domain": "number"}}),
                vol.Required(
                    "hsem_battery_max_capacity",
                    default=DEFAULT_HSEM_BATTERY_MAX_CAPACITY,
                ): selector(
                    {
                        "number": {
                            "min": 0,
                            "max": 42,
                            "step": 1,
                            "unit_of_measurement": "kWh",
                            "mode": "slider",
                        }
                    }
                ),
                vol.Required(
                    "hsem_battery_conversion_loss",
                    default=DEFAULT_HSEM_BATTERY_CONVERSION_LOSS,
                ): selector(
                    {
                        "number": {
                            "min": 0,
                            "max": 50,
                            "step": 1,
                            "unit_of_measurement": "%",
                            "mode": "slider",
                        }
                    }
                ),
            }
        )

        # Show the form to the user for working mode
        return self.async_show_form(
            step_id="huawei_solar",
            data_schema=data_schema,
            errors=self._errors,
            last_step=True,
        )

    async def async_step_power(self, user_input=None):
        """Handle the step for power sensors."""
        self._errors = {}

        if user_input is not None:
            # Validate input_sensor and other necessary fields
            if not user_input.get("hsem_house_consumption_power"):
                self._errors["hsem_house_consumption_power"] = "required"
            elif not user_input.get("hsem_solar_production_power"):
                self._errors["hsem_solar_production_power"] = "required"
            else:
                # Save energidata input and move to the next step (working mode)
                self._user_input.update(user_input)
                return await self.async_step_solcast()

        # Define the form schema steps
        data_schema = vol.Schema(
            {
                vol.Required(
                    "hsem_house_consumption_power",
                    default=DEFAULT_HSEM_HOUSE_CONSUMPTION_POWER,
                ): selector({"entity": {"domain": "sensor"}}),
                vol.Required(
                    "hsem_solar_production_power",
                    default=DEFAULT_HSEM_SOLAR_PRODUCTION_POWER,
                ): selector({"entity": {"domain": "sensor"}}),
            }
        )

        # Show the form to the user for energy data services
        return self.async_show_form(
            step_id="power",
            data_schema=data_schema,
            errors=self._errors,
            last_step=False,
        )

    async def async_step_solcast(self, user_input=None):
        """Handle the step for power sensors."""
        self._errors = {}

        if user_input is not None:
            # Validate input_sensor and other necessary fields
            if not user_input.get("hsem_solcast_pv_forecast_forecast_today"):
                self._errors["hsem_solcast_pv_forecast_forecast_today"] = "required"
            elif not user_input.get("hsem_solcast_pv_forecast_forecast_tomorrow"):
                self._errors["hsem_solcast_pv_forecast_forecast_tomorrow"] = "required"
            else:
                # Save energidata input and move to the next step (working mode)
                self._user_input.update(user_input)
                return await self.async_step_misc()

        # Define the form schema steps
        data_schema = vol.Schema(
            {
                vol.Required(
                    "hsem_solcast_pv_forecast_forecast_today",
                    default=DEFAULT_HSEM_SOLCAST_PV_FORECAST_FORECAST_TODAY,
                ): selector({"entity": {"domain": "sensor"}}),
                vol.Required(
                    "hsem_solcast_pv_forecast_forecast_tomorrow",
                    default=DEFAULT_HSEM_SOLCAST_PV_FORECAST_FORECAST_TOMORROW,
                ): selector({"entity": {"domain": "sensor"}}),
            }
        )

        # Show the form to the user for energy data services
        return self.async_show_form(
            step_id="solcast",
            data_schema=data_schema,
            errors=self._errors,
            last_step=False,
        )

    async def async_step_misc(self, user_input=None):
        """Handle the step for power sensors."""
        self._errors = {}

        if user_input is not None:
            # Validate input_sensor and other necessary fields
            if not user_input.get("hsem_morning_energy_need"):
                self._errors["hsem_morning_energy_need"] = "required"
            else:
                # Save energidata input and move to the next step (working mode)
                self._user_input.update(user_input)
                return await self.async_step_huawei_solar()

        # Define the form schema steps
        data_schema = vol.Schema(
            {
                vol.Required(
                    "hsem_morning_energy_need",
                    default=DEFAULT_HSEM_MORNING_ENERGY_NEED,
                ): selector(
                    {
                        "number": {
                            "min": 0,
                            "max": 10,
                            "step": 0.1,
                            "unit_of_measurement": "kWh",
                            "mode": "slider",
                        }
                    }
                ),
                vol.Optional(
                    "hsem_ev_charger_status",
                    default=DEFAULT_HSEM_EV_CHARGER_STATUS,
                ): selector({"entity": {"domain": "sensor"}}),
                vol.Optional(
                    "hsem_ev_charger_power",
                    default=DEFAULT_HSEM_EV_CHARGER_POWER,
                ): selector({"entity": {"domain": "sensor"}}),
                vol.Optional(
                    "hsem_house_power_includes_ev_charger_power",
                    default=DEFAULT_HSEM_HOUSE_POWER_INCLUDES_EV_CHARGER_POWER,
                ): selector({"boolean": {}}),
            }
        )

        # Show the form to the user for energy data services
        return self.async_show_form(
            step_id="misc",
            data_schema=data_schema,
            errors=self._errors,
            last_step=False,
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

    def update_config_entry_data(self):
        """Update config_entry.data with the latest configuration values from options."""
        updated_data = {**self.config_entry.data, **self.config_entry.options}
        self.hass.config_entries.async_update_entry(
            self.config_entry, data=updated_data
        )

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
                vol.Required(
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
                return await self.async_step_power()

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
            if not user_input.get("hsem_huawei_solar_device_id_inverter_1"):
                self._errors["hsem_huawei_solar_device_id_inverter_1"] = "required"
            elif not user_input.get("hsem_huawei_solar_device_id_batteries"):
                self._errors["hsem_huawei_solar_device_id_batteries"] = "required"
            elif not user_input.get("hsem_huawei_solar_batteries_working_mode"):
                self._errors["hsem_huawei_solar_batteries_working_mode"] = "required"
            elif not user_input.get("hsem_huawei_solar_batteries_state_of_capacity"):
                self._errors["hsem_huawei_solar_batteries_state_of_capacity"] = (
                    "required"
                )
            elif not user_input.get("hsem_huawei_solar_inverter_active_power_control"):
                self._errors["hsem_huawei_solar_inverter_active_power_control"] = (
                    "required"
                )
            elif not user_input.get("hsem_huawei_solar_inverter_active_power_control"):
                self._errors["hsem_huawei_solar_inverter_active_power_control"] = (
                    "required"
                )
            elif not user_input.get(
                "hsem_huawei_solar_batteries_maximum_charging_power"
            ):
                self._errors["hsem_huawei_solar_batteries_maximum_charging_power"] = (
                    "required"
                )
            elif not user_input.get("hsem_battery_max_capacity"):
                self._errors["hsem_battery_max_capacity"] = "required"
            elif not user_input.get("hsem_battery_conversion_loss"):
                self._errors["hsem_battery_conversion_loss"] = "required"
            elif not user_input.get("hsem_huawei_solar_batteries_grid_charge_cutoff_soc"):
                self._errors["hsem_huawei_solar_batteries_grid_charge_cutoff_soc"] = (
                    "required"
                )
            else:
                # Combine user inputs and create the entry
                final_data = {**self._user_input, **user_input}
                self.update_config_entry_data()
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
                        "hsem_huawei_solar_device_id_inverter_2", ""
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
                vol.Required(
                    "hsem_huawei_solar_batteries_maximum_charging_power",
                    default=self.config_entry.options.get(
                        "hsem_huawei_solar_batteries_maximum_charging_power",
                        DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_MAXIMUM_CHARGING_POWER,
                    ),
                ): selector({"entity": {"domain": "number"}}),
                vol.Required(
                    "hsem_huawei_solar_batteries_grid_charge_cutoff_soc",
                    default=self.config_entry.options.get(
                        "hsem_huawei_solar_batteries_grid_charge_cutoff_soc",
                        DEFAULT_HSEM_HUAWEI_SOLAR_BATTERIES_GRID_CHARGE_CUTOFF_SOC,
                    ),
                ): selector({"entity": {"domain": "number"}}),
                vol.Required(
                    "hsem_battery_max_capacity",
                    default=self.config_entry.options.get(
                        "hsem_battery_max_capacity",
                        DEFAULT_HSEM_BATTERY_MAX_CAPACITY,
                    ),
                ): selector(
                    {
                        "number": {
                            "min": 0,
                            "max": 42,
                            "step": 1,
                            "unit_of_measurement": "kWh",
                            "mode": "slider",
                        }
                    }
                ),
                vol.Required(
                    "hsem_battery_conversion_loss",
                    default=self.config_entry.options.get(
                        "hsem_battery_conversion_loss",
                        DEFAULT_HSEM_BATTERY_CONVERSION_LOSS,
                    ),
                ): selector(
                    {
                        "number": {
                            "min": 0,
                            "max": 50,
                            "step": 1,
                            "unit_of_measurement": "%",
                            "mode": "slider",
                        }
                    }
                ),
            }
        )

        return self.async_show_form(
            step_id="huawei_solar", data_schema=data_schema, errors=self._errors
        )

    async def async_step_power(self, user_input=None):
        """Handle the step for energy data services."""
        self._errors = {}

        if user_input is not None:
            if not user_input.get("hsem_house_consumption_power"):
                self._errors["hsem_house_consumption_power"] = "required"
            elif not user_input.get("hsem_solar_production_power"):
                self._errors["hsem_solar_production_power"] = "required"
            else:
                # Save energidata input and move to the next step (working mode)
                self._user_input.update(user_input)
                return await self.async_step_solcast()

        # Define the form schema for energy data services step
        data_schema = vol.Schema(
            {
                vol.Required(
                    "hsem_house_consumption_power",
                    default=self.config_entry.options.get(
                        "hsem_house_consumption_power",
                        DEFAULT_HSEM_HOUSE_CONSUMPTION_POWER,
                    ),
                ): selector({"entity": {"domain": "sensor"}}),
                vol.Required(
                    "hsem_solar_production_power",
                    default=self.config_entry.options.get(
                        "hsem_solar_production_power",
                        DEFAULT_HSEM_SOLAR_PRODUCTION_POWER,
                    ),
                ): selector({"entity": {"domain": "sensor"}}),
            }
        )

        return self.async_show_form(
            step_id="power",
            data_schema=data_schema,
            errors=self._errors,
            last_step=False,
        )

    async def async_step_solcast(self, user_input=None):
        """Handle the step for energy data services."""
        self._errors = {}

        if user_input is not None:
            if not user_input.get("hsem_solcast_pv_forecast_forecast_today"):
                self._errors["hsem_solcast_pv_forecast_forecast_today"] = "required"
            elif not user_input.get("hsem_solcast_pv_forecast_forecast_tomorrow"):
                self._errors["hsem_solcast_pv_forecast_forecast_tomorrow"] = "required"
            else:
                # Save energidata input and move to the next step (working mode)
                self._user_input.update(user_input)
                return await self.async_step_misc()

        # Define the form schema for energy data services step
        data_schema = vol.Schema(
            {
                vol.Required(
                    "hsem_solcast_pv_forecast_forecast_today",
                    default=self.config_entry.options.get(
                        "hsem_solcast_pv_forecast_forecast_today",
                        DEFAULT_HSEM_SOLCAST_PV_FORECAST_FORECAST_TODAY,
                    ),
                ): selector({"entity": {"domain": "sensor"}}),
                vol.Required(
                    "hsem_solcast_pv_forecast_forecast_tomorrow",
                    default=self.config_entry.options.get(
                        "hsem_solcast_pv_forecast_forecast_tomorrow",
                        DEFAULT_HSEM_SOLCAST_PV_FORECAST_FORECAST_TOMORROW,
                    ),
                ): selector({"entity": {"domain": "sensor"}}),
            }
        )

        return self.async_show_form(
            step_id="solcast",
            data_schema=data_schema,
            errors=self._errors,
            last_step=False,
        )

    async def async_step_misc(self, user_input=None):
        """Handle the step for energy data services."""
        self._errors = {}

        if user_input is not None:
            if not user_input.get("hsem_morning_energy_need"):
                self._errors["hsem_morning_energy_need"] = "required"
            else:
                # Save energidata input and move to the next step (working mode)
                self._user_input.update(user_input)
                return await self.async_step_huawei_solar()

        # Define the form schema for energy data services step
        data_schema = vol.Schema(
            {
                vol.Required(
                    "hsem_morning_energy_need",
                    default=self.config_entry.options.get(
                        "hsem_morning_energy_need",
                        DEFAULT_HSEM_MORNING_ENERGY_NEED,
                    ),
                ): selector(
                    {
                        "number": {
                            "min": 0,
                            "max": 10,
                            "step": 0.1,
                            "unit_of_measurement": "kWh",
                            "mode": "slider",
                        }
                    }
                ),
                vol.Optional(
                    "hsem_ev_charger_status",
                    default=self.config_entry.options.get(
                        "hsem_ev_charger_status",
                        DEFAULT_HSEM_EV_CHARGER_STATUS,
                    ),
                ): selector({"entity": {"domain": "sensor"}}),
                vol.Optional(
                    "hsem_ev_charger_power",
                    default=self.config_entry.options.get(
                        "hsem_ev_charger_power",
                        DEFAULT_HSEM_EV_CHARGER_POWER,
                    ),
                ): selector({"entity": {"domain": "sensor"}}),
                vol.Optional(
                    "hsem_house_power_includes_ev_charger_power",
                    default=self.config_entry.options.get(
                        "hsem_house_power_includes_ev_charger_power",
                        DEFAULT_HSEM_HOUSE_POWER_INCLUDES_EV_CHARGER_POWER,
                    ),
                ): selector({"boolean": {}}),
            }
        )

        return self.async_show_form(
            step_id="misc",
            data_schema=data_schema,
            errors=self._errors,
            last_step=False,
        )
