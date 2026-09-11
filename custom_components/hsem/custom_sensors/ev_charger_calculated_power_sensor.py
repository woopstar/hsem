"""Diagnostic sensors exposing the planner's calculated EV charger power.

The planner writes its per-slot EV power *command* onto the current
:class:`~custom_components.hsem.models.hourly_recommendation.HourlyRecommendation`
(``ev_charger_calculated_power`` for the primary charger and
``ev_second_charger_calculated_power`` for the second charger).  These values
are otherwise only visible inside the working-mode sensor attributes, which
makes them hard to track in history graphs or use directly in automations.

This module exposes each value as its own sensor whose **state equals the
calculated power in Watts** for the currently active planning slot.  The state
is ``0`` when the planner has not allocated any EV charging for the slot.

Both sensors are *diagnostic* entities
(``entity_category = EntityCategory.DIAGNOSTIC``) so they appear in the
*Diagnostic* section of the device page and are excluded from the default
Lovelace dashboard.

The sensors subscribe to
:class:`~custom_components.hsem.coordinator.HSEMDataUpdateCoordinator` and
update automatically after every coordinator cycle.
"""

from __future__ import annotations

from typing import Any, override

from homeassistant.components.sensor import SensorEntity
from homeassistant.components.sensor.const import SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
    UnitOfPower,
)
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.coordinator import (
    CoordinatorData,
    HSEMDataUpdateCoordinator,
)
from custom_components.hsem.entity import HSEMCoordinatorEntity, HSEMEntity
from custom_components.hsem.utils.sensornames.ev import (
    get_ev_charger_calculated_power_sensor_entity_id,
    get_ev_charger_calculated_power_sensor_unique_id,
    get_ev_second_charger_calculated_power_sensor_entity_id,
    get_ev_second_charger_calculated_power_sensor_unique_id,
)


class HSEMEVChargerCalculatedPowerSensorBase(
    HSEMCoordinatorEntity,
    RestoreEntity,
    SensorEntity,
    HSEMEntity,
):
    """Base class for the EV charger calculated power sensors.

    Subclasses set :attr:`_is_second` to select which planner field the
    sensor exposes, plus the name/unique-id/entity-id getters.
    """

    _attr_icon = "mdi:ev-station"
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_suggested_display_precision = 0
    _is_second: bool = False

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: HSEMDataUpdateCoordinator,
    ) -> None:
        """Initialise the calculated power sensor.

        Args:
            config_entry: The HSEM config entry.
            coordinator: The shared :class:`HSEMDataUpdateCoordinator`.
        """
        HSEMCoordinatorEntity.__init__(self, coordinator)
        HSEMEntity.__init__(self, config_entry)

        self._config_entry = config_entry
        self._restored_state: str | None = None

    # ------------------------------------------------------------------
    # HA entity properties
    # ------------------------------------------------------------------

    @property
    @override
    def unique_id(self) -> str | None:
        """Return the unique ID."""
        return self._attr_unique_id

    @property
    @override
    def native_value(self) -> float | None:
        """Return the calculated charger power (W) for the active slot."""
        data: CoordinatorData | None = self.coordinator.data
        rec = data.hourly_recommendation if data is not None else None
        if rec is None:
            if self._restored_state is not None:
                try:
                    return float(self._restored_state)
                except ValueError, TypeError:
                    pass
            return None
        if self._is_second:
            return rec.ev_second_charger_calculated_power
        return rec.ev_charger_calculated_power

    @property
    @override
    def should_poll(self) -> bool:
        """No polling — driven by the coordinator."""
        return False

    @property
    @override
    def available(self) -> bool:
        """True once the coordinator has completed at least one successful cycle."""
        return (
            self.coordinator.last_update_success and self.coordinator.data is not None
        ) or self._restored_state is not None

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the active slot context for the calculated power value."""
        data: CoordinatorData | None = self.coordinator.data
        rec = data.hourly_recommendation if data is not None else None
        if rec is None:
            return {
                "slot_start": None,
                "slot_end": None,
                "ev_total_planned_load_kwh": None,
            }
        return {
            "slot_start": rec.start.isoformat(),
            "slot_end": rec.end.isoformat(),
            "ev_total_planned_load_kwh": rec.ev_total_planned_load_kwh,
        }

    # ------------------------------------------------------------------
    # HA lifecycle
    # ------------------------------------------------------------------

    @override
    async def async_added_to_hass(self) -> None:
        """Restore previous state and register coordinator listener."""
        await super().async_added_to_hass()
        restored = await self.async_get_last_state()
        if restored is not None and restored.state not in {
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
            None,
        }:
            self._restored_state = restored.state


class HSEMEVChargerCalculatedPowerSensor(HSEMEVChargerCalculatedPowerSensorBase):
    """Sensor exposing the primary EV charger's calculated power (W)."""

    _attr_translation_key = "ev_charger_calculated_power"
    _is_second = False

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: HSEMDataUpdateCoordinator,
    ) -> None:
        """Initialise the primary EV charger calculated power sensor."""
        super().__init__(config_entry, coordinator)
        self._attr_unique_id = get_ev_charger_calculated_power_sensor_unique_id(
            config_entry.entry_id
        )
        self.entity_id = get_ev_charger_calculated_power_sensor_entity_id()


class HSEMEVSecondChargerCalculatedPowerSensor(HSEMEVChargerCalculatedPowerSensorBase):
    """Sensor exposing the second EV charger's calculated power (W)."""

    _attr_translation_key = "ev_second_charger_calculated_power"
    _is_second = True

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: HSEMDataUpdateCoordinator,
    ) -> None:
        """Initialise the second EV charger calculated power sensor."""
        super().__init__(config_entry, coordinator)
        self._attr_unique_id = get_ev_second_charger_calculated_power_sensor_unique_id(
            config_entry.entry_id
        )
        self.entity_id = get_ev_second_charger_calculated_power_sensor_entity_id()
