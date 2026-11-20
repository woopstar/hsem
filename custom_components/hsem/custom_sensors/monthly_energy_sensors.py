"""Monthly energy sensors: what the installation did, in plain energy figures.

Four sensors, each the current calendar month so far (issue #1435):

- ``sensor.hsem_monthly_self_sufficiency`` — share of the house consumption
  that did not come from the grid (%).
- ``sensor.hsem_monthly_battery_cycles`` — full equivalent battery cycles.
- ``sensor.hsem_monthly_battery_wear_cost`` — cycle cost of the battery
  throughput (currency).
- ``sensor.hsem_monthly_round_trip_loss`` — measured share of the energy
  charged into the battery that did not come out again (%).

All four publish the same attributes: ``this_month`` and ``previous_month``
with every figure and the energy totals behind it, and ``monthly`` with the
last 12 months.  ``reason`` says why this sensor's value is unknown.

Every figure is a ratio of sums over the daily records of
:class:`~custom_components.hsem.models.energy_tracker.EnergyTracker`.  The
values restart each month, so the sensors use the ``measurement`` state class
and the amount carries no ``monetary`` device class (Home Assistant allows
that one only on totals).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, override

from homeassistant.components.sensor import SensorEntity
from homeassistant.components.sensor.const import SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory

from custom_components.hsem.coordinator import HSEMDataUpdateCoordinator
from custom_components.hsem.devices import HSEMDevice
from custom_components.hsem.entity import HSEMCoordinatorEntity, HSEMEntity
from custom_components.hsem.models.energy_tracker import EnergyTracker
from custom_components.hsem.utils.datetime_utils import now as hsem_now
from custom_components.hsem.utils.sensornames.monthly_energy import (
    get_monthly_battery_cycles_entity_id,
    get_monthly_battery_cycles_unique_id,
    get_monthly_battery_wear_cost_entity_id,
    get_monthly_battery_wear_cost_unique_id,
    get_monthly_round_trip_loss_entity_id,
    get_monthly_round_trip_loss_unique_id,
    get_monthly_self_sufficiency_entity_id,
    get_monthly_self_sufficiency_unique_id,
)

#: Unit of the cycle count.  Not a Home Assistant unit constant.
UNIT_CYCLES = "cycles"


@dataclass(frozen=True)
class MonthlyEnergySensorDescription:
    """What sets one monthly energy sensor apart from the others.

    Attributes:
        key: The KPI: the month summary's field, and part of the entity ID
            and the translation key.
        icon: The sensor's icon.
        unit: The unit, or ``None`` for an amount in the HA currency.
        precision: Decimals suggested for display.
        entity_id: Returns the sensor's entity ID.
        unique_id: Returns the sensor's unique ID for a config entry.
    """

    key: str
    icon: str
    unit: str | None
    precision: int
    entity_id: Callable[[], str]
    unique_id: Callable[[str], str]


MONTHLY_ENERGY_SENSORS: tuple[MonthlyEnergySensorDescription, ...] = (
    MonthlyEnergySensorDescription(
        key="self_sufficiency",
        icon="mdi:home-lightning-bolt",
        unit=PERCENTAGE,
        precision=1,
        entity_id=get_monthly_self_sufficiency_entity_id,
        unique_id=get_monthly_self_sufficiency_unique_id,
    ),
    MonthlyEnergySensorDescription(
        key="battery_cycles",
        icon="mdi:battery-sync",
        unit=UNIT_CYCLES,
        precision=1,
        entity_id=get_monthly_battery_cycles_entity_id,
        unique_id=get_monthly_battery_cycles_unique_id,
    ),
    MonthlyEnergySensorDescription(
        key="battery_wear_cost",
        icon="mdi:battery-clock",
        unit=None,
        precision=2,
        entity_id=get_monthly_battery_wear_cost_entity_id,
        unique_id=get_monthly_battery_wear_cost_unique_id,
    ),
    MonthlyEnergySensorDescription(
        key="round_trip_loss",
        icon="mdi:battery-arrow-down",
        unit=PERCENTAGE,
        precision=1,
        entity_id=get_monthly_round_trip_loss_entity_id,
        unique_id=get_monthly_round_trip_loss_unique_id,
    ),
)


class HSEMMonthlyEnergySensor(HSEMCoordinatorEntity, SensorEntity, HSEMEntity):
    """One energy KPI of the current calendar month."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    # The month tables are kept in the tracker's own history file; only the
    # state and two scalars belong in the recorder (issue #1099).
    _unrecorded_attributes = frozenset({"this_month", "previous_month", "monthly"})

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: HSEMDataUpdateCoordinator,
        description: MonthlyEnergySensorDescription,
    ) -> None:
        """Initialise the sensor.

        Args:
            config_entry: The HSEM config entry.
            coordinator: The shared HSEM coordinator.
            description: Which KPI this sensor reports.
        """
        HSEMCoordinatorEntity.__init__(self, coordinator)
        HSEMEntity.__init__(self, config_entry)
        self._hsem_device = HSEMDevice.FINANCIAL
        self._description = description
        self._attr_icon = description.icon
        self._attr_suggested_display_precision = description.precision
        self._attr_translation_key = f"monthly_{description.key}"
        self._attr_unique_id = description.unique_id(config_entry.entry_id)
        self.entity_id = description.entity_id()

    @property
    @override
    def native_unit_of_measurement(self) -> str | None:
        """Return the KPI's unit, the HA currency for the wear cost."""
        if self._description.unit is None:
            return str(self.coordinator.hass.config.currency)
        return self._description.unit

    @property
    @override
    def native_value(self) -> float | None:
        """Return the KPI for the current month so far, or ``None`` if unknown."""
        attributes = self.extra_state_attributes
        if attributes is None:
            return None
        value: float | None = attributes["this_month"][self._description.key]
        return value

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the month tables and why this sensor's value is unknown."""
        tracker = self._get_tracker()
        if tracker is None:
            return None
        attributes = tracker.as_sensor_attributes(hsem_now().date())
        attributes["reason"] = attributes["this_month"][
            f"{self._description.key}_reason"
        ]
        return attributes

    @property
    @override
    def should_poll(self) -> bool:
        """No polling — driven by the coordinator."""
        return False

    def _get_tracker(self) -> EnergyTracker | None:
        """Return the energy tracker from the coordinator."""
        return getattr(self.coordinator, "_energy_tracker", None)


def build_monthly_energy_sensors(
    config_entry: ConfigEntry, coordinator: HSEMDataUpdateCoordinator
) -> list[HSEMMonthlyEnergySensor]:
    """Return one sensor per monthly energy KPI.

    Args:
        config_entry: The HSEM config entry.
        coordinator: The shared HSEM coordinator.
    """
    return [
        HSEMMonthlyEnergySensor(config_entry, coordinator, description)
        for description in MONTHLY_ENERGY_SENSORS
    ]
