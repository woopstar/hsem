"""Per-hour-block house consumption power sensor for HSEM.

Tracks the instantaneous power drawn by the house during a specific
one-hour window (e.g. 13:00–14:00) and dynamically creates derived
child sensors:

- :class:`HSEMIntegrationSensor` — converts power (W) to energy (kWh).
- :class:`HSEMUtilityMeterSensor` — daily-resetting energy meter.
- :class:`HSEMAvgSensor` — rolling 1/3/7/14-day energy averages.

These derived sensors feed the planner's house-consumption estimates.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, override

import voluptuous as vol

import homeassistant.util.dt as dt_util
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.sensor.const import SensorDeviceClass, SensorStateClass
from homeassistant.components.utility_meter.const import (
    DATA_TARIFF_SENSORS,
    DATA_UTILITY,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower, UnitOfTime
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.custom_sensors.avg_sensor import HSEMAvgSensor
from custom_components.hsem.custom_sensors.integration_sensor import (
    HSEMIntegrationSensor,
)
from custom_components.hsem.custom_sensors.utility_meter_sensor import (
    HSEMUtilityMeterSensor,
)
from custom_components.hsem.entity import HSEMEntity
from custom_components.hsem.utils.conversion import convert_to_float
from custom_components.hsem.utils.ha_helpers import ha_get_entity_state_and_convert
from custom_components.hsem.utils.logger import HSEM_LOGGER as _LOGGER
from custom_components.hsem.utils.misc import get_config_value
from custom_components.hsem.utils.sensornames import (
    get_energy_average_sensor_entity_id,
    get_energy_average_sensor_name,
    get_energy_average_sensor_unique_id,
    get_house_consumption_power_sensor_entity_id,
    get_house_consumption_power_sensor_name,
    get_house_consumption_power_sensor_unique_id,
    get_integral_sensor_entity_id,
    get_integral_sensor_name,
    get_integral_sensor_unique_id,
    get_utility_meter_sensor_entity_id,
    get_utility_meter_sensor_name,
    get_utility_meter_sensor_unique_id,
)


class HSEMHouseConsumptionPowerSensor(RestoreEntity, SensorEntity, HSEMEntity):
    """Sensor that tracks power consumption for a specific one-hour block.

    Measures the house power draw (minus EV when configured) during the
    active hour window and dynamically creates derived energy sensors.
    """

    _attr_icon = "mdi:flash"
    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    # List all attributes to exclude from recording, except state and last_updated
    _unrecorded_attributes = frozenset(
        [
            "status",
            "description",
            "unique_id",
            "house_consumption_power_entity",
            "house_consumption_power_state",
            "ev_charger_power_entity",
            "ev_charger_power_state",
            "ev_second_charger_power_entity",
            "ev_second_charger_power_state",
            "house_power_includes_ev_charger_power",
            "hour_start",
            "hour_end",
        ]
    )

    def __init__(
        self,
        config_entry: ConfigEntry,
        hour_start: int,
        hour_end: int,
        async_add_entities: Any,
    ) -> None:
        """Initialize the house consumption power sensor.

        Args:
            config_entry: The HSEM config entry.
            hour_start: Start hour (0-23) of the measurement block.
            hour_end: End hour (0-23) of the measurement block.
            async_add_entities: HA callback to register derived child entities.
        """
        super().__init__(config_entry)
        self._available = False
        self._missing_input_entities = True
        self._hsem_house_consumption_power = None
        self._hsem_house_consumption_power_state = 0.0
        self._hsem_ev_charger_power = None
        self._hsem_ev_charger_power_state = 0.0
        self._hsem_ev_second_charger_power = None
        self._hsem_ev_second_charger_power_state = 0.0
        self._hsem_house_power_includes_ev_charger_power = None
        self._hour_start = hour_start
        self._hour_end = hour_end
        self._attr_unique_id = get_house_consumption_power_sensor_unique_id(
            config_entry.entry_id, hour_start, hour_end
        )
        self.entity_id = get_house_consumption_power_sensor_entity_id(
            hour_start, hour_end
        )
        self._state: float | None = None
        self._state_previous: float | None = None
        self._config_entry = config_entry
        self._last_updated: str | None = None
        # Track which derived sensors have already been created to avoid duplicate adds.
        self._derived_sensors_created: set[str] = set()
        self._tracked_entities: set[str] = set()
        # Unsubscribe callbacks registered by async_track_* helpers.
        self._unsub_callbacks: list = []
        self._async_add_entities = async_add_entities
        self._name = get_house_consumption_power_sensor_name(
            self._hour_start, self._hour_end
        )
        self._update_settings()

    @property
    @override
    def name(self) -> str:
        return self._name

    @property
    @override
    def unique_id(self) -> str | None:
        return self._attr_unique_id

    @property  # type: ignore[misc]  # HA stub declares state as @final
    @override
    def state(self) -> float | None:
        return self._state

    @property
    @override
    def should_poll(self) -> bool:
        return True

    @property
    @override
    def available(self) -> bool:
        return self._available

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes for the sensor.

        Includes source entity configurations and live power readings.
        When input entities are missing, returns an error status instead.
        """
        if self._missing_input_entities:
            return {
                "status": "error",
                "description": "Some of the required input sensors from the config flow is missing or not reporting a state. Check your configuration and make sure input sensors are configured correctly.",
                "last_updated": self._last_updated,
                "unique_id": self._attr_unique_id,
            }

        return {
            "house_consumption_power_entity": self._hsem_house_consumption_power,
            "house_consumption_power_state": round(
                self._hsem_house_consumption_power_state, 2
            ),
            "ev_charger_power_entity": self._hsem_ev_charger_power,
            "ev_charger_power_state": round(self._hsem_ev_charger_power_state, 2),
            "ev_second_charger_power_entity": self._hsem_ev_second_charger_power,
            "ev_second_charger_power_state": round(
                self._hsem_ev_second_charger_power_state, 2
            ),
            "house_power_includes_ev_charger_power": self._hsem_house_power_includes_ev_charger_power,
            "hour_start": self._hour_start,
            "hour_end": self._hour_end,
            "last_updated": self._last_updated,
            "unique_id": self._attr_unique_id,
        }

    async def async_update(self, event: Any | None = None) -> None:
        """Manually trigger the sensor update."""
        await self._async_handle_update(event)

    async def async_options_updated(self, config_entry: ConfigEntry) -> None:
        """Handle options update from configuration change."""

        self._update_settings()

        await self._async_handle_update(None)

    @override
    async def async_added_to_hass(self) -> None:
        """Handle when sensor is added to Home Assistant.

        Restores the previous state so HA does not show ``unknown`` immediately
        after a restart.  The sensor is marked **unavailable** after restore so
        that the downstream ``IntegrationSensor`` pauses accumulation until the
        first live measurement is taken inside the active hour window.  Once the
        active window is entered, ``_async_handle_update`` sets a real value and
        flips ``_available`` to ``True``.

        The ``else`` branch in ``_async_handle_update`` will reset ``_state``
        to ``None`` (and ``_available`` to ``False``) as soon as a tick fires
        outside the active window — so the restored value is only visible
        briefly on restart and never feeds stale data into the energy integral.
        """
        old_state = await self.async_get_last_state()
        if old_state is not None:
            restored = convert_to_float(old_state.state)
            if restored is not None:
                self._state = round(restored, 2)
            self._last_updated = old_state.attributes.get("last_updated", None)

        # Mark unavailable so the IntegrationSensor does not accumulate the
        # restored value as live power before the first real measurement.
        self._available = False

        # Initial update — will set a live value (and available=True) when
        # the current hour matches the active window, or reset state to None
        # (and available=False) when outside it.
        await self._async_handle_update(None)

        await super().async_added_to_hass()

    @override
    async def async_will_remove_from_hass(self) -> None:
        """Cancel all tracked listeners when the entity is removed."""
        for unsub in self._unsub_callbacks:
            unsub()
        self._unsub_callbacks.clear()
        await super().async_will_remove_from_hass()

    def _update_settings(self) -> None:
        """Fetch updated settings from config_entry options."""
        self._hsem_house_consumption_power = get_config_value(
            self._config_entry, "hsem_house_consumption_power"
        )

        self._hsem_ev_charger_power = get_config_value(
            self._config_entry, "hsem_ev_charger_power"
        )

        if self._hsem_ev_charger_power == vol.UNDEFINED:
            self._hsem_ev_charger_power = None

        self._hsem_ev_second_charger_power = get_config_value(
            self._config_entry, "hsem_ev_second_charger_power"
        )

        if self._hsem_ev_second_charger_power == vol.UNDEFINED:
            self._hsem_ev_second_charger_power = None

        self._hsem_house_power_includes_ev_charger_power = get_config_value(
            self._config_entry, "hsem_house_power_includes_ev_charger_power"
        )

    async def _async_handle_update(self, event: Any | None = None) -> None:
        """Handle updates to the source sensor.

        ``_state`` is reset to ``None`` at the start of every cycle so that a
        failed sensor fetch inside the active window never leaves stale power
        in place for the ``IntegrationSensor`` to accumulate.

        Power is only measured inside the active hour window
        (``now.hour == self._hour_start``).  Outside that window the state
        remains ``None``, which HA exposes as ``unknown``, causing the
        ``IntegrationSensor`` to pause accumulation and the
        ``UtilityMeterSensor`` to stop counting — preventing cross-hour
        contamination of the energy reading for this slot.
        """
        now = dt_util.now()

        # Ensure config flow settings are reloaded if it changed.
        self._update_settings()

        # Track state changes for the source sensors. Also if they change.
        await self._async_track_entities()

        # Always reset state to None before attempting to measure.  This ensures
        # that a failed fetch inside the active window clears any stale value
        # rather than leaving the previous reading in place for the IntegrationSensor
        # to continue accumulating.
        self._state = None

        if now.hour == self._hour_start:
            # Active window: measure the current power.
            await self._async_fetch_sensor_states()

            if (
                not self._missing_input_entities
                and isinstance(self._hsem_ev_charger_power_state, (int, float))
                and isinstance(self._hsem_house_consumption_power_state, (int, float))
            ):
                if self._hsem_house_power_includes_ev_charger_power:
                    # Subtract both EV chargers from house consumption
                    ev_total_power = (
                        self._hsem_ev_charger_power_state
                        + self._hsem_ev_second_charger_power_state
                    )
                    self._state = round(
                        float(
                            self._hsem_house_consumption_power_state - ev_total_power
                        ),
                        2,
                    )
                else:
                    self._state = round(
                        float(self._hsem_house_consumption_power_state), 2
                    )
            # else: fetch failed — _state remains None, integral pauses

        # Ensure derived sensors exist — create them only once; skip if already present.
        await self._async_add_integral_sensor()
        await self._async_add_utility_meter_sensor()
        await self._async_add_energy_average_sensors(1)
        await self._async_add_energy_average_sensors(3)
        await self._async_add_energy_average_sensors(7)
        await self._async_add_energy_average_sensors(14)

        # Available only inside the active hour window (when state is non-None).
        self._available = self._state is not None

        if self._state_previous is None or self._state_previous != self._state:
            self._last_updated = now.isoformat()
            self._state_previous = self._state

            # Trigger an update in Home Assistant
            self.async_write_ha_state()

    async def _async_track_entities(self) -> None:
        """Register state-change listeners for source power entities."""
        # Track state changes for the source sensor
        if self._hsem_house_consumption_power:
            if self._hsem_house_consumption_power not in self._tracked_entities:
                _LOGGER.debug(
                    f"Starting to track state changes for {self._hsem_house_consumption_power}"
                )
                unsub = async_track_state_change_event(
                    self.hass,
                    [self._hsem_house_consumption_power],
                    self._async_handle_update,
                )
                self._unsub_callbacks.append(unsub)
                self._tracked_entities.add(self._hsem_house_consumption_power)

        if self._hsem_ev_charger_power:
            if self._hsem_ev_charger_power not in self._tracked_entities:
                _LOGGER.debug(
                    f"Starting to track state changes for {self._hsem_ev_charger_power}"
                )
                unsub = async_track_state_change_event(
                    self.hass, [self._hsem_ev_charger_power], self._async_handle_update
                )
                self._unsub_callbacks.append(unsub)
                self._tracked_entities.add(self._hsem_ev_charger_power)

        if self._hsem_ev_second_charger_power:
            if self._hsem_ev_second_charger_power not in self._tracked_entities:
                _LOGGER.debug(
                    f"Starting to track state changes for {self._hsem_ev_second_charger_power}"
                )
                unsub = async_track_state_change_event(
                    self.hass,
                    [self._hsem_ev_second_charger_power],
                    self._async_handle_update,
                )
                self._unsub_callbacks.append(unsub)
                self._tracked_entities.add(self._hsem_ev_second_charger_power)

    async def _async_fetch_sensor_states(self) -> None:
        """Read live power values from the configured HA source entities."""
        # Update the state of the sensor for house consumption power

        self._missing_input_entities = False

        try:
            if self._hsem_house_consumption_power:
                raw = ha_get_entity_state_and_convert(
                    self, self._hsem_house_consumption_power, "float"
                )
                self._hsem_house_consumption_power_state = convert_to_float(raw) or 0.0

            # Update the state of the sensor for EV charger power
            if self._hsem_ev_charger_power:
                raw_ev = ha_get_entity_state_and_convert(
                    self, self._hsem_ev_charger_power, "float"
                )
                self._hsem_ev_charger_power_state = convert_to_float(raw_ev) or 0.0

            # Update the state of the sensor for second EV charger power
            if self._hsem_ev_second_charger_power:
                raw_ev2 = ha_get_entity_state_and_convert(
                    self, self._hsem_ev_second_charger_power, "float"
                )
                self._hsem_ev_second_charger_power_state = (
                    convert_to_float(raw_ev2) or 0.0
                )

        except (HomeAssistantError, ValueError, TypeError) as exc:
            _LOGGER.warning(
                "Sensor read failed for entity_id=%s (operation=_async_fetch_sensor_states): "
                "%s: %s",
                self._hsem_house_consumption_power
                or self._hsem_ev_charger_power
                or self._hsem_ev_second_charger_power,
                type(exc).__name__,
                repr(exc),
            )
            self._missing_input_entities = True

    async def _async_add_integral_sensor(self) -> None:
        """Add an integral sensor dynamically to convert power to energy.

        Uses only the per-runtime ``_derived_sensors_created`` set to prevent
        duplicate creation within the same HA session.  The entity registry is
        NOT consulted here: the registry entry survives restarts but the entity
        instance does not, so after a restart we must always create a fresh
        instance with the same stable ``unique_id``.  HA will bind the new
        instance to the existing registry entry automatically, allowing
        ``IntegrationSensor`` to restore its accumulated state.
        """
        integral_sensor_unique_id = get_integral_sensor_unique_id(
            self._config_entry.entry_id, self._hour_start, self._hour_end
        )

        # Per-runtime guard — skip if already created in this HA session.
        if integral_sensor_unique_id in self._derived_sensors_created:
            return

        # The source entity_id is deterministic — derive it directly rather than
        # resolving via the registry so this also works on first boot.
        source_entity = get_house_consumption_power_sensor_entity_id(
            self._hour_start, self._hour_end
        )

        integral_sensor_name = get_integral_sensor_name(
            self._hour_start, self._hour_end
        )
        integral_sensor_entity_id = get_integral_sensor_entity_id(
            self._hour_start, self._hour_end
        )

        _LOGGER.debug(
            "Adding integral sensor %s for %s", integral_sensor_name, source_entity
        )

        integral_sensor = HSEMIntegrationSensor(
            integration_method="left",
            name=integral_sensor_name,
            round_digits=2,
            source_entity=source_entity,
            unique_id=integral_sensor_unique_id,
            unit_prefix="k",
            unit_time=UnitOfTime.HOURS,
            max_sub_interval=timedelta(minutes=0),
            hass=self.hass,
            e_id=integral_sensor_entity_id,
            id=integral_sensor_unique_id,
            config_entry=self._config_entry,
        )

        self._async_add_entities([integral_sensor])
        self._derived_sensors_created.add(integral_sensor_unique_id)

    async def _async_add_utility_meter_sensor(self) -> None:
        """Add a utility meter sensor dynamically.

        Uses only the per-runtime ``_derived_sensors_created`` set to prevent
        duplicate creation within the same HA session.  The utility meter must
        track the *energy* integral sensor (kWh), not the raw power sensor (W).
        The source entity_id is derived deterministically — no registry lookup
        needed.
        """
        utility_meter_unique_id = get_utility_meter_sensor_unique_id(
            self._config_entry.entry_id, self._hour_start, self._hour_end
        )

        # Per-runtime guard — skip if already created in this HA session.
        if utility_meter_unique_id in self._derived_sensors_created:
            return

        # Source is always the integral sensor — derive its entity_id directly.
        source_entity = get_integral_sensor_entity_id(self._hour_start, self._hour_end)

        # Ensure DATA_UTILITY structure exists in hass.data
        if DATA_UTILITY not in self.hass.data:
            self.hass.data[DATA_UTILITY] = {}
        if source_entity not in self.hass.data[DATA_UTILITY]:
            self.hass.data[DATA_UTILITY][source_entity] = {DATA_TARIFF_SENSORS: []}

        utility_meter_name = get_utility_meter_sensor_name(
            self._hour_start, self._hour_end
        )
        utility_meter_entity_id = get_utility_meter_sensor_entity_id(
            self._hour_start, self._hour_end
        )

        _LOGGER.debug(
            "Adding utility meter sensor %s for %s", utility_meter_name, source_entity
        )

        utility_meter_sensor = HSEMUtilityMeterSensor(
            cron_pattern=None,
            delta_values=False,
            meter_offset=timedelta(hours=int(self._hour_start)),
            meter_type="daily",
            name=utility_meter_name,
            net_consumption=True,
            parent_meter=source_entity,
            periodically_resetting=True,
            source_entity=source_entity,
            tariff_entity=None,
            tariff=None,
            unique_id=utility_meter_unique_id,
            hass=self.hass,
            sensor_always_available=True,
            id=utility_meter_unique_id,
            e_id=utility_meter_entity_id,
            config_entry=self._config_entry,
        )

        utility_meter_sensor.entity_id = utility_meter_entity_id

        self._async_add_entities([utility_meter_sensor])
        self._derived_sensors_created.add(utility_meter_unique_id)

        # Register with the HA utility-meter book-keeping so the daily-reset
        # event reaches this sensor.
        self.hass.data[DATA_UTILITY][source_entity][DATA_TARIFF_SENSORS].append(
            utility_meter_sensor
        )

    async def _async_add_energy_average_sensors(self, avg: int) -> None:
        """Add a template sensor for the energy average over a given number of days.

        Uses only the per-runtime ``_derived_sensors_created`` set to prevent
        duplicate creation within the same HA session.  The tracked utility-meter
        entity_id is derived deterministically — no registry lookup needed.
        """
        avg_energy_sensor_unique_id = get_energy_average_sensor_unique_id(
            self._config_entry.entry_id, self._hour_start, self._hour_end, avg
        )

        # Per-runtime guard — skip if already created in this HA session.
        if avg_energy_sensor_unique_id in self._derived_sensors_created:
            return

        # Tracked entity is always the utility meter — derive its entity_id directly.
        utility_meter_entity_id = get_utility_meter_sensor_entity_id(
            self._hour_start, self._hour_end
        )

        avg_energy_sensor_name = get_energy_average_sensor_name(
            self._hour_start, self._hour_end, avg
        )
        avg_energy_sensor_entity_id = get_energy_average_sensor_entity_id(
            self._hour_start, self._hour_end, avg
        )

        _LOGGER.debug("Creating new average energy sensor '%s'", avg_energy_sensor_name)

        avg_sensor = HSEMAvgSensor(
            config_entry=self._config_entry,
            hour_start=self._hour_start,
            hour_end=self._hour_end,
            avg=avg,
            tracked_entity=utility_meter_entity_id,
            name=avg_energy_sensor_name,
            unique_id=avg_energy_sensor_unique_id,
            entity_id=avg_energy_sensor_entity_id,
        )

        self._async_add_entities([avg_sensor])
        self._derived_sensors_created.add(avg_energy_sensor_unique_id)
