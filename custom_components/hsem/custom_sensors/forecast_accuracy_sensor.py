"""Diagnostic sensor that exposes forecast-vs-actual accuracy metrics.

State
-----
The sensor state is the overall PV MAE in kWh (rounded to 3 decimal places),
or ``"unavailable"`` when no slots have been finalised yet.

Attributes
----------
All fields from :class:`~custom_components.hsem.utils.forecast_tracker.ForecastErrorSummary`
are exposed as flat state attributes, plus:

- ``latest_pv_forecast_kwh`` — most recent finalised slot PV forecast.
- ``latest_pv_actual_kwh`` — most recent finalised slot PV actual.
- ``latest_load_forecast_kwh`` — most recent finalised slot load forecast.
- ``latest_load_actual_kwh`` — most recent finalised slot load actual.
- ``latest_bias_pv_kwh`` — bias for the most recent finalised slot.
- ``latest_bias_load_kwh`` — bias for the most recent finalised slot.

The sensor is a *diagnostic* entity (``EntityCategory.DIAGNOSTIC``).
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.coordinator import (
    CoordinatorData,
    HSEMDataUpdateCoordinator,
)
from custom_components.hsem.entity import HSEMCoordinatorEntity, HSEMEntity
from custom_components.hsem.utils.sensornames import (
    get_forecast_accuracy_sensor_entity_id,
    get_forecast_accuracy_sensor_name,
    get_forecast_accuracy_sensor_unique_id,
)


class HSEMForecastAccuracySensor(
    HSEMCoordinatorEntity,
    SensorEntity,
    HSEMEntity,
    RestoreEntity,
):
    """Diagnostic sensor exposing forecast-vs-actual accuracy metrics.

    State: PV MAE in kWh (rounded).
    Attributes: all :class:`ForecastErrorSummary` fields plus latest slot data.
    """

    _attr_icon = "mdi:chart-line"
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        config_entry,
        coordinator: HSEMDataUpdateCoordinator,
    ) -> None:
        """Initialise the sensor.

        Args:
            config_entry: The HSEM config entry.
            coordinator: The shared HSEM coordinator.
        """
        super().__init__(coordinator)
        HSEMEntity.__init__(self, config_entry)
        self._attr_unique_id = get_forecast_accuracy_sensor_unique_id()
        self._attr_name = get_forecast_accuracy_sensor_name()
        self._attr_entity_id = get_forecast_accuracy_sensor_entity_id()

    @property
    def native_value(self) -> str | float | None:
        """Return the sensor state.

        State is the PV MAE (kWh) when records exist, otherwise
        ``None`` (unavailable).
        """
        data: CoordinatorData | None = self.coordinator.data
        if data is None:
            return None
        tracker = getattr(self.coordinator, "_forecast_tracker", None)
        if tracker is None:
            return None
        summary = tracker.summary
        if summary.finalised_count == 0:
            return None
        return round(summary.mae_pv_kwh, 3)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return diagnostic attributes for the sensor."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None:
            return None
        tracker = getattr(self.coordinator, "_forecast_tracker", None)
        if tracker is None:
            return None

        summary = tracker.summary
        attrs = summary.as_dict()

        # Add latest slot info
        finalised = [r for r in tracker.records if r.finalised]
        if finalised:
            latest = finalised[-1]
            attrs["latest_pv_forecast_kwh"] = round(latest.forecast_pv_kwh, 3)
            attrs["latest_pv_actual_kwh"] = round(latest.actual_pv_kwh, 3)
            attrs["latest_load_forecast_kwh"] = round(latest.forecast_load_kwh, 3)
            attrs["latest_load_actual_kwh"] = round(latest.actual_load_kwh, 3)
            attrs["latest_bias_pv_kwh"] = (
                round(latest.bias_pv, 4) if latest.bias_pv is not None else None
            )
            attrs["latest_bias_load_kwh"] = (
                round(latest.bias_load, 4) if latest.bias_load is not None else None
            )

        return attrs

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the unit of measurement."""
        return "kWh"
