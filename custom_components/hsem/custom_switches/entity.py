from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from custom_components.hsem.const import DOMAIN
from custom_components.hsem.entity import HSEMEntity
from custom_components.hsem.utils.misc import get_config_value


class HSEMSwitch(SwitchEntity, HSEMEntity):
    """Custom switch for HSEM integration."""

    _attr_icon = "mdi:toggle-switch"

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        key: str,
        name: str,
        description: str,
    ) -> None:
        """Initialize the switch."""
        super().__init__(config_entry)

        self.hass = hass
        self._config_entry = config_entry
        self._key = key
        self._name = name
        self._description = description
        self._is_on = bool(get_config_value(self._config_entry, key))

    @property
    def name(self) -> str:
        """Return the name of the switch."""
        return self._name

    @property
    def is_on(self) -> bool:
        """Return the state of the switch."""
        return self._is_on

    @property
    def unique_id(self) -> str | None:
        """Return a unique ID for the switch."""
        return f"{DOMAIN}_{self._key}_switch"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        return {"description": self._description}

    async def async_turn_on(self, **kwargs) -> None:
        """Turn the switch on."""
        self._is_on = True
        await self._update_config_entry()

    async def async_turn_off(self, **kwargs) -> None:
        """Turn the switch off."""
        self._is_on = False
        await self._update_config_entry()

    async def _update_config_entry(self) -> None:
        """Update the config entry with the new switch state."""
        updated_options = {**self._config_entry.options, self._key: self._is_on}
        self.hass.config_entries.async_update_entry(
            self._config_entry, options=updated_options
        )
