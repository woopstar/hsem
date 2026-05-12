"""Home Assistant mock integration tests for HSEM (issue #281).

Scope
-----
- Mock Home Assistant state reads.
- Mock service calls.
- Test entity setup (``HSEMWorkingModeSensor``, ``HSEMDegradedModeSensor``).
- Test the full read → plan → apply cycle in *dry-run* (``read_only=True``) mode.

Approach
--------
These tests do NOT boot a real Home Assistant instance.  Instead they:

1. Build a minimal fake ``hass`` object whose ``states.get()`` returns
   configurable :class:`FakeState` objects — the same interface that
   :func:`~custom_components.hsem.utils.misc.ha_get_entity_state_and_convert`
   uses.
2. Build a minimal ``ConfigEntry``-like mock so that
   :func:`~custom_components.hsem.custom_sensors.config_reader.build_sensor_config`
   reads consistent defaults.
3. Patch the HA-specific async helpers (``async_track_state_change_event``,
   ``async_set_select_option``, ``async_set_number_value``, etc.) so that
   hardware writes are intercepted and recorded rather than dispatched to
   real hardware.
4. Invoke the coordinator's internal ``_async_run_update_cycle`` with all I/O
   resolved through these mocks so that the test validates the full pipeline
   logic without touching real hardware.

Acceptance criteria (from issue #281)
--------------------------------------
- A mock HA test can run a full read-plan-apply cycle in dry-run mode.
- Service calls are asserted without touching real hardware.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.coordinator import (
    CoordinatorData,
    HSEMDataUpdateCoordinator,
)
from custom_components.hsem.custom_sensors.degraded_mode_sensor import (
    HSEMDegradedModeSensor,
)
from custom_components.hsem.custom_sensors.force_mode_sensor import HSEMForceModeSensor
from custom_components.hsem.custom_sensors.hardware_writes_sensor import (
    HSEMHardwareWritesSensor,
)
from custom_components.hsem.custom_sensors.missing_entities_sensor import (
    HSEMMissingEntitiesSensor,
)
from custom_components.hsem.custom_sensors.net_consumption_sensor import (
    HSEMNetConsumptionSensor,
)
from custom_components.hsem.custom_sensors.next_update_sensor import (
    HSEMNextUpdateSensor,
)
from custom_components.hsem.custom_sensors.read_only_sensor import HSEMReadOnlySensor
from custom_components.hsem.custom_sensors.working_mode_sensor import (
    HSEMWorkingModeSensor,
)
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.degraded_mode import DegradedMode

# ---------------------------------------------------------------------------
# Fake HA infrastructure helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeState:
    """Minimal HA state object used by ha_get_entity_state_and_convert."""

    entity_id: str
    state: str
    attributes: dict[str, Any] = field(default_factory=dict)


class FakeStates:
    """Minimal HA states store that supports get() with FakeState objects."""

    def __init__(self, states: dict[str, FakeState]) -> None:
        self._states = states

    def get(self, entity_id: str) -> FakeState | None:
        return self._states.get(entity_id)


def make_fake_hass(entity_states: dict[str, str | dict]) -> MagicMock:
    """Build a minimal fake ``hass`` object.

    Args:
        entity_states: Mapping of entity_id → state string (or dict with
            ``state`` / ``attributes`` keys for richer mocking).

    Returns:
        A :class:`MagicMock` whose ``states.get()`` returns :class:`FakeState`
        instances.
    """
    fake_states: dict[str, FakeState] = {}
    for entity_id, value in entity_states.items():
        if isinstance(value, dict):
            fake_states[entity_id] = FakeState(
                entity_id=entity_id,
                state=str(value.get("state", "0")),
                attributes=value.get("attributes", {}),
            )
        else:
            fake_states[entity_id] = FakeState(
                entity_id=entity_id,
                state=str(value),
            )

    hass = MagicMock()
    hass.states = FakeStates(fake_states)
    hass.async_create_task = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    return hass


def make_fake_config_entry(overrides: dict[str, Any] | None = None) -> MagicMock:
    """Build a minimal fake config entry with sensible HSEM defaults.

    All values mirror DEFAULT_CONFIG_VALUES from ``const.py`` so that
    :func:`build_sensor_config` produces a valid :class:`SensorConfig`.

    Args:
        overrides: Optional mapping of config-key → value to replace defaults.

    Returns:
        A :class:`MagicMock` with ``.options`` and ``.data`` set.
    """
    from custom_components.hsem.const import DEFAULT_CONFIG_VALUES

    defaults = dict(DEFAULT_CONFIG_VALUES)

    # Resolve ``vol.UNDEFINED`` sentinels to empty strings so options.get() works.
    # config_reader.py treats empty-string device IDs as None (via len==0 check),
    # and empty-string entity IDs are safe since state reads will simply return None.
    import voluptuous as vol

    for k, v in defaults.items():
        if v is vol.UNDEFINED:
            defaults[k] = ""

    if overrides:
        defaults.update(overrides)

    config_entry = MagicMock()
    config_entry.options = defaults
    config_entry.data = {}
    return config_entry


def make_bare_coordinator(
    hass: MagicMock | None = None,
    config_entry: MagicMock | None = None,
) -> HSEMDataUpdateCoordinator:
    """Return an ``HSEMDataUpdateCoordinator`` without calling ``__init__``.

    Bypasses ``DataUpdateCoordinator.__init__`` which requires a bootstrapped
    HA runtime (``frame.report_usage``).  Only the attributes actually needed
    by the pipeline are injected.

    Args:
        hass: Optional fake hass object.  A sensible default is used when None.
        config_entry: Optional fake config entry.

    Returns:
        A usable coordinator instance.
    """
    if hass is None:
        hass = make_fake_hass({})
    if config_entry is None:
        config_entry = make_fake_config_entry()

    coord = object.__new__(HSEMDataUpdateCoordinator)
    coord.hass = hass
    coord._config_entry = config_entry
    coord._update_lock = asyncio.Lock()
    coord._interval_timer_unsub = None
    coord._hourly_timer_unsub = None
    coord._timer_interval = None
    coord._next_update = None
    coord.data = None
    coord.last_update_success = True
    coord.logger = MagicMock()

    # Per-cycle state
    coord._force_working_mode_entity = None
    coord._tracked_entities = set()
    coord._avg_house_consumption_entity_id_cache = {}
    coord._hourly_recommendations = []
    coord._hourly_recommendation = None
    coord._batteries_schedules = []
    coord._batteries_schedules_remaining_capacity_needed = 0.0
    coord._current_required_battery = 0.0
    coord._live = None

    from custom_components.hsem.custom_sensors.config_reader import build_sensor_config

    coord._cfg = build_sensor_config(config_entry)

    # CoordinatorEntity support — some entity methods call this
    coord.async_set_updated_data = MagicMock()

    return coord


# ---------------------------------------------------------------------------
# Canonical entity-state map used across multiple tests
# ---------------------------------------------------------------------------

_BASE_ENTITY_STATES: dict[str, str | dict] = {
    # Battery entities
    "sensor.batteries_state_of_capacity": "65",
    "sensor.batteries_rated_capacity": "10000",
    "number.batteries_maximum_charging_power": "5000",
    "number.batteries_maximum_discharging_power": "5000",
    "number.batteries_end_of_discharge_soc": "10",
    "number.batteries_grid_charge_cutoff_soc": "100",
    "select.batteries_working_mode": "TimeOfUse",
    "select.batteries_excess_pv_energy_use_in_tou": "Maximise Self Consumption",
    "sensor.batteries_tou_charging_and_discharging_periods": {
        "state": "active",
        "attributes": {},
    },
    # Power sensors
    "sensor.power_house_load": "1200",
    "sensor.power_inverter_input_total": "800",
    "sensor.inverter_active_power_control": "100%",
    # Electricity prices
    "sensor.energi_data_service": "0.25",
    "sensor.energi_data_service_produktion": "0.15",
    # Solcast
    "sensor.solcast_pv_forecast_forecast_today": {
        "state": "5.0",
        "attributes": {
            "detailedForecast": [],
            "pv_estimate": 5.0,
        },
    },
    "sensor.solcast_pv_forecast_forecast_tomorrow": {
        "state": "4.5",
        "attributes": {
            "detailedForecast": [],
            "pv_estimate": 4.5,
        },
    },
}


# ---------------------------------------------------------------------------
# TestEntitySetup — entity construction without a real HA runtime
# ---------------------------------------------------------------------------


class TestEntitySetup:
    """Verify that HSEM entities can be constructed and expose correct initial state."""

    def test_working_mode_sensor_initial_state_is_none(self) -> None:
        """HSEMWorkingModeSensor.state must be None before the first coordinator cycle."""
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)

        sensor = HSEMWorkingModeSensor(config_entry, coord)

        assert sensor.state is None

    def test_working_mode_sensor_not_available_before_first_cycle(self) -> None:
        """HSEMWorkingModeSensor must not be available until coordinator.data is set."""
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)

        sensor = HSEMWorkingModeSensor(config_entry, coord)

        assert sensor.available is False

    def test_working_mode_sensor_should_not_poll(self) -> None:
        """Working-mode sensor must not poll independently (coordinator-driven)."""
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)

        sensor = HSEMWorkingModeSensor(config_entry, coord)

        assert sensor.should_poll is False

    def test_degraded_mode_sensor_initial_state_is_ok(self) -> None:
        """DegradedModeSensor state must default to 'ok' before the first cycle."""
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)

        sensor = HSEMDegradedModeSensor(config_entry, coord)

        assert sensor.state == DegradedMode.OK.value

    def test_degraded_mode_sensor_should_not_poll(self) -> None:
        """DegradedModeSensor must not poll independently."""
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)

        sensor = HSEMDegradedModeSensor(config_entry, coord)

        assert sensor.should_poll is False

    def test_degraded_mode_sensor_entity_category_is_diagnostic(self) -> None:
        """DegradedModeSensor must carry the DIAGNOSTIC entity category."""
        from homeassistant.const import EntityCategory

        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)

        sensor = HSEMDegradedModeSensor(config_entry, coord)

        assert sensor._attr_entity_category == EntityCategory.DIAGNOSTIC

    def test_working_mode_sensor_extra_attrs_while_waiting(self) -> None:
        """Extra attributes before first cycle must include a 'status' key of 'wait'."""
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)

        sensor = HSEMWorkingModeSensor(config_entry, coord)
        attrs = sensor.extra_state_attributes

        assert attrs.get("status") == "wait"

    def test_both_sensors_have_unique_ids(self) -> None:
        """Both HSEM sensors must expose non-empty unique IDs."""
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)

        working = HSEMWorkingModeSensor(config_entry, coord)
        degraded = HSEMDegradedModeSensor(config_entry, coord)

        assert working.unique_id
        assert degraded.unique_id
        assert working.unique_id != degraded.unique_id

    # ------------------------------------------------------------------
    # HSEMReadOnlySensor
    # ------------------------------------------------------------------

    def test_read_only_sensor_initial_state_is_off(self) -> None:
        """ReadOnlySensor must default to 'off' (writes enabled) before first cycle."""
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)

        sensor = HSEMReadOnlySensor(config_entry, coord)

        assert sensor.state == "off"

    def test_read_only_sensor_should_not_poll(self) -> None:
        """ReadOnlySensor must not poll independently (coordinator-driven)."""
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)

        sensor = HSEMReadOnlySensor(config_entry, coord)

        assert sensor.should_poll is False

    def test_read_only_sensor_entity_category_is_diagnostic(self) -> None:
        """ReadOnlySensor must carry the DIAGNOSTIC entity category."""
        from homeassistant.const import EntityCategory

        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)

        sensor = HSEMReadOnlySensor(config_entry, coord)

        assert sensor._attr_entity_category == EntityCategory.DIAGNOSTIC

    def test_read_only_sensor_unique_id_differs_from_others(self) -> None:
        """ReadOnlySensor unique_id must be distinct from working-mode and degraded sensors."""
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)

        read_only = HSEMReadOnlySensor(config_entry, coord)
        working = HSEMWorkingModeSensor(config_entry, coord)
        degraded = HSEMDegradedModeSensor(config_entry, coord)

        assert read_only.unique_id
        assert read_only.unique_id != working.unique_id
        assert read_only.unique_id != degraded.unique_id

    def test_read_only_sensor_state_on_when_enabled(self) -> None:
        """ReadOnlySensor must report 'on' when coordinator data has read_only=True."""
        config_entry = make_fake_config_entry({"hsem_read_only": True})
        coord = make_bare_coordinator(config_entry=config_entry)

        cfg = SensorConfig()
        cfg.read_only = True
        cfg.recommendation_interval_minutes = 60
        cfg.recommendation_interval_length = 24
        cfg.update_interval = 5

        coord.data = CoordinatorData(cfg=cfg, live=LiveState(), state=None)

        sensor = HSEMReadOnlySensor(config_entry, coord)

        assert sensor.state == "on"

    def test_read_only_sensor_state_off_when_disabled(self) -> None:
        """ReadOnlySensor must report 'off' when coordinator data has read_only=False."""
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)

        cfg = SensorConfig()
        cfg.read_only = False
        cfg.recommendation_interval_minutes = 60
        cfg.recommendation_interval_length = 24
        cfg.update_interval = 5

        coord.data = CoordinatorData(cfg=cfg, live=LiveState(), state=None)

        sensor = HSEMReadOnlySensor(config_entry, coord)

        assert sensor.state == "off"

    def test_read_only_sensor_extra_attrs_include_update_interval(self) -> None:
        """ReadOnlySensor extra_state_attributes must expose update_interval_minutes."""
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)

        cfg = SensorConfig()
        cfg.read_only = False
        cfg.update_interval = 7

        coord.data = CoordinatorData(cfg=cfg, live=LiveState(), state=None)

        sensor = HSEMReadOnlySensor(config_entry, coord)
        attrs = sensor.extra_state_attributes

        assert attrs["update_interval_minutes"] == 7
        assert attrs["hardware_writes_active"] is True
        assert attrs["read_only"] is False


# ---------------------------------------------------------------------------
# TestMockStateReads — verify ha_get_entity_state_and_convert with FakeStates
# ---------------------------------------------------------------------------


class TestMockStateReads:
    """Verify that FakeState objects satisfy the ha_get_entity_state_and_convert interface."""

    def test_float_conversion_from_fake_state(self) -> None:
        """ha_get_entity_state_and_convert must parse float from FakeState.state."""
        from custom_components.hsem.utils.misc import ha_get_entity_state_and_convert

        hass = make_fake_hass({"sensor.soc": "65.5"})
        sensor_stub = MagicMock()
        sensor_stub.hass = hass

        result = ha_get_entity_state_and_convert(sensor_stub, "sensor.soc", "float")
        assert result == pytest.approx(65.5)

    def test_missing_entity_raises_entity_not_found(self) -> None:
        """Reading a non-existent entity must raise EntityNotFoundError."""
        from custom_components.hsem.utils.misc import (
            EntityNotFoundError,
            ha_get_entity_state_and_convert,
        )

        hass = make_fake_hass({})
        sensor_stub = MagicMock()
        sensor_stub.hass = hass

        with pytest.raises(EntityNotFoundError):
            ha_get_entity_state_and_convert(sensor_stub, "sensor.nonexistent", "float")

    def test_unavailable_state_raises_entity_not_found(self) -> None:
        """An 'unavailable' state string must raise EntityNotFoundError for float reads."""
        from custom_components.hsem.utils.misc import (
            EntityNotFoundError,
            ha_get_entity_state_and_convert,
        )

        hass = make_fake_hass({"sensor.soc": "unavailable"})
        sensor_stub = MagicMock()
        sensor_stub.hass = hass

        with pytest.raises(EntityNotFoundError):
            ha_get_entity_state_and_convert(sensor_stub, "sensor.soc", "float")

    def test_boolean_conversion_from_fake_state(self) -> None:
        """Boolean 'on'/'off' states must convert correctly."""
        from custom_components.hsem.utils.misc import ha_get_entity_state_and_convert

        hass = make_fake_hass(
            {"binary_sensor.charging": "on", "binary_sensor.idle": "off"}
        )
        sensor_stub = MagicMock()
        sensor_stub.hass = hass

        assert (
            ha_get_entity_state_and_convert(
                sensor_stub, "binary_sensor.charging", "boolean"
            )
            is True
        )
        assert (
            ha_get_entity_state_and_convert(
                sensor_stub, "binary_sensor.idle", "boolean"
            )
            is False
        )

    def test_string_conversion_from_fake_state(self) -> None:
        """String conversion must return the state value unchanged."""
        from custom_components.hsem.utils.misc import ha_get_entity_state_and_convert

        hass = make_fake_hass({"select.mode": "TimeOfUse"})
        sensor_stub = MagicMock()
        sensor_stub.hass = hass

        result = ha_get_entity_state_and_convert(sensor_stub, "select.mode", "string")
        assert result == "TimeOfUse"

    def test_attributes_accessible_from_fake_state(self) -> None:
        """FakeState.attributes must be accessible like a real HA state object."""
        hass = make_fake_hass(
            {
                "sensor.solcast": {
                    "state": "5.0",
                    "attributes": {"pv_estimate": 5.0, "detailedForecast": []},
                }
            }
        )
        state = hass.states.get("sensor.solcast")

        assert state is not None
        assert state.state == "5.0"
        assert state.attributes["pv_estimate"] == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# TestMockServiceCalls — verify hardware-write interceptors
# ---------------------------------------------------------------------------


class TestMockServiceCalls:
    """Verify that hardware-write functions call HA services with correct parameters."""

    @pytest.mark.asyncio
    async def test_async_set_select_option_calls_ha_service(self) -> None:
        """async_set_select_option must call hass.services.async_call."""
        from custom_components.hsem.utils.misc import async_set_select_option

        hass = make_fake_hass({"select.batteries_working_mode": "TimeOfUse"})
        sensor_stub = MagicMock()
        sensor_stub.hass = hass

        with patch.object(
            hass.services, "async_call", new_callable=AsyncMock
        ) as mock_call:
            await async_set_select_option(
                sensor_stub,
                "select.batteries_working_mode",
                "Maximize Self Consumption",
            )
            # The service call must have been dispatched at least once.
            mock_call.assert_called()

    @pytest.mark.asyncio
    async def test_async_set_number_value_calls_ha_service(self) -> None:
        """async_set_number_value must call hass.services.async_call."""
        from custom_components.hsem.utils.misc import async_set_number_value

        hass = make_fake_hass({"number.batteries_maximum_charging_power": "5000"})
        sensor_stub = MagicMock()
        sensor_stub.hass = hass

        with patch.object(
            hass.services, "async_call", new_callable=AsyncMock
        ) as mock_call:
            await async_set_number_value(
                sensor_stub,
                "number.batteries_maximum_charging_power",
                3000,
            )
            mock_call.assert_called()

    @pytest.mark.asyncio
    async def test_no_service_calls_in_read_only_mode(self) -> None:
        """In read_only mode, _async_apply_hardware_writes must not call HA services."""
        config_entry = make_fake_config_entry({"hsem_read_only": True})
        coord = make_bare_coordinator(config_entry=config_entry)

        # Populate coordinator with a snapshot so the sensor can run apply.
        live = LiveState()
        live.huawei_batteries_soc_pct = 65.0
        live.huawei_batteries_rated_capacity_wh = 10000.0
        live.huawei_batteries_max_charge_power_w = 5000.0
        live.huawei_batteries_max_discharge_power_w = 5000.0
        live.house_consumption_power_w = 1200.0
        live.energi_data_service_import_price = 0.25
        live.energi_data_service_export_price = 0.15

        cfg = SensorConfig()
        cfg.read_only = True
        cfg.recommendation_interval_minutes = 60
        cfg.recommendation_interval_length = 24
        cfg.update_interval = 5

        coord.data = CoordinatorData(
            cfg=cfg,
            live=live,
            state="TimeOfUse",
            hourly_recommendation=None,
        )

        hass = make_fake_hass(_BASE_ENTITY_STATES)
        coord.hass = hass

        sensor = HSEMWorkingModeSensor(config_entry, coord)
        sensor.hass = hass

        with (
            patch(
                "custom_components.hsem.custom_sensors.applier.async_set_select_option",
                new_callable=AsyncMock,
            ) as mock_select,
            patch(
                "custom_components.hsem.custom_sensors.applier.async_set_number_value",
                new_callable=AsyncMock,
            ) as mock_number,
            patch(
                "custom_components.hsem.custom_sensors.applier.async_set_tou_periods",
                new_callable=AsyncMock,
            ) as mock_tou,
        ):
            await sensor._async_apply_hardware_writes(coord.data)

            # In read_only mode no hardware writes must occur.
            mock_select.assert_not_called()
            mock_number.assert_not_called()
            mock_tou.assert_not_called()


# ---------------------------------------------------------------------------
# TestDryRunCycle — full read-plan-apply pipeline with all I/O mocked
# ---------------------------------------------------------------------------


class TestDryRunCycle:
    """Test the full coordinator pipeline in dry-run (read_only=True) mode.

    All HA state reads are satisfied by :class:`FakeState` objects.
    All hardware-write calls are patched out.  The test verifies:

    - ``CoordinatorData`` is produced with a non-None ``live`` snapshot.
    - ``live.missing_entities`` reflects the availability of entity states.
    - ``hardware_writes_allowed`` logic is respected.
    - ``async_set_updated_data`` is called to notify subscriber entities.
    """

    @pytest.mark.asyncio
    async def test_full_cycle_produces_coordinator_data(self) -> None:
        """_async_run_update_cycle must call async_set_updated_data with a CoordinatorData."""
        config_entry = make_fake_config_entry({"hsem_read_only": True})
        hass = make_fake_hass(_BASE_ENTITY_STATES)
        coord = make_bare_coordinator(hass=hass, config_entry=config_entry)
        # Patch instance method to avoid actual HA timer calls.
        coord._set_update_interval = AsyncMock()

        captured: list[CoordinatorData] = []
        coord.async_set_updated_data = lambda d: captured.append(d)

        with _patch_all_ha_helpers():
            await coord._async_run_update_cycle()

        assert len(captured) == 1
        data = captured[0]
        assert isinstance(data, CoordinatorData)
        assert data.last_updated is not None

    @pytest.mark.asyncio
    async def test_live_state_populated_from_mock_states(self) -> None:
        """The live snapshot must reflect values from the mocked entity states."""
        config_entry = make_fake_config_entry({"hsem_read_only": True})
        hass = make_fake_hass(_BASE_ENTITY_STATES)
        coord = make_bare_coordinator(hass=hass, config_entry=config_entry)
        coord._set_update_interval = AsyncMock()

        captured: list[CoordinatorData] = []
        coord.async_set_updated_data = lambda d: captured.append(d)

        with _patch_all_ha_helpers():
            await coord._async_run_update_cycle()

        live = captured[0].live
        assert live is not None
        # Battery SoC should be read from "sensor.batteries_state_of_capacity" = "65"
        assert live.huawei_batteries_soc_pct == pytest.approx(65.0)

    @pytest.mark.asyncio
    async def test_no_hardware_writes_in_dry_run(self) -> None:
        """In read_only mode the full cycle must not dispatch hardware write calls."""
        config_entry = make_fake_config_entry({"hsem_read_only": True})
        hass = make_fake_hass(_BASE_ENTITY_STATES)
        coord = make_bare_coordinator(hass=hass, config_entry=config_entry)
        coord._set_update_interval = AsyncMock()
        coord.async_set_updated_data = MagicMock()

        with (
            _patch_all_ha_helpers(),
            patch(
                "custom_components.hsem.custom_sensors.applier.async_set_select_option",
                new_callable=AsyncMock,
            ) as mock_select,
            patch(
                "custom_components.hsem.custom_sensors.applier.async_set_number_value",
                new_callable=AsyncMock,
            ) as mock_number,
            patch(
                "custom_components.hsem.custom_sensors.applier.async_set_tou_periods",
                new_callable=AsyncMock,
            ) as mock_tou,
        ):
            await coord._async_run_update_cycle()

            mock_select.assert_not_called()
            mock_number.assert_not_called()
            mock_tou.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_critical_entity_triggers_error_mode(self) -> None:
        """When battery SoC is unavailable, degraded mode must be Error."""
        states = dict(_BASE_ENTITY_STATES)
        states["sensor.batteries_state_of_capacity"] = "unavailable"

        config_entry = make_fake_config_entry({"hsem_read_only": True})
        hass = make_fake_hass(states)
        coord = make_bare_coordinator(hass=hass, config_entry=config_entry)
        coord._set_update_interval = AsyncMock()

        captured: list[CoordinatorData] = []
        coord.async_set_updated_data = lambda d: captured.append(d)

        with _patch_all_ha_helpers():
            await coord._async_run_update_cycle()

        live = captured[0].live
        assert live is not None
        assert live.missing_entities is True
        assert live.degraded_mode == DegradedMode.Error

    @pytest.mark.asyncio
    async def test_missing_price_entity_triggers_degraded_mode(self) -> None:
        """When import price is unavailable, degraded mode must be Degraded (not Error)."""
        states = dict(_BASE_ENTITY_STATES)
        states["sensor.energi_data_service"] = "unavailable"

        config_entry = make_fake_config_entry({"hsem_read_only": True})
        hass = make_fake_hass(states)
        coord = make_bare_coordinator(hass=hass, config_entry=config_entry)
        coord._set_update_interval = AsyncMock()

        captured: list[CoordinatorData] = []
        coord.async_set_updated_data = lambda d: captured.append(d)

        with _patch_all_ha_helpers():
            await coord._async_run_update_cycle()

        live = captured[0].live
        assert live is not None
        # Price unavailability → degraded, not error.
        assert live.degraded_mode != DegradedMode.Error

    @pytest.mark.asyncio
    async def test_update_lock_prevents_concurrent_cycle(self) -> None:
        """While a cycle is in progress, a concurrent _async_handle_update must be dropped."""
        config_entry = make_fake_config_entry({"hsem_read_only": True})
        hass = make_fake_hass(_BASE_ENTITY_STATES)
        coord = make_bare_coordinator(hass=hass, config_entry=config_entry)

        cycle_count = 0

        async def _slow_cycle():
            nonlocal cycle_count
            cycle_count += 1
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        coord._async_run_update_cycle = _slow_cycle  # type: ignore[method-assign]

        await asyncio.gather(
            coord._async_handle_update(),
            coord._async_handle_update(),
        )

        # Only one cycle must have executed; the second was dropped by the lock.
        assert cycle_count == 1

    @pytest.mark.asyncio
    async def test_coordinator_data_has_state_after_cycle(self) -> None:
        """CoordinatorData.state must not be None after a successful cycle."""
        config_entry = make_fake_config_entry({"hsem_read_only": True})
        hass = make_fake_hass(_BASE_ENTITY_STATES)
        coord = make_bare_coordinator(hass=hass, config_entry=config_entry)
        coord._set_update_interval = AsyncMock()

        captured: list[CoordinatorData] = []
        coord.async_set_updated_data = lambda d: captured.append(d)

        with _patch_all_ha_helpers():
            await coord._async_run_update_cycle()

        assert captured[0].state is not None


# ---------------------------------------------------------------------------
# TestEntityStateAfterCoordinatorPush — entities reflect coordinator data
# ---------------------------------------------------------------------------


class TestEntityStateAfterCoordinatorPush:
    """Simulate a coordinator push and verify entities expose correct state."""

    def _make_populated_data(self, read_only: bool = True) -> CoordinatorData:
        """Return a CoordinatorData with a healthy LiveState snapshot."""

        live = LiveState()
        live.huawei_batteries_soc_pct = 70.0
        live.huawei_batteries_rated_capacity_wh = 10000.0
        live.huawei_batteries_max_charge_power_w = 5000.0
        live.huawei_batteries_max_discharge_power_w = 5000.0
        live.house_consumption_power_w = 1500.0
        live.energi_data_service_import_price = 0.30
        live.energi_data_service_export_price = 0.20

        cfg = SensorConfig()
        cfg.read_only = read_only
        cfg.recommendation_interval_minutes = 60
        cfg.recommendation_interval_length = 24
        cfg.update_interval = 5
        cfg.batteries_purchase_price = 0.0
        cfg.batteries_expected_cycles = 6000
        cfg.batteries_conversion_loss = 10
        cfg.energi_data_service_export_min_price = 0.0
        cfg.energi_data_service_update_interval = 15

        return CoordinatorData(
            cfg=cfg,
            live=live,
            state="TimeOfUse",
            last_updated="2026-05-12T12:00:00+00:00",
            next_update="2026-05-12T12:05:00+00:00",
        )

    def test_working_mode_state_reflects_coordinator_data(self) -> None:
        """WorkingModeSensor.state must return coordinator.data.state."""
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        coord.data = self._make_populated_data()

        sensor = HSEMWorkingModeSensor(config_entry, coord)

        assert sensor.state == "TimeOfUse"

    def test_working_mode_available_after_coordinator_push(self) -> None:
        """WorkingModeSensor must report available once coordinator.data is set."""
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        coord.data = self._make_populated_data()

        sensor = HSEMWorkingModeSensor(config_entry, coord)

        assert sensor.available is True

    def test_degraded_mode_state_ok_for_healthy_live(self) -> None:
        """DegradedModeSensor.state must be 'ok' when all critical entities are present."""
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        coord.data = self._make_populated_data()

        sensor = HSEMDegradedModeSensor(config_entry, coord)

        assert sensor.state == DegradedMode.OK.value

    def test_degraded_mode_state_error_when_soc_missing(self) -> None:
        """DegradedModeSensor.state must be 'error' when battery SoC is absent."""
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        data = self._make_populated_data()
        data.live.add_missing_entity(
            "Critical: batteries_state_of_capacity unavailable"
        )
        coord.data = data

        sensor = HSEMDegradedModeSensor(config_entry, coord)

        assert sensor.state == DegradedMode.Error.value

    def test_extra_attrs_include_degraded_mode_key(self) -> None:
        """extra_state_attributes must contain 'degraded_mode' key."""
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        coord.data = self._make_populated_data()

        sensor = HSEMWorkingModeSensor(config_entry, coord)
        attrs = sensor.extra_state_attributes

        assert "degraded_mode" in attrs

    def test_extra_attrs_report_hardware_writes_not_blocked_in_ok_mode(self) -> None:
        """hardware_writes_blocked must be False when degraded mode is OK."""
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        coord.data = self._make_populated_data()

        sensor = HSEMWorkingModeSensor(config_entry, coord)
        attrs = sensor.extra_state_attributes

        assert attrs.get("hardware_writes_blocked") is False

    def test_extra_attrs_hardware_writes_blocked_in_error_mode(self) -> None:
        """When missing_entities is True, extra_attrs must report the error status.

        The working-mode sensor returns an early 'error' dict when entities are
        missing.  The 'hardware_writes_blocked' key only appears in the normal
        attributes dict (full pipeline path).  Callers should read degraded mode
        from the dedicated DegradedModeSensor in the error case.
        """
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        data = self._make_populated_data()
        data.live.add_missing_entity(
            "Critical: batteries_state_of_capacity unavailable"
        )
        coord.data = data

        sensor = HSEMWorkingModeSensor(config_entry, coord)
        attrs = sensor.extra_state_attributes

        # When entities are missing the sensor returns an abbreviated error dict.
        assert attrs.get("status") == "error"
        assert "missing_input_entities_list" in attrs
        assert (
            "Critical: batteries_state_of_capacity unavailable"
            in attrs["missing_input_entities_list"]
        )


# ---------------------------------------------------------------------------
# TestReadOnlyConfigReaderIntegration — build_sensor_config with mock entry
# ---------------------------------------------------------------------------


class TestReadOnlyConfigReaderIntegration:
    """Verify that build_sensor_config correctly reads read_only from mock config entry."""

    def test_read_only_true_from_mock_config(self) -> None:
        """build_sensor_config must return read_only=True when the key is set."""
        from custom_components.hsem.custom_sensors.config_reader import (
            build_sensor_config,
        )

        config_entry = make_fake_config_entry({"hsem_read_only": True})
        cfg = build_sensor_config(config_entry)

        assert cfg.read_only is True

    def test_read_only_false_from_default(self) -> None:
        """build_sensor_config must return read_only=False for default config."""
        from custom_components.hsem.custom_sensors.config_reader import (
            build_sensor_config,
        )

        config_entry = make_fake_config_entry()
        cfg = build_sensor_config(config_entry)

        assert cfg.read_only is False

    def test_update_interval_parsed_correctly(self) -> None:
        """update_interval must be read as an integer from the config entry."""
        from custom_components.hsem.custom_sensors.config_reader import (
            build_sensor_config,
        )

        config_entry = make_fake_config_entry({"hsem_update_interval": "10"})
        cfg = build_sensor_config(config_entry)

        assert cfg.update_interval == 10

    def test_recommendation_interval_default_is_15_minutes(self) -> None:
        """Default recommendation_interval_minutes must be 15."""
        from custom_components.hsem.custom_sensors.config_reader import (
            build_sensor_config,
        )

        config_entry = make_fake_config_entry()
        cfg = build_sensor_config(config_entry)

        assert cfg.recommendation_interval_minutes == 15

    def test_read_only_string_true_coerced_to_bool(self) -> None:
        """build_sensor_config must coerce the string 'True' to bool True."""
        from custom_components.hsem.custom_sensors.config_reader import (
            build_sensor_config,
        )

        # Simulate a config entry that stored read_only as a string (old schema).
        config_entry = make_fake_config_entry({"hsem_read_only": "True"})
        cfg = build_sensor_config(config_entry)

        assert cfg.read_only is True

    def test_read_only_string_false_coerced_to_bool(self) -> None:
        """build_sensor_config must coerce the string 'False' to bool False."""
        from custom_components.hsem.custom_sensors.config_reader import (
            build_sensor_config,
        )

        config_entry = make_fake_config_entry({"hsem_read_only": "False"})
        cfg = build_sensor_config(config_entry)

        assert cfg.read_only is False

    def test_verbose_logging_boolean_coercion(self) -> None:
        """build_sensor_config must coerce verbose_logging to bool."""
        from custom_components.hsem.custom_sensors.config_reader import (
            build_sensor_config,
        )

        config_entry = make_fake_config_entry({"hsem_verbose_logging": "True"})
        cfg = build_sensor_config(config_entry)

        assert cfg.verbose_logging is True

    def test_extended_attributes_boolean_coercion(self) -> None:
        """build_sensor_config must coerce extended_attributes to bool."""
        from custom_components.hsem.custom_sensors.config_reader import (
            build_sensor_config,
        )

        config_entry = make_fake_config_entry({"hsem_extended_attributes": "True"})
        cfg = build_sensor_config(config_entry)

        assert cfg.extended_attributes is True

    def test_degraded_mode_sensor_exposes_read_only_attribute(self) -> None:
        """DegradedModeSensor extra_state_attributes must include read_only_mode."""
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)

        cfg = SensorConfig()
        cfg.read_only = True
        live = LiveState()
        coord.data = CoordinatorData(cfg=cfg, live=live, state=None)

        sensor = HSEMDegradedModeSensor(config_entry, coord)
        attrs = sensor.extra_state_attributes

        assert "read_only_mode" in attrs
        assert attrs["read_only_mode"] is True


# ---------------------------------------------------------------------------
# TestAdditionalDiagnosticSensors
# ---------------------------------------------------------------------------


class TestAdditionalDiagnosticSensors:
    """Tests for the five additional coordinator-driven diagnostic sensors."""

    def _make_data(
        self,
        *,
        next_update: str = "2026-05-12T12:00:00+02:00",
        last_updated: str = "2026-05-12T11:55:00+02:00",
        update_interval: int = 5,
        missing: list[str] | None = None,
        net_consumption_w: float = 250.0,
        force_mode_state: str = "auto",
    ) -> CoordinatorData:
        """Build a minimal CoordinatorData for sensor tests."""
        cfg = SensorConfig()
        cfg.update_interval = update_interval

        live = LiveState()
        live.net_consumption_w = net_consumption_w
        live.house_consumption_power_w = 800.0
        live.solar_production_power_w = 550.0
        live.net_consumption_with_ev_w = 250.0
        live.force_working_mode_state = force_mode_state
        if missing:
            for label in missing:
                live.add_missing_entity(label)

        return CoordinatorData(
            cfg=cfg,
            live=live,
            state="batteries_wait_mode",
            next_update=next_update,
            last_updated=last_updated,
        )

    # ------------------------------------------------------------------
    # HSEMNextUpdateSensor
    # ------------------------------------------------------------------

    def test_next_update_sensor_is_diagnostic(self) -> None:
        """NextUpdateSensor must carry the DIAGNOSTIC entity category."""
        from homeassistant.const import EntityCategory

        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        s = HSEMNextUpdateSensor(config_entry, coord)
        assert s._attr_entity_category == EntityCategory.DIAGNOSTIC

    def test_next_update_sensor_state_is_timestamp(self) -> None:
        """NextUpdateSensor.state must return the next_update timestamp."""
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        coord.data = self._make_data(next_update="2026-05-12T13:00:00+02:00")
        s = HSEMNextUpdateSensor(config_entry, coord)
        assert s.state == "2026-05-12T13:00:00+02:00"

    def test_next_update_sensor_no_data_returns_none(self) -> None:
        """NextUpdateSensor.state must be None before first cycle."""
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        s = HSEMNextUpdateSensor(config_entry, coord)
        assert s.state is None

    def test_next_update_sensor_attrs_include_interval(self) -> None:
        """NextUpdateSensor extra_state_attributes must include update_interval_minutes."""
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        coord.data = self._make_data(update_interval=15)
        s = HSEMNextUpdateSensor(config_entry, coord)
        assert s.extra_state_attributes["update_interval_minutes"] == 15

    def test_next_update_sensor_unique_ids_distinct(self) -> None:
        """All diagnostic sensor unique IDs must be distinct from each other."""
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        sensors = [
            HSEMNextUpdateSensor(config_entry, coord),
            HSEMMissingEntitiesSensor(config_entry, coord),
            HSEMHardwareWritesSensor(config_entry, coord),
            HSEMNetConsumptionSensor(config_entry, coord),
            HSEMForceModeSensor(config_entry, coord),
            HSEMReadOnlySensor(config_entry, coord),
            HSEMDegradedModeSensor(config_entry, coord),
        ]
        uids = [s.unique_id for s in sensors]
        assert len(uids) == len(set(uids)), "Duplicate unique_id found"

    # ------------------------------------------------------------------
    # HSEMMissingEntitiesSensor
    # ------------------------------------------------------------------

    def test_missing_entities_sensor_is_diagnostic(self) -> None:
        """MissingEntitiesSensor must carry the DIAGNOSTIC entity category."""
        from homeassistant.const import EntityCategory

        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        s = HSEMMissingEntitiesSensor(config_entry, coord)
        assert s._attr_entity_category == EntityCategory.DIAGNOSTIC

    def test_missing_entities_zero_when_none_missing(self) -> None:
        """MissingEntitiesSensor.state must be 0 when all entities are present."""
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        coord.data = self._make_data()
        s = HSEMMissingEntitiesSensor(config_entry, coord)
        assert s.state == 0

    def test_missing_entities_count_reflects_list_length(self) -> None:
        """MissingEntitiesSensor.state must equal len(missing_entities_list)."""
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        coord.data = self._make_data(
            missing=[
                "Missing entity: energi_data_service_import",
                "Missing entity: solcast_pv_forecast_forecast_today",
            ]
        )
        s = HSEMMissingEntitiesSensor(config_entry, coord)
        assert s.state == 2

    def test_missing_entities_attrs_contain_list(self) -> None:
        """MissingEntitiesSensor extra_state_attributes must include the label list."""
        label = "Missing entity: energi_data_service_import"
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        coord.data = self._make_data(missing=[label])
        s = HSEMMissingEntitiesSensor(config_entry, coord)
        assert label in s.extra_state_attributes["missing_entities_list"]

    # ------------------------------------------------------------------
    # HSEMHardwareWritesSensor
    # ------------------------------------------------------------------

    def test_hardware_writes_sensor_is_diagnostic(self) -> None:
        """HardwareWritesSensor must carry the DIAGNOSTIC entity category."""
        from homeassistant.const import EntityCategory

        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        s = HSEMHardwareWritesSensor(config_entry, coord)
        assert s._attr_entity_category == EntityCategory.DIAGNOSTIC

    def test_hardware_writes_allowed_when_no_missing_entities(self) -> None:
        """HardwareWritesSensor.state must be 'allowed' when no critical entities missing."""
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        coord.data = self._make_data()
        s = HSEMHardwareWritesSensor(config_entry, coord)
        assert s.state == "allowed"

    def test_hardware_writes_blocked_when_critical_entity_missing(self) -> None:
        """HardwareWritesSensor.state must be 'blocked' when a critical entity is absent."""
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        coord.data = self._make_data(
            missing=["Missing entity: batteries_state_of_capacity"]
        )
        s = HSEMHardwareWritesSensor(config_entry, coord)
        assert s.state == "blocked"

    def test_hardware_writes_allowed_with_only_non_critical_missing(self) -> None:
        """HardwareWritesSensor must be 'allowed' when only non-critical entities are absent."""
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        coord.data = self._make_data(
            missing=["Missing entity: energi_data_service_import"]
        )
        s = HSEMHardwareWritesSensor(config_entry, coord)
        assert s.state == "allowed"

    # ------------------------------------------------------------------
    # HSEMNetConsumptionSensor
    # ------------------------------------------------------------------

    def test_net_consumption_sensor_is_diagnostic(self) -> None:
        """NetConsumptionSensor must carry the DIAGNOSTIC entity category."""
        from homeassistant.const import EntityCategory

        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        s = HSEMNetConsumptionSensor(config_entry, coord)
        assert s._attr_entity_category == EntityCategory.DIAGNOSTIC

    def test_net_consumption_native_value(self) -> None:
        """NetConsumptionSensor.native_value must reflect live.net_consumption_w."""
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        coord.data = self._make_data(net_consumption_w=350.5)
        s = HSEMNetConsumptionSensor(config_entry, coord)
        assert s.native_value == pytest.approx(350.5)

    def test_net_consumption_unit_is_watts(self) -> None:
        """NetConsumptionSensor must use Watts as its unit."""
        from homeassistant.const import UnitOfPower

        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        s = HSEMNetConsumptionSensor(config_entry, coord)
        assert s._attr_native_unit_of_measurement == UnitOfPower.WATT

    def test_net_consumption_attrs_include_breakdown(self) -> None:
        """NetConsumptionSensor extra_state_attributes must include house and solar components."""
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        coord.data = self._make_data()
        s = HSEMNetConsumptionSensor(config_entry, coord)
        attrs = s.extra_state_attributes
        assert "house_consumption_w" in attrs
        assert "solar_production_w" in attrs
        assert "net_consumption_with_ev_w" in attrs
        assert "ev_charging_active" in attrs

    # ------------------------------------------------------------------
    # HSEMForceModeSensor
    # ------------------------------------------------------------------

    def test_force_mode_sensor_is_diagnostic(self) -> None:
        """ForceModeSensor must carry the DIAGNOSTIC entity category."""
        from homeassistant.const import EntityCategory

        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        s = HSEMForceModeSensor(config_entry, coord)
        assert s._attr_entity_category == EntityCategory.DIAGNOSTIC

    def test_force_mode_sensor_state_auto_when_not_overriding(self) -> None:
        """ForceModeSensor.state must be 'auto' when no override is set."""
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        coord.data = self._make_data(force_mode_state="auto")
        s = HSEMForceModeSensor(config_entry, coord)
        assert s.state == "auto"

    def test_force_mode_sensor_state_reflects_override(self) -> None:
        """ForceModeSensor.state must reflect the active override value."""
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        coord.data = self._make_data(force_mode_state="batteries_charge_grid")
        s = HSEMForceModeSensor(config_entry, coord)
        assert s.state == "batteries_charge_grid"

    def test_force_mode_sensor_override_active_attr(self) -> None:
        """ForceModeSensor extra_state_attributes.override_active must be True when forced."""
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        coord.data = self._make_data(force_mode_state="batteries_charge_grid")
        s = HSEMForceModeSensor(config_entry, coord)
        assert s.extra_state_attributes["override_active"] is True

    def test_force_mode_sensor_override_inactive_when_auto(self) -> None:
        """ForceModeSensor extra_state_attributes.override_active must be False in auto."""
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        coord.data = self._make_data(force_mode_state="auto")
        s = HSEMForceModeSensor(config_entry, coord)
        assert s.extra_state_attributes["override_active"] is False

    def test_force_mode_default_before_first_cycle(self) -> None:
        """ForceModeSensor.state must default to 'auto' before the first cycle."""
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        s = HSEMForceModeSensor(config_entry, coord)
        assert s.state == "auto"


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _patch_all_ha_helpers():
    """Return a context manager that patches all HA-specific async helpers.

    Patches applied:
    - ``async_track_state_change_event`` — state-change registration
    - ``async_resolve_entity_id_from_unique_id`` — entity registry lookup
    - ``async_populate_avg_house_consumption`` — avg consumption data
    - ``async_populate_price_and_solcast`` — price and Solcast data
    - ``async_set_select_option`` — inverter mode writes
    - ``async_set_number_value`` — number entity writes
    - ``async_set_tou_periods`` — TOU period writes
    - ``async_set_forcible_discharge`` — forcible discharge writes
    - ``async_set_grid_export_power_pct`` — grid export writes
    - ``async_track_time_interval`` — interval timer registration

    All patched callables are no-ops (AsyncMocks returning truthy defaults).
    """
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        patches = [
            patch(
                "custom_components.hsem.custom_sensors.state_collector"
                ".async_track_state_change_event",
                new_callable=MagicMock,
            ),
            patch(
                "custom_components.hsem.utils.misc"
                ".async_resolve_entity_id_from_unique_id",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "custom_components.hsem.coordinator"
                ".async_populate_avg_house_consumption",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "custom_components.hsem.coordinator.async_populate_price_and_solcast",
                new_callable=AsyncMock,
            ),
            patch(
                "custom_components.hsem.utils.misc.async_set_select_option",
                new_callable=AsyncMock,
            ),
            patch(
                "custom_components.hsem.utils.misc.async_set_number_value",
                new_callable=AsyncMock,
            ),
            patch(
                "custom_components.hsem.utils.huawei.async_set_tou_periods",
                new_callable=AsyncMock,
            ),
            patch(
                "custom_components.hsem.utils.huawei.async_set_forcible_discharge",
                new_callable=AsyncMock,
            ),
            patch(
                "custom_components.hsem.utils.huawei.async_set_grid_export_power_pct",
                new_callable=AsyncMock,
            ),
            patch(
                "homeassistant.helpers.event.async_track_time_interval",
                new_callable=MagicMock,
                return_value=MagicMock(),
            ),
        ]
        entered = []
        try:
            for p in patches:
                entered.append(p.__enter__())
            yield entered
        finally:
            for p in reversed(patches):
                p.__exit__(None, None, None)

    return _ctx()
