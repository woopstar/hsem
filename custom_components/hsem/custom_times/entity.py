"""Time entity for the HSEM integration.

Defines :class:`HSEMTimeEntityDescription` and :class:`HSEMTimeEntity`.

:class:`HSEMTimeEntity` is a standard :class:`TimeEntity` that persists its
value to the config entry options so it survives HA restarts.
"""

from dataclasses import dataclass
from datetime import datetime, time
from typing import Any

from homeassistant.components.time import TimeEntity, TimeEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant

from custom_components.hsem.entity import HSEMEntity
from custom_components.hsem.utils.sensornames import (
    get_schedule_1_end_time_entity_id,
    get_schedule_1_end_time_key,
    get_schedule_1_end_time_unique_id,
    get_schedule_1_start_time_entity_id,
    get_schedule_1_start_time_key,
    get_schedule_1_start_time_unique_id,
    get_schedule_2_end_time_entity_id,
    get_schedule_2_end_time_key,
    get_schedule_2_end_time_unique_id,
    get_schedule_2_start_time_entity_id,
    get_schedule_2_start_time_key,
    get_schedule_2_start_time_unique_id,
    get_schedule_3_end_time_entity_id,
    get_schedule_3_end_time_key,
    get_schedule_3_end_time_unique_id,
    get_schedule_3_start_time_entity_id,
    get_schedule_3_start_time_key,
    get_schedule_3_start_time_unique_id,
)

# Map from config-entry key → (unique_id, entity_id)
_TIME_ID_MAP: dict[str, tuple[str, str]] = {
    get_schedule_1_start_time_key(): (
        get_schedule_1_start_time_unique_id(),
        get_schedule_1_start_time_entity_id(),
    ),
    get_schedule_1_end_time_key(): (
        get_schedule_1_end_time_unique_id(),
        get_schedule_1_end_time_entity_id(),
    ),
    get_schedule_2_start_time_key(): (
        get_schedule_2_start_time_unique_id(),
        get_schedule_2_start_time_entity_id(),
    ),
    get_schedule_2_end_time_key(): (
        get_schedule_2_end_time_unique_id(),
        get_schedule_2_end_time_entity_id(),
    ),
    get_schedule_3_start_time_key(): (
        get_schedule_3_start_time_unique_id(),
        get_schedule_3_start_time_entity_id(),
    ),
    get_schedule_3_end_time_key(): (
        get_schedule_3_end_time_unique_id(),
        get_schedule_3_end_time_entity_id(),
    ),
}


@dataclass(frozen=True)
class HSEMTimeEntityDescription(TimeEntityDescription):
    """Extended entity description that adds a human-readable description field.

    Attributes
    ----------
    description:
        Short human-readable description of the time entity's purpose, exposed
        as an entity attribute for dashboard display.
    default_value:
        Initial time value as an ``"HH:MM:SS"`` string.
    """

    description: str = ""
    default_value: str = "00:00:00"


class HSEMTimeEntity(TimeEntity, HSEMEntity):
    """Time entity for an HSEM schedule slot (start or end time).

    Inherits from :class:`TimeEntity` so Home Assistant's platform dispatcher
    routes it correctly to the ``time`` domain.
    """

    _attr_icon = "mdi:clock"
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    entity_description: HSEMTimeEntityDescription

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        description: HSEMTimeEntityDescription,
    ) -> None:
        """Initialize the time entity.

        Parameters
        ----------
        hass:
            The Home Assistant instance.
        config_entry:
            The config entry this entity belongs to.
        description:
            Entity description carrying ``key``, ``name``, ``description``,
            and ``default_value``.
        """
        super().__init__(config_entry)

        self.hass = hass
        self._config_entry = config_entry
        self.entity_description = description
        self._attr_name = description.name
        # Resolve unique_id and entity_id from the centralized sensornames map.
        unique_id, entity_id = _TIME_ID_MAP[description.key]
        self._attr_unique_id = unique_id
        self.entity_id = entity_id
        self._attr_native_value: time | None = self._parse_time(
            description.default_value
        )

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
                return datetime.strptime(value, fmt).time()
            except ValueError:
                continue
        return None

    # ------------------------------------------------------------------
    # Entity properties
    # ------------------------------------------------------------------

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
        return {"description": self.entity_description.description}

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
        await self._persist_value()
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Config-entry persistence
    # ------------------------------------------------------------------

    async def _persist_value(self) -> None:
        """Persist the current native value to the config entry options."""
        str_value = (
            self._attr_native_value.strftime("%H:%M:%S")
            if self._attr_native_value is not None
            else ""
        )
        updated_options = {
            **self._config_entry.options,
            self.entity_description.key: str_value,
        }
        self.hass.config_entries.async_update_entry(
            self._config_entry, options=updated_options
        )
