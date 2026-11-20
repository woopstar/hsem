"""Diagnostic sensor for the measured battery efficiency (issue #1431).

State is the measured round-trip efficiency in percent.  The attributes hold
the apparent charge and discharge curve per 500 W step, the sample count per
step and the configured round trip the planner uses.

Only the round trip stands on its own.  The charge and discharge figures are
measured against the rated capacity, so they carry the battery's capacity
error and are named *apparent*.

The sensor only reports.  The planner keeps the configured efficiencies.
"""

from __future__ import annotations

from typing import Any, override

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory

from custom_components.hsem.coordinator import HSEMDataUpdateCoordinator
from custom_components.hsem.devices import HSEMDevice
from custom_components.hsem.entity import HSEMCoordinatorEntity, HSEMEntity
from custom_components.hsem.utils.conversion import convert_to_float
from custom_components.hsem.utils.efficiency_learner import (
    CHARGE,
    DISCHARGE,
    MIN_ENERGY_KWH,
    MIN_SAMPLES,
    MIN_TOTAL_ENERGY_KWH,
    MIN_TOTAL_SAMPLES,
    STEP_W,
    EfficiencyLearner,
)
from custom_components.hsem.utils.sensornames.diagnostics import (
    get_battery_efficiency_sensor_entity_id,
    get_battery_efficiency_sensor_unique_id,
)


def _rounded(value: float | None) -> float | None:
    """Return *value* rounded to one decimal, keeping ``None``."""
    return None if value is None else round(value, 1)


class HSEMBatteryEfficiencySensor(HSEMCoordinatorEntity, SensorEntity, HSEMEntity):
    """Diagnostic sensor exposing the measured battery efficiency."""

    _attr_icon = "mdi:battery-sync"
    _attr_has_entity_name = True
    _attr_translation_key = "battery_efficiency"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1
    _unrecorded_attributes = frozenset({"charge_curve", "discharge_curve"})

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: HSEMDataUpdateCoordinator,
    ) -> None:
        """Initialise the battery efficiency sensor.

        Args:
            config_entry: The HSEM config entry.
            coordinator: The shared :class:`HSEMDataUpdateCoordinator`.
        """
        HSEMCoordinatorEntity.__init__(self, coordinator)
        HSEMEntity.__init__(self, config_entry)
        self._hsem_device = HSEMDevice.BATTERY_ENERGY
        self._attr_unique_id = get_battery_efficiency_sensor_unique_id(
            config_entry.entry_id
        )
        self.entity_id = get_battery_efficiency_sensor_entity_id()

    @property
    @override
    def native_value(self) -> float | None:
        """Return the measured round-trip efficiency (%), or ``None``."""
        learner = self._get_learner()
        if learner is None:
            return None
        return _rounded(learner.round_trip_efficiency_pct)

    @property
    @override
    def should_poll(self) -> bool:
        """No polling — driven by the coordinator."""
        return False

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the curves, sample counts and the configured round trip."""
        learner = self._get_learner()
        if learner is None:
            return None
        measured = learner.round_trip_efficiency_pct
        configured = self._configured_round_trip_pct()
        return {
            "measured_round_trip_efficiency_pct": _rounded(measured),
            "configured_round_trip_efficiency_pct": _rounded(configured),
            "round_trip_difference_pct": (
                _rounded(measured - configured)
                if measured is not None and configured is not None
                else None
            ),
            "round_trip_status": learner.round_trip_status,
            "capacity_basis": "rated",
            "apparent_charge_efficiency_pct": _rounded(learner.charge_efficiency_pct),
            "apparent_discharge_efficiency_pct": _rounded(
                learner.discharge_efficiency_pct
            ),
            "charge_status": learner.status(CHARGE),
            "discharge_status": learner.status(DISCHARGE),
            "charge_samples": learner.sample_count(CHARGE),
            "discharge_samples": learner.sample_count(DISCHARGE),
            "charge_curve": learner.curve(CHARGE),
            "discharge_curve": learner.curve(DISCHARGE),
            "power_step_w": STEP_W,
            "min_samples_per_step": MIN_SAMPLES,
            "min_energy_per_step_kwh": MIN_ENERGY_KWH,
            "min_samples_per_direction": MIN_TOTAL_SAMPLES,
            "min_energy_per_direction_kwh": MIN_TOTAL_ENERGY_KWH,
        }

    def _get_learner(self) -> EfficiencyLearner | None:
        """Return the efficiency learner from the coordinator."""
        return getattr(self.coordinator, "_efficiency_learner", None)

    def _configured_round_trip_pct(self) -> float | None:
        """Return configured charge x discharge efficiency (%), or ``None``."""
        data = self.coordinator.data
        cfg = data.cfg if data is not None else None
        if cfg is None:
            return None
        charge = convert_to_float(cfg.batteries_charge_efficiency)
        discharge = convert_to_float(cfg.batteries_discharge_efficiency)
        if charge is None or discharge is None:
            return None
        return charge * discharge / 100.0
