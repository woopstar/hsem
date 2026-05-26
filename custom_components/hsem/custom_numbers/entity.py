"""Number entity for the HSEM battery charge/discharge efficiency.

This module defines :class:`HSEMBatteryEfficiencyNumber`, a standard
:class:`homeassistant.components.number.NumberEntity` that lets users
adjust the battery charge/discharge efficiency percentage from the UI
without re-running the config/options flow.

The ``unique_id`` and ``entity_id`` are injected by the ``number.py``
platform module after construction, so this class does not hard-code
any entity identifiers.

The value is persisted to the config entry options so it survives
HA restarts.
"""

from __future__ import annotations

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
            config_key: Config entry option key (e.g. ``hsem_batteries_charge_efficiency``).
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
        self._attr_name = str(raw_name) if isinstance(raw_name, str) else None

        # Load initial value from the config entry.
        stored = convert_to_float(get_config_value(config_entry, config_key))
        self._attr_native_value = stored if stored is not None else default

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
