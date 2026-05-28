"""Switch entity for the HSEM integration.

Defines :class:`HSEMSwitch`.

:class:`HSEMSwitch` is a standard :class:`SwitchEntity` that persists its
on/off state to the config entry options so it survives HA restarts.
"""

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant

from custom_components.hsem.custom_switches.description import (
    _SWITCH_ID_MAP,
    HSEMSwitchEntityDescription,
)
from custom_components.hsem.entity import HSEMEntity
from custom_components.hsem.utils.misc import get_config_value


class HSEMSwitch(SwitchEntity, HSEMEntity):
    """Boolean on/off control for HSEM integration settings.

    Each switch maps to a single key in the config entry options, so toggling
    it immediately persists the new value without requiring a UI reconfigure.
    """

    _attr_icon = "mdi:toggle-switch"
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    entity_description: HSEMSwitchEntityDescription

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        description: HSEMSwitchEntityDescription,
    ) -> None:
        """Initialize the switch.

        Parameters
        ----------
        hass:
            The Home Assistant instance.
        config_entry:
            The config entry this switch belongs to.
        description:
            Entity description carrying ``key``, ``name``, and ``description``.
        """
        super().__init__(config_entry)

        self.hass = hass
        self._config_entry = config_entry
        self.entity_description = description
        # Do NOT set _attr_name — let the translation system resolve it
        # via entity_description.translation_key.  Setting _attr_name even
        # to UNDEFINED blocks the translation lookup in _name_internal().
        # Resolve unique_id and entity_id from the centralized sensornames map.
        unique_id, entity_id = _SWITCH_ID_MAP[description.key]
        self._attr_unique_id = unique_id
        self.entity_id = entity_id
        self._attr_is_on = bool(get_config_value(self._config_entry, description.key))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
        return {"description": self.entity_description.description}

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on and persist to config entry."""
        self._attr_is_on = True
        await self._persist_state()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off and persist to config entry."""
        self._attr_is_on = False
        await self._persist_state()
        self.async_write_ha_state()

    async def _persist_state(self) -> None:
        """Write the current on/off value to the config entry options."""
        updated_options = {
            **self._config_entry.options,
            self.entity_description.key: self._attr_is_on,
        }
        self.hass.config_entries.async_update_entry(
            self._config_entry, options=updated_options
        )
