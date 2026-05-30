"""Solcast PV forecast likelihood selector entity for the HSEM integration.

This module defines :class:`HSEMSolcastLikelihoodSelector`, a standard
:class:`homeassistant.components.select.SelectEntity` that lets users pick
which Solcast PV forecast likelihood to use (``pv_estimate``,
``pv_estimate10``, or ``pv_estimate90``).

The selected value is persisted to the config entry options so it survives
HA restarts.
"""

from typing import override

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant

from custom_components.hsem.entity import HSEMEntity
from custom_components.hsem.utils.misc import get_config_value
from custom_components.hsem.utils.sensornames import (
    get_solcast_likelihood_selector_entity_id,
    get_solcast_likelihood_selector_key,
)

_OPTIONS = ["pv_estimate", "pv_estimate10", "pv_estimate90"]
_DEFAULT = "pv_estimate"
_CONFIG_KEY = "hsem_solcast_pv_forecast_forecast_likelihood"


class HSEMSolcastLikelihoodSelector(SelectEntity, HSEMEntity):
    """Select entity for the Solcast PV forecast likelihood.

    Presents the three Solcast likelihood options.  Changing the selection
    updates both the HA state and the config entry options so the value
    persists across restarts.
    """

    _attr_icon = "mdi:solar-power"
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        description: SelectEntityDescription,
    ) -> None:
        """Initialize the selector entity.

        Args:
            hass: The Home Assistant instance.
            config_entry: The config entry this entity belongs to.
            description: Entity description carrying ``key``, ``name``,
                and ``options``.
        """
        super().__init__(config_entry)

        self.hass = hass
        self._config_entry = config_entry
        self.entity_description = description
        self._attr_unique_id = get_solcast_likelihood_selector_key()
        self.entity_id = get_solcast_likelihood_selector_entity_id()
        self._attr_options = list(description.options or _OPTIONS)
        raw_name = description.name
        if isinstance(raw_name, str):
            self._attr_name = str(raw_name)
        # When description.name is UNDEFINED (no explicit name),
        # leave _attr_name unset so the translation system can
        # resolve the name via entity_description.translation_key.

        # Initialise from the config entry value.
        stored = get_config_value(config_entry, _CONFIG_KEY)
        self._attr_current_option = str(stored) if stored in _OPTIONS else _DEFAULT

    @override
    async def async_select_option(self, option: str) -> None:
        """Handle the user selecting a new option.

        Updates HA state and persists the choice to the config entry options.

        Args:
            option: The likelihood key chosen by the user.

        Raises:
            ValueError: If *option* is not in :attr:`_attr_options`.
        """
        if option not in self._attr_options:
            raise ValueError(f"Invalid option: {option}")

        self._attr_current_option = option
        self.async_write_ha_state()

        # Persist to config entry options so it survives restart.
        new_options = {**self._config_entry.options, _CONFIG_KEY: option}
        self.hass.config_entries.async_update_entry(
            self._config_entry, options=new_options
        )
