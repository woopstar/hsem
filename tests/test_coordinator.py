"""Tests for HSEMDataUpdateCoordinator (issue #283).

Acceptance criteria:
- Data is fetched once per interval (single coordinator, not per entity).
- Entities do not independently fetch the same data.
- Coordinator exposes last update status via coordinator.last_update_success.
- Update lock prevents concurrent pipeline executions.
- CoordinatorData contains a consistent snapshot after each cycle.
- async_setup registers timers; async_teardown cancels them.
- async_options_updated triggers a fresh pipeline cycle.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from custom_components.hsem.coordinator import (
    CoordinatorData,
    HSEMDataUpdateCoordinator,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config_entry(**overrides) -> MagicMock:
    """Minimal mock ConfigEntry whose options hold the given overrides."""
    defaults = {
        "hsem_read_only": True,
        "hsem_verbose_logging": False,
        "hsem_extended_attributes": False,
        "hsem_update_interval": 5,
        "hsem_recommendation_interval_minutes": 60,
        "hsem_recommendation_interval_length": 24,
        "hsem_energi_data_service_update_interval": 60,
        "hsem_months_winter": [],
        "hsem_months_summer": [],
        "hsem_huawei_solar_device_id_inverter_1": "inv1",
        "hsem_huawei_solar_device_id_inverter_2": "",
        "hsem_huawei_solar_device_id_batteries": "bat1",
        "hsem_huawei_solar_batteries_working_mode": "sensor.wm",
        "hsem_huawei_solar_batteries_end_of_discharge_soc": "sensor.eod",
        "hsem_huawei_solar_batteries_state_of_capacity": "sensor.soc",
        "hsem_huawei_solar_batteries_grid_charge_cutoff_soc": "sensor.gc",
        "hsem_huawei_solar_batteries_maximum_charging_power": "sensor.mcp",
        "hsem_huawei_solar_batteries_maximum_discharging_power": "sensor.mdp",
        "hsem_huawei_solar_batteries_tou_charging_and_discharging_periods": "sensor.tou",
        "hsem_huawei_solar_batteries_excess_pv_energy_use_in_tou": "select.excess",
        "hsem_huawei_solar_inverter_active_power_control": "sensor.apc",
        "hsem_huawei_solar_batteries_rated_capacity": "sensor.rc",
        "hsem_house_consumption_power": "sensor.house",
        "hsem_solar_production_power": "sensor.solar",
        "hsem_house_power_includes_ev_charger_power": False,
        "hsem_solcast_pv_forecast_forecast_today": "sensor.sc_today",
        "hsem_solcast_pv_forecast_forecast_tomorrow": "sensor.sc_tom",
        "hsem_solcast_pv_forecast_forecast_likelihood": "pv_estimate",
        "hsem_energi_data_service_import": "sensor.eds_import",
        "hsem_energi_data_service_export": "sensor.eds_export",
        "hsem_energi_data_service_export_min_price": 0.05,
        "hsem_ev_charger_status": None,
        "hsem_ev_charger_power": None,
        "hsem_ev_soc": None,
        "hsem_ev_soc_target": None,
        "hsem_ev_connected": None,
        "hsem_ev_allow_charge_past_target_soc": False,
        "hsem_ev_charger_force_max_discharge_power": False,
        "hsem_ev_charger_max_discharge_power": 0,
        "hsem_ev_second_charger_status": None,
        "hsem_ev_second_charger_power": None,
        "hsem_ev_second_soc": None,
        "hsem_ev_second_soc_target": None,
        "hsem_ev_second_connected": None,
        "hsem_ev_second_allow_charge_past_target_soc": False,
        "hsem_ev_second_charger_force_max_discharge_power": False,
        "hsem_ev_second_charger_max_discharge_power": 0,
        "hsem_ev_second_enabled": False,
        "hsem_batteries_conversion_loss": 5.0,
        "hsem_batteries_purchase_price": 8000.0,
        "hsem_batteries_expected_cycles": 6000,
        "hsem_house_consumption_energy_weight_1d": 25,
        "hsem_house_consumption_energy_weight_3d": 30,
        "hsem_house_consumption_energy_weight_7d": 30,
        "hsem_house_consumption_energy_weight_14d": 15,
        "hsem_batteries_rated_capacity_min_factor": 0.8,
        "hsem_batteries_enable_excess_export": False,
        "hsem_batteries_excess_export_discharge_buffer": 10.0,
        "hsem_batteries_excess_export_price_threshold": 0.1,
        "hsem_batteries_schedule_1_enabled": False,
        "hsem_batteries_schedule_1_start": "00:00",
        "hsem_batteries_schedule_1_end": "06:00",
        "hsem_batteries_schedule_1_min_price_difference": 0.1,
        "hsem_batteries_schedule_2_enabled": False,
        "hsem_batteries_schedule_2_start": "12:00",
        "hsem_batteries_schedule_2_end": "16:00",
        "hsem_batteries_schedule_2_min_price_difference": 0.1,
        "hsem_batteries_schedule_3_enabled": False,
        "hsem_batteries_schedule_3_start": "20:00",
        "hsem_batteries_schedule_3_end": "23:00",
        "hsem_batteries_schedule_3_min_price_difference": 0.1,
    }
    defaults.update(overrides)
    entry = MagicMock()
    entry.options = defaults
    entry.data = {}
    entry.entry_id = "test_entry_id"
    return entry


def _make_hass() -> MagicMock:
    """Return a minimal hass mock sufficient for coordinator construction."""
    hass = MagicMock()
    hass.data = {}
    hass.loop = asyncio.get_event_loop()
    hass.async_create_task = MagicMock()
    return hass


# ---------------------------------------------------------------------------
# CoordinatorData unit tests
# ---------------------------------------------------------------------------


class TestCoordinatorData:
    """Verify the CoordinatorData dataclass defaults and field types."""

    def test_default_instance_has_no_live_state(self) -> None:
        """A fresh CoordinatorData must have live=None."""
        data = CoordinatorData()
        assert data.live is None
        assert data.cfg is None
        assert data.state is None
        assert data.last_updated is None
        assert data.next_update is None

    def test_empty_list_fields_are_mutable(self) -> None:
        """List fields must be independent instances (not shared default)."""
        d1 = CoordinatorData()
        d2 = CoordinatorData()
        d1.hourly_recommendations.append("x")
        assert "x" not in d2.hourly_recommendations

    def test_numeric_fields_default_to_zero(self) -> None:
        """Numeric accumulator fields must default to 0.0."""
        data = CoordinatorData()
        assert data.batteries_schedules_remaining_capacity_needed == pytest.approx(0.0)
        assert data.current_required_battery == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Coordinator construction tests
# ---------------------------------------------------------------------------


class TestCoordinatorConstruction:
    """Verify coordinator is constructed correctly and owns the update lock."""

    def test_update_lock_is_asyncio_lock(self) -> None:
        """The coordinator must own an asyncio.Lock for concurrent-update protection."""
        config_entry = _make_config_entry()
        hass = _make_hass()
        coordinator = HSEMDataUpdateCoordinator(hass, config_entry)
        assert isinstance(coordinator._update_lock, asyncio.Lock)

    def test_initial_data_is_none(self) -> None:
        """Coordinator.data must be None before the first cycle completes."""
        config_entry = _make_config_entry()
        hass = _make_hass()
        coordinator = HSEMDataUpdateCoordinator(hass, config_entry)
        assert coordinator.data is None

    def test_timer_handles_start_as_none(self) -> None:
        """Timer unsub handles must be None before async_setup is called."""
        config_entry = _make_config_entry()
        hass = _make_hass()
        coordinator = HSEMDataUpdateCoordinator(hass, config_entry)
        assert coordinator._interval_timer_unsub is None
        assert coordinator._hourly_timer_unsub is None


# ---------------------------------------------------------------------------
# Concurrent update lock tests
# ---------------------------------------------------------------------------


class _StubCoordinator:
    """Minimal stub that replaces only the locking behaviour of HSEMDataUpdateCoordinator."""

    def __init__(self) -> None:
        self._update_lock = asyncio.Lock()
        self._cycle_count = 0
        self._skip_count = 0
        self._cfg = MagicMock()
        self._cfg.verbose_logging = False

    async def _async_handle_update(self, event=None) -> None:
        """Identical guard logic to the production coordinator."""
        if self._update_lock.locked():
            self._skip_count += 1
            return
        async with self._update_lock:
            await self._async_run_update_cycle()

    async def _async_run_update_cycle(self) -> None:
        """Simulated slow cycle (2 event-loop ticks)."""
        self._cycle_count += 1
        await asyncio.sleep(0)
        await asyncio.sleep(0)


class TestCoordinatorUpdateLock:
    """Verify the asyncio.Lock guard inside _async_handle_update."""

    @pytest.mark.asyncio
    async def test_single_call_runs_once(self) -> None:
        """A lone call executes exactly one cycle."""
        coord = _StubCoordinator()
        await coord._async_handle_update()
        assert coord._cycle_count == 1
        assert coord._skip_count == 0

    @pytest.mark.asyncio
    async def test_concurrent_second_call_is_dropped(self) -> None:
        """While a cycle is running a second concurrent call is skipped, not queued."""
        coord = _StubCoordinator()
        await asyncio.gather(
            coord._async_handle_update(),
            coord._async_handle_update(),
        )
        assert coord._cycle_count == 1, f"Expected 1 cycle, got {coord._cycle_count}"
        assert coord._skip_count == 1, f"Expected 1 skip, got {coord._skip_count}"

    @pytest.mark.asyncio
    async def test_sequential_calls_both_execute(self) -> None:
        """Two non-overlapping sequential calls both run the cycle."""
        coord = _StubCoordinator()
        await coord._async_handle_update()
        await coord._async_handle_update()
        assert coord._cycle_count == 2


# ---------------------------------------------------------------------------
# Coordinator async_teardown
# ---------------------------------------------------------------------------


class TestCoordinatorTeardown:
    """Verify async_teardown cancels all registered timer subscriptions."""

    @pytest.mark.asyncio
    async def test_teardown_cancels_timers(self) -> None:
        """async_teardown must call the unsub callables for both timers."""
        config_entry = _make_config_entry()
        hass = _make_hass()
        coordinator = HSEMDataUpdateCoordinator(hass, config_entry)

        interval_unsub = MagicMock()
        hourly_unsub = MagicMock()
        coordinator._interval_timer_unsub = interval_unsub
        coordinator._hourly_timer_unsub = hourly_unsub

        await coordinator.async_teardown()

        interval_unsub.assert_called_once()
        hourly_unsub.assert_called_once()
        assert coordinator._interval_timer_unsub is None
        assert coordinator._hourly_timer_unsub is None

    @pytest.mark.asyncio
    async def test_teardown_safe_when_no_timers(self) -> None:
        """async_teardown must not raise when no timers were registered."""
        config_entry = _make_config_entry()
        hass = _make_hass()
        coordinator = HSEMDataUpdateCoordinator(hass, config_entry)
        # Both handles are None — no error expected
        await coordinator.async_teardown()


# ---------------------------------------------------------------------------
# Coordinator recommendation interval generation
# ---------------------------------------------------------------------------


class TestGenerateRecommendationIntervals:
    """Verify the recommendation-slot generation helper inside the coordinator."""

    def test_generates_correct_count_for_60min_24h(self) -> None:
        """60-minute slots over 24 hours must produce 24 slots."""
        config_entry = _make_config_entry()
        hass = _make_hass()
        coordinator = HSEMDataUpdateCoordinator(hass, config_entry)
        slots = coordinator._generate_recommendation_intervals(60, 24)
        assert len(slots) == 24

    def test_generates_correct_count_for_15min_48h(self) -> None:
        """15-minute slots over 48 hours must produce 192 slots."""
        config_entry = _make_config_entry()
        hass = _make_hass()
        coordinator = HSEMDataUpdateCoordinator(hass, config_entry)
        slots = coordinator._generate_recommendation_intervals(15, 48)
        assert len(slots) == 192

    def test_slots_start_at_midnight(self) -> None:
        """The first slot must start at midnight of the current day."""
        config_entry = _make_config_entry()
        hass = _make_hass()
        coordinator = HSEMDataUpdateCoordinator(hass, config_entry)
        slots = coordinator._generate_recommendation_intervals(60, 24)
        first = slots[0]
        assert first.start.hour == 0
        assert first.start.minute == 0

    def test_consecutive_slots_are_contiguous(self) -> None:
        """Each slot's end must equal the next slot's start."""
        config_entry = _make_config_entry()
        hass = _make_hass()
        coordinator = HSEMDataUpdateCoordinator(hass, config_entry)
        slots = coordinator._generate_recommendation_intervals(15, 2)
        for i in range(len(slots) - 1):
            assert slots[i].end == slots[i + 1].start

    def test_slots_have_zero_defaults(self) -> None:
        """All numeric fields on a freshly generated slot must be 0.0."""
        config_entry = _make_config_entry()
        hass = _make_hass()
        coordinator = HSEMDataUpdateCoordinator(hass, config_entry)
        slots = coordinator._generate_recommendation_intervals(60, 1)
        slot = slots[0]
        assert slot.import_price == pytest.approx(0.0)
        assert slot.export_price == pytest.approx(0.0)
        assert slot.solcast_pv_estimate == pytest.approx(0.0)
        assert slot.avg_house_consumption == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Single-poll guarantee
# ---------------------------------------------------------------------------


class TestSinglePollGuarantee:
    """Entities must not independently poll; only the coordinator fetches data."""

    def test_working_mode_sensor_should_poll_is_false(self) -> None:
        """HSEMWorkingModeSensor.should_poll must return False."""
        from custom_components.hsem.custom_sensors.working_mode_sensor import (
            HSEMWorkingModeSensor,
        )

        assert HSEMWorkingModeSensor.should_poll.fget is not None  # type: ignore[attr-defined]
        # Instantiate a minimal stub to call the property
        sensor = object.__new__(HSEMWorkingModeSensor)
        # Inject a minimal coordinator mock
        coord = MagicMock()
        coord.last_update_success = False
        coord.data = None
        sensor.coordinator = coord
        assert sensor.should_poll is False

    def test_degraded_mode_sensor_should_poll_is_false(self) -> None:
        """HSEMDegradedModeSensor.should_poll must return False."""
        from custom_components.hsem.custom_sensors.degraded_mode_sensor import (
            HSEMDegradedModeSensor,
        )

        sensor = object.__new__(HSEMDegradedModeSensor)
        coord = MagicMock()
        coord.last_update_success = False
        coord.data = None
        sensor.coordinator = coord
        sensor._restored_state = None
        assert sensor.should_poll is False


# ---------------------------------------------------------------------------
# Coordinator data exposure
# ---------------------------------------------------------------------------


class TestCoordinatorDataExposure:
    """Verify coordinator exposes last_update_success and data correctly."""

    def test_last_update_success_defaults_true(self) -> None:
        """DataUpdateCoordinator initialises last_update_success to True (HA default).

        The coordinator is considered healthy until its first failed cycle, which
        is the standard behaviour for HA's DataUpdateCoordinator base class.
        """
        config_entry = _make_config_entry()
        hass = _make_hass()
        coordinator = HSEMDataUpdateCoordinator(hass, config_entry)
        # HA base class defaults to True — entities read coordinator.data is None
        # to detect "not yet fetched" rather than last_update_success.
        assert coordinator.last_update_success is True

    def test_data_is_none_before_first_cycle(self) -> None:
        """coordinator.data must be None before async_setup is called."""
        config_entry = _make_config_entry()
        hass = _make_hass()
        coordinator = HSEMDataUpdateCoordinator(hass, config_entry)
        assert coordinator.data is None
