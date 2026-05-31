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
from custom_components.hsem import async_setup  # noqa: F401
from custom_components.hsem.config_flow import HSEMConfigFlow  # noqa: F401

# Config flow steps (called dynamically by HA)
_ = HSEMConfigFlow.async_step_user  # noqa: S905
_ = HSEMConfigFlow.async_step_reconfigure  # noqa: S905

from custom_components.hsem.options_flow import (  # noqa: F401
    HSEMOptionsFlow as _OptionsFlow,
)

# Options flow steps (called dynamically by HA)
_ = _OptionsFlow.async_step_init  # noqa: S905
_ = _OptionsFlow.async_step_huawei_solar  # noqa: S905
_ = _OptionsFlow.async_step_house_consumption  # noqa: S905
_ = _OptionsFlow.async_step_solar_production  # noqa: S905
_ = _OptionsFlow.async_step_energy_prices  # noqa: S905
_ = _OptionsFlow.async_step_batteries  # noqa: S905
_ = _OptionsFlow.async_step_ev_charger  # noqa: S905
_ = _OptionsFlow.async_step_ev_planned_load  # noqa: S905
_ = _OptionsFlow.async_step_ev_second_planned_load  # noqa: S905
_ = _OptionsFlow.async_step_recommendations  # noqa: S905
_ = _OptionsFlow.async_step_working_mode  # noqa: S905
_ = _OptionsFlow.async_step_misc  # noqa: S905

from custom_components.hsem.entity import HSEMEntity as _Entity  # noqa: F401

# Properties consumed by HA entity registry
_ = _Entity.device_info  # noqa: S905

from custom_components.hsem.const import DOMAIN  # noqa: F401
from custom_components.hsem.time import async_setup_entry as _time_setup  # noqa: F401

# async_setup is referenced here so vulture knows the function is live (called by HA).
# This suppresses any false-positive "unused" warnings on its parameters.
_ = async_setup  # noqa: S905
