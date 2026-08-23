"""Diagnostic sensors publishing HSEM's per-slot EV charging ceiling in amps.

The planner already decides *how much* to charge in every future slot — the
per-slot ``ev_charger_calculated_power`` command varies with price, forecast
PV, fuse headroom, and competition with the house batteries.  Nothing
downstream of the planner previously consumed that decision as a controllable
limit, so an external current controller could only see whether the charger
should run at all and had to ramp on its own toward whatever the fuse
allowed, discarding the planner's per-slot portioning entirely.

Publishing the ceiling separates the two concerns cleanly:

* HSEM owns the **economics** — how many amps are worth drawing this slot.
* An external current controller keeps **final authority** for fuse safety
  and may only ramp *within* the published ceiling.

The sensor state is the ceiling for the slot active right now, in whole
amps.  The forward schedule is exposed as an attribute so the intended
profile can be inspected and graphed without re-deriving it from
diagnostics.

Rounding is always down: a partial amp the charger cannot be commanded to
draw must never be published as available headroom.

Both sensors are *diagnostic* entities (``entity_category =
EntityCategory.DIAGNOSTIC``), matching
:mod:`~custom_components.hsem.custom_sensors.ev_charger_calculated_power_sensor`,
and subscribe to
:class:`~custom_components.hsem.coordinator.HSEMDataUpdateCoordinator` for
automatic updates after every coordinator cycle.
"""

from __future__ import annotations

from typing import Any, override

from homeassistant.components.sensor import SensorEntity
from homeassistant.components.sensor.const import SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
    UnitOfElectricCurrent,
)
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.coordinator import (
    CoordinatorData,
    HSEMDataUpdateCoordinator,
)
from custom_components.hsem.entity import HSEMCoordinatorEntity, HSEMEntity
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.utils.phase_power import (
    charger_power_to_current_a,
    normalize_ev_phase_topology,
)
from custom_components.hsem.utils.sensornames.ev import (
    get_ev_charger_current_limit_sensor_entity_id,
    get_ev_charger_current_limit_sensor_name,
    get_ev_charger_current_limit_sensor_unique_id,
    get_ev_second_charger_current_limit_sensor_entity_id,
    get_ev_second_charger_current_limit_sensor_name,
    get_ev_second_charger_current_limit_sensor_unique_id,
)

#: Number of future slots published in the forward-schedule attribute.
#: Enough to cover a full overnight or working-day charge without making
#: the attribute unwieldy for the recorder.
_SCHEDULE_SLOTS = 24


class HSEMEVChargerCurrentLimitSensorBase(
    HSEMCoordinatorEntity,
    RestoreEntity,
    SensorEntity,
    HSEMEntity,
):
    """Base class for the EV charger current-limit sensors.

    Subclasses set :attr:`_is_second` to select which planner power field and
    phase-topology config the sensor reads, plus the name/unique-id/entity-id
    getters.
    """

    _attr_icon = "mdi:ev-station"
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _is_second: bool = False
    # Set by concrete subclasses in __init__.
    _name: str

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: HSEMDataUpdateCoordinator,
    ) -> None:
        """Initialise the current-limit sensor.

        Args:
            config_entry: The HSEM config entry.
            coordinator: The shared :class:`HSEMDataUpdateCoordinator`.
        """
        HSEMCoordinatorEntity.__init__(self, coordinator)
        HSEMEntity.__init__(self, config_entry)

        self._config_entry = config_entry
        self._restored_state: int | None = None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _topology(self, data: CoordinatorData) -> str:
        """Return the configured phase topology for this charger."""
        cfg = data.cfg
        if cfg is None:
            return normalize_ev_phase_topology(None)
        raw = (
            cfg.ev_second_planned_load_charger_phase_topology
            if self._is_second
            else cfg.ev_planned_load_charger_phase_topology
        )
        return normalize_ev_phase_topology(raw)

    def _power_w(self, rec: HourlyRecommendation) -> float:
        """Return this charger's planned AC power for one recommendation slot."""
        value = (
            rec.ev_second_charger_calculated_power
            if self._is_second
            else rec.ev_charger_calculated_power
        )
        return max(float(value), 0.0)

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

    @property
    @override
    def native_value(self) -> int | None:
        """Return the ceiling in whole amps for the currently active slot.

        ``0`` is a meaningful command — it means HSEM does not want this
        charger drawing in this slot — so it is published rather than
        suppressed.
        """
        data: CoordinatorData | None = self.coordinator.data
        if data is None or data.hourly_recommendation is None:
            return self._restored_state
        return charger_power_to_current_a(
            self._power_w(data.hourly_recommendation),
            self._topology(data),
        )

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
        """Return the forward ceiling schedule and the topology it assumes."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None:
            return {"phase_topology": None, "schedule": []}
        topology = self._topology(data)
        current = data.hourly_recommendation
        schedule: list[dict[str, Any]] = []
        for rec in data.hourly_recommendations:
            if current is not None and rec.start < current.start:
                continue
            power_w = self._power_w(rec)
            schedule.append(
                {
                    "start": rec.start.isoformat(),
                    "current_a": charger_power_to_current_a(power_w, topology),
                    "power_w": round(power_w, 1),
                }
            )
            if len(schedule) >= _SCHEDULE_SLOTS:
                break
        return {"phase_topology": topology, "schedule": schedule}

    # ------------------------------------------------------------------
    # HA lifecycle
    # ------------------------------------------------------------------

    @override
    async def async_added_to_hass(self) -> None:
        """Restore the previous ceiling and register the coordinator listener."""
        await super().async_added_to_hass()
        restored = await self.async_get_last_state()
        if restored is not None and restored.state not in {
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
            None,
        }:
            try:
                self._restored_state = int(float(restored.state))
            except TypeError, ValueError:
                self._restored_state = None


class HSEMEVChargerCurrentLimitSensor(HSEMEVChargerCurrentLimitSensorBase):
    """Publish the primary EV charger's current slot ceiling in whole amps."""

    _attr_translation_key = "ev_charger_current_limit"
    _is_second = False

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: HSEMDataUpdateCoordinator,
    ) -> None:
        """Initialise the primary EV charger current-limit sensor."""
        super().__init__(config_entry, coordinator)
        self._attr_unique_id = get_ev_charger_current_limit_sensor_unique_id(
            config_entry.entry_id
        )
        self.entity_id = get_ev_charger_current_limit_sensor_entity_id()
        self._name = get_ev_charger_current_limit_sensor_name()


class HSEMEVSecondChargerCurrentLimitSensor(HSEMEVChargerCurrentLimitSensorBase):
    """Publish the second EV charger's current slot ceiling in whole amps."""

    _attr_translation_key = "ev_second_charger_current_limit"
    _is_second = True

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: HSEMDataUpdateCoordinator,
    ) -> None:
        """Initialise the second EV charger current-limit sensor."""
        super().__init__(config_entry, coordinator)
        self._attr_unique_id = get_ev_second_charger_current_limit_sensor_unique_id(
            config_entry.entry_id
        )
        self.entity_id = get_ev_second_charger_current_limit_sensor_entity_id()
        self._name = get_ev_second_charger_current_limit_sensor_name()
