"""Diagnostic sensor that exposes the instantaneous net power consumption.

The state is the net consumption in Watts at the time of the last coordinator
cycle.  Net consumption = house load − solar production (− EV draw when the
EV sensor is tracked separately from the house meter).

This sensor makes the real-time power balance visible on the HSEM device page
and is useful for debugging planner decisions.

The sensor is a *diagnostic* entity (``entity_category = EntityCategory.DIAGNOSTIC``)
so it appears in the *Diagnostic* section of the device page and is excluded from
the default Lovelace dashboard.

This sensor subscribes to :class:`~custom_components.hsem.coordinator.HSEMDataUpdateCoordinator`
and updates automatically after every coordinator cycle.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.components.sensor.const import SensorDeviceClass
from homeassistant.const import EntityCategory, UnitOfPower
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.coordinator import (
    CoordinatorData,
    HSEMDataUpdateCoordinator,
)
from custom_components.hsem.entity import HSEMCoordinatorEntity, HSEMEntity
from custom_components.hsem.utils.sensornames import (
    get_net_consumption_sensor_entity_id,
    get_net_consumption_sensor_name,
    get_net_consumption_sensor_unique_id,
)


class HSEMNetConsumptionSensor(
    HSEMCoordinatorEntity,
    SensorEntity,
    HSEMEntity,
    RestoreEntity,
):
    """Diagnostic sensor exposing the instantaneous net power consumption (W).

    State is the net consumption in Watts from the last coordinator cycle.
    Positive values mean the house is drawing from grid/battery; negative
    values mean excess solar is available.

    The sensor subscribes to the shared coordinator and is updated automatically
    after every coordinator cycle.
    """

    _attr_icon = "mdi:home-lightning-bolt-outline"
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_suggested_display_precision = 0

    def __init__(
        self,
        config_entry,
        coordinator: HSEMDataUpdateCoordinator,
    ) -> None:
        """Initialise the net-consumption sensor.

        Args:
            config_entry: The HSEM config entry.
            coordinator: The shared :class:`HSEMDataUpdateCoordinator`.
        """
        HSEMCoordinatorEntity.__init__(self, coordinator)
        HSEMEntity.__init__(self, config_entry)

        self._config_entry = config_entry

        self._attr_unique_id = get_net_consumption_sensor_unique_id()
        self.entity_id = get_net_consumption_sensor_entity_id()
        self._name = get_net_consumption_sensor_name()

        self._restored_state: str | None = None

    # ------------------------------------------------------------------
    # HA entity properties
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        """Return the display name."""
        return self._name

    @property
    def unique_id(self) -> str | None:
        """Return the unique ID."""
        return self._attr_unique_id

    @property
    def native_value(self) -> float | None:
        """Return net consumption in Watts."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None or data.live is None:
            if self._restored_state is not None:
                try:
                    return float(self._restored_state)
                except (ValueError, TypeError):
                    pass
            return None
        return data.live.net_consumption_w

    @property
    def should_poll(self) -> bool:
        """No polling — driven by the coordinator."""
        return False

    @property
    def available(self) -> bool:
        """True once the coordinator has completed at least one successful cycle."""
        return (
            self.coordinator.last_update_success and self.coordinator.data is not None
        ) or self._restored_state is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return breakdown of the net consumption components."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None or data.live is None:
            return {
                "house_consumption_w": None,
                "solar_production_w": None,
                "net_consumption_with_ev_w": None,
                "ev_charging_active": False,
            }
        live = data.live
        return {
            "house_consumption_w": live.house_consumption_power_w,
            "solar_production_w": live.solar_production_power_w,
            "net_consumption_with_ev_w": live.net_consumption_with_ev_w,
            "ev_charging_active": live.any_ev_charging,
        }

    # ------------------------------------------------------------------
    # HA lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Restore previous state and register coordinator listener."""
        await super().async_added_to_hass()
        restored = await self.async_get_last_state()
        if restored is not None and restored.state not in {
            "unavailable",
            "unknown",
            None,
        }:
            self._restored_state = restored.state
