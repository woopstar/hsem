"""House Load Power sensor derived from the Huawei inverters and meter (issue #1310).

State is ``Σ inverter active power − power meter active power`` in W, the house's
own consumption on an installation without an EMMA or SDongle load sensor (see
:mod:`custom_components.hsem.utils.house_load`).  The sensor is unavailable,
never 0, while any input is unavailable or non-numeric, so the per-hour
consumption profile does not learn a zero load.

It is created only when an inverter and the power meter are configured in the
power step, and it updates on every state change of its inputs.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, override

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower
from homeassistant.core import Event, EventStateChangedData, callback
from homeassistant.helpers.event import async_track_state_change_event

from custom_components.hsem.devices import HSEMDevice
from custom_components.hsem.entity import HSEMEntity
from custom_components.hsem.utils.conversion import convert_to_float
from custom_components.hsem.utils.ha_helpers import normalize_entity_float
from custom_components.hsem.utils.house_load import derived_house_load_w
from custom_components.hsem.utils.sensornames.energy import (
    get_house_load_power_sensor_entity_id,
    get_house_load_power_sensor_unique_id,
)


class HSEMHouseLoadPowerSensor(SensorEntity, HSEMEntity):
    """House load power in W derived from the inverters and the power meter."""

    _attr_icon = "mdi:home-lightning-bolt"
    _attr_has_entity_name = True
    _attr_translation_key = "house_load_power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_should_poll = False
    # The inputs never change while the entity exists (changing them reloads
    # the entry), so recording them on every state would only add rows.
    _unrecorded_attributes = frozenset(
        ["inverter_active_power_entities", "power_meter_active_power_entity"]
    )

    def __init__(
        self,
        config_entry: ConfigEntry,
        inverters: list[str],
        power_meter: str,
    ) -> None:
        """Initialise the sensor.

        Args:
            config_entry: The HSEM config entry.
            inverters: Entity IDs of the inverters' active power, at least one.
            power_meter: Entity ID of the power meter's active power.
        """
        super().__init__(config_entry)
        self._hsem_device = HSEMDevice.BATTERY_ENERGY
        self._inverters = list(inverters)
        self._power_meter = power_meter
        self._attr_unique_id = get_house_load_power_sensor_unique_id(
            config_entry.entry_id
        )
        self.entity_id = get_house_load_power_sensor_entity_id()
        self._value_w: float | None = None
        self._unsub: Callable[[], None] | None = None

    @property
    @override
    def native_value(self) -> float | None:
        """Return the house load in W, rounded to 0.1 W."""
        return None if self._value_w is None else round(self._value_w, 1)

    @property
    @override
    def available(self) -> bool:
        """Available only while every input reports a number."""
        return self._value_w is not None

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the input entities the value is derived from."""
        return {
            "inverter_active_power_entities": list(self._inverters),
            "power_meter_active_power_entity": self._power_meter,
        }

    def _read_w(self, entity_id: str) -> float | None:
        """Read one input in W, or ``None`` when it has no numeric state."""
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        return normalize_entity_float(
            self,
            entity_id,
            convert_to_float(state.state),
            UnitOfPower.WATT,
            label="house_load_power",
        )

    def _recompute(self) -> None:
        """Recompute the derived value from the inputs' current states."""
        self._value_w = derived_house_load_w(
            [self._read_w(entity_id) for entity_id in self._inverters],
            self._read_w(self._power_meter),
        )

    @callback
    def _async_input_changed(self, _event: Event[EventStateChangedData]) -> None:
        """Recompute and publish when any input changes."""
        self._recompute()
        self.async_write_ha_state()

    @override
    async def async_added_to_hass(self) -> None:
        """Start tracking the inputs and publish the first value."""
        await super().async_added_to_hass()
        self._unsub = async_track_state_change_event(
            self.hass,
            [*self._inverters, self._power_meter],
            self._async_input_changed,
        )
        self._recompute()

    @override
    async def async_will_remove_from_hass(self) -> None:
        """Stop tracking the inputs."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        await super().async_will_remove_from_hass()
