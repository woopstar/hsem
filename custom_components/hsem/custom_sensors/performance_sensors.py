"""Performance (KPI) sensors: how good the last day's control turned out to be.

The trailing 24 hours scored against perfect hindsight, over the same slots
and the same realized prices.

Stage 1 (issue #1182), the result:

- ``sensor.hsem_performance_savings_capture`` — the share of what the best
  possible control could have saved over plain inverter self-consumption that
  HSEM actually saved (%).  Unknown when there was almost nothing to gain.
- ``sensor.hsem_performance_actual_benefit`` — what HSEM's control was worth
  against self-consumption (currency).
- ``sensor.hsem_performance_missed_opportunity`` — what perfect hindsight
  would have saved on top (currency).  The most robust of the three.

Stage 2 (issue #1270), where the missed opportunity was lost:

- ``sensor.hsem_performance_battery_utilisation`` — the share of the potential
  not lost through battery dispatch (%).
- ``sensor.hsem_performance_solar_utilisation`` — the share of the potential
  not lost through the handling of PV surplus (%).

Stage 3 (issue #1271), the timing of EV charging, only when EV planning is
enabled for a charger:

- ``sensor.hsem_performance_ev_charging_efficiency`` — the share of what the
  best timing saves over plugging in and charging that was realized (%).

All of them publish the same attributes: the cost breakdown, the window, how
much of it was observed and how much of that HSEM was in control, why a value
is unknown (``reason``, ``breakdown_reason``, ``ev_reason``), and the same figures for
yesterday and for the last 7 and 30 completed days.

They are rolling 24-hour values, not totals, so they use the ``measurement``
state class and carry no ``monetary`` device class (Home Assistant allows
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
from custom_components.hsem.models.performance_record import PerformanceScore
from custom_components.hsem.models.performance_tracker import PerformanceTracker
from custom_components.hsem.utils.datetime_utils import now as hsem_now
from custom_components.hsem.utils.misc import get_config_value
from custom_components.hsem.utils.sensornames.financial import (
    get_performance_actual_benefit_entity_id,
    get_performance_actual_benefit_unique_id,
    get_performance_battery_utilisation_entity_id,
    get_performance_battery_utilisation_unique_id,
    get_performance_ev_charging_efficiency_entity_id,
    get_performance_ev_charging_efficiency_unique_id,
    get_performance_missed_opportunity_entity_id,
    get_performance_missed_opportunity_unique_id,
    get_performance_savings_capture_entity_id,
    get_performance_savings_capture_unique_id,
    get_performance_solar_utilisation_entity_id,
    get_performance_solar_utilisation_unique_id,
)


def _capture_percent(score: PerformanceScore) -> float | None:
    capture = score.capture
    return None if capture is None else round(capture * 100.0, 1)


def _battery_utilisation_percent(score: PerformanceScore) -> float | None:
    share = score.battery_utilisation
    return None if share is None else round(share * 100.0, 1)


def _solar_utilisation_percent(score: PerformanceScore) -> float | None:
    share = score.solar_utilisation
    return None if share is None else round(share * 100.0, 1)


def _ev_efficiency_percent(score: PerformanceScore) -> float | None:
    share = score.ev_efficiency
    return None if share is None else round(share * 100.0, 1)


def _actual_benefit(score: PerformanceScore) -> float | None:
    savings = score.savings
    return None if savings is None else round(savings, 3)


def _missed_opportunity(score: PerformanceScore) -> float | None:
    regret = score.regret
    return None if regret is None else round(regret, 3)


@dataclass(frozen=True)
class PerformanceSensorDescription:
    """What sets one performance sensor apart from the others.

    Attributes:
        key: The KPI, used in the translation key.
        icon: The sensor's icon.
        value: Reads the sensor's state off a window score.
        entity_id: Returns the sensor's entity ID.
        unique_id: Returns the sensor's unique ID for a config entry.
        is_share: ``True`` for a percentage, ``False`` for an amount of money.
    """

    key: str
    icon: str
    value: Callable[[PerformanceScore], float | None]
    entity_id: Callable[[], str]
    unique_id: Callable[[str], str]
    is_share: bool = False


#: The KPIs: stage 1 (issue #1182) and the battery / solar split of stage 2
#: (issue #1270).  Later stages add their sensors here.
PERFORMANCE_SENSORS: tuple[PerformanceSensorDescription, ...] = (
    PerformanceSensorDescription(
        key="savings_capture",
        icon="mdi:bullseye-arrow",
        value=_capture_percent,
        entity_id=get_performance_savings_capture_entity_id,
        unique_id=get_performance_savings_capture_unique_id,
        is_share=True,
    ),
    PerformanceSensorDescription(
        key="actual_benefit",
        icon="mdi:cash-check",
        value=_actual_benefit,
        entity_id=get_performance_actual_benefit_entity_id,
        unique_id=get_performance_actual_benefit_unique_id,
    ),
    PerformanceSensorDescription(
        key="missed_opportunity",
        icon="mdi:cash-remove",
        value=_missed_opportunity,
        entity_id=get_performance_missed_opportunity_entity_id,
        unique_id=get_performance_missed_opportunity_unique_id,
    ),
    PerformanceSensorDescription(
        key="battery_utilisation",
        icon="mdi:battery-charging-high",
        value=_battery_utilisation_percent,
        entity_id=get_performance_battery_utilisation_entity_id,
        unique_id=get_performance_battery_utilisation_unique_id,
        is_share=True,
    ),
    PerformanceSensorDescription(
        key="solar_utilisation",
        icon="mdi:solar-power-variant",
        value=_solar_utilisation_percent,
        entity_id=get_performance_solar_utilisation_entity_id,
        unique_id=get_performance_solar_utilisation_unique_id,
        is_share=True,
    ),
)


#: Stage 3 (issue #1271).  Only created when EV planning is enabled for a
#: charger: without one there is no session to score.
EV_PERFORMANCE_SENSOR = PerformanceSensorDescription(
    key="ev_charging_efficiency",
    icon="mdi:ev-station",
    value=_ev_efficiency_percent,
    entity_id=get_performance_ev_charging_efficiency_entity_id,
    unique_id=get_performance_ev_charging_efficiency_unique_id,
    is_share=True,
)

#: Config keys that switch EV planning on for a charger.  Both are entity
#: gating keys, so the entry is reloaded when one of them changes.
_EV_PLANNING_KEYS = (
    "hsem_ev_planned_load_enabled",
    "hsem_ev_second_planned_load_enabled",
)


class HSEMPerformanceSensor(HSEMCoordinatorEntity, SensorEntity, HSEMEntity):
    """One KPI of the trailing 24 hours, scored against perfect hindsight."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    # The period rollups and notes are kept in the tracker's own history
    # file; only the state belongs in the recorder (issue #1099).
    _unrecorded_attributes = frozenset(
        {"yesterday", "last_7_days", "last_30_days", "daily", "notes"}
    )

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: HSEMDataUpdateCoordinator,
        description: PerformanceSensorDescription,
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
        self._attr_translation_key = f"performance_{description.key}"
        self._attr_unique_id = description.unique_id(config_entry.entry_id)
        self.entity_id = description.entity_id()

    @property
    @override
    def native_unit_of_measurement(self) -> str | None:
        """Return percent for the capture, the HA currency for an amount."""
        if self._description.is_share:
            return PERCENTAGE
        return str(self.coordinator.hass.config.currency)

    @property
    @override
    def native_value(self) -> float | None:
        """Return the KPI over the trailing 24 hours, or ``None`` if unknown."""
        tracker = self._get_tracker()
        if tracker is None or tracker.window is None:
            return None
        return self._description.value(tracker.window)

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the cost breakdown, diagnostics and period figures."""
        tracker = self._get_tracker()
        if tracker is None:
            return None
        return tracker.as_sensor_attributes(hsem_now())

    @property
    @override
    def should_poll(self) -> bool:
        """No polling — driven by the coordinator."""
        return False

    def _get_tracker(self) -> PerformanceTracker | None:
        """Return the performance tracker from the coordinator."""
        return getattr(self.coordinator, "_performance_tracker", None)


def build_performance_sensors(
    config_entry: ConfigEntry, coordinator: HSEMDataUpdateCoordinator
) -> list[HSEMPerformanceSensor]:
    """Return one sensor per performance KPI.

    The EV charging efficiency sensor is included only when EV planning is
    enabled for at least one charger.

    Args:
        config_entry: The HSEM config entry.
        coordinator: The shared HSEM coordinator.
    """
    descriptions = list(PERFORMANCE_SENSORS)
    if any(bool(get_config_value(config_entry, key)) for key in _EV_PLANNING_KEYS):
        descriptions.append(EV_PERFORMANCE_SENSOR)
    return [
        HSEMPerformanceSensor(config_entry, coordinator, description)
        for description in descriptions
    ]
