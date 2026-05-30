"""Diagnostic sensor that exposes the result of the last inverter apply cycle.

State values
------------
``ok``
    All hardware writes in the last cycle were verified successfully, or no
    writes were needed (all values already matched — ``skipped`` is also
    treated as ``ok`` from a health perspective).

``unverified``
    At least one write could not be verified because the entity read-back
    returned ``None`` (entity unavailable after the write).

``failed``
    At least one write failed all retry attempts — the inverter did not accept
    the new value within the allowed tolerance.

``pending``
    The coordinator has not yet completed a hardware-write cycle (e.g. on HA
    restart before the first coordinator tick).

The sensor is a *diagnostic* entity (``entity_category = EntityCategory.DIAGNOSTIC``)
so it appears in the *Diagnostic* section of the device page.

This sensor subscribes to :class:`~custom_components.hsem.coordinator.HSEMDataUpdateCoordinator`
and updates automatically after every coordinator cycle, including after the
working-mode sensor mutates :attr:`CoordinatorData.apply_summary`.
"""

from __future__ import annotations

from typing import Any, override

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.coordinator import (
    CoordinatorData,
    HSEMDataUpdateCoordinator,
)
from custom_components.hsem.entity import HSEMCoordinatorEntity, HSEMEntity
from custom_components.hsem.utils.inverter_verify import ApplyStatus
from custom_components.hsem.utils.sensornames import (
    get_applier_status_sensor_entity_id,
    get_applier_status_sensor_name,
    get_applier_status_sensor_unique_id,
)

_PENDING = "pending"
_VALID_STATES = {s.value for s in ApplyStatus} | {_PENDING}


class HSEMApplierStatusSensor(
    HSEMCoordinatorEntity,
    RestoreEntity,
    SensorEntity,
    HSEMEntity,
):
    """Diagnostic sensor exposing the last hardware-write cycle outcome.

    State reflects the worst-case :class:`~utils.inverter_verify.ApplyStatus`
    across all writes in the most recent coordinator cycle.

    Attributes
    ----------
    ``last_apply_details``
        List of per-entity result dicts (entity_id, desired, actual, status,
        attempts, error_message).
    ``failed_entities``
        List of entity IDs whose writes failed all retries.
    ``unverified_entities``
        List of entity IDs whose read-back returned None.
    """

    _attr_icon = "mdi:check-network"
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: HSEMDataUpdateCoordinator,
    ) -> None:
        """Initialise the applier-status sensor.

        Args:
            config_entry: The HSEM config entry.
            coordinator: The shared :class:`HSEMDataUpdateCoordinator`.
        """
        HSEMCoordinatorEntity.__init__(self, coordinator)
        HSEMEntity.__init__(self, config_entry)

        self._config_entry = config_entry
        self._attr_unique_id = get_applier_status_sensor_unique_id()
        self.entity_id = get_applier_status_sensor_entity_id()
        self._name = get_applier_status_sensor_name()

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

    @property  # type: ignore[misc]  # HA stub declares state as @final
    @override
    def state(self) -> str:
        """Return the worst-case apply status for the last cycle.

        Returns ``"pending"`` when no cycle has completed yet, restoring a
        previously persisted state if available.
        """
        data: CoordinatorData | None = self.coordinator.data
        if data is None or data.apply_summary is None:
            return self._restored_state or _PENDING
        return data.apply_summary.overall_status.value

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
        """Return per-entity write results and aggregated failure lists."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None or data.apply_summary is None:
            return {
                "last_apply_details": [],
                "failed_entities": [],
                "unverified_entities": [],
                "total_writes": 0,
            }

        summary = data.apply_summary
        details = [
            {
                "entity_id": r.entity_id,
                "desired": r.desired,
                "actual": r.actual,
                "status": r.status.value,
                "attempts": r.attempts,
                "error_message": r.error_message,
            }
            for r in summary.results
        ]
        return {
            "last_apply_details": details,
            "failed_entities": summary.failed_entities,
            "unverified_entities": summary.unverified_entities,
            "total_writes": len(summary.results),
        }

    # ------------------------------------------------------------------
    # HA lifecycle
    # ------------------------------------------------------------------

    @override
    async def async_added_to_hass(self) -> None:
        """Restore previous state and register coordinator listener."""
        await super().async_added_to_hass()
        restored = await self.async_get_last_state()
        if restored is not None and restored.state in _VALID_STATES:
            self._restored_state = restored.state
