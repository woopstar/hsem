"""Diagnostic sensor that exposes whether any EV charger managed by HSEM is active.

The state is ``"on"`` when at least one EV charger reports an active charging
session, ``"off"`` otherwise.  This makes EV charging activity directly
automatable without parsing the working-mode sensor attributes.

Both the primary and secondary EV charger are considered.  The secondary charger
is included only when it is enabled in the HSEM configuration.

The sensor is a *diagnostic* entity (``entity_category = EntityCategory.DIAGNOSTIC``)
so it appears in the *Diagnostic* section of the device page and is excluded from
the default Lovelace dashboard.

This sensor subscribes to :class:`~custom_components.hsem.coordinator.HSEMDataUpdateCoordinator`
and updates automatically after every coordinator cycle.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF, STATE_ON, EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.coordinator import (
    CoordinatorData,
    HSEMDataUpdateCoordinator,
)
from custom_components.hsem.entity import HSEMCoordinatorEntity, HSEMEntity
from custom_components.hsem.utils.sensornames import (
    get_ev_charging_sensor_entity_id,
    get_ev_charging_sensor_name,
    get_ev_charging_sensor_unique_id,
)

_VALID_STATES = {STATE_ON, STATE_OFF}


class HSEMEVChargingSensor(
    HSEMCoordinatorEntity,
    RestoreEntity,
    SensorEntity,
    HSEMEntity,
):
    """Diagnostic sensor exposing whether any HSEM-managed EV charger is active.

    State is ``"on"`` when at least one EV charger is charging, ``"off"``
    otherwise.

    The sensor subscribes to the shared coordinator and is updated automatically
    after every coordinator cycle.
    """

    _attr_icon = "mdi:ev-station"
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: HSEMDataUpdateCoordinator,
    ) -> None:
        """Initialise the EV-charging sensor.

        Args:
            config_entry: The HSEM config entry.
            coordinator: The shared :class:`HSEMDataUpdateCoordinator`.
        """
        HSEMCoordinatorEntity.__init__(self, coordinator)
        HSEMEntity.__init__(self, config_entry)

        self._config_entry = config_entry

        self._attr_unique_id = get_ev_charging_sensor_unique_id()
        self.entity_id = get_ev_charging_sensor_entity_id()
        self._name = get_ev_charging_sensor_name()

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

    @property  # type: ignore[misc]  # HA stub declares state as @final
    def state(self) -> str:
        """Return ``'on'`` when any EV charger is active, ``'off'`` otherwise."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None or data.live is None:
            return self._restored_state or STATE_OFF
        return STATE_ON if data.live.any_ev_charging else STATE_OFF

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
        """Return individual charger states."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None or data.live is None:
            return {
                "ev_charging": False,
                "ev_power_w": None,
                "ev_soc_pct": None,
                "ev_second_enabled": False,
                "ev_second_charging": False,
                "ev_second_power_w": None,
                "ev_second_soc_pct": None,
            }
        live = data.live
        cfg = data.cfg
        return {
            "ev_charging": live.ev.is_charging,
            "ev_power_w": live.ev.power_w,
            "ev_soc_pct": live.ev.soc_pct,
            "ev_second_enabled": (
                bool(cfg.ev_second_enabled) if cfg is not None else False
            ),
            "ev_second_charging": live.ev_second.is_charging,
            "ev_second_power_w": live.ev_second.power_w,
            "ev_second_soc_pct": live.ev_second.soc_pct,
        }

    # ------------------------------------------------------------------
    # HA lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Restore previous state and register coordinator listener."""
        await super().async_added_to_hass()
        restored = await self.async_get_last_state()
        if restored is not None and restored.state in _VALID_STATES:
            self._restored_state = restored.state
