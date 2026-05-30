"""Number entity for HSEM battery charge/discharge efficiency.

Defines :class:`HSEMBatteryEfficiencyNumber`, a standard
:class:`homeassistant.components.number.NumberEntity` that lets users
adjust the battery charge/discharge efficiency percentage from the UI
without re-running the config/options flow.

The value is persisted to the config entry options so it survives
HA restarts.
"""

from __future__ import annotations

from typing import override

from homeassistant.components.number import NumberEntity, NumberEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant

from custom_components.hsem.entity import HSEMEntity
from custom_components.hsem.utils.misc import convert_to_float, get_config_value


class HSEMBatteryEfficiencyNumber(NumberEntity, HSEMEntity):
    """Number entity for a battery-side efficiency percentage.

    Exposes a slider (50–100 %, step 1) that lets users adjust the
    charge or discharge efficiency at runtime.  The value is written
    back to ``config_entry.options`` so it persists across HA restarts.
    """

    _attr_has_entity_name = True
    _attr_native_min_value = 50.0
    _attr_native_max_value = 100.0
    _attr_native_step = 1.0
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:battery-high"

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        description: NumberEntityDescription,
        config_key: str,
        default: float = 98.0,
        *,
        unique_id: str = "",
        entity_id: str = "",
    ) -> None:
        """Initialize the number entity.

        Args:
            hass: The Home Assistant instance.
            config_entry: The config entry this entity belongs to.
            description: Entity description carrying ``key`` and ``name``.
            config_key: Config entry option key
                (e.g. ``hsem_batteries_charge_efficiency``).
            default: Default value when no config entry value exists yet.
            unique_id: Stable unique ID for HA entity registry.
            entity_id: The desired entity_id string for this entity.
        """
        super().__init__(config_entry)

        self.hass = hass
        self._config_entry = config_entry
        self._config_key = config_key
        self._default = default
        self.entity_description = description
        self._attr_unique_id = unique_id if unique_id else description.key
        if entity_id:
            self.entity_id = entity_id

        raw_name = description.name
        if isinstance(raw_name, str):
            self._attr_name = str(raw_name)
        # When description.name is UNDEFINED (no explicit name),
        # leave _attr_name unset so the translation system can
        # resolve the name via entity_description.translation_key.

        # Load initial value from the config entry.
        stored = convert_to_float(get_config_value(config_entry, config_key))
        self._attr_native_value = stored if stored is not None else default

    @override
    async def async_set_native_value(self, value: float) -> None:
        """Handle the user setting a new value.

        Updates HA state and persists the choice to the config entry options.

        Args:
            value: The new efficiency percentage.
        """
        clamped = max(
            self._attr_native_min_value, min(self._attr_native_max_value, value)
        )
        self._attr_native_value = clamped
        self.async_write_ha_state()

        # Persist to config entry options so it survives restart.
        new_options = {**self._config_entry.options, self._config_key: clamped}
        self.hass.config_entries.async_update_entry(
            self._config_entry, options=new_options
        )
