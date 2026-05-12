from datetime import time
from typing import Any

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from custom_components.hsem.const import DOMAIN
from custom_components.hsem.entity import HSEMEntity


class HSEMTimeEntity(TimeEntity, HSEMEntity):
    """Custom time entity for HSEM.

    Inherits from :class:`homeassistant.components.time.TimeEntity` so that
    Home Assistant treats this as a proper ``time`` platform entity and not a
    toggle/switch.
    """

    _attr_icon = "mdi:clock"

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        key: str,
        name: str,
        description: str,
        default_value: str,
    ) -> None:
        """Initialize the time entity.

        Parameters
        ----------
        hass:
            The Home Assistant instance.
        config_entry:
            The config entry this entity belongs to.
        key:
            The config-entry option key used to persist the value.
        name:
            Human-readable entity name.
        description:
            Short description exposed as an entity attribute.
        default_value:
            Initial time value as an ISO-8601 string (``"HH:MM:SS"``).
        """
        super().__init__(config_entry)

        self.hass = hass
        self._config_entry = config_entry
        self._key = key
        self._attr_name = name
        self._description = description
        self._attr_native_value: time | None = self._parse_time(default_value)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_time(value: str) -> time | None:
        """Parse an ISO-8601 time string into a :class:`datetime.time` object.

        Parameters
        ----------
        value:
            A string in ``"HH:MM"`` or ``"HH:MM:SS"`` format.

        Returns
        -------
        datetime.time | None
            The parsed time, or ``None`` if parsing fails.
        """
        if not value:
            return None
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                from datetime import datetime

                return datetime.strptime(value, fmt).time()
            except ValueError:
                continue
        return None

    # ------------------------------------------------------------------
    # Entity properties
    # ------------------------------------------------------------------

    @property
    def unique_id(self) -> str | None:
        """Return a unique ID for the time entity."""
        return f"{DOMAIN}_{self._key}_time"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
        return {"description": self._description}

    # ------------------------------------------------------------------
    # TimeEntity interface
    # ------------------------------------------------------------------

    async def async_set_value(self, value: time) -> None:
        """Handle a user-requested time change.

        Persists the new value both in the entity state and in the config
        entry so it survives HA restarts.

        Parameters
        ----------
        value:
            The new time selected by the user.
        """
        self._attr_native_value = value
        await self._update_config_entry()
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Config-entry persistence
    # ------------------------------------------------------------------

    async def _update_config_entry(self) -> None:
        """Persist the current native value to the config entry options."""
        str_value = (
            self._attr_native_value.strftime("%H:%M:%S")
            if self._attr_native_value is not None
            else ""
        )
        updated_options = {**self._config_entry.options, self._key: str_value}
        self.hass.config_entries.async_update_entry(
            self._config_entry, options=updated_options
        )
