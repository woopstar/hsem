"""Diagnostic sensors exposing OCPP charger state to Home Assistant.

Provides four sensors:

- ``sensor.hsem_ocpp_charger_status`` — Connection status and charging state.
- ``sensor.hsem_ocpp_charger_power`` — Live charging power (kW).
- ``sensor.hsem_ocpp_charger_info`` — Vendor, model, firmware, serial.
- ``sensor.hsem_ocpp_charger_sessions`` — Completed session log.

All sensors are diagnostic entities that subscribe to the shared
:class:`~custom_components.hsem.coordinator.HSEMDataUpdateCoordinator`.
They read charger state from :attr:`CoordinatorData.ocpp_chargers`.
"""

from __future__ import annotations

from typing import Any, override

from homeassistant.components.sensor import SensorEntity
from homeassistant.components.sensor.const import SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    UnitOfPower,
)
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.coordinator import (
    CoordinatorData,
    HSEMDataUpdateCoordinator,
)
from custom_components.hsem.entity import HSEMCoordinatorEntity, HSEMEntity
from custom_components.hsem.utils.sensornames.ocpp import (
    get_ocpp_charger_info_sensor_entity_id,
    get_ocpp_charger_info_sensor_name,
    get_ocpp_charger_info_sensor_unique_id,
    get_ocpp_charger_power_sensor_entity_id,
    get_ocpp_charger_power_sensor_name,
    get_ocpp_charger_power_sensor_unique_id,
    get_ocpp_charger_sessions_sensor_entity_id,
    get_ocpp_charger_sessions_sensor_name,
    get_ocpp_charger_sessions_sensor_unique_id,
    get_ocpp_charger_status_sensor_entity_id,
    get_ocpp_charger_status_sensor_name,
    get_ocpp_charger_status_sensor_unique_id,
)

# ---------------------------------------------------------------------------
# OCPP Charger Status Sensor
# ---------------------------------------------------------------------------


class HSEMOCPPChargerStatusSensor(
    HSEMCoordinatorEntity,
    RestoreEntity,
    SensorEntity,
    HSEMEntity,
):
    """Diagnostic sensor exposing OCPP charger connection and charging status.

    State is one of:
    - ``"disconnected"`` — No charger connected.
    - ``"Available"`` — Charger connected but idle.
    - ``"Preparing"`` — Preparing to charge.
    - ``"Charging"`` — Actively charging.
    - ``"Finishing"`` — Finishing a charge session.
    """

    _attr_icon = "mdi:ev-station"
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: HSEMDataUpdateCoordinator,
    ) -> None:
        """Initialise the OCPP charger status sensor.

        Args:
            config_entry: The HSEM config entry.
            coordinator: The shared coordinator.
        """
        HSEMCoordinatorEntity.__init__(self, coordinator)
        HSEMEntity.__init__(self, config_entry)

        self._config_entry = config_entry
        self._attr_unique_id = get_ocpp_charger_status_sensor_unique_id(
            config_entry.entry_id
        )
        self.entity_id = get_ocpp_charger_status_sensor_entity_id()
        self._name = get_ocpp_charger_status_sensor_name()
        self._restored_state: str | None = None

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

    @property  # type: ignore[misc]
    @override
    def state(self) -> str:
        """Return the charger connection/charging status."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None:
            return self._restored_state or "disconnected"

        chargers = data.ocpp_chargers or {}
        if not chargers:
            return "disconnected"

        # Return the status of the first connected charger
        first = next(iter(chargers.values()))
        return first.status if first.status else "disconnected"

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return per-charger status details."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None or not data.ocpp_chargers:
            return {}

        attrs: dict[str, Any] = {}
        for cpid, session in data.ocpp_chargers.items():
            attrs[cpid] = {
                "status": session.status,
                "power_w": round(session.current_power_w, 1),
                "transaction_id": session.transaction_id,
                "connected_at": (
                    session.connected_at.isoformat() if session.connected_at else None
                ),
            }
        return attrs

    @property
    @override
    def should_poll(self) -> bool:
        """No polling — driven by the coordinator."""
        return False

    @property
    @override
    def available(self) -> bool:
        """Return True when the coordinator has data."""
        return (
            self.coordinator.last_update_success and self.coordinator.data is not None
        ) or self._restored_state is not None

    @override
    async def async_added_to_hass(self) -> None:
        """Restore previous state on startup."""
        await super().async_added_to_hass()
        restored = await self.async_get_last_state()
        if restored is not None:
            self._restored_state = restored.state


# ---------------------------------------------------------------------------
# OCPP Charger Power Sensor
# ---------------------------------------------------------------------------


class HSEMOCPPChargerPowerSensor(
    HSEMCoordinatorEntity,
    RestoreEntity,
    SensorEntity,
    HSEMEntity,
):
    """Diagnostic sensor exposing live OCPP charger power in kW.

    State is a float representing the current charging power in kilowatts.
    """

    _attr_icon = "mdi:flash"
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: HSEMDataUpdateCoordinator,
    ) -> None:
        """Initialise the OCPP charger power sensor.

        Args:
            config_entry: The HSEM config entry.
            coordinator: The shared coordinator.
        """
        HSEMCoordinatorEntity.__init__(self, coordinator)
        HSEMEntity.__init__(self, config_entry)

        self._config_entry = config_entry
        self._attr_unique_id = get_ocpp_charger_power_sensor_unique_id(
            config_entry.entry_id
        )
        self.entity_id = get_ocpp_charger_power_sensor_entity_id()
        self._name = get_ocpp_charger_power_sensor_name()
        self._restored_state: str | None = None

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

    @property  # type: ignore[misc]
    @override
    def state(self) -> float | str:
        """Return the current charging power in kW."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None or not data.ocpp_chargers:
            return self._restored_state or "0.0"

        first = next(iter(data.ocpp_chargers.values()))
        return float(round(first.current_power_w / 1000.0, 2))  # type: ignore[no-any-return]

    @property
    @override
    def should_poll(self) -> bool:
        """No polling — driven by the coordinator."""
        return False

    @property
    @override
    def available(self) -> bool:
        """Return True when the coordinator has data."""
        return (
            self.coordinator.last_update_success and self.coordinator.data is not None
        ) or self._restored_state is not None

    @override
    async def async_added_to_hass(self) -> None:
        """Restore previous state on startup."""
        await super().async_added_to_hass()
        restored = await self.async_get_last_state()
        if restored is not None:
            self._restored_state = restored.state


# ---------------------------------------------------------------------------
# OCPP Charger Info Sensor
# ---------------------------------------------------------------------------


class HSEMOCPPChargerInfoSensor(
    HSEMCoordinatorEntity,
    RestoreEntity,
    SensorEntity,
    HSEMEntity,
):
    """Diagnostic sensor exposing OCPP charger identity information.

    State is the charger model string.  Attributes include vendor,
    firmware version, and serial number.
    """

    _attr_icon = "mdi:information-outline"
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: HSEMDataUpdateCoordinator,
    ) -> None:
        """Initialise the OCPP charger info sensor.

        Args:
            config_entry: The HSEM config entry.
            coordinator: The shared coordinator.
        """
        HSEMCoordinatorEntity.__init__(self, coordinator)
        HSEMEntity.__init__(self, config_entry)

        self._config_entry = config_entry
        self._attr_unique_id = get_ocpp_charger_info_sensor_unique_id(
            config_entry.entry_id
        )
        self.entity_id = get_ocpp_charger_info_sensor_entity_id()
        self._name = get_ocpp_charger_info_sensor_name()
        self._restored_state: str | None = None

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

    @property  # type: ignore[misc]
    @override
    def state(self) -> str:
        """Return the charger model or 'disconnected'."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None or not data.ocpp_chargers:
            return self._restored_state or "disconnected"

        first = next(iter(data.ocpp_chargers.values()))
        return first.model or "unknown"

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return charger identity details."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None or not data.ocpp_chargers:
            return {}

        first = next(iter(data.ocpp_chargers.values()))
        return {
            "vendor": first.vendor,
            "model": first.model,
            "firmware": first.firmware,
            "serial": first.serial,
            "cpid": first.cpid,
        }

    @property
    @override
    def should_poll(self) -> bool:
        """No polling — driven by the coordinator."""
        return False

    @property
    @override
    def available(self) -> bool:
        """Return True when the coordinator has data."""
        return (
            self.coordinator.last_update_success and self.coordinator.data is not None
        ) or self._restored_state is not None

    @override
    async def async_added_to_hass(self) -> None:
        """Restore previous state on startup."""
        await super().async_added_to_hass()
        restored = await self.async_get_last_state()
        if restored is not None:
            self._restored_state = restored.state


# ---------------------------------------------------------------------------
# OCPP Charger Sessions Sensor
# ---------------------------------------------------------------------------


class HSEMOCPPChargerSessionsSensor(
    HSEMCoordinatorEntity,
    RestoreEntity,
    SensorEntity,
    HSEMEntity,
):
    """Diagnostic sensor exposing OCPP completed session log.

    State is the number of completed sessions (0 when none).  Attributes
    expose the session history.
    """

    _attr_icon = "mdi:history"
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: HSEMDataUpdateCoordinator,
    ) -> None:
        """Initialise the OCPP charger sessions sensor.

        Args:
            config_entry: The HSEM config entry.
            coordinator: The shared coordinator.
        """
        HSEMCoordinatorEntity.__init__(self, coordinator)
        HSEMEntity.__init__(self, config_entry)

        self._config_entry = config_entry
        self._attr_unique_id = get_ocpp_charger_sessions_sensor_unique_id(
            config_entry.entry_id
        )
        self.entity_id = get_ocpp_charger_sessions_sensor_entity_id()
        self._name = get_ocpp_charger_sessions_sensor_name()
        self._restored_state: str | None = None

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

    @property  # type: ignore[misc]
    @override
    def state(self) -> str:
        """Return the session count."""
        data: CoordinatorData | None = self.coordinator.data
        sessions = data.ocpp_sessions if data is not None else None
        if sessions is None:
            return self._restored_state or "0"
        return str(len(sessions))

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return session history."""
        data: CoordinatorData | None = self.coordinator.data
        sessions = data.ocpp_sessions if data is not None else None
        if sessions is None:
            return {}
        return {"sessions": sessions}

    @property
    @override
    def should_poll(self) -> bool:
        """No polling — driven by the coordinator."""
        return False

    @property
    @override
    def available(self) -> bool:
        """Return True when the coordinator has data."""
        return (
            self.coordinator.last_update_success and self.coordinator.data is not None
        ) or self._restored_state is not None

    @override
    async def async_added_to_hass(self) -> None:
        """Restore previous state on startup."""
        await super().async_added_to_hass()
        restored = await self.async_get_last_state()
        if restored is not None:
            self._restored_state = restored.state
