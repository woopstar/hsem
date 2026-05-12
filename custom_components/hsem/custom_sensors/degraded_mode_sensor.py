"""Diagnostic sensor that exposes the HSEM system health / degraded-mode state.

The sensor reports one of three states that mirror :class:`DegradedMode`:

``ok``
    All required input entities are present and readable.  HSEM is operating
    normally; hardware writes are permitted.

``degraded``
    One or more *non-critical* entities are unavailable (e.g. electricity
    price feed).  Read-only planner calculations continue on best-effort
    values; hardware writes are still allowed because the battery state data
    is intact.

``error``
    One or more *critical* entities are missing (battery SoC, max charge /
    discharge power, rated capacity, or house consumption power).  Hardware
    writes are **blocked** to prevent acting on incomplete state.

The sensor is a *diagnostic* entity (``entity_category = EntityCategory.DIAGNOSTIC``)
so it appears in the *Diagnostic* section of the device page and is excluded
from the default Lovelace dashboard.

The working-mode sensor calls :meth:`HSEMDegradedModeSensor.async_update_from_live`
at the end of every update cycle so both sensors always stay in sync without an
extra polling round-trip.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.entity import HSEMEntity
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.utils.degraded_mode import DegradedMode
from custom_components.hsem.utils.sensornames import (
    get_degraded_mode_sensor_entity_id,
    get_degraded_mode_sensor_name,
    get_degraded_mode_sensor_unique_id,
)


class HSEMDegradedModeSensor(SensorEntity, HSEMEntity, RestoreEntity):
    """Diagnostic sensor exposing the current HSEM system-health state.

    The state is one of ``"ok"``, ``"degraded"``, or ``"error"`` — matching
    the :attr:`DegradedMode.value` strings.

    The sensor has **no** polling or timer of its own; it is updated by
    ``HSEMWorkingModeSensor`` via :meth:`async_update_from_live` at the end
    of each update cycle.
    """

    _attr_icon = "mdi:shield-check"
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, config_entry) -> None:
        """Initialise the sensor with an ``ok`` state."""
        super().__init__(config_entry)

        self._config_entry = config_entry
        self._state: str = DegradedMode.OK.value
        self._available: bool = False

        self._attr_unique_id = get_degraded_mode_sensor_unique_id()
        self.entity_id = get_degraded_mode_sensor_entity_id()
        self._name = get_degraded_mode_sensor_name()

        self._missing_entities_list: list[str] = []
        self._hardware_writes_blocked: bool = False

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
    def state(self) -> str:
        """Return the current health state string."""
        return self._state

    @property
    def should_poll(self) -> bool:
        """No polling — updated by the working-mode sensor."""
        return False

    @property
    def available(self) -> bool:
        """Return True after the first update cycle completes."""
        return self._available

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return diagnostic attributes visible on the entity detail page."""
        return {
            "missing_entities": self._missing_entities_list,
            "hardware_writes_blocked": self._hardware_writes_blocked,
        }

    # ------------------------------------------------------------------
    # HA lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Restore previous state if available."""
        await super().async_added_to_hass()
        restored = await self.async_get_last_state()
        if restored is not None and restored.state in {m.value for m in DegradedMode}:
            self._state = restored.state
            self._available = True

    # ------------------------------------------------------------------
    # Public update API (called by HSEMWorkingModeSensor)
    # ------------------------------------------------------------------

    async def async_update_from_live(self, live: LiveState | None) -> None:
        """Refresh the sensor state from the latest :class:`LiveState`.

        Called by ``HSEMWorkingModeSensor._async_run_update_cycle`` at the
        end of every cycle so both sensors report a consistent snapshot.

        Args:
            live: The :class:`LiveState` produced in the current cycle, or
                ``None`` if the cycle did not produce a live state (e.g. on
                first init before the first collect).
        """
        if live is None:
            self._state = DegradedMode.OK.value
            self._missing_entities_list = []
            self._hardware_writes_blocked = False
        else:
            mode = live.degraded_mode
            self._state = mode.value
            self._missing_entities_list = list(live.missing_entities_list)
            from custom_components.hsem.utils.degraded_mode import (
                hardware_writes_allowed,
            )

            self._hardware_writes_blocked = not hardware_writes_allowed(mode)

        self._available = True
        self.async_write_ha_state()
