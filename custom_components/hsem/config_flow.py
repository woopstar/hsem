"""This module defines the configuration flow for the HSEM integration in Home Assistant."""

import logging
from typing import Any

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.core import HomeAssistant, callback

from custom_components.hsem.const import DOMAIN, NAME
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
from custom_components.hsem.flows.energy_and_ml import (
    get_energy_and_ml_step_schema,
    validate_energy_and_ml_input,
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
from custom_components.hsem.options_flow import HSEMOptionsFlow
from custom_components.hsem.utils.conversion import convert_months_to_int

_LOGGER = logging.getLogger(__name__)

# Keys that were renamed between config version 1 (v5.1.0 era) and
# version 2 (v6.0.0).  The left-hand side is the v1 name; the
# right-hand side is the v2 replacement.
_V1_TO_V2_KEY_RENAMES: dict[str, str] = {
    "hsem_energi_data_service_export_min_price": "hsem_export_electricity_min_price",
    "hsem_energi_data_service_update_interval": "hsem_electricity_price_update_interval",
    "hsem_energi_data_service_export": "hsem_export_electricity_price_sensor",
    "hsem_energi_data_service_import": "hsem_import_electricity_price_sensor",
}

# Keys that existed in v1 but have no equivalent in v2 (removed).
_V1_DEPRECATED_KEYS: frozenset[str] = frozenset(
    {
        "hsem_batteries_enable_batteries_schedule_1_min_price_difference",
        "hsem_batteries_enable_batteries_schedule_2_min_price_difference",
        "hsem_batteries_enable_batteries_schedule_3_min_price_difference",
        "hsem_batteries_conversion_loss",
    }
)

# New keys introduced in v2 that did not exist in v1.  When
# migrating a v1 entry these are backfilled with their defaults.
_V2_NEW_KEY_DEFAULTS: dict[str, Any] = {
    # Battery economics
    "hsem_batteries_charge_efficiency": 98,
    "hsem_batteries_discharge_efficiency": 98,
    "hsem_batteries_purchase_price": 0.0,
    "hsem_batteries_expected_cycles": 6000,
    "hsem_batteries_cycle_cost": 0.0,
    "hsem_batteries_capacity_loss_pct": 30,
    # Excess export
    "hsem_batteries_enable_excess_export": False,
    "hsem_batteries_excess_export_discharge_buffer": 10,
    # Energy price forecast sensors (optional — None = not configured)
    "hsem_import_electricity_price_forecast_sensor": None,
    "hsem_export_electricity_price_forecast_sensor": None,
    # EV smart charging flags
    "hsem_ev_target_soc": 80,
    "hsem_ev_deadline_time": "07:00",
    "hsem_ev_smart_charging": False,
    "hsem_ev_force_charge_now": False,
    "hsem_ev_second_target_soc": 80,
    "hsem_ev_second_deadline_time": "07:00",
    "hsem_ev_second_smart_charging": False,
    "hsem_ev_second_force_charge_now": False,
    # EV planned load (disabled by default)
    "hsem_ev_planned_load_enabled": False,
    "hsem_ev_planned_load_battery_capacity_kwh": 0.0,
    "hsem_ev_planned_load_charger_power_kw": 0.0,
    "hsem_ev_planned_load_charger_efficiency": 100,
    "hsem_ev_planned_load_charger_min_power_w": 1380,
    "hsem_ev_second_planned_load_enabled": False,
    "hsem_ev_second_planned_load_battery_capacity_kwh": 0.0,
    "hsem_ev_second_planned_load_charger_power_kw": 0.0,
    "hsem_ev_second_planned_load_charger_efficiency": 100,
    "hsem_ev_second_planned_load_charger_min_power_w": 1380,
    # Planner hysteresis
    "hsem_planner_hysteresis_enabled": True,
    "hsem_planner_hysteresis_absolute": 0.0,
    "hsem_planner_hysteresis_percentage": 5.0,
    "hsem_planner_window_hysteresis_minutes": 0,
    "hsem_planner_min_resolve_interval_minutes": 15,
    # Huawei Solar additions in v2
    "hsem_huawei_solar_batteries_charging_cutoff_capacity": (
        "number.batteries_end_of_charge_soc"
    ),
    "hsem_huawei_solar_batteries_forcible_charge": ("sensor.batteries_forcible_charge"),
}


class HSEMConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):  # pyright: ignore[reportGeneralTypeIssues]  # HA ConfigFlow class hierarchy triggers false-positive on MRO
    """Config flow for HSEM."""

    VERSION = 2

    async def async_migrate_entry(
        self, hass: HomeAssistant, config_entry: ConfigEntry
    ) -> bool:
        """Migrate old config entries to the current version."""
        _LOGGER.debug(
            "Migrating config entry %s from version %s to %s",
            config_entry.entry_id,
            config_entry.version,
            self.VERSION,
        )

        if config_entry.version > self.VERSION:
            _LOGGER.error(
                "Config entry %s has future version %s (current %s) — cannot migrate",
                config_entry.entry_id,
                config_entry.version,
                self.VERSION,
            )
            return False

        if config_entry.version == 1:
            data = _migrate_v1_to_v2(dict(config_entry.data))
            hass.config_entries.async_update_entry(config_entry, data=data, version=2)
            _LOGGER.info(
                "Config entry %s migrated from v1 to v2",
                config_entry.entry_id,
            )

        return True

    def __init__(self) -> None:
        """Initialise the config flow with instance-level state.

        Each flow instance owns its own ``_user_input`` dict so that
        concurrent or sequential config flow instances cannot share or
        corrupt each other's in-progress user data.
        """
        super().__init__()
        self._user_input: dict = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the user config flow step.

        Validates user input and advances to the next step in the config flow.
        """
        errors = {}

        # Set a stable, deterministic unique id so that the guard below
        # can detect a second config flow immediately — before the user
        # fills in any form fields.  Using the integration domain as the
        # unique id enforces the "only one entry" constraint.
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        # If user_input is not None, the user has submitted the form.
        if user_input is not None:
            errors = await validate_init_step_input(user_input)
            if not errors:
                self._user_input.update(user_input)
                return await self.async_step_prices()

        data_schema = await get_init_step_schema(None)

        # Show the init form.
        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_prices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the prices config flow step.

        Validates user input and advances to the next step in the config flow.
        """
        errors = {}

        if user_input is not None:
            errors = await validate_prices_input(self.hass, user_input)
            if not errors:
                self._user_input.update(user_input)
                return await self.async_step_months()

        data_schema = await get_prices_step_schema(None)

        return self.async_show_form(
            step_id="prices",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_months(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the months config flow step.

        Validates user input and advances to the next step in the config flow.
        """
        errors = {}

        if user_input is not None:
            errors = await validate_months_input(self.hass, user_input)
            if not errors:
                # Convert winter months to integers.
                winter_months = convert_months_to_int(
                    user_input.get("hsem_months_winter", [])
                )
                self._user_input.update(user_input)

                # Calculate summer months as the complement of winter months.
                all_months = set(range(1, 13))
                summer_months = sorted(all_months - set(winter_months))

                # Update both winter and summer months as integers.
                self._user_input["hsem_months_winter"] = winter_months
                self._user_input["hsem_months_summer"] = summer_months

                return await self.async_step_solcast()

        data_schema = await get_months_schema(None)

        return self.async_show_form(
            step_id="months",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_solcast(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the solcast config flow step.

        Validates user input and advances to the next step in the config flow.
        """
        errors = {}

        if user_input is not None:
            errors = await validate_solcast_step_input(self.hass, user_input)
            if not errors:
                self._user_input.update(user_input)
                return await self.async_step_huawei_solar()

        data_schema = await get_solcast_step_schema(None)

        return self.async_show_form(
            step_id="solcast",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_huawei_solar(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the huawei_solar config flow step.

        Validates user input and advances to the next step in the config flow.
        """
        errors = {}

        if user_input is not None:
            errors = await validate_huawei_solar_input(self.hass, user_input)
            if not errors:
                self._user_input.update(user_input)

                # Ensure that optional inverter_id is set to an empty string if not provided.
                self._user_input["hsem_huawei_solar_device_id_inverter_2"] = (
                    self._user_input.get("hsem_huawei_solar_device_id_inverter_2", "")
                )

                # Ensure that optional ev_charger_status is set to None if not provided.
                self._user_input["hsem_ev_charger_status"] = self._user_input.get(
                    "hsem_ev_charger_status", None
                )
                self._user_input["hsem_ev_charger_power"] = self._user_input.get(
                    "hsem_ev_charger_power", None
                )

                return await self.async_step_battery_economics()

        data_schema = await get_huawei_solar_step_schema(None)

        return self.async_show_form(
            step_id="huawei_solar",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_battery_economics(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the battery_economics config flow step.

        Validates user input and advances to the next step in the config flow.
        """
        errors = {}

        if user_input is not None:
            errors = await validate_battery_economics_input(user_input)
            if not errors:
                self._user_input.update(user_input)
                return await self.async_step_power()

        data_schema = await get_battery_economics_step_schema(None)

        return self.async_show_form(
            step_id="battery_economics",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_power(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the power config flow step.

        Validates user input and advances to the next step in the config flow.
        """
        errors = {}

        if user_input is not None:
            errors = await validate_power_step_input(self.hass, user_input)
            if not errors:
                self._user_input.update(user_input)
                return await self.async_step_ev()

        data_schema = await get_power_step_schema(None)

        return self.async_show_form(
            step_id="power",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_ev(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the ev config flow step.

        Validates user input and advances to the next step in the config flow.
        """
        errors = {}

        if user_input is not None:
            errors = await validate_ev_step_input(self.hass, user_input)
            if not errors:
                self._user_input.update(user_input)

                if bool(self._user_input.get("hsem_ev_second_enabled")):
                    return await self.async_step_ev_second()

                return await self.async_step_ev_planned_load()

        data_schema = await get_ev_step_schema(None)

        return self.async_show_form(
            step_id="ev",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_ev_second(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the ev_second config flow step.

        Validates user input and advances to the next step in the config flow.
        """
        errors = {}

        if user_input is not None:
            errors = await validate_ev_second_step_input(self.hass, user_input)
            if not errors:
                self._user_input.update(user_input)
                return await self.async_step_ev_planned_load()

        data_schema = await get_ev_second_step_schema(None)

        return self.async_show_form(
            step_id="ev_second",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_ev_planned_load(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the ev_planned_load config flow step.

        Validates user input and advances to the next step in the config flow.
        """
        errors = {}

        if user_input is not None:
            errors = await validate_ev_planned_load_input(self.hass, user_input)
            if not errors:
                self._user_input.update(user_input)
                if bool(self._user_input.get("hsem_ev_second_enabled")):
                    return await self.async_step_ev_second_planned_load()
                return await self.async_step_batteries_schedules()

        data_schema = await get_ev_planned_load_step_schema(None)

        return self.async_show_form(
            step_id="ev_planned_load",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_ev_second_planned_load(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the ev_second_planned_load config flow step.

        Validates user input and advances to the next step in the config flow.
        """
        errors = {}

        if user_input is not None:
            errors = await validate_ev_second_planned_load_input(self.hass, user_input)
            if not errors:
                self._user_input.update(user_input)
                return await self.async_step_batteries_schedules()

        data_schema = await get_ev_second_planned_load_step_schema(None)

        return self.async_show_form(
            step_id="ev_second_planned_load",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_batteries_schedules(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the batteries_schedules config flow step.

        Validates user input and advances to the next step in the config flow.
        """
        errors = {}

        if user_input is not None:
            errors = await validate_batteries_schedules_input(user_input)
            if not errors:
                self._user_input.update(user_input)
                return await self.async_step_batteries_excess_export()

        data_schema = await get_batteries_schedules_step_schema(
            None, hass=self.hass, user_input=self._user_input
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
        """Handle the batteries_excess_export config flow step.

        Validates user input and advances to the next step in the config flow.
        """
        errors = {}

        if user_input is not None:
            errors = await validate_batteries_excess_export_input(user_input)
            if not errors:
                self._user_input.update(user_input)
                return await self.async_step_weighted_values()

        data_schema = await get_batteries_excess_export_step_schema(
            None, self._user_input, _hass=self.hass
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
        """Handle the weighted_values config flow step.

        Validates user input and advances to the next step in the config flow.
        """
        errors = {}

        if user_input is not None:
            errors = await validate_weighted_values_input(user_input)
            if not errors:
                self._user_input.update(user_input)
                return await self.async_step_energy_and_ml()

        data_schema = await get_weighted_values_step_schema(None)

        return self.async_show_form(
            step_id="weighted_values",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_energy_and_ml(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the energy_and_ml config flow step.

        Validates user input, tests connections to critical entities,
        and creates the config entry.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            errors = await validate_energy_and_ml_input(self.hass, user_input)
            if not errors:
                # Test connections to critical entities before creating
                # the entry (Bronze rule: test-before-configure).
                connection_errors = await self._async_test_connections()
                if connection_errors:
                    return self.async_show_form(
                        step_id="energy_and_ml",
                        data_schema=await get_energy_and_ml_step_schema(None),
                        errors=connection_errors,
                        last_step=True,
                    )

                self._user_input.update(user_input)
                return self.async_create_entry(
                    title=self._user_input.get("device_name", NAME),
                    data=self._user_input,
                )

        data_schema = await get_energy_and_ml_step_schema(None)

        return self.async_show_form(
            step_id="energy_and_ml",
            data_schema=data_schema,
            errors=errors,
            last_step=True,
        )

    async def _async_test_connections(self) -> dict[str, str]:
        """Test that critical external entities return usable data."""
        errors: dict[str, str] = {}

        critical_entities: dict[str, str] = {
            "hsem_import_electricity_price_sensor": "Import price sensor",
            "hsem_export_electricity_price_sensor": "Export price sensor",
            "hsem_huawei_solar_batteries_state_of_capacity": "Battery SoC sensor",
        }

        for field_key, label in critical_entities.items():
            entity_id = self._user_input.get(field_key)
            if not entity_id:
                continue
            state = self.hass.states.get(entity_id)
            if state is None:
                errors[field_key] = "entity_not_found"
            elif state.state in ("unknown", "unavailable"):
                _LOGGER.warning(
                    "Connection test: %s (%s) is '%s'",
                    label,
                    entity_id,
                    state.state,
                )
                errors[field_key] = "entity_unavailable"

        return errors

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> HSEMOptionsFlow:
        """Return the options flow."""
        return HSEMOptionsFlow(config_entry)


def _migrate_v1_to_v2(data: dict[str, Any]) -> dict[str, Any]:
    """Convert a v1 (v5.1.0 era) config data dict to v2.

    Idempotent — safe to call on data that has already been partially migrated.
    """
    migrated = dict(data)

    # 1. Rename old keys to new keys (only if the new key is absent).
    for old_key, new_key in _V1_TO_V2_KEY_RENAMES.items():
        if old_key in migrated and new_key not in migrated:
            migrated[new_key] = migrated.pop(old_key)

    # 2. Drop deprecated keys that have no v2 equivalent.
    for key in _V1_DEPRECATED_KEYS:
        migrated.pop(key, None)

    # 3. Backfill new keys with their defaults (only if absent).
    for key, default in _V2_NEW_KEY_DEFAULTS.items():
        if key not in migrated:
            migrated[key] = default

    # 4. Convert month lists from strings to ints (v5.1.0 stored them as
    #    strings; v6.0.0 expects integers).
    for month_key in ("hsem_months_summer", "hsem_months_winter"):
        raw = migrated.get(month_key, [])
        if raw and isinstance(raw[0], str):
            migrated[month_key] = convert_months_to_int(raw)

    return migrated
