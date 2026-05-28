"""Vulture whitelist for HSEM.

This file lists symbols that Vulture would flag as unused but are actually
called dynamically by Home Assistant.  They must never be deleted.

Reference: https://github.com/jendrikseipp/vulture#whitelists

Style note: imports appear after section comments in this file because vulture
whitelists require imports to be adjacent to the symbols they reference.
The ``# noqa: E402`` suppressor on the first import silences ruff's
module-level-import-not-at-top warning for the entire file.
"""

# ruff: noqa: E402
from custom_components.hsem import (  # noqa: F401
    async_migrate_entry,
    async_setup,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.hsem.config_flow import HSEMConfigFlow  # noqa: F401

# Config flow steps (called dynamically by HA)
HSEMConfigFlow.async_step_user
HSEMConfigFlow.async_step_reconfigure

from custom_components.hsem.options_flow import HSEMOptionsFlow as _OptionsFlow  # noqa: F401

# Options flow steps (called dynamically by HA)
_OptionsFlow.async_step_init
_OptionsFlow.async_step_huawei_solar
_OptionsFlow.async_step_house_consumption
_OptionsFlow.async_step_solar_production
_OptionsFlow.async_step_energy_prices
_OptionsFlow.async_step_batteries
_OptionsFlow.async_step_ev_charger
_OptionsFlow.async_step_ev_planned_load
_OptionsFlow.async_step_ev_second_planned_load
_OptionsFlow.async_step_recommendations
_OptionsFlow.async_step_working_mode
_OptionsFlow.async_step_misc

from custom_components.hsem.diagnostics import (  # noqa: F401
    async_get_config_entry_diagnostics,
)
from custom_components.hsem.entity import HSEMEntity as _Entity  # noqa: F401

# Properties consumed by HA entity registry
_Entity.device_info

from custom_components.hsem.const import DOMAIN  # noqa: F401
from custom_components.hsem.time import async_setup_entry as _time_setup  # noqa: F401

# async_setup is referenced here so vulture knows the function is live (called by HA).
# This suppresses any false-positive "unused" warnings on its parameters.
async_setup
