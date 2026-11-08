"""Diagnostic sensor exposing the EV state of charge (SoC) HSEM plans with.

The state is :meth:`LiveState.ev_planning_soc_pct` for the EV: the effective
SoC (reported SoC plus the bounded delivered-energy credit, issue #789) while
it is known, otherwise the SoC reported by the configured source sensor. It
therefore always equals the value the planner and EV overrides act on.

The sensor is ``unavailable`` when no SoC is known, never ``0`` (issue #988).
Because the entity follows whatever SoC source is configured in the options
flow, dashboards and automations keep working when the source changes, and
the ``measurement`` state class gives the value long-term statistics
(issue #1388).

One sensor exists per configured EV: ``sensor.hsem_ev_soc`` and, when the
second EV is configured, ``sensor.hsem_ev_second_soc``.

This sensor subscribes to :class:`~custom_components.hsem.coordinator.HSEMDataUpdateCoordinator`
and updates automatically after every coordinator cycle.
"""

from __future__ import annotations

from typing import Any, override

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.components.sensor.const import SensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.coordinator import (
    CoordinatorData,
    HSEMDataUpdateCoordinator,
)
from custom_components.hsem.devices import HSEMDevice
from custom_components.hsem.entity import HSEMCoordinatorEntity, HSEMEntity
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.utils.conversion import convert_to_float
from custom_components.hsem.utils.misc import get_config_value, optional_entity_id
from custom_components.hsem.utils.sensornames.ev import (
    get_ev_second_soc_sensor_entity_id,
    get_ev_second_soc_sensor_unique_id,
    get_ev_soc_sensor_entity_id,
    get_ev_soc_sensor_unique_id,
)

SOC_KIND_EFFECTIVE = "effective"
SOC_KIND_REPORTED = "reported"


class HSEMEVSoCSensor(
    HSEMCoordinatorEntity,
    RestoreEntity,
    SensorEntity,
    HSEMEntity,
):
    """Diagnostic sensor exposing the SoC HSEM plans with for one EV.

    State is the planning SoC in percent, or ``None`` (unavailable) when
    neither the effective nor the reported SoC is known.
    """

    _attr_icon = "mdi:car-battery"
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 1

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: HSEMDataUpdateCoordinator,
        *,
        is_second: bool = False,
    ) -> None:
        """Initialise the EV SoC sensor.

        Args:
            config_entry: The HSEM config entry.
            coordinator: The shared :class:`HSEMDataUpdateCoordinator`.
            is_second: ``True`` for the second EV, ``False`` for the primary.
        """
        HSEMCoordinatorEntity.__init__(self, coordinator)
        HSEMEntity.__init__(self, config_entry)
        self._is_second = is_second
        self._hsem_device = (
            HSEMDevice.EV_SECONDARY if is_second else HSEMDevice.EV_PRIMARY
        )
        self._attr_translation_key = "ev_second_soc" if is_second else "ev_soc"

        self._config_entry = config_entry

        if is_second:
            self._attr_unique_id = get_ev_second_soc_sensor_unique_id(
                config_entry.entry_id
            )
            self.entity_id = get_ev_second_soc_sensor_entity_id()
        else:
            self._attr_unique_id = get_ev_soc_sensor_unique_id(config_entry.entry_id)
            self.entity_id = get_ev_soc_sensor_entity_id()

        self._restored_value: float | None = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _live(self) -> LiveState | None:
        """Return the live state of the last cycle, or ``None``."""
        data: CoordinatorData | None = self.coordinator.data
        return None if data is None else data.live

    def _reported_soc_pct(self, live: LiveState) -> float | None:
        """Return the SoC reported by the configured source sensor."""
        return convert_to_float(
            live.ev_second_planned_load_current_soc_pct
            if self._is_second
            else live.ev_planned_load_current_soc_pct
        )

    def _source_entity(self) -> str | None:
        """Return the configured SoC source entity id, or ``None``."""
        key = "hsem_ev_second_soc" if self._is_second else "hsem_ev_soc"
        return optional_entity_id(get_config_value(self._config_entry, key))

    def _source_last_updated(self, source_entity: str | None) -> str | None:
        """Return the ISO ``last_updated`` of the source entity, or ``None``."""
        hass = getattr(self, "hass", None)
        if hass is None or source_entity is None:
            return None
        state = hass.states.get(source_entity)
        return None if state is None else state.last_updated.isoformat()

    # ------------------------------------------------------------------
    # HA entity properties
    # ------------------------------------------------------------------

    @property
    @override
    def unique_id(self) -> str | None:
        """Return the unique ID."""
        return self._attr_unique_id

    @property
    @override
    def native_value(self) -> float | None:
        """Return the planning SoC, or the restored value before the first cycle."""
        live = self._live()
        if live is None:
            return self._restored_value
        return live.ev_planning_soc_pct(is_second=self._is_second)

    @property
    @override
    def should_poll(self) -> bool:
        """No polling — driven by the coordinator."""
        return False

    @property
    @override
    def available(self) -> bool:
        """True when a SoC is known for this EV."""
        if self._live() is None:
            return self._restored_value is not None
        return self.coordinator.last_update_success and self.native_value is not None

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the reported and effective SoC and the source sensor."""
        source_entity = self._source_entity()
        attrs: dict[str, Any] = {
            "reported_soc_pct": None,
            "effective_soc_pct": None,
            "delivered_energy_credit_kwh": 0.0,
            "source_entity": source_entity,
            "source_last_updated": self._source_last_updated(source_entity),
            "soc_kind": None,
        }
        live = self._live()
        if live is None:
            return attrs
        ev = live.ev_second if self._is_second else live.ev
        reported = self._reported_soc_pct(live)
        effective = convert_to_float(ev.effective_soc_pct)
        attrs["reported_soc_pct"] = reported
        attrs["effective_soc_pct"] = effective
        attrs["delivered_energy_credit_kwh"] = ev.delivered_energy_credit_kwh
        if effective is not None:
            attrs["soc_kind"] = SOC_KIND_EFFECTIVE
        elif reported is not None:
            attrs["soc_kind"] = SOC_KIND_REPORTED
        return attrs

    # ------------------------------------------------------------------
    # HA lifecycle
    # ------------------------------------------------------------------

    @override
    async def async_added_to_hass(self) -> None:
        """Restore the previous SoC and register the coordinator listener."""
        await super().async_added_to_hass()
        restored = await self.async_get_last_state()
        if restored is not None:
            self._restored_value = convert_to_float(restored.state)
