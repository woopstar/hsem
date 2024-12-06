from datetime import datetime

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
    ):
        """Initialize the time entity."""
        super().__init__(config_entry)

        self.hass = hass
        self._config_entry = config_entry
        self._key = key
        self._name = name
        self._description = description
        self._value = default_value

    @property
    def name(self):
        """Return the name of the entity."""
        return self._name

    @property
    def state(self):
        """Return the current value."""
        return self._value

    @property
    def unique_id(self):
        """Return a unique ID for the switch."""
        return f"{DOMAIN}_{self._key}_time"

    @property
    def icon(self):
        """Return an icon for the entity."""
        return "mdi:clock"

    async def _update_config_entry(self):
        """Update the config entry with the new value."""
        updated_options = {**self._config_entry.options, self._key: self._value}
        self.hass.config_entries.async_update_entry(
            self._config_entry, options=updated_options
        )

    @property
    def extra_state_attributes(self):
        """Return additional attributes."""
        return {"description": self._description}

    async def async_set_time(self, value: str):
        """Set a new time after validation."""
        validation_errors = self._validate_time(value)
        if validation_errors:
            raise ValueError(f"Time validation failed: {validation_errors}")

        self._value = value
        await self._update_config_entry()

    def _validate_time(self, new_time: str) -> dict[str, str]:
        """Validate the new time against related times."""
        errors = {}
        try:
            new_time_obj = datetime.strptime(new_time, "%H:%M:%S").time()

            # Retrieve related time entities for validation
            options = self._config_entry.options
            if self._key.endswith("_day_start"):
                day_end = options.get("hsem_batteries_enable_charge_hours_day_end")
                if day_end:
                    day_end_obj = datetime.strptime(day_end, "%H:%M:%S").time()
                    if new_time_obj >= day_end_obj:
                        errors["day_start"] = "start_time_after_end_time"
            elif self._key.endswith("_day_end"):
                day_start = options.get("hsem_batteries_enable_charge_hours_day_start")
                if day_start:
                    day_start_obj = datetime.strptime(day_start, "%H:%M:%S").time()
                    if new_time_obj <= day_start_obj:
                        errors["day_end"] = "end_time_before_start_time"
            elif self._key.endswith("_night_start"):
                night_end = options.get("hsem_batteries_enable_charge_hours_night_end")
                if night_end:
                    night_end_obj = datetime.strptime(night_end, "%H:%M:%S").time()
                    if new_time_obj >= night_end_obj:
                        errors["night_start"] = "start_time_after_end_time"
            elif self._key.endswith("_night_end"):
                night_start = options.get(
                    "hsem_batteries_enable_charge_hours_night_start"
                )
                if night_start:
                    night_start_obj = datetime.strptime(night_start, "%H:%M:%S").time()
                    if new_time_obj <= night_start_obj:
                        errors["night_end"] = "end_time_before_start_time"

        except (ValueError, TypeError):
            errors["base"] = "invalid_time_format"

        return errors
