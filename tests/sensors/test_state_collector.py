"""Tests for custom_sensors/state_collector.py.

Covers :func:`build_sensor_config` and the private helpers
:func:`_compute_battery_capacities` and :func:`_compute_net_consumption`
which can be exercised without a running Home Assistant instance.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.hsem.custom_sensors.state_collector import (
    _compute_battery_capacities,
    _compute_net_consumption,
    build_battery_schedules,
    build_sensor_config,
)
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config_entry(**overrides) -> MagicMock:
    """Return a minimal mock ConfigEntry whose options contain the given overrides."""
    defaults = {
        "hsem_read_only": False,
        "hsem_verbose_logging": True,
        "hsem_extended_attributes": False,
        "hsem_update_interval": 1,
        "hsem_recommendation_interval_minutes": 15,
        "hsem_recommendation_interval_length": 48,
        "hsem_electricity_price_update_interval": 15,
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
        "hsem_import_electricity_price_sensor": "sensor.eds_import",
        "hsem_export_electricity_price_sensor": "sensor.eds_export",
        "hsem_export_electricity_min_price": 0.05,
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
        "hsem_batteries_conversion_loss": 5.0,
        "hsem_batteries_purchase_price": 8000.0,
        "hsem_batteries_expected_cycles": 6000,
        "hsem_batteries_enable_batteries_schedule_1": False,
        "hsem_batteries_enable_batteries_schedule_1_start": "07:00:00",
        "hsem_batteries_enable_batteries_schedule_1_end": "09:00:00",
        "hsem_batteries_enable_batteries_schedule_1_min_price_difference": 0.0,
        "hsem_batteries_enable_batteries_schedule_2": False,
        "hsem_batteries_enable_batteries_schedule_2_start": "17:00:00",
        "hsem_batteries_enable_batteries_schedule_2_end": "21:00:00",
        "hsem_batteries_enable_batteries_schedule_2_min_price_difference": 0.0,
        "hsem_batteries_enable_batteries_schedule_3": False,
        "hsem_batteries_enable_batteries_schedule_3_start": "07:00:00",
        "hsem_batteries_enable_batteries_schedule_3_end": "09:00:00",
        "hsem_batteries_enable_batteries_schedule_3_min_price_difference": 0.0,
        "hsem_batteries_enable_excess_export": False,
        "hsem_batteries_excess_export_discharge_buffer": 10.0,
        "hsem_batteries_excess_export_price_threshold": 0.10,
        "hsem_house_consumption_energy_weight_1d": 50,
        "hsem_house_consumption_energy_weight_3d": 20,
        "hsem_house_consumption_energy_weight_7d": 15,
        "hsem_house_consumption_energy_weight_14d": 10,
    }
    defaults.update(overrides)

    entry = MagicMock()
    entry.options = defaults
    entry.data = {}
    return entry


# ---------------------------------------------------------------------------
# build_sensor_config
# ---------------------------------------------------------------------------


class TestBuildSensorConfig:
    def test_returns_sensor_config_instance(self):
        cfg = build_sensor_config(_make_config_entry())
        assert isinstance(cfg, SensorConfig)

    def test_read_only_default_false(self):
        cfg = build_sensor_config(_make_config_entry())
        assert cfg.read_only is False

    def test_read_only_true_propagates(self):
        cfg = build_sensor_config(_make_config_entry(hsem_read_only=True))
        assert cfg.read_only is True

    def test_inverter_2_empty_string_becomes_none(self):
        cfg = build_sensor_config(
            _make_config_entry(hsem_huawei_solar_device_id_inverter_2="")
        )
        assert cfg.huawei_solar_device_id_inverter_2 is None

    def test_recommendation_interval_minutes(self):
        cfg = build_sensor_config(
            _make_config_entry(hsem_recommendation_interval_minutes=60)
        )
        assert cfg.recommendation_interval_minutes == 60

    def test_consumption_weights(self):
        cfg = build_sensor_config(
            _make_config_entry(
                hsem_house_consumption_energy_weight_1d=25,
                hsem_house_consumption_energy_weight_3d=30,
                hsem_house_consumption_energy_weight_7d=30,
                hsem_house_consumption_energy_weight_14d=15,
            )
        )
        assert cfg.house_consumption_energy_weight_1d == 25
        assert cfg.house_consumption_energy_weight_3d == 30
        assert cfg.house_consumption_energy_weight_7d == 30
        assert cfg.house_consumption_energy_weight_14d == 15
        assert cfg.weights_sum() == 100

    def test_schedule_1_propagates(self):
        cfg = build_sensor_config(
            _make_config_entry(
                hsem_batteries_enable_batteries_schedule_1=True,
                hsem_batteries_enable_batteries_schedule_1_start="06:00:00",
                hsem_batteries_enable_batteries_schedule_1_end="09:00:00",
            )
        )
        assert cfg.batteries_schedule_1.enabled is True

    def test_months_winter_list_converted(self):
        # convert_months_to_int accepts numeric strings like '1', '2'
        cfg = build_sensor_config(_make_config_entry(hsem_months_winter=["1", "2"]))
        assert 1 in cfg.months_winter
        assert 2 in cfg.months_winter

    def test_excess_export_settings(self):
        cfg = build_sensor_config(
            _make_config_entry(
                hsem_batteries_enable_excess_export=True,
                hsem_batteries_excess_export_discharge_buffer=15.0,
                hsem_batteries_excess_export_price_threshold=0.20,
            )
        )
        assert cfg.batteries_enable_excess_export is True
        assert cfg.batteries_excess_export_discharge_buffer == pytest.approx(15.0)
        assert cfg.batteries_excess_export_price_threshold == pytest.approx(0.20)


# ---------------------------------------------------------------------------
# _compute_battery_capacities
# ---------------------------------------------------------------------------


class TestComputeBatteryCapacities:
    def _make_live(
        self, rated_wh: float, soc_pct: float, eod_pct: float = 5.0
    ) -> LiveState:
        live = LiveState()
        live.huawei_batteries_rated_capacity_wh = rated_wh
        live.huawei_batteries_soc_pct = soc_pct
        live.huawei_batteries_end_of_discharge_soc_pct = eod_pct
        return live

    def test_full_battery(self):
        live = self._make_live(rated_wh=10_000, soc_pct=100.0, eod_pct=5.0)
        _compute_battery_capacities(live)
        # usable = 10 kWh - 5% reserve = 9.5 kWh
        assert live.battery_usable_capacity_kwh == pytest.approx(9.5, abs=0.01)
        # current = 100% of 10 kWh - 0.5 kWh reserve = 9.5 kWh
        assert live.battery_current_capacity_kwh == pytest.approx(9.5, abs=0.01)

    def test_half_battery(self):
        live = self._make_live(rated_wh=10_000, soc_pct=50.0, eod_pct=10.0)
        _compute_battery_capacities(live)
        # usable = 9 kWh, current = 5 kWh - 1 kWh reserve = 4 kWh
        assert live.battery_usable_capacity_kwh == pytest.approx(9.0, abs=0.01)
        assert live.battery_current_capacity_kwh == pytest.approx(4.0, abs=0.01)

    def test_below_discharge_floor(self):
        live = self._make_live(rated_wh=10_000, soc_pct=3.0, eod_pct=5.0)
        _compute_battery_capacities(live)
        # current available = max(3% - 5%, 0) = 0
        assert live.battery_current_capacity_kwh == pytest.approx(0.0)

    def test_missing_soc_no_update(self):
        live = LiveState()
        live.huawei_batteries_rated_capacity_wh = 10_000
        live.huawei_batteries_soc_pct = None
        _compute_battery_capacities(live)
        assert live.battery_usable_capacity_kwh == pytest.approx(0.0)

    def test_missing_rated_no_update(self):
        live = LiveState()
        live.huawei_batteries_rated_capacity_wh = None
        live.huawei_batteries_soc_pct = 80.0
        _compute_battery_capacities(live)
        assert live.battery_usable_capacity_kwh == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# _compute_net_consumption
# ---------------------------------------------------------------------------


class TestComputeNetConsumption:
    def _cfg(self, includes_ev: bool = False) -> SensorConfig:
        cfg = SensorConfig()
        cfg.house_power_includes_ev_charger_power = includes_ev
        return cfg

    def _live(
        self, house: float, solar: float, ev1: float = 0.0, ev2: float = 0.0
    ) -> LiveState:
        from custom_components.hsem.models.live_state import EVLiveState

        live = LiveState()
        live.house_consumption_power_w = house
        live.solar_production_power_w = solar
        live.ev = EVLiveState(power_w=ev1)
        live.ev_second = EVLiveState(power_w=ev2)
        return live

    def test_no_ev_no_solar(self):
        live = self._live(house=1500, solar=0)
        _compute_net_consumption(live, self._cfg())
        assert live.net_consumption_w == pytest.approx(1500.0)
        assert live.net_consumption_with_ev_w == pytest.approx(1500.0)

    def test_solar_surplus(self):
        live = self._live(house=1000, solar=2000)
        _compute_net_consumption(live, self._cfg())
        assert live.net_consumption_w == pytest.approx(-1000.0)

    def test_ev_separate_from_house(self):
        """When EV is NOT included in house meter, it adds to net_consumption_with_ev."""
        live = self._live(house=1000, solar=500, ev1=3000)
        _compute_net_consumption(live, self._cfg(includes_ev=False))
        assert live.net_consumption_w == pytest.approx(500.0)
        assert live.net_consumption_with_ev_w == pytest.approx(3500.0)

    def test_ev_included_in_house(self):
        """When EV IS included in house meter:

        - net_consumption_w = house - solar - ev  (strips EV so only pure house net remains)
        - net_consumption_with_ev_w = house - solar  (total as-measured; EV already in it)
        """
        live = self._live(house=4000, solar=500, ev1=3000)
        _compute_net_consumption(live, self._cfg(includes_ev=True))
        # pure house net = 4000 - 500 - 3000 = 500 W
        assert live.net_consumption_w == pytest.approx(500.0)
        # total measured net = 4000 - 500 = 3500 W
        assert live.net_consumption_with_ev_w == pytest.approx(3500.0)

    def test_none_house_power_yields_zero(self):
        live = LiveState()
        live.house_consumption_power_w = None
        live.solar_production_power_w = 500.0
        _compute_net_consumption(live, self._cfg())
        assert live.net_consumption_w == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# build_battery_schedules
# ---------------------------------------------------------------------------


class TestBuildBatterySchedules:
    def test_returns_three_schedules(self):
        cfg = build_sensor_config(_make_config_entry())
        schedules = build_battery_schedules(cfg)
        assert len(schedules) == 3

    def test_disabled_schedules_have_enabled_false(self):
        cfg = build_sensor_config(_make_config_entry())
        schedules = build_battery_schedules(cfg)
        assert all(not s.enabled for s in schedules)

    def test_enabled_schedule_propagates(self):
        cfg = build_sensor_config(
            _make_config_entry(hsem_batteries_enable_batteries_schedule_1=True)
        )
        schedules = build_battery_schedules(cfg)
        assert schedules[0].enabled is True

    def test_initial_avg_import_price_zero(self):
        cfg = build_sensor_config(_make_config_entry())
        for s in build_battery_schedules(cfg):
            assert s.avg_import_price == pytest.approx(0.0)
