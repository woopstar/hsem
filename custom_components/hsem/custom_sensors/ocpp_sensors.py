"""Diagnostic sensors exposing OCPP charger state to Home Assistant.

Provides four sensors per OCPP server (one server per EV):

- ``sensor.hsem_ocpp_charger_status`` — Connection status and charging state.
- ``sensor.hsem_ocpp_charger_power`` — Live charging power (kW).
- ``sensor.hsem_ocpp_charger_info`` — Vendor, model, firmware, serial.
- ``sensor.hsem_ocpp_charger_sessions`` — Completed session log.

The second EV's server (when configured) exposes the same four sensors with
``_second`` entity IDs, reading from the second server's charger state.

All sensors are diagnostic entities that subscribe to the shared
:class:`~custom_components.hsem.coordinator.HSEMDataUpdateCoordinator`.
They read charger state from :attr:`CoordinatorData.ocpp_chargers` and
:attr:`CoordinatorData.ocpp_second_chargers`.
"""

from __future__ import annotations

from typing import Any, override
from urllib.parse import urlparse

from homeassistant.components.sensor import SensorEntity
from homeassistant.components.sensor.const import SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    UnitOfPower,
)
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.coordinator import (
    CoordinatorData,
    HSEMDataUpdateCoordinator,
)
from custom_components.hsem.entity import HSEMCoordinatorEntity, HSEMEntity
from custom_components.hsem.models.sensor_config import SensorConfig
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


def _chargers_for(data: CoordinatorData | None, charger_index: int) -> dict | None:
    """Return the charger dict for the given EV's OCPP server.

    Args:
        data: Coordinator data (may be ``None``).
        charger_index: ``1`` for the primary EV server, ``2`` for the second.
    """
    if data is None:
        return None
    if charger_index == 2:
        return data.ocpp_second_chargers
    return data.ocpp_chargers


def _ocpp_enabled_for(cfg: SensorConfig, charger_index: int) -> bool:
    """Return whether OCPP is configured/enabled for the given EV's server."""
    if charger_index == 2:
        return cfg.ocpp_second_enabled
    return cfg.ocpp_enabled


def _is_listening(data: CoordinatorData | None, charger_index: int) -> bool:
    """Return whether the given EV's OCPP server is currently listening."""
    if data is None:
        return False
    if charger_index == 2:
        return data.ocpp_second_listening
    return data.ocpp_listening


def _configured_port(cfg: SensorConfig, charger_index: int) -> int:
    """Return the configured TCP port for the given EV's OCPP server."""
    return cfg.ocpp_second_port if charger_index == 2 else cfg.ocpp_port


def _connection_url(hass: Any, port: int) -> str | None:
    """Build a best-effort ``ws://<host>:<port>/`` connection URL.

    The embedded server binds ``0.0.0.0`` (all interfaces), which isn't a
    usable address for an EVSE to dial. Resolve HA's own LAN-reachable host
    via :func:`homeassistant.helpers.network.get_url` instead. Returns
    ``None`` when no usable URL can be resolved — the caller falls back to
    showing host/port separately.
    """
    try:
        base = get_url(hass, allow_internal=True, allow_ip=True, prefer_external=False)
    except NoURLAvailableError:
        return None
    host = urlparse(base).hostname
    if not host:
        return None
    return f"ws://{host}:{port}/"


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
    - ``"not_configured"`` — This EV's OCPP server isn't enabled in config.
    - ``"disconnected"`` — Server enabled but no charger connected.
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
        charger_index: int = 1,
    ) -> None:
        """Initialise the OCPP charger status sensor.

        Args:
            config_entry: The HSEM config entry.
            coordinator: The shared coordinator.
            charger_index: ``1`` for the primary EV server, ``2`` for the
                second EV's server.
        """
        HSEMCoordinatorEntity.__init__(self, coordinator)
        HSEMEntity.__init__(self, config_entry)

        self._config_entry = config_entry
        self._charger_index = charger_index
        self._attr_unique_id = get_ocpp_charger_status_sensor_unique_id(
            config_entry.entry_id, charger_index=charger_index
        )
        self.entity_id = get_ocpp_charger_status_sensor_entity_id(
            charger_index=charger_index
        )
        self._name = get_ocpp_charger_status_sensor_name(charger_index)
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

    @property  # type: ignore[misc]  # HA stub declares state as @final
    @override
    def state(self) -> str:
        """Return the charger connection/charging status."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None:
            return self._restored_state or "disconnected"

        if data.cfg is not None and not _ocpp_enabled_for(
            data.cfg, self._charger_index
        ):
            return "not_configured"

        chargers = _chargers_for(data, self._charger_index) or {}
        if not chargers:
            return "disconnected"

        # Return the status of the first connected charger
        first = next(iter(chargers.values()))
        return first.status if first.status else "disconnected"

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return server diagnostics and per-charger status details."""
        data: CoordinatorData | None = self.coordinator.data
        if (
            data is None
            or data.cfg is None
            or not _ocpp_enabled_for(data.cfg, self._charger_index)
        ):
            return {}

        port = _configured_port(data.cfg, self._charger_index)
        attrs: dict[str, Any] = {
            "listening": _is_listening(data, self._charger_index),
            "port": port,
        }
        url = _connection_url(self.hass, port)
        if url is not None:
            attrs["url"] = url

        chargers = _chargers_for(data, self._charger_index)
        if chargers:
            for cpid, session in chargers.items():
                attrs[cpid] = {
                    "status": session.status,
                    "power_w": round(session.current_power_w, 1),
                    "transaction_id": session.transaction_id,
                    "connected_at": (
                        session.connected_at.isoformat()
                        if session.connected_at
                        else None
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
        charger_index: int = 1,
    ) -> None:
        """Initialise the OCPP charger power sensor.

        Args:
            config_entry: The HSEM config entry.
            coordinator: The shared coordinator.
            charger_index: ``1`` for the primary EV server, ``2`` for the
                second EV's server.
        """
        HSEMCoordinatorEntity.__init__(self, coordinator)
        HSEMEntity.__init__(self, config_entry)

        self._config_entry = config_entry
        self._charger_index = charger_index
        self._attr_unique_id = get_ocpp_charger_power_sensor_unique_id(
            config_entry.entry_id, charger_index=charger_index
        )
        self.entity_id = get_ocpp_charger_power_sensor_entity_id(
            charger_index=charger_index
        )
        self._name = get_ocpp_charger_power_sensor_name(charger_index)
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

    @property  # type: ignore[misc]  # HA stub declares state as @final
    @override
    def state(self) -> float | str:
        """Return the current charging power in kW."""
        data: CoordinatorData | None = self.coordinator.data
        chargers = _chargers_for(data, self._charger_index)
        if not chargers:
            return self._restored_state or "0.0"

        first = next(iter(chargers.values()))
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
        charger_index: int = 1,
    ) -> None:
        """Initialise the OCPP charger info sensor.

        Args:
            config_entry: The HSEM config entry.
            coordinator: The shared coordinator.
            charger_index: ``1`` for the primary EV server, ``2`` for the
                second EV's server.
        """
        HSEMCoordinatorEntity.__init__(self, coordinator)
        HSEMEntity.__init__(self, config_entry)

        self._config_entry = config_entry
        self._charger_index = charger_index
        self._attr_unique_id = get_ocpp_charger_info_sensor_unique_id(
            config_entry.entry_id, charger_index=charger_index
        )
        self.entity_id = get_ocpp_charger_info_sensor_entity_id(
            charger_index=charger_index
        )
        self._name = get_ocpp_charger_info_sensor_name(charger_index)
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

    @property  # type: ignore[misc]  # HA stub declares state as @final
    @override
    def state(self) -> str:
        """Return the charger model or 'disconnected'."""
        data: CoordinatorData | None = self.coordinator.data
        chargers = _chargers_for(data, self._charger_index)
        if not chargers:
            return self._restored_state or "disconnected"

        first = next(iter(chargers.values()))
        return first.model or "unknown"

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return charger identity details."""
        data: CoordinatorData | None = self.coordinator.data
        chargers = _chargers_for(data, self._charger_index)
        if not chargers:
            return {}

        first = next(iter(chargers.values()))
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
        charger_index: int = 1,
    ) -> None:
        """Initialise the OCPP charger sessions sensor.

        Args:
            config_entry: The HSEM config entry.
            coordinator: The shared coordinator.
            charger_index: ``1`` for the primary EV server, ``2`` for the
                second EV's server.
        """
        HSEMCoordinatorEntity.__init__(self, coordinator)
        HSEMEntity.__init__(self, config_entry)

        self._config_entry = config_entry
        self._charger_index = charger_index
        self._attr_unique_id = get_ocpp_charger_sessions_sensor_unique_id(
            config_entry.entry_id, charger_index=charger_index
        )
        self.entity_id = get_ocpp_charger_sessions_sensor_entity_id(
            charger_index=charger_index
        )
        self._name = get_ocpp_charger_sessions_sensor_name(charger_index)
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

    @property  # type: ignore[misc]  # HA stub declares state as @final
    @override
    def state(self) -> str:
        """Return the session count."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None:
            sessions = None
        elif self._charger_index == 2:
            sessions = data.ocpp_second_sessions
        else:
            sessions = data.ocpp_sessions
        if sessions is None:
            return self._restored_state or "0"
        return str(len(sessions))

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return session history."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None:
            sessions = None
        elif self._charger_index == 2:
            sessions = data.ocpp_second_sessions
        else:
            sessions = data.ocpp_sessions
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
