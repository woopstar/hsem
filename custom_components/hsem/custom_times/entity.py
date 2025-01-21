from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import ToggleEntity

from custom_components.hsem.const import DOMAIN
from custom_components.hsem.entity import HSEMEntity


class HSEMTimeEntity(ToggleEntity, HSEMEntity):
    """Custom time entity for HSEM."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        key: str,
        name: str,
        description: str,
        default_value: str,
    ) -> None:
        """Initialize the time entity."""
        super().__init__(config_entry)

        self.hass = hass
        self._config_entry = config_entry
        self._key = key
        self._name = name
        self._description = description
        self._value = default_value

    @property
    def name(self) -> str:
        """Return the name of the entity."""
        return self._name

    @property
    def state(self) -> str:
        """Return the current value."""
        return self._value

    @property
    def unique_id(self) -> str | None:
        """Return a unique ID for the switch."""
        return f"{DOMAIN}_{self._key}_time"

    @property
    def icon(self) -> str:
        """Return an icon for the entity."""
        return "mdi:clock"

    async def _update_config_entry(self) -> None:
        """Update the config entry with the new value."""
        updated_options = {**self._config_entry.options, self._key: self._value}
        self.hass.config_entries.async_update_entry(
            self._config_entry, options=updated_options
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        return {"description": self._description}

    async def async_set_value(self, value: str) -> None:
        """Set a new time after validation."""

        self._value = value
        await self._update_config_entry()
