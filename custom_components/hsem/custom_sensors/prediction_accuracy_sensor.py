"""Diagnostic sensor that exposes prediction-vs-actual accuracy metrics.

State
-----
The sensor state is the overall SoC MAE over 7 days rounded to 3 decimal
places, or ``None`` (unavailable) when no records have been collected yet.

Attributes
----------
- ``soc_mae_7d`` — Mean absolute error of SoC prediction (%) over 7 days.
- ``soc_mae_30d`` — Mean absolute error of SoC prediction (%) over 30 days.
- ``solar_mape`` — Mean absolute percentage error of PV forecast (%).
- ``load_mae_kwh`` — Mean absolute error of load prediction (kWh).
- ``action_mix`` — Fraction of slots per action (charge / discharge / idle).
- ``records_count`` — Number of records in the rolling buffer.

The sensor is a *diagnostic* entity (``EntityCategory.DIAGNOSTIC``).
"""

from __future__ import annotations

from typing import Any, cast, override

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory

from custom_components.hsem.coordinator import HSEMDataUpdateCoordinator
from custom_components.hsem.entity import HSEMCoordinatorEntity, HSEMEntity
from custom_components.hsem.utils.sensornames.diagnostics import (
    get_prediction_accuracy_sensor_entity_id,
    get_prediction_accuracy_sensor_name,
    get_prediction_accuracy_sensor_unique_id,
)


class HSEMPredictionAccuracySensor(
    HSEMCoordinatorEntity,
    SensorEntity,
    HSEMEntity,
):
    """Diagnostic sensor exposing prediction-vs-actual accuracy metrics.

    State: SoC MAE over 7 days (pct, rounded).
    Attributes: soc_mae_7d, soc_mae_30d, solar_mape, load_mae_kwh,
                action_mix, records_count.
    """

    _attr_icon = "mdi:target"
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
        self._attr_unique_id = get_prediction_accuracy_sensor_unique_id(
            config_entry.entry_id
        )
        self._attr_name = get_prediction_accuracy_sensor_name()
        self.entity_id = get_prediction_accuracy_sensor_entity_id()

    @property
    @override
    def should_poll(self) -> bool:
        """No polling — driven by the coordinator."""
        return False

    @property
    @override
    def native_value(self) -> str | float | None:
        """Return the sensor state.

        State is the SoC MAE over 7 days (pct) when records exist,
        otherwise ``None`` (unavailable).
        """
        if self.coordinator.data is None:
            return None
        tracker = getattr(self.coordinator, "_prediction_tracker", None)
        if tracker is None or tracker.soc_mae_7d is None:
            return None
        return cast(float, round(tracker.soc_mae_7d, 3))

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return diagnostic attributes for the sensor."""
        if self.coordinator.data is None:
            return None
        tracker = getattr(self.coordinator, "_prediction_tracker", None)
        if tracker is None:
            return None

        attrs: dict[str, Any] = {
            "soc_mae_7d": (
                round(tracker.soc_mae_7d, 4) if tracker.soc_mae_7d is not None else None
            ),
            "soc_mae_30d": (
                round(tracker.soc_mae_30d, 4)
                if tracker.soc_mae_30d is not None
                else None
            ),
            "solar_mape": (
                round(tracker.solar_mape, 2) if tracker.solar_mape is not None else None
            ),
            "load_mae_kwh": (
                round(tracker.load_mae_kwh, 4)
                if tracker.load_mae_kwh is not None
                else None
            ),
            "action_mix": tracker.action_mix,
            "records_count": len(tracker.records),
        }
        return cast(dict[str, Any], attrs)
