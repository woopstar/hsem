"""Diagnostic sensor exposing per-hour solar forecast accuracy factors.

State
-----
The sensor state is the mean per-hour accuracy factor across all learned hours,
or ``unavailable`` when no factors have been learned yet.

Attributes
----------
- ``hour_factors`` — JSON-serialized dict of per-hour factors (0–23).
- ``confidence`` — Configured confidence percentile.
- ``residual_count`` — Number of intra-hour residuals in the buffer.
- ``_solar_corrector_data`` — Serialised corrector state for reboot persistence.

The sensor is a *diagnostic* entity (``EntityCategory.DIAGNOSTIC``).
"""

from __future__ import annotations

import json
from typing import Any, cast, override

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
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
from custom_components.hsem.utils.sensornames.diagnostics import (
    get_solar_confidence_sensor_entity_id,
    get_solar_confidence_sensor_name,
    get_solar_confidence_sensor_unique_id,
)
from custom_components.hsem.utils.solar_corrector import SolarForecastCorrector


class HSEMSolarConfidenceSensor(
    HSEMCoordinatorEntity,
    RestoreEntity,
    SensorEntity,
    HSEMEntity,
):
    """Diagnostic sensor exposing per-hour solar forecast accuracy factors.

    State: mean per-hour accuracy factor (or unavailable).
    Attributes: all learned factors, confidence, residual count.
    """

    _attr_icon = "mdi:solar-power"
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: HSEMDataUpdateCoordinator,
    ) -> None:
        """Initialise the sensor.

        Args:
            config_entry: The HSEM config entry.
            coordinator: The shared HSEM coordinator.
        """
        HSEMCoordinatorEntity.__init__(self, coordinator)
        HSEMEntity.__init__(self, config_entry)
        self._attr_unique_id = get_solar_confidence_sensor_unique_id(
            config_entry.entry_id
        )
        self._attr_name = get_solar_confidence_sensor_name()
        self.entity_id = get_solar_confidence_sensor_entity_id()
        self._restored_state: str | None = None

    @property
    @override
    def should_poll(self) -> bool:
        """This entity does not poll — updates are pushed by the coordinator."""
        return False

    @property
    @override
    def available(self) -> bool:
        """Return True when the coordinator is healthy or a restored state is available."""
        return (
            self.coordinator.last_update_success and self.coordinator.data is not None
        ) or self._restored_state is not None

    @property
    @override
    def native_value(self) -> str | float | None:
        """Return the mean per-hour accuracy factor, or fall back to restored state if no data."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None:
            if self._restored_state is not None:
                return float(self._restored_state)
            return None

        factors = data.solar_hour_factors
        if not factors:
            return None

        mean_factor = sum(factors.values()) / len(factors)
        return cast(float, round(mean_factor, 4))

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return diagnostic attributes for the sensor."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None:
            return None

        corrector: SolarForecastCorrector | None = getattr(
            self.coordinator, "_solar_corrector", None
        )

        attrs: dict[str, Any] = {
            "hour_factors": json.dumps(data.solar_hour_factors),
            "confidence": (corrector.confidence if corrector is not None else 0.50),
            "residual_count": (
                len(corrector._recent_residuals) if corrector is not None else 0
            ),
        }

        # Include serialised corrector state for reboot persistence.
        if corrector is not None:
            attrs["_solar_corrector_data"] = corrector.to_dict()

        return cast(dict[str, Any], attrs)

    @property
    @override
    def native_unit_of_measurement(self) -> str | None:
        """Return the unit of measurement (ratio, so no unit)."""
        return None

    # ------------------------------------------------------------------
    # HA lifecycle — reboot persistence
    # ------------------------------------------------------------------

    @override
    async def async_added_to_hass(self) -> None:
        """Restore solar corrector data and sensor state from the previous HA session."""
        await super().async_added_to_hass()
        restored = await self.async_get_last_state()
        if restored is None:
            return

        # Restore sensor state
        if restored.state not in {STATE_UNAVAILABLE, STATE_UNKNOWN, None}:
            self._restored_state = restored.state

        corrector_data = restored.attributes.get("_solar_corrector_data")
        if corrector_data is None:
            return

        corrector: SolarForecastCorrector | None = getattr(
            self.coordinator, "_solar_corrector", None
        )
        if corrector is not None:
            corrector.load_from_dict(corrector_data)
