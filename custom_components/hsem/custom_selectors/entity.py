"""
Selector entity for forcing a working mode in the HSEM integration.

This module defines a selector entity that allows users to choose a working mode,
including an "Auto" option as default. The available working modes are imported
from utils/workingmodes.py.
"""

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from custom_components.hsem.entity import HSEMEntity


class HSEMWorkingModeSelector(SelectEntity, HSEMEntity):
    """
    Selector entity for forcing a specific working mode.

    Presents all working modes plus an "Auto" option.
    """

    _attr_icon = "mdi:chart-timeline-variant"
    _attr_has_entity_name = True

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        unique_id: str,
        name: str,
        description: str,
        options: list[str],
        default: str,
    ) -> None:
        """Initialize the switch."""
        super().__init__(config_entry)

        """Initialize the selector entity."""
        self.hass = hass
        self._config_entry = config_entry
        self._attr_unique_id = unique_id
        self._attr_name = name
        self._attr_options = options
        self._attr_current_option = default
        self._description = description

    @property
    def current_option(self) -> str | None:
        """Return the currently selected option."""
        return self._attr_current_option

    async def async_select_option(self, option: str) -> None:
        """
        Handle user selecting a new option.

        Parameters:
        option (str): The selected working mode.
        """
        if option not in self._attr_options:
            raise ValueError(f"Invalid option: {option}")
        self._attr_current_option = option

    @property
    def unique_id(self) -> str | None:
        """Return a unique ID for the selector."""
        return self._attr_unique_id

    @property
    def extra_state_attributes(self) -> dict:
        """Return additional attributes."""
        return {
            "description": self._description,
        }
