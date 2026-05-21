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
from custom_components.hsem.coordinator_builder import (
    generate_recommendation_intervals,
    utc_key,
)
from custom_components.hsem.custom_sensors.battery_soc_sensor import (
    HSEMBatterySoCSensor,
)
from custom_components.hsem.custom_sensors.degraded_mode_sensor import (
    HSEMDegradedModeSensor,
)
from custom_components.hsem.custom_sensors.ev_charging_sensor import (
    HSEMEVChargingSensor,
)
from custom_components.hsem.custom_sensors.force_mode_sensor import HSEMForceModeSensor
from custom_components.hsem.custom_sensors.hardware_writes_sensor import (
    HSEMHardwareWritesSensor,
)
from custom_components.hsem.custom_sensors.last_updated_sensor import (
    HSEMLastUpdatedSensor,
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
from custom_components.hsem.custom_sensors.recommendation_interval_sensor import (
    HSEMRecommendationIntervalSensor,
)
from custom_components.hsem.custom_sensors.update_interval_sensor import (
    HSEMUpdateIntervalSensor,
)
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
    coord._listener_unsubs = []
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
    coord._snapshot = None

    from custom_components.hsem.models.planner_outputs import (
        DataQuality,
        PlanExplanation,
    )

    coord._plan_explanation = PlanExplanation()
    coord._data_quality = DataQuality()
    coord._ev_charging_plan = None
    coord._ev_second_charging_plan = None

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

    def test_unavailable_state_returns_none(self) -> None:
        """An 'unavailable' state string must return None for float reads."""
        from custom_components.hsem.utils.misc import ha_get_entity_state_and_convert

        hass = make_fake_hass({"sensor.soc": "unavailable"})
        sensor_stub = MagicMock()
        sensor_stub.hass = hass

        result = ha_get_entity_state_and_convert(sensor_stub, "sensor.soc", "float")
        assert result is None

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
                "custom_components.hsem.custom_sensors.working_mode_sensor.async_logger",
                new_callable=AsyncMock,
            ),
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
    async def test_missing_critical_entity_triggers_degraded_mode(self) -> None:
        """When battery SoC is unavailable, degraded must be Degraded (Error requires entity not found)."""
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
        assert live.degraded_mode == DegradedMode.Degraded

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
# TestRemainingDiagnosticSensors — #2, #4, #8, #11, #12
# ---------------------------------------------------------------------------


class TestRemainingDiagnosticSensors:
    """Tests for HSEMUpdateIntervalSensor, HSEMLastUpdatedSensor,
    HSEMBatterySoCSensor, HSEMRecommendationIntervalSensor, HSEMEVChargingSensor."""

    def _make_cfg(
        self,
        *,
        update_interval: int = 5,
        recommendation_interval_minutes: int = 15,
        recommendation_interval_length: int = 48,
        ev_second_enabled: bool = False,
    ) -> SensorConfig:
        cfg = SensorConfig()
        cfg.update_interval = update_interval
        cfg.recommendation_interval_minutes = recommendation_interval_minutes
        cfg.recommendation_interval_length = recommendation_interval_length
        cfg.ev_second_enabled = ev_second_enabled
        return cfg

    def _make_live(
        self,
        *,
        soc_pct: float | None = 75.0,
        ev_charging: bool = False,
        ev_second_charging: bool = False,
    ) -> LiveState:
        from custom_components.hsem.models.live_state import EVLiveState

        live = LiveState()
        live.huawei_batteries_soc_pct = soc_pct
        live.battery_current_capacity_kwh = 6.0
        live.battery_usable_capacity_kwh = 9.0
        live.huawei_batteries_rated_capacity_wh = 10000.0
        live.huawei_batteries_end_of_discharge_soc_pct = 10.0
        live.ev = EVLiveState(
            is_charging=ev_charging,
            power_w=7400.0 if ev_charging else 0.0,
            soc_pct=55.0,
        )
        live.ev_second = EVLiveState(
            is_charging=ev_second_charging, power_w=0.0, soc_pct=None
        )
        return live

    def _make_data(
        self, cfg: SensorConfig | None = None, live: LiveState | None = None
    ) -> CoordinatorData:
        return CoordinatorData(
            cfg=cfg or self._make_cfg(),
            live=live or self._make_live(),
            state="batteries_wait_mode",
            last_updated="2026-05-12T11:55:00+02:00",
            next_update="2026-05-12T12:00:00+02:00",
        )

    # ------------------------------------------------------------------
    # HSEMUpdateIntervalSensor (#2)
    # ------------------------------------------------------------------

    def test_update_interval_sensor_is_diagnostic(self) -> None:
        from homeassistant.const import EntityCategory

        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        s = HSEMUpdateIntervalSensor(config_entry, coord)
        assert s._attr_entity_category == EntityCategory.DIAGNOSTIC

    def test_update_interval_native_value(self) -> None:
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        coord.data = self._make_data(cfg=self._make_cfg(update_interval=15))
        s = HSEMUpdateIntervalSensor(config_entry, coord)
        assert s.native_value == 15

    def test_update_interval_unit_is_minutes(self) -> None:
        from homeassistant.const import UnitOfTime

        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        s = HSEMUpdateIntervalSensor(config_entry, coord)
        assert s._attr_native_unit_of_measurement == UnitOfTime.MINUTES

    def test_update_interval_attrs_include_recommendation_settings(self) -> None:
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        coord.data = self._make_data(
            cfg=self._make_cfg(
                recommendation_interval_minutes=60, recommendation_interval_length=24
            )
        )
        s = HSEMUpdateIntervalSensor(config_entry, coord)
        attrs = s.extra_state_attributes
        assert attrs["recommendation_interval_minutes"] == 60
        assert attrs["recommendation_interval_length_hours"] == 24

    def test_update_interval_none_before_first_cycle(self) -> None:
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        s = HSEMUpdateIntervalSensor(config_entry, coord)
        assert s.native_value is None

    # ------------------------------------------------------------------
    # HSEMLastUpdatedSensor (#4)
    # ------------------------------------------------------------------

    def test_last_updated_sensor_is_diagnostic(self) -> None:
        from homeassistant.const import EntityCategory

        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        s = HSEMLastUpdatedSensor(config_entry, coord)
        assert s._attr_entity_category == EntityCategory.DIAGNOSTIC

    def test_last_updated_state_is_timestamp(self) -> None:
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        coord.data = self._make_data()
        s = HSEMLastUpdatedSensor(config_entry, coord)
        assert s.state == "2026-05-12T11:55:00+02:00"

    def test_last_updated_none_before_first_cycle(self) -> None:
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        s = HSEMLastUpdatedSensor(config_entry, coord)
        assert s.state is None

    def test_last_updated_attrs_include_next_update(self) -> None:
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        coord.data = self._make_data()
        s = HSEMLastUpdatedSensor(config_entry, coord)
        assert s.extra_state_attributes["next_update"] == "2026-05-12T12:00:00+02:00"

    # ------------------------------------------------------------------
    # HSEMBatterySoCSensor (#8)
    # ------------------------------------------------------------------

    def test_battery_soc_sensor_is_diagnostic(self) -> None:
        from homeassistant.const import EntityCategory

        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        s = HSEMBatterySoCSensor(config_entry, coord)
        assert s._attr_entity_category == EntityCategory.DIAGNOSTIC

    def test_battery_soc_native_value(self) -> None:
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        coord.data = self._make_data(live=self._make_live(soc_pct=83.5))
        s = HSEMBatterySoCSensor(config_entry, coord)
        assert s.native_value == pytest.approx(83.5)

    def test_battery_soc_unit_is_percent(self) -> None:
        from homeassistant.const import PERCENTAGE

        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        s = HSEMBatterySoCSensor(config_entry, coord)
        assert s._attr_native_unit_of_measurement == PERCENTAGE

    def test_battery_soc_none_when_unavailable(self) -> None:
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        coord.data = self._make_data(live=self._make_live(soc_pct=None))
        s = HSEMBatterySoCSensor(config_entry, coord)
        assert s.native_value is None

    def test_battery_soc_attrs_include_capacity(self) -> None:
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        coord.data = self._make_data()
        s = HSEMBatterySoCSensor(config_entry, coord)
        attrs = s.extra_state_attributes
        assert "battery_current_capacity_kwh" in attrs
        assert "battery_usable_capacity_kwh" in attrs
        assert "battery_rated_capacity_wh" in attrs
        assert "end_of_discharge_soc_pct" in attrs

    # ------------------------------------------------------------------
    # HSEMRecommendationIntervalSensor (#11)
    # ------------------------------------------------------------------

    def test_recommendation_interval_sensor_is_diagnostic(self) -> None:
        from homeassistant.const import EntityCategory

        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        s = HSEMRecommendationIntervalSensor(config_entry, coord)
        assert s._attr_entity_category == EntityCategory.DIAGNOSTIC

    def test_recommendation_interval_native_value(self) -> None:
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        coord.data = self._make_data(
            cfg=self._make_cfg(recommendation_interval_minutes=60)
        )
        s = HSEMRecommendationIntervalSensor(config_entry, coord)
        assert s.native_value == 60

    def test_recommendation_interval_unit_is_minutes(self) -> None:
        from homeassistant.const import UnitOfTime

        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        s = HSEMRecommendationIntervalSensor(config_entry, coord)
        assert s._attr_native_unit_of_measurement == UnitOfTime.MINUTES

    def test_recommendation_interval_attrs_total_slots(self) -> None:
        """48h / 15min = 192 slots."""
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        coord.data = self._make_data(
            cfg=self._make_cfg(
                recommendation_interval_minutes=15, recommendation_interval_length=48
            )
        )
        s = HSEMRecommendationIntervalSensor(config_entry, coord)
        attrs = s.extra_state_attributes
        assert attrs["total_planning_slots"] == 192
        assert attrs["recommendation_interval_length_hours"] == 48

    # ------------------------------------------------------------------
    # HSEMEVChargingSensor (#12)
    # ------------------------------------------------------------------

    def test_ev_charging_sensor_is_diagnostic(self) -> None:
        from homeassistant.const import EntityCategory

        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        s = HSEMEVChargingSensor(config_entry, coord)
        assert s._attr_entity_category == EntityCategory.DIAGNOSTIC

    def test_ev_charging_off_when_not_charging(self) -> None:
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        coord.data = self._make_data(live=self._make_live(ev_charging=False))
        s = HSEMEVChargingSensor(config_entry, coord)
        assert s.state == "off"

    def test_ev_charging_on_when_primary_is_charging(self) -> None:
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        coord.data = self._make_data(live=self._make_live(ev_charging=True))
        s = HSEMEVChargingSensor(config_entry, coord)
        assert s.state == "on"

    def test_ev_charging_on_when_secondary_is_charging(self) -> None:
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        coord.data = self._make_data(
            live=self._make_live(ev_charging=False, ev_second_charging=True)
        )
        s = HSEMEVChargingSensor(config_entry, coord)
        assert s.state == "on"

    def test_ev_charging_default_off_before_first_cycle(self) -> None:
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        s = HSEMEVChargingSensor(config_entry, coord)
        assert s.state == "off"

    def test_ev_charging_attrs_include_individual_states(self) -> None:
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        coord.data = self._make_data(live=self._make_live(ev_charging=True))
        s = HSEMEVChargingSensor(config_entry, coord)
        attrs = s.extra_state_attributes
        assert attrs["ev_charging"] is True
        assert "ev_power_w" in attrs
        assert "ev_soc_pct" in attrs
        assert "ev_second_enabled" in attrs
        assert "ev_second_charging" in attrs

    def test_all_12_diagnostic_sensors_have_distinct_unique_ids(self) -> None:
        """All 12 coordinator-driven diagnostic sensors must have distinct unique IDs."""
        config_entry = make_fake_config_entry()
        coord = make_bare_coordinator(config_entry=config_entry)
        sensors = [
            HSEMDegradedModeSensor(config_entry, coord),
            HSEMReadOnlySensor(config_entry, coord),
            HSEMNextUpdateSensor(config_entry, coord),
            HSEMLastUpdatedSensor(config_entry, coord),
            HSEMUpdateIntervalSensor(config_entry, coord),
            HSEMRecommendationIntervalSensor(config_entry, coord),
            HSEMMissingEntitiesSensor(config_entry, coord),
            HSEMHardwareWritesSensor(config_entry, coord),
            HSEMNetConsumptionSensor(config_entry, coord),
            HSEMBatterySoCSensor(config_entry, coord),
            HSEMForceModeSensor(config_entry, coord),
            HSEMEVChargingSensor(config_entry, coord),
        ]
        uids = [s.unique_id for s in sensors]
        assert len(uids) == len(set(uids)), f"Duplicate unique_ids: {uids}"


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _patch_all_ha_helpers():
    """Return a context manager that patches all HA-specific async helpers.

    Patches applied:
    - ``async_track_state_change_event`` — state-change registration
    - ``async_resolve_entity_id_from_unique_id`` — entity registry lookup
    - ``populate_avg_house_consumption_from_snapshot`` — snapshot-based avg
      consumption (replaces old async_populate_avg_house_consumption)
    - ``populate_price_and_solcast_from_snapshot`` — snapshot-based price and
      Solcast data (replaces old async_populate_price_and_solcast)
    - ``async_set_select_option`` — inverter mode writes
    - ``async_set_number_value`` — number entity writes
    - ``async_set_tou_periods`` — TOU period writes
    - ``async_set_forcible_discharge`` — forcible discharge writes
    - ``async_set_grid_export_power_pct`` — grid export writes
    - ``async_track_time_interval`` — interval timer registration

    All patched callables are no-ops (returning truthy defaults).
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
                ".populate_avg_house_consumption_from_snapshot",
                return_value=True,
            ),
            patch(
                "custom_components.hsem.coordinator"
                ".populate_price_and_solcast_from_snapshot",
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


# ---------------------------------------------------------------------------
# TestApplyPlannerOutputEvLoad — _apply_planner_output propagates ev_planned_load_kwh
# ---------------------------------------------------------------------------


class TestApplyPlannerOutputEvLoad:
    """Verify that ``_apply_planner_output`` correctly propagates
    ``ev_planned_load_kwh`` and all other planner fields into the coordinator's
    ``_hourly_recommendations`` list.

    Tests cover:
    - Normal same-tzinfo matching
    - Mixed ZoneInfo vs fixed-offset timezone matching (UTC normalisation)
    - Microsecond-carrying rec.start vs zero-microsecond slot.start
    - 15-minute interval slots
    - Warning emission when a slot cannot be matched
    - All energy fields are copied, not just ev_planned_load_kwh
    - Non-EV hours stay zero
    """

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_coord_with_recs(self, interval_minutes: int = 60, total_hours: int = 24):
        """Return a bare coordinator whose _hourly_recommendations are pre-generated."""
        coord = make_bare_coordinator()
        coord._batteries_schedules = []
        coord._hourly_recommendations = generate_recommendation_intervals(
            interval_minutes, total_hours
        )
        return coord

    def _make_output_from_recs(
        self,
        recs,
        ev_load_by_hour: dict[int, float],
        ev_accounted_by_hour: dict[int, float] | None = None,
        ev_total_by_hour: dict[int, float] | None = None,
    ):
        """Build a PlannerOutput whose slot starts exactly match *recs*.

        Args:
            recs: The HourlyRecommendation objects whose starts define slot keys.
            ev_load_by_hour: Mapping of hour → ev_planned_load_kwh (injected).
            ev_accounted_by_hour: Mapping of hour → ev_accounted_load_kwh.  When
                omitted, defaults to ``0.0`` for all hours.
            ev_total_by_hour: Mapping of hour → ev_total_planned_load_kwh.  When
                omitted, defaults to ``ev_load + ev_accounted`` per slot.
        """
        from custom_components.hsem.models.planner_outputs import (
            PlannedSlot,
            PlannerOutput,
        )
        from custom_components.hsem.utils.prices import SlotPrice

        if ev_accounted_by_hour is None:
            ev_accounted_by_hour = {}
        slots = []
        for rec in recs:
            h = rec.start.hour
            ev = ev_load_by_hour.get(h, 0.0)
            ev_acc = ev_accounted_by_hour.get(h, 0.0)
            ev_tot = (
                ev_total_by_hour.get(h, ev + ev_acc)
                if ev_total_by_hour is not None
                else ev + ev_acc
            )
            slots.append(
                PlannedSlot(
                    start=rec.start,
                    end=rec.end,
                    price=SlotPrice(import_price=0.20, export_price=0.05),
                    avg_house_consumption_kwh=1.0,
                    solcast_pv_estimate_kwh=0.5,
                    ev_planned_load_kwh=ev,
                    ev_accounted_load_kwh=ev_acc,
                    ev_total_planned_load_kwh=ev_tot,
                    estimated_net_consumption_kwh=round(1.0 + ev - 0.5, 3),
                    recommendation="batteries_wait_mode",
                    batteries_charged_kwh=0.0,
                    batteries_discharged_kwh=0.2,
                    estimated_battery_soc_pct=55.0,
                    estimated_battery_capacity_kwh=4.5,
                    estimated_cost_currency=0.08,
                    grid_import_kwh=0.1,
                    grid_export_kwh=0.0,
                )
            )
        return PlannerOutput(slots=slots)

    # ------------------------------------------------------------------
    # Core field-copy tests
    # ------------------------------------------------------------------

    def test_ev_load_copied_to_all_matching_hours(self):
        """ev_planned_load_kwh must be written to every rec whose hour matches."""
        coord = self._make_coord_with_recs()
        ev_hours = {14: 3.7, 15: 3.7, 16: 2.1}
        output = self._make_output_from_recs(coord._hourly_recommendations, ev_hours)

        coord._apply_planner_output(output)

        for rec in coord._hourly_recommendations:
            expected = ev_hours.get(rec.start.hour, 0.0)
            assert abs(rec.ev_planned_load_kwh - expected) < 1e-9, (
                f"Hour {rec.start.hour}: expected ev_load={expected}, "
                f"got {rec.ev_planned_load_kwh}"
            )

    def test_zero_ev_hours_stay_zero_after_apply(self):
        """Rec slots whose hour has no EV load must remain at ev_planned_load_kwh=0."""
        coord = self._make_coord_with_recs()
        output = self._make_output_from_recs(coord._hourly_recommendations, {14: 3.7})

        coord._apply_planner_output(output)

        for rec in coord._hourly_recommendations:
            if rec.start.hour != 14:
                assert (
                    abs(rec.ev_planned_load_kwh) < 1e-9
                ), f"Hour {rec.start.hour} should be 0 but got {rec.ev_planned_load_kwh}"

    def test_all_energy_fields_are_copied(self):
        """Every field written by _apply_planner_output must reach the rec object."""
        coord = self._make_coord_with_recs()
        output = self._make_output_from_recs(coord._hourly_recommendations, {10: 2.5})

        coord._apply_planner_output(output)

        rec_10 = next(r for r in coord._hourly_recommendations if r.start.hour == 10)
        assert rec_10.recommendation == "batteries_wait_mode"
        assert abs(rec_10.ev_planned_load_kwh - 2.5) < 1e-9
        assert (
            abs(rec_10.estimated_net_consumption_kwh - round(1.0 + 2.5 - 0.5, 3)) < 1e-6
        )
        assert abs(rec_10.solcast_pv_estimate_kwh - 0.5) < 1e-9
        assert abs(rec_10.batteries_discharged_kwh - 0.2) < 1e-9
        assert abs(rec_10.estimated_battery_soc_pct - 55.0) < 1e-9
        assert abs(rec_10.estimated_battery_capacity_kwh - 4.5) < 1e-9
        assert abs(rec_10.estimated_cost_currency - 0.08) < 1e-9
        assert abs(rec_10.grid_import_kwh - 0.1) < 1e-9
        assert abs(rec_10.grid_export_kwh - 0.0) < 1e-9

    def test_all_24_recs_matched_with_utc_normalisation(self):
        """After UTC-normalisation all 24 hourly recs must match planner slots."""
        coord = self._make_coord_with_recs()
        output = self._make_output_from_recs(coord._hourly_recommendations, {12: 1.5})

        # Verify via utc_key
        utc_key_func = utc_key
        slot_by_utc = {utc_key_func(s.start): s for s in output.slots}
        matched = sum(
            1
            for rec in coord._hourly_recommendations
            if slot_by_utc.get(utc_key_func(rec.start)) is not None
        )
        assert matched == 24, f"Only {matched}/24 recs matched after UTC normalisation"

    # ------------------------------------------------------------------
    # Timezone-equivalence: rec uses ZoneInfo, slot uses fixed offset
    # ------------------------------------------------------------------

    def test_zoneinfo_rec_matches_fixed_offset_slot(self):
        """Slots with ZoneInfo tzinfo and equivalent fixed-offset instants must match.

        Simulates the real production case where _generate_recommendation_intervals
        creates recs with ZoneInfo('Europe/Copenhagen') while the planner builds
        slots from datetime.fromisoformat(now_iso) which carries a fixed +02:00 offset.
        """
        from datetime import datetime, timedelta, timezone
        from zoneinfo import ZoneInfo

        from custom_components.hsem.models.hourly_recommendation import (
            HourlyRecommendation,
        )
        from custom_components.hsem.models.planner_outputs import (
            PlannedSlot,
            PlannerOutput,
        )
        from custom_components.hsem.utils.prices import SlotPrice

        tz_zone = ZoneInfo("Europe/Copenhagen")
        tz_fixed = timezone(timedelta(hours=2))  # +02:00, same offset in summer

        midnight_zone = datetime(2024, 6, 15, 0, 0, 0, tzinfo=tz_zone)
        midnight_fixed = datetime(2024, 6, 15, 0, 0, 0, tzinfo=tz_fixed)

        # Build recs with ZoneInfo
        recs = []
        for h in range(24):
            t_start = midnight_zone + timedelta(hours=h)
            t_end = t_start + timedelta(hours=1)
            recs.append(
                HourlyRecommendation(
                    start=t_start,
                    end=t_end,
                    avg_house_consumption_kwh=0.0,
                    avg_house_consumption_1d_kwh=0.0,
                    avg_house_consumption_3d_kwh=0.0,
                    avg_house_consumption_7d_kwh=0.0,
                    avg_house_consumption_14d_kwh=0.0,
                    batteries_charged_kwh=0.0,
                    batteries_discharged_kwh=0.0,
                    estimated_battery_capacity_kwh=0.0,
                    estimated_battery_soc_pct=0,
                    estimated_cost_currency=0.0,
                    estimated_net_consumption_kwh=0.0,
                    ev_planned_load_kwh=0.0,
                    export_price=0.0,
                    grid_export_kwh=0.0,
                    grid_import_kwh=0.0,
                    import_price=0.0,
                    recommendation=None,
                    solcast_pv_estimate_kwh=0.0,
                )
            )

        # Build slots with fixed-offset — same instants, different tzinfo type
        slots = []
        for h in range(24):
            t_start = midnight_fixed + timedelta(hours=h)
            t_end = t_start + timedelta(hours=1)
            ev = 3.5 if h == 15 else 0.0
            slots.append(
                PlannedSlot(
                    start=t_start,
                    end=t_end,
                    price=SlotPrice(import_price=0.20, export_price=0.05),
                    ev_planned_load_kwh=ev,
                    estimated_net_consumption_kwh=round(1.0 + ev - 0.5, 3),
                    recommendation=(
                        "ev_smart_charging" if ev > 0 else "batteries_wait_mode"
                    ),
                )
            )

        coord = make_bare_coordinator()
        coord._batteries_schedules = []
        coord._hourly_recommendations = recs

        coord._apply_planner_output(PlannerOutput(slots=slots))

        rec_15 = next(r for r in recs if r.start.hour == 15)
        assert abs(rec_15.ev_planned_load_kwh - 3.5) < 1e-9, (
            f"ZoneInfo/fixed-offset mismatch: ev_planned_load_kwh={rec_15.ev_planned_load_kwh}, "
            f"rec.start tzinfo={type(rec_15.start.tzinfo).__name__}, "
            f"slot.start tzinfo={type(slots[15].start.tzinfo).__name__}"
        )

    def test_microsecond_in_rec_start_still_matches(self):
        """rec.start with non-zero microseconds must still match the planner slot.

        dt_util.now() can return datetimes with microseconds; the planner
        builds slot starts via timedelta arithmetic from midnight (always zero
        microseconds).  _utc_key strips microseconds on both sides so the match
        succeeds regardless.
        """
        from datetime import UTC, datetime, timedelta

        from custom_components.hsem.models.hourly_recommendation import (
            HourlyRecommendation,
        )
        from custom_components.hsem.models.planner_outputs import (
            PlannedSlot,
            PlannerOutput,
        )
        from custom_components.hsem.utils.prices import SlotPrice

        midnight = datetime(2024, 6, 15, 0, 0, 0, tzinfo=UTC)

        # Rec starts carry microseconds (simulating dt_util.now() sub-second jitter)
        recs = []
        for h in range(24):
            t_start = midnight + timedelta(hours=h, microseconds=123456)
            t_end = t_start + timedelta(hours=1)
            recs.append(
                HourlyRecommendation(
                    start=t_start,
                    end=t_end,
                    avg_house_consumption_kwh=0.0,
                    avg_house_consumption_1d_kwh=0.0,
                    avg_house_consumption_3d_kwh=0.0,
                    avg_house_consumption_7d_kwh=0.0,
                    avg_house_consumption_14d_kwh=0.0,
                    batteries_charged_kwh=0.0,
                    batteries_discharged_kwh=0.0,
                    estimated_battery_capacity_kwh=0.0,
                    estimated_battery_soc_pct=0,
                    estimated_cost_currency=0.0,
                    estimated_net_consumption_kwh=0.0,
                    ev_planned_load_kwh=0.0,
                    export_price=0.0,
                    grid_export_kwh=0.0,
                    grid_import_kwh=0.0,
                    import_price=0.0,
                    recommendation=None,
                    solcast_pv_estimate_kwh=0.0,
                )
            )

        # Planner slots have zero microseconds
        slots = []
        for h in range(24):
            t_start = midnight + timedelta(hours=h)  # microsecond=0
            t_end = t_start + timedelta(hours=1)
            ev = 2.2 if h == 8 else 0.0
            slots.append(
                PlannedSlot(
                    start=t_start,
                    end=t_end,
                    price=SlotPrice(import_price=0.20, export_price=0.05),
                    ev_planned_load_kwh=ev,
                    estimated_net_consumption_kwh=round(1.0 + ev - 0.5, 3),
                    recommendation="batteries_wait_mode",
                )
            )

        coord = make_bare_coordinator()
        coord._batteries_schedules = []
        coord._hourly_recommendations = recs

        coord._apply_planner_output(PlannerOutput(slots=slots))

        rec_8 = next(r for r in recs if r.start.hour == 8)
        assert abs(rec_8.ev_planned_load_kwh - 2.2) < 1e-9, (
            f"Microsecond mismatch not healed: ev_planned_load_kwh={rec_8.ev_planned_load_kwh}, "
            f"rec.start.microsecond={rec_8.start.microsecond}"
        )

    # ------------------------------------------------------------------
    # Warning on unmatched slots
    # ------------------------------------------------------------------

    def test_warning_emitted_for_unmatched_rec_slot(self):
        """A WARNING must be logged when a rec cannot be matched to any planner slot."""
        import io
        import logging
        from datetime import UTC, datetime, timedelta

        from custom_components.hsem.models.hourly_recommendation import (
            HourlyRecommendation,
        )
        from custom_components.hsem.models.planner_outputs import (
            PlannedSlot,
            PlannerOutput,
        )
        from custom_components.hsem.utils.logger import HSEM_LOGGER
        from custom_components.hsem.utils.prices import SlotPrice

        midnight = datetime(2024, 6, 15, 0, 0, 0, tzinfo=UTC)

        # One rec that has NO matching planner slot
        orphan_rec = HourlyRecommendation(
            start=midnight + timedelta(hours=22),
            end=midnight + timedelta(hours=23),
            avg_house_consumption_kwh=0.0,
            avg_house_consumption_1d_kwh=0.0,
            avg_house_consumption_3d_kwh=0.0,
            avg_house_consumption_7d_kwh=0.0,
            avg_house_consumption_14d_kwh=0.0,
            batteries_charged_kwh=0.0,
            batteries_discharged_kwh=0.0,
            estimated_battery_capacity_kwh=0.0,
            estimated_battery_soc_pct=0,
            estimated_cost_currency=0.0,
            estimated_net_consumption_kwh=0.0,
            ev_planned_load_kwh=0.0,
            export_price=0.0,
            grid_export_kwh=0.0,
            grid_import_kwh=0.0,
            import_price=0.0,
            recommendation=None,
            solcast_pv_estimate_kwh=0.0,
        )

        # Planner only covers hours 0-21 (22 is missing)
        slots = [
            PlannedSlot(
                start=midnight + timedelta(hours=h),
                end=midnight + timedelta(hours=h + 1),
                price=SlotPrice(import_price=0.20, export_price=0.05),
            )
            for h in range(22)
        ]

        coord = make_bare_coordinator()
        coord._batteries_schedules = []
        coord._hourly_recommendations = [orphan_rec]

        # Capture WARNING from HSEM_LOGGER directly (propagation is False)
        capture = io.StringIO()
        handler = logging.StreamHandler(capture)
        handler.setLevel(logging.WARNING)
        HSEM_LOGGER.addHandler(handler)
        try:
            coord._apply_planner_output(PlannerOutput(slots=slots))
            output = capture.getvalue()
        finally:
            HSEM_LOGGER.removeHandler(handler)

        assert any(word in output.lower() for word in ("unmatched", "no matching")), (
            "Expected a WARNING about unmatched slots but found none. "
            f"Logged output: {output}"
        )

    def test_unmatched_rec_fields_stay_at_default(self):
        """An unmatched rec must not have its fields mutated — they stay at 0.0."""
        from datetime import UTC, datetime, timedelta

        from custom_components.hsem.models.hourly_recommendation import (
            HourlyRecommendation,
        )
        from custom_components.hsem.models.planner_outputs import (
            PlannedSlot,
            PlannerOutput,
        )
        from custom_components.hsem.utils.prices import SlotPrice

        midnight = datetime(2024, 6, 15, 0, 0, 0, tzinfo=UTC)

        orphan = HourlyRecommendation(
            start=midnight + timedelta(hours=23),
            end=midnight + timedelta(hours=24),
            avg_house_consumption_kwh=0.0,
            avg_house_consumption_1d_kwh=0.0,
            avg_house_consumption_3d_kwh=0.0,
            avg_house_consumption_7d_kwh=0.0,
            avg_house_consumption_14d_kwh=0.0,
            batteries_charged_kwh=0.0,
            batteries_discharged_kwh=0.0,
            estimated_battery_capacity_kwh=0.0,
            estimated_battery_soc_pct=0,
            estimated_cost_currency=0.0,
            estimated_net_consumption_kwh=0.0,
            ev_planned_load_kwh=0.0,
            export_price=0.0,
            grid_export_kwh=0.0,
            grid_import_kwh=0.0,
            import_price=0.0,
            recommendation=None,
            solcast_pv_estimate_kwh=0.0,
        )

        # Planner has no slot at hour 23
        slots = [
            PlannedSlot(
                start=midnight + timedelta(hours=h),
                end=midnight + timedelta(hours=h + 1),
                price=SlotPrice(import_price=0.20, export_price=0.05),
                ev_planned_load_kwh=9.9,
                recommendation="batteries_charge_grid",
            )
            for h in range(23)
        ]

        coord = make_bare_coordinator()
        coord._batteries_schedules = []
        coord._hourly_recommendations = [orphan]

        coord._apply_planner_output(PlannerOutput(slots=slots))

        assert orphan.ev_planned_load_kwh == 0.0
        assert orphan.recommendation is None

    # ------------------------------------------------------------------
    # 15-minute intervals
    # ------------------------------------------------------------------

    def test_ev_load_survives_15min_interval(self):
        """With 15-minute slots, ev_planned_load_kwh must be copied to all 4 sub-slots."""
        coord = self._make_coord_with_recs(interval_minutes=15, total_hours=24)
        output = self._make_output_from_recs(coord._hourly_recommendations, {14: 1.85})

        coord._apply_planner_output(output)

        hour14_recs = [r for r in coord._hourly_recommendations if r.start.hour == 14]
        assert (
            len(hour14_recs) == 4
        ), f"Expected 4 × 15-min slots, got {len(hour14_recs)}"
        for rec in hour14_recs:
            assert (
                abs(rec.ev_planned_load_kwh - 1.85) < 1e-9
            ), f"15-min slot {rec.start}: expected 1.85, got {rec.ev_planned_load_kwh}"

    def test_non_ev_hours_zero_at_15min_interval(self):
        """15-minute slots outside the EV hour must remain at 0."""
        coord = self._make_coord_with_recs(interval_minutes=15, total_hours=24)
        output = self._make_output_from_recs(coord._hourly_recommendations, {14: 1.85})

        coord._apply_planner_output(output)

        non_ev_recs = [r for r in coord._hourly_recommendations if r.start.hour != 14]
        for rec in non_ev_recs:
            assert abs(rec.ev_planned_load_kwh) < 1e-9

    # ------------------------------------------------------------------
    # utc_key helper
    # ------------------------------------------------------------------

    def test_utc_key_strips_microseconds(self):
        """_utc_key must produce identical keys for datetimes differing only in microseconds."""
        from datetime import UTC, datetime

        t1 = datetime(2024, 6, 15, 14, 0, 0, microsecond=0, tzinfo=UTC)
        t2 = datetime(2024, 6, 15, 14, 0, 0, microsecond=999999, tzinfo=UTC)

        assert utc_key(t1) == utc_key(t2)

    def test_utc_key_normalises_across_timezone_types(self):
        """_utc_key must return equal keys for ZoneInfo and fixed-offset at the same instant."""
        from datetime import datetime, timedelta, timezone
        from zoneinfo import ZoneInfo

        tz_zone = ZoneInfo("Europe/Copenhagen")
        tz_fixed = timezone(timedelta(hours=2))

        t_zone = datetime(2024, 6, 15, 14, 0, 0, tzinfo=tz_zone)
        t_fixed = datetime(2024, 6, 15, 14, 0, 0, tzinfo=tz_fixed)

        assert utc_key(t_zone) == utc_key(t_fixed)

    # ------------------------------------------------------------------
    # base_load_includes_ev=True: ev_accounted and ev_total must be copied
    # ------------------------------------------------------------------

    def test_ev_accounted_and_total_copied_when_base_includes_ev(self):
        """When base_load_includes_ev=True, ev_planned_load_kwh is 0 but
        ev_accounted_load_kwh and ev_total_planned_load_kwh must be > 0 and
        must be correctly propagated to HourlyRecommendation by _apply_planner_output.

        This is the key regression test for the runtime issue: the planner sets
        ev_total_planned_load_kwh on the slot, but unless _apply_planner_output
        copies it, hourly_recommendations will always show 0.
        """
        coord = self._make_coord_with_recs()

        # Simulate base_load_includes_ev=True: ev_planned_load_kwh=0,
        # ev_accounted_load_kwh=5.5, ev_total_planned_load_kwh=5.5
        output = self._make_output_from_recs(
            coord._hourly_recommendations,
            ev_load_by_hour={10: 0.0},  # zero — already in base load
            ev_accounted_by_hour={10: 5.5},
            ev_total_by_hour={10: 5.5},
        )

        coord._apply_planner_output(output)

        rec_10 = next(r for r in coord._hourly_recommendations if r.start.hour == 10)

        # ev_planned_load_kwh must be 0 (not injected into net consumption)
        assert rec_10.ev_planned_load_kwh == pytest.approx(
            0.0
        ), f"ev_planned_load_kwh should be 0 (base includes EV), got {rec_10.ev_planned_load_kwh}"
        # ev_accounted_load_kwh must be > 0 (EV load is planned but already in base)
        assert rec_10.ev_accounted_load_kwh == pytest.approx(5.5), (
            f"ev_accounted_load_kwh should be 5.5, got {rec_10.ev_accounted_load_kwh}. "
            "_apply_planner_output may not be copying this field."
        )
        # ev_total_planned_load_kwh must equal ev_accounted (since injected is 0)
        assert rec_10.ev_total_planned_load_kwh == pytest.approx(5.5), (
            f"ev_total_planned_load_kwh should be 5.5, got {rec_10.ev_total_planned_load_kwh}. "
            "_apply_planner_output may not be copying this field."
        )

    def test_all_three_ev_fields_stay_zero_for_non_ev_hours(self):
        """Non-EV hours must have all three EV fields at 0.0 after apply."""
        coord = self._make_coord_with_recs()
        output = self._make_output_from_recs(
            coord._hourly_recommendations,
            ev_load_by_hour={10: 0.0},
            ev_accounted_by_hour={10: 3.3},
            ev_total_by_hour={10: 3.3},
        )
        coord._apply_planner_output(output)

        for rec in coord._hourly_recommendations:
            if rec.start.hour != 10:
                assert rec.ev_planned_load_kwh == pytest.approx(0.0)
                assert rec.ev_accounted_load_kwh == pytest.approx(0.0)
                assert rec.ev_total_planned_load_kwh == pytest.approx(0.0)

    def test_ev_total_invariant_holds_in_recs_after_apply(self):
        """ev_total_planned_load_kwh == ev_planned_load_kwh + ev_accounted_load_kwh
        must hold in every HourlyRecommendation after _apply_planner_output.
        """
        coord = self._make_coord_with_recs()
        # Partially injected: ev_planned=2.0, ev_accounted=3.5, ev_total=5.5
        output = self._make_output_from_recs(
            coord._hourly_recommendations,
            ev_load_by_hour={10: 2.0},
            ev_accounted_by_hour={10: 3.5},
            ev_total_by_hour={10: 5.5},
        )
        coord._apply_planner_output(output)

        for rec in coord._hourly_recommendations:
            assert rec.ev_total_planned_load_kwh == pytest.approx(
                rec.ev_planned_load_kwh + rec.ev_accounted_load_kwh, abs=1e-9
            ), (
                f"Hour {rec.start.hour}: ev_total ({rec.ev_total_planned_load_kwh}) "
                f"!= ev_planned ({rec.ev_planned_load_kwh}) "
                f"+ ev_accounted ({rec.ev_accounted_load_kwh})"
            )


# ---------------------------------------------------------------------------
# TestEvFieldsEndToEnd — full run_planner + _apply_planner_output pipeline
# Proves that ev_accounted_load_kwh and ev_total_planned_load_kwh from the
# planner engine reach the final HourlyRecommendation objects.
# ---------------------------------------------------------------------------


class TestEvFieldsEndToEnd:
    """Full pipeline test: run_planner with base_load_includes_ev=True then
    _apply_planner_output — proves the three EV load fields propagate all
    the way to hourly_recommendations as they would in production.

    This is the real regression guard for the runtime issue: even though
    ev_planned_load_kwh=0 when base_load_includes_ev=True, the other two
    fields must be visible in hourly_recommendations.
    """

    def _run_end_to_end(self, base_includes_ev: bool):
        """Run planner + apply_planner_output and return (coordinator, output)."""
        from datetime import datetime, timedelta

        from custom_components.hsem.models.hourly_recommendation import (
            HourlyRecommendation,
        )
        from custom_components.hsem.models.planner_inputs import (
            HourlyConsumptionAverage,
            PlannerInput,
            PricePoint,
            SolcastSlot,
        )
        from custom_components.hsem.planner import run_planner

        now_iso = "2024-06-15T06:00:00+00:00"
        now = datetime.fromisoformat(now_iso)
        deadline = now + timedelta(hours=6)

        prices = [
            PricePoint(hour=h, import_price=0.20, export_price=0.05) for h in range(24)
        ]
        pv = [SolcastSlot(hour=h, pv_estimate=0.0) for h in range(24)]
        avgs = [
            HourlyConsumptionAverage(
                hour=h, avg_1d=2.0, avg_3d=2.0, avg_7d=2.0, avg_14d=2.0
            )
            for h in range(24)
        ]

        inp = PlannerInput(
            now_iso=now_iso,
            interval_minutes=60,
            interval_length_hours=24,
            battery_soc_pct=50.0,
            battery_rated_capacity_kwh=10.0,
            battery_end_of_discharge_soc_pct=10.0,
            battery_max_soc_pct=90.0,
            battery_max_charge_power_w=5000.0,
            battery_max_discharge_power_w=5000.0,
            battery_charge_efficiency_pct=100.0,
            battery_discharge_efficiency_pct=100.0,
            weight_1d=25,
            weight_3d=30,
            weight_7d=30,
            weight_14d=15,
            consumption_averages=avgs,
            price_points=prices,
            solcast_slots=pv,
            ev_planned_load_enabled=True,
            ev_planned_load_connected=True,
            ev_planned_load_smart_charging_enabled=True,
            ev_planned_load_current_soc_pct=0.0,
            ev_planned_load_target_soc_pct=5.0,  # 5 kWh needed
            ev_planned_load_battery_capacity_kwh=100.0,
            ev_planned_load_charger_power_kw=11.0,
            ev_planned_load_charger_efficiency_pct=100.0,
            ev_planned_load_deadline=deadline,
            ev_planned_load_base_load_includes_ev=base_includes_ev,
        )

        planner_output = run_planner(inp)

        # Build coordinator with matching hourly_recommendations
        coord = make_bare_coordinator()
        coord._batteries_schedules = []
        # Generate recs aligned to planner slot starts
        coord._hourly_recommendations = [
            HourlyRecommendation(
                start=s.start,
                end=s.end,
                avg_house_consumption_kwh=0.0,
                avg_house_consumption_1d_kwh=0.0,
                avg_house_consumption_3d_kwh=0.0,
                avg_house_consumption_7d_kwh=0.0,
                avg_house_consumption_14d_kwh=0.0,
                batteries_charged_kwh=0.0,
                batteries_discharged_kwh=0.0,
                estimated_battery_capacity_kwh=0.0,
                estimated_battery_soc_pct=0.0,
                estimated_cost_currency=0.0,
                estimated_net_consumption_kwh=0.0,
                ev_planned_load_kwh=0.0,
                export_price=0.0,
                grid_export_kwh=0.0,
                grid_import_kwh=0.0,
                import_price=0.0,
                recommendation=None,
                solcast_pv_estimate_kwh=0.0,
            )
            for s in planner_output.slots
        ]

        coord._apply_planner_output(planner_output)
        return coord, planner_output

    def test_base_excludes_ev_ev_planned_nonzero_in_recs(self):
        """When base_load_includes_ev=False, ev_planned_load_kwh must be > 0
        in HourlyRecommendation for charging slots after _apply_planner_output.
        """
        coord, output = self._run_end_to_end(base_includes_ev=False)

        total_injected = sum(
            r.ev_planned_load_kwh for r in coord._hourly_recommendations
        )
        assert total_injected > 1e-9, (
            "base_load_includes_ev=False: ev_planned_load_kwh should be > 0 in recs. "
            f"Got {total_injected:.3f}. EV load not reaching hourly_recommendations."
        )

    def test_base_includes_ev_ev_planned_zero_but_total_nonzero_in_recs(self):
        """When base_load_includes_ev=True, hourly_recommendations must show:
        - ev_planned_load_kwh == 0 (not injected into net consumption)
        - ev_accounted_load_kwh > 0 (EV is planned but already in base load)
        - ev_total_planned_load_kwh > 0 (total always visible)

        This is the PRIMARY regression test for the runtime issue.
        """
        coord, output = self._run_end_to_end(base_includes_ev=True)

        # Verify planner produced non-zero ev_total in slots first
        total_ev_total_in_slots = sum(s.ev_total_planned_load_kwh for s in output.slots)
        assert total_ev_total_in_slots > 1e-9, (
            "Planner produced no ev_total_planned_load_kwh > 0 in slots — "
            "check EV plan state and surplus calculation."
        )

        # Now verify these fields made it through to HourlyRecommendation
        total_injected_in_recs = sum(
            r.ev_planned_load_kwh for r in coord._hourly_recommendations
        )
        total_accounted_in_recs = sum(
            r.ev_accounted_load_kwh for r in coord._hourly_recommendations
        )
        total_ev_total_in_recs = sum(
            r.ev_total_planned_load_kwh for r in coord._hourly_recommendations
        )

        assert total_injected_in_recs == pytest.approx(0.0), (
            f"base_load_includes_ev=True: ev_planned_load_kwh should be 0 "
            f"in all recs, got {total_injected_in_recs:.3f}"
        )
        assert total_accounted_in_recs > 1e-9, (
            f"base_load_includes_ev=True: ev_accounted_load_kwh should be > 0 "
            f"in recs but got {total_accounted_in_recs:.3f}. "
            "_apply_planner_output is not copying ev_accounted_load_kwh."
        )
        assert total_ev_total_in_recs > 1e-9, (
            f"base_load_includes_ev=True: ev_total_planned_load_kwh should be > 0 "
            f"in recs but got {total_ev_total_in_recs:.3f}. "
            "_apply_planner_output is not copying ev_total_planned_load_kwh. "
            "This is the runtime regression: EV plan is invisible to dashboard."
        )

    def test_ev_total_equals_accounted_when_base_includes_ev(self):
        """When base_load_includes_ev=True, ev_total == ev_accounted in every rec
        (since ev_planned is 0).
        """
        coord, _ = self._run_end_to_end(base_includes_ev=True)

        for rec in coord._hourly_recommendations:
            assert rec.ev_total_planned_load_kwh == pytest.approx(
                rec.ev_accounted_load_kwh, abs=1e-9
            ), (
                f"Hour {rec.start.hour}: ev_total ({rec.ev_total_planned_load_kwh}) "
                f"!= ev_accounted ({rec.ev_accounted_load_kwh}) "
                "when base_load_includes_ev=True and ev_planned=0"
            )

    def test_ev_total_invariant_throughout_pipeline(self):
        """ev_total == ev_planned + ev_accounted must hold in every rec."""
        for base_includes in (False, True):
            coord, _ = self._run_end_to_end(base_includes_ev=base_includes)
            for rec in coord._hourly_recommendations:
                assert rec.ev_total_planned_load_kwh == pytest.approx(
                    rec.ev_planned_load_kwh + rec.ev_accounted_load_kwh, abs=1e-9
                ), (
                    f"base_includes={base_includes}, hour {rec.start.hour}: "
                    f"ev_total ({rec.ev_total_planned_load_kwh}) != "
                    f"ev_planned ({rec.ev_planned_load_kwh}) + "
                    f"ev_accounted ({rec.ev_accounted_load_kwh})"
                )


# ---------------------------------------------------------------------------
# TestEvSlotKeyNormalisation — UTC-normalised key matching in ev_planner
# Proves apply_ev_planned_load_to_slots uses UTC-normalised keys so that
# timezone-representation mismatches don't silently drop EV load.
# ---------------------------------------------------------------------------


class TestEvSlotKeyNormalisation:
    """apply_ev_planned_load_to_slots must match slots by UTC instant, not
    by isoformat() string.  Two datetimes at the same instant with different
    tzinfo representations must produce the same match.
    """

    def test_fixed_offset_ev_slot_matches_utc_planner_slot(self):
        """EV slot with fixed +02:00 offset must inject into a UTC planner slot
        at the same instant.

        This would silently fail with isoformat() comparison because
        '2024-06-15T10:00:00+02:00' != '2024-06-15T08:00:00+00:00'
        even though they are the same instant.
        """
        from datetime import UTC, datetime, timedelta, timezone

        from custom_components.hsem.planner.ev_planner import (
            EVChargingPlan,
            EVChargingSlot,
            apply_ev_planned_load_to_slots,
        )

        tz_fixed = timezone(timedelta(hours=2))
        # Planner slot starts in UTC
        utc_start = datetime(2024, 6, 15, 8, 0, 0, tzinfo=UTC)
        # EV slot carries the same instant but with +02:00 tzinfo
        ev_start = datetime(2024, 6, 15, 10, 0, 0, tzinfo=tz_fixed)

        plan = EVChargingPlan()
        plan.state = "charging"
        ev_slot = EVChargingSlot(
            start=ev_start,
            end=ev_start + timedelta(hours=1),
            estimated_charged_kwh=3.0,
            ac_load_kwh=3.0,
        )
        plan.charging_slots.append(ev_slot)

        result = [0.0] * 3
        slot_starts = [
            utc_start - timedelta(hours=1),  # 07:00 UTC
            utc_start,  # 08:00 UTC = 10:00 +02:00
            utc_start + timedelta(hours=1),  # 09:00 UTC
        ]

        apply_ev_planned_load_to_slots(
            slot_starts, result, plan, base_load_includes_ev=False
        )

        assert result[1] == pytest.approx(3.0), (
            f"UTC-normalised slot key failed: expected 3.0 at index 1, "
            f"got {result[1]}. Fixed-offset EV slot start did not match UTC "
            f"planner slot start at the same instant."
        )
        assert result[0] == pytest.approx(0.0)
        assert result[2] == pytest.approx(0.0)

    def test_zoninfo_ev_slot_matches_fixed_offset_planner_slot(self):
        """EV slot with ZoneInfo tzinfo must inject into a fixed-offset planner slot
        at the same instant.
        """
        from datetime import datetime, timedelta, timezone
        from zoneinfo import ZoneInfo

        from custom_components.hsem.planner.ev_planner import (
            EVChargingPlan,
            EVChargingSlot,
            apply_ev_planned_load_to_slots,
        )

        tz_zone = ZoneInfo("Europe/Copenhagen")
        tz_fixed = timezone(timedelta(hours=2))

        # Planner slots use fixed offset
        slot_starts = [
            datetime(2024, 6, 15, h, 0, 0, tzinfo=tz_fixed) for h in range(24)
        ]
        # EV slot carries ZoneInfo at hour 10
        ev_start = datetime(2024, 6, 15, 10, 0, 0, tzinfo=tz_zone)

        plan = EVChargingPlan()
        plan.state = "charging"
        ev_slot = EVChargingSlot(
            start=ev_start,
            end=ev_start + timedelta(hours=1),
            estimated_charged_kwh=4.5,
            ac_load_kwh=4.5,
        )
        plan.charging_slots.append(ev_slot)

        result = [0.0] * 24
        apply_ev_planned_load_to_slots(
            slot_starts, result, plan, base_load_includes_ev=False
        )

        assert result[10] == pytest.approx(4.5), (
            f"ZoneInfo EV slot did not match fixed-offset planner slot: "
            f"expected 4.5 at index 10, got {result[10]}."
        )
        for i, v in enumerate(result):
            if i != 10:
                assert v == pytest.approx(0.0), f"Unexpected load at index {i}: {v}"

    def test_isoformat_mismatch_would_fail_without_utc_normalisation(self):
        """Document that isoformat()-based matching is fragile by showing
        two equal instants produce different isoformat strings.
        """
        from datetime import UTC, datetime, timedelta, timezone

        utc_dt = datetime(2024, 6, 15, 8, 0, 0, tzinfo=UTC)
        fixed_dt = datetime(2024, 6, 15, 10, 0, 0, tzinfo=timezone(timedelta(hours=2)))

        # Same instant, different isoformat strings — this is why we use UTC keys
        assert (
            utc_dt.isoformat() != fixed_dt.isoformat()
        ), "Test setup error: these should produce different isoformat strings"
        # But they should be equal as UTC-normalised datetimes (using coordinator._utc_key)
        assert utc_key(utc_dt) == utc_key(
            fixed_dt
        ), "utc_key must normalise timezone-representation mismatches"
