"""Working mode selector entity for the HSEM integration.

This module defines :class:`HSEMWorkingModeSelector`, a standard
:class:`homeassistant.components.select.SelectEntity` that lets users pick a
specific working mode or leave it on ``"auto"``.
"""

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from custom_components.hsem.entity import HSEMEntity


class HSEMWorkingModeSelector(SelectEntity, HSEMEntity):
    """Selector entity for forcing a specific working mode.

    Presents all working modes plus an ``"auto"`` option.  Inherits from
    :class:`SelectEntity` so that Home Assistant's platform dispatcher routes
    it correctly to the ``select`` domain.
    """

    _attr_icon = "mdi:chart-timeline-variant"
    _attr_has_entity_name = True

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
        # Scope unique_id to the config entry so multiple HSEM instances
        # each have a distinct entity in the registry.
        self._attr_unique_id = f"{config_entry.entry_id}_{description.key}"
        self._attr_options = list(description.options or [])
        self._attr_current_option = default
        self._attr_name = description.name

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
