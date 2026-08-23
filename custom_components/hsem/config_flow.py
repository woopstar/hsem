"""This module defines the configuration flow for the HSEM integration in Home Assistant."""

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from custom_components.hsem.const import DEFAULT_CONFIG_VALUES, DOMAIN, NAME
from custom_components.hsem.flows.batteries_excess_export import (
    get_batteries_excess_export_step_schema,
    validate_batteries_excess_export_input,
)
from custom_components.hsem.flows.batteries_schedules import (
    get_batteries_schedules_step_schema,
    validate_batteries_schedules_input,
)
from custom_components.hsem.flows.batteries_wait_mode import (
    get_batteries_wait_mode_step_schema,
    validate_batteries_wait_mode_input,
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
from custom_components.hsem.flows.migrations import (  # noqa: F401 — re-exported
    _CHARGE_RATE_BUCKETS,
    _V1_DEPRECATED_KEYS,
    _V1_TO_V2_KEY_RENAMES,
    _V2_NEW_KEY_DEFAULTS,
    _V3_DEPRECATED_KEYS,
    _migrate_v1_to_v2,
    _migrate_v2_to_v3,
    _remove_v3_charge_rate_registry_entries,
)
from custom_components.hsem.flows.months import get_months_schema, validate_months_input
from custom_components.hsem.flows.ocpp import (
    get_ocpp_step_schema,
    validate_ocpp_step_input,
)
from custom_components.hsem.flows.power import (
    get_power_step_schema,
    validate_power_step_input,
)
from custom_components.hsem.flows.prices import (
    get_prices_step_schema,
    validate_prices_input,
)
from custom_components.hsem.flows.quick_setup import (
    _DETECTION_TO_CONFIG,
    CRITICAL_DETECTION_KEYS,
    auto_detect_entities,
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


class HSEMConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):  # pyright: ignore[reportGeneralTypeIssues]  # HA ConfigFlow class hierarchy triggers false-positive on MRO
    """Config flow for HSEM."""

    VERSION = 3

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

        original_version = config_entry.version
        migrated_version = original_version
        data = dict(config_entry.data)
        options = dict(config_entry.options)

        if migrated_version == 1:
            data = _migrate_v1_to_v2(data)
            # v6.0.0 (#523) prefixed every entity unique_id with the config
            # entry id, but shipped no entity-registry migration. Rename those
            # rows in place so entity IDs and history are preserved.
            _prefix = f"{DOMAIN}_{config_entry.entry_id}_"

            @callback
            def _migrate_unique_id(
                entity_entry: er.RegistryEntry,
            ) -> dict[str, str] | None:
                uid = entity_entry.unique_id
                if not uid.startswith(f"{DOMAIN}_") or uid.startswith(_prefix):
                    return None
                return {"new_unique_id": f"{_prefix}{uid[len(DOMAIN) + 1 :]}"}

            await er.async_migrate_entries(
                hass, config_entry.entry_id, _migrate_unique_id
            )
            migrated_version = 2

        if migrated_version == 2:
            data = _migrate_v2_to_v3(data)
            options = _migrate_v2_to_v3(options)
            _remove_v3_charge_rate_registry_entries(hass, config_entry.entry_id)
            migrated_version = 3

        if migrated_version != original_version:
            hass.config_entries.async_update_entry(
                config_entry,
                data=data,
                options=options,
                version=migrated_version,
            )
            _LOGGER.info(
                "Config entry %s migrated from v%s to v%s",
                config_entry.entry_id,
                original_version,
                migrated_version,
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
                return await self.async_step_quick_setup()

        data_schema = await get_init_step_schema(None)

        # Show the init form.
        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_quick_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the quick setup auto-detection step.

        Auto-detects HSEM-relevant entities and offers a pre-filled form.
        The user can confirm to use detected entities (quick setup) or
        choose advanced setup to go through the full entity-by-entity wizard.
        """
        detected = await auto_detect_entities(self.hass)
        errors: dict[str, str] = {}

        if user_input is not None:
            use_quick_setup = user_input.pop("use_quick_setup", True)

            if use_quick_setup:
                # Map detected form values to config keys.
                for detect_key, config_key in _DETECTION_TO_CONFIG.items():
                    value = user_input.get(detect_key) or detected.get(detect_key)
                    if value:
                        self._user_input[config_key] = value

                # Fill remaining defaults from DEFAULT_CONFIG_VALUES.
                for key, default_value in DEFAULT_CONFIG_VALUES.items():
                    if key not in self._user_input:
                        # vol.UNDEFINED is not JSON-serializable; skip it.
                        if default_value is vol.UNDEFINED:
                            continue
                        self._user_input[key] = default_value

                # Ensure months are processed (winter/summer split).
                winter_months = self._user_input.get("hsem_months_winter", [])
                if not isinstance(winter_months, list):
                    winter_months = convert_months_to_int(
                        winter_months if winter_months else []
                    )
                all_months = set(range(1, 13))
                summer_months = sorted(all_months - set(winter_months))
                self._user_input["hsem_months_winter"] = winter_months
                self._user_input["hsem_months_summer"] = summer_months

                # Ensure optional fields have safe defaults.
                self._user_input.setdefault(
                    "hsem_huawei_solar_device_id_inverter_1", ""
                )
                self._user_input.setdefault(
                    "hsem_huawei_solar_device_id_inverter_2", ""
                )
                self._user_input.setdefault("hsem_huawei_solar_device_id_batteries", "")
                self._user_input.setdefault(
                    "hsem_huawei_solar_device_id_batteries_2", ""
                )

                return await self.async_step_battery_economics()

            # Advanced setup — go through full wizard.
            return await self.async_step_prices()

        # Build the quick-setup form schema.
        schema_fields: dict[vol.Marker, type] = {}

        for detect_key in _DETECTION_TO_CONFIG:
            default_value = detected.get(detect_key) or ""
            schema_fields[vol.Optional(detect_key, default=default_value)] = str

        schema_fields[vol.Required("use_quick_setup", default=True)] = bool

        # Build warning for missing critical entities.
        missing_critical = [k for k in CRITICAL_DETECTION_KEYS if not detected.get(k)]
        description_placeholders: dict[str, str] = {}
        if missing_critical:
            description_placeholders["warning"] = (
                "Critical entities not detected: " + ", ".join(missing_critical)
            )

        return self.async_show_form(
            step_id="quick_setup",
            data_schema=vol.Schema(schema_fields),
            errors=errors,
            description_placeholders=description_placeholders,
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
                self._user_input["hsem_huawei_solar_device_id_batteries_2"] = (
                    self._user_input.get("hsem_huawei_solar_device_id_batteries_2", "")
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
                return await self.async_step_ocpp()

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
                return await self.async_step_ocpp()

        data_schema = await get_ev_second_planned_load_step_schema(None)

        return self.async_show_form(
            step_id="ev_second_planned_load",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_ocpp(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the ocpp config flow step.

        Validates user input and advances to the next step in the config flow.
        """
        errors = {}

        if user_input is not None:
            errors = await validate_ocpp_step_input(self.hass, user_input)
            if not errors:
                self._user_input.update(user_input)
                return await self.async_step_batteries_schedules()

        data_schema = await get_ocpp_step_schema(
            None,
            second_ev_enabled=bool(self._user_input.get("hsem_ev_second_enabled")),
        )

        return self.async_show_form(
            step_id="ocpp",
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
                return await self.async_step_batteries_wait_mode()

        data_schema = await get_batteries_schedules_step_schema(
            None, hass=self.hass, user_input=self._user_input
        )

        return self.async_show_form(
            step_id="batteries_schedules",
            data_schema=data_schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_batteries_wait_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the batteries_wait_mode config flow step.

        Allows the user to choose between strict wait and self-consumption
        with reserve protection.
        """
        errors = {}

        if user_input is not None:
            errors = await validate_batteries_wait_mode_input(user_input)
            if not errors:
                self._user_input.update(user_input)
                return await self.async_step_batteries_excess_export()

        data_schema = await get_batteries_wait_mode_step_schema(
            None, self._user_input, _hass=self.hass
        )

        return self.async_show_form(
            step_id="batteries_wait_mode",
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
            elif state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
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
