"""Working mode selector entity for the HSEM integration.

This module defines :class:`HSEMWorkingModeSelector`, a standard
:class:`homeassistant.components.select.SelectEntity` that lets users pick a
specific working mode or leave it on ``"auto"``.
"""

from typing import override

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant

from custom_components.hsem.entity import HSEMEntity
from custom_components.hsem.utils.sensornames import (
    get_force_working_mode_selector_entity_id,
    get_force_working_mode_selector_unique_id,
)


class HSEMWorkingModeSelector(HSEMEntity, SelectEntity):
    """Selector entity for forcing a specific working mode.

    Presents all working modes plus an ``"auto"`` option.  Inherits from
    :class:`SelectEntity` so that Home Assistant's platform dispatcher routes
    it correctly to the ``select`` domain.
    """

    _attr_icon = "mdi:chart-timeline-variant"
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        description: SelectEntityDescription,
        default: str,
    ) -> None:
        """Initialize the selector entity.

        Parameters
        ----------
        hass:
            The Home Assistant instance.
        config_entry:
            The config entry this entity belongs to.
        description:
            Entity description carrying ``key``, ``name``, and ``options``.
        default:
            The initial selected option.
        """
        super().__init__(config_entry)

        self.hass = hass
        self._config_entry = config_entry
        self.entity_description = description
        # unique_id and entity_id are sourced from sensornames.py to keep
        # all HSEM entity identifiers in one canonical location.
        self._attr_unique_id = get_force_working_mode_selector_unique_id(
            config_entry.entry_id
        )
        self.entity_id = get_force_working_mode_selector_entity_id()
        self._attr_options = list(description.options or [])
        self._attr_current_option = default
        # description.name may be UndefinedType (HA sentinel) or None when not
        # set; fall back to None so HA derives the name from the entity key.
        raw_name = description.name
        if isinstance(raw_name, str):
            self._attr_name = str(raw_name)
        # When description.name is UNDEFINED (no explicit name),
        # leave _attr_name unset so the translation system can
        # resolve the name via entity_description.translation_key.

    @override
    async def async_select_option(self, option: str) -> None:
        """Handle the user selecting a new option.

        Parameters
        ----------
        option:
            The working mode chosen by the user.

        Raises
        ------
        ValueError
            If *option* is not in :attr:`_attr_options`.
        """
        if option not in self._attr_options:
            raise ValueError(f"Invalid option: {option}")
        self._attr_current_option = option
        self.async_write_ha_state()
