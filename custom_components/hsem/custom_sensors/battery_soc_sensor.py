"""Diagnostic sensor that mirrors the Huawei Solar battery state-of-charge (SoC).

Exposes the battery SoC as a first-class sensor on the HSEM device page so users
can track it without navigating to the Huawei Solar integration.  It also makes
the value directly available to HSEM-scoped automations.

The value is a snapshot taken at the start of the most recent coordinator cycle,
not a real-time reading.  For live SoC use the underlying Huawei Solar sensor.

The sensor is a *diagnostic* entity (``entity_category = EntityCategory.DIAGNOSTIC``)
so it appears in the *Diagnostic* section of the device page and is excluded from
the default Lovelace dashboard.

This sensor subscribes to :class:`~custom_components.hsem.coordinator.HSEMDataUpdateCoordinator`
and updates automatically after every coordinator cycle.
"""

from __future__ import annotations

from typing import Any, override

from homeassistant.components.sensor import SensorEntity
from homeassistant.components.sensor.const import SensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
)
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.coordinator import (
    CoordinatorData,
    HSEMDataUpdateCoordinator,
)
from custom_components.hsem.entity import HSEMCoordinatorEntity, HSEMEntity
from custom_components.hsem.utils.sensornames import (
    get_battery_soc_sensor_entity_id,
    get_battery_soc_sensor_name,
    get_battery_soc_sensor_unique_id,
)


class HSEMBatterySoCSensor(
    HSEMCoordinatorEntity,
    RestoreEntity,
    SensorEntity,
    HSEMEntity,
):
    """Diagnostic sensor mirroring the battery SoC snapshot from the last cycle.

    State is the SoC percentage (float, 0–100) recorded at the start of the most
    recently completed coordinator cycle, or ``None`` when not yet available.

    The sensor subscribes to the shared coordinator and is updated automatically
    after every coordinator cycle.
    """

    _attr_icon = "mdi:battery-high"
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 1

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: HSEMDataUpdateCoordinator,
    ) -> None:
        """Initialise the battery-SoC sensor.

        Args:
            config_entry: The HSEM config entry.
            coordinator: The shared :class:`HSEMDataUpdateCoordinator`.
        """
        HSEMCoordinatorEntity.__init__(self, coordinator)
        HSEMEntity.__init__(self, config_entry)

        self._config_entry = config_entry

        self._attr_unique_id = get_battery_soc_sensor_unique_id()
        self.entity_id = get_battery_soc_sensor_entity_id()
        self._name = get_battery_soc_sensor_name()

        self._restored_state: str | None = None

    # ------------------------------------------------------------------
    # HA entity properties
    # ------------------------------------------------------------------

    @property
    @override
    def name(self) -> str:
        """Return the display name."""
        return self._name

    @property
    @override
    def unique_id(self) -> str | None:
        """Return the unique ID."""
        return self._attr_unique_id

    @property
    @override
    def native_value(self) -> float | None:
        """Return the battery SoC percentage from the last cycle."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None or data.live is None:
            if self._restored_state is not None:
                try:
                    return float(self._restored_state)
                except (ValueError, TypeError):
                    pass
            return None
        return data.live.huawei_batteries_soc_pct

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
        """Return battery capacity context."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None or data.live is None:
            return {
                "battery_current_capacity_kwh": None,
                "battery_usable_capacity_kwh": None,
                "battery_rated_capacity_wh": None,
                "end_of_discharge_soc_pct": None,
            }
        live = data.live
        return {
            "battery_current_capacity_kwh": live.battery_current_capacity_kwh,
            "battery_usable_capacity_kwh": live.battery_usable_capacity_kwh,
            "battery_rated_capacity_wh": live.huawei_batteries_rated_capacity_wh,
            "end_of_discharge_soc_pct": live.huawei_batteries_end_of_discharge_soc_pct,
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
