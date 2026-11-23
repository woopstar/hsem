"""Tests for the per-cycle feeding of the energy tracker (issues #1435, #1450)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from custom_components.hsem import coordinator_energy
from custom_components.hsem.coordinator_energy import (
    HISTORY_FILE,
    async_track_energy,
    build_battery_site,
    build_energy_sample,
    consumption_power_w,
    init_energy_tracker,
    power_sensors_in_another_unit,
    read_battery_ac_power,
)
from custom_components.hsem.coordinator_meter_tracking import (
    async_accumulate_meter_trackers,
)
from custom_components.hsem.coordinator_state import CoordinatorSharedState
from custom_components.hsem.coordinator_tracking import init_financial_tracker
from custom_components.hsem.custom_sensors.monthly_energy_sensors import (
    build_monthly_energy_sensors,
)
from custom_components.hsem.models.energy_tracker import (
    REASON_AC_BATTERY_POWER_NO_READINGS,
    REASON_AC_BATTERY_POWER_NOT_CONFIGURED,
    REASON_AC_BATTERY_POWER_UNIT_NOT_POWER,
    REASON_CONSUMPTION_NOT_MEASURED,
    REASON_NO_DATA,
    REASON_THROUGHPUT_BELOW_MINIMUM,
    EnergyTracker,
)
from custom_components.hsem.models.financial_tracker import FinancialTracker
from custom_components.hsem.models.live_state import EVLiveState, LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from tests.test_coordinator_cycle_paths import _coordinator, _run_cycle
from tests.test_ha_mock_integration import FakeState, make_fake_hass

_T0 = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
_HOUSE = "sensor.power_house_load"
_PV = "sensor.power_inverter_input_total"
_EV = "sensor.ev_charger_power"
_AC = "sensor.battery_ac_power"
_CFG = SensorConfig(
    house_consumption_power=_HOUSE,
    solar_production_power=_PV,
    house_power_includes_ev_charger_power=True,
    update_interval=5,
    batteries_charge_efficiency=95.0,
    batteries_discharge_efficiency=90.0,
    batteries_purchase_price=36000.0,
    batteries_expected_cycles=6000,
    batteries_capacity_loss_pct=30.0,
    batteries_cycle_cost=0.02,
)
_SENSORS = "custom_components.hsem.custom_sensors.monthly_energy_sensors"
_CYCLE = "custom_components.hsem.coordinator_cycle"
_TRACKING = "custom_components.hsem.coordinator_tracking"


def _state(tmp_path: Path) -> CoordinatorSharedState:
    """Return the slice of the coordinator the energy tracking uses."""
    hass = _hass()
    hass.config = SimpleNamespace(config_dir=str(tmp_path))
    return cast(
        CoordinatorSharedState,
        SimpleNamespace(hass=hass, _energy_tracker=EnergyTracker()),
    )


def _hass(**states: str) -> Any:
    """Return a hass in which the house and PV power sensors give readings."""
    return make_fake_hass({_HOUSE: "1200", _PV: "2400", **states})


def _live(
    *,
    grid_import: float | None = 100.0,
    pv: float | None = 50.0,
    soc: float | None = 50.0,
    battery_power_w: float | None = 3000.0,
) -> LiveState:
    live = LiveState()
    live.grid_import_energy_kwh = grid_import
    live.grid_export_energy_kwh = 20.0
    live.pv_energy_kwh = pv
    live.solar_production_power_w = 2400.0
    live.house_consumption_power_w = 1200.0
    live.huawei_batteries_charge_discharge_power_w = battery_power_w
    live.huawei_batteries_soc_pct = soc
    live.huawei_batteries_rated_capacity_wh = 10000.0
    live.battery_usable_capacity_kwh = 9.0
    return live


class TestBuildSample:
    def test_the_sample_carries_the_live_readings(self) -> None:
        sample = build_energy_sample(_hass(), _T0, _live(), _CFG)

        assert sample.at == _T0
        assert sample.grid_import_energy_kwh == pytest.approx(100.0)
        assert sample.grid_export_energy_kwh == pytest.approx(20.0)
        assert sample.pv_energy_kwh == pytest.approx(50.0)
        assert sample.pv_power_w == pytest.approx(2400.0)
        assert sample.house_power_w == pytest.approx(1200.0)
        assert sample.battery_power_w == pytest.approx(3000.0)
        assert sample.soc_pct == pytest.approx(50.0)

    def test_a_missing_or_non_finite_reading_is_none(self) -> None:
        live = _live(grid_import=None, pv=float("nan"), soc=float("inf"))

        sample = build_energy_sample(_hass(), _T0, live, _CFG)

        assert sample.grid_import_energy_kwh is None
        assert sample.pv_energy_kwh is None
        assert sample.soc_pct is None


def _ac_hass(state: str, unit: str | None = "W") -> Any:
    """Return a hass whose AC-side battery power sensor has *state* and *unit*."""
    attributes = {} if unit is None else {"unit_of_measurement": unit}
    return make_fake_hass({_AC: {"state": state, "attributes": attributes}})


class TestAcSideBatteryPower:
    """The optional AC-side battery power sensor (issue #1450)."""

    _AC_CFG = SensorConfig(battery_ac_power_entity=_AC)

    def _read(self, hass: Any, cfg: SensorConfig | None = None) -> Any:
        return read_battery_ac_power(hass, cfg or self._AC_CFG, _T0, 600.0)

    def test_without_a_configured_sensor_there_is_no_reading(self) -> None:
        assert self._read(_ac_hass("2500"), SensorConfig()) == (
            None,
            REASON_AC_BATTERY_POWER_NOT_CONFIGURED,
        )

    def test_a_sensor_that_does_not_exist_gives_no_reading(self) -> None:
        assert self._read(_hass()) == (None, REASON_AC_BATTERY_POWER_NO_READINGS)

    @pytest.mark.parametrize("state", ["unavailable", "unknown", "", "nan", "inf"])
    def test_a_state_that_is_no_finite_number_gives_no_reading(
        self, state: str
    ) -> None:
        assert self._read(_ac_hass(state)) == (
            None,
            REASON_AC_BATTERY_POWER_NO_READINGS,
        )

    def test_the_sign_is_kept_and_zero_is_a_reading(self) -> None:
        value, _reason = self._read(_ac_hass("-1800"))
        assert value == pytest.approx(-1800.0)
        value, _reason = self._read(_ac_hass("0"))
        assert value == pytest.approx(0.0)

    @pytest.mark.parametrize(
        ("state", "unit", "watts"),
        [("2500", "W", 2500.0), ("2.5", "kW", 2500.0), ("0.0025", "MW", 2500.0)],
    )
    def test_a_power_unit_is_converted_to_watt(
        self, state: str, unit: str, watts: float
    ) -> None:
        value, reason = self._read(_ac_hass(state, unit))

        assert value == pytest.approx(watts)
        # The reason a month without readings would get.
        assert reason == REASON_AC_BATTERY_POWER_NO_READINGS

    @pytest.mark.parametrize(
        "unit", ["kVA", "VA", "A", "kWh", "Wh", "%", "foo", "", None]
    )
    def test_a_unit_that_is_not_power_is_not_read_as_watt(
        self, unit: str | None
    ) -> None:
        assert self._read(_ac_hass("2500", unit)) == (
            None,
            REASON_AC_BATTERY_POWER_UNIT_NOT_POWER,
        )

    @pytest.mark.parametrize(
        ("age_s", "watts"), [(0.0, 2000.0), (600.0, 2000.0), (601.0, None)]
    )
    def test_a_state_that_was_not_reported_for_too_long_is_not_a_reading(
        self, age_s: float, watts: float | None
    ) -> None:
        """A sensor that stopped keeps its last state in Home Assistant."""
        hass = _ac_hass("2000")
        hass.states._states[_AC] = SimpleNamespace(
            state="2000",
            attributes={"unit_of_measurement": "W"},
            last_reported=_T0 - timedelta(seconds=age_s),
        )

        value, reason = self._read(hass)

        assert value == (None if watts is None else pytest.approx(watts))
        assert reason == REASON_AC_BATTERY_POWER_NO_READINGS

    def test_the_sample_carries_the_reading_it_is_given(self) -> None:
        sample = build_energy_sample(
            _hass(), _T0, _live(), _CFG, battery_ac_power_w=3100.0
        )

        assert sample.battery_ac_power_w == pytest.approx(3100.0)
        assert sample.battery_power_w == pytest.approx(3000.0)
        assert (
            build_energy_sample(_hass(), _T0, _live(), _CFG).battery_ac_power_w is None
        )


class TestAcSideTracking:
    """What a cycle books from the AC-side sensor, and what it says (#1450)."""

    _CFG = SensorConfig(
        house_consumption_power=_HOUSE,
        solar_production_power=_PV,
        update_interval=5,
        battery_ac_power_entity=_AC,
    )

    async def _cycles(
        self, coordinator: CoordinatorSharedState, start: int, count: int
    ) -> None:
        for step in range(start, start + count):
            await async_track_energy(
                coordinator, _T0 + timedelta(minutes=5 * step), _live(), self._CFG
            )

    @staticmethod
    def _set(coordinator: CoordinatorSharedState, state: Any) -> None:
        states: Any = coordinator.hass.states
        states._states[_AC] = state

    @pytest.mark.asyncio
    async def test_a_reading_in_watt_is_booked(self, tmp_path: Path) -> None:
        coordinator = _state(tmp_path)
        self._set(coordinator, FakeState(_AC, "2400", {"unit_of_measurement": "W"}))

        await self._cycles(coordinator, 0, 3)

        entry = coordinator._energy_tracker.daily_log["2026-06-10"]
        assert entry.ac_charged_kwh == pytest.approx(0.4)
        assert entry.ac_hours == pytest.approx(10.0 / 60.0)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("unit", ["kVA", "A", "kWh", "%", None])
    async def test_a_wrong_unit_is_not_booked_and_warned_about_once(
        self, tmp_path: Path, unit: str | None
    ) -> None:
        coordinator = _state(tmp_path)
        attributes = {} if unit is None else {"unit_of_measurement": unit}
        self._set(coordinator, FakeState(_AC, "2400", attributes))

        with patch.object(coordinator_energy, "async_log") as log:
            await self._cycles(coordinator, 0, 4)

        tracker = coordinator._energy_tracker
        entry = tracker.daily_log["2026-06-10"]
        assert entry.ac_charged_kwh == pytest.approx(0.0)
        assert entry.ac_power_hours == pytest.approx(0.0)
        assert tracker.ac_sensor_reason == REASON_AC_BATTERY_POWER_UNIT_NOT_POWER
        assert (
            tracker.month_summary("2026-06")["ac_round_trip_loss_reason"]
            == REASON_AC_BATTERY_POWER_UNIT_NOT_POWER
        )
        warnings = [call for call in log.call_args_list if call.args[0] == "warning"]
        assert len(warnings) == 1
        assert warnings[0].args[2] == _AC

    @pytest.mark.asyncio
    async def test_the_warning_comes_back_when_the_unit_breaks_again(
        self, tmp_path: Path
    ) -> None:
        coordinator = _state(tmp_path)
        wrong = FakeState(_AC, "2400", {"unit_of_measurement": "kVA"})
        right = FakeState(_AC, "2400", {"unit_of_measurement": "W"})

        with patch.object(coordinator_energy, "async_log") as log:
            for step, state in enumerate((wrong, wrong, right, right, wrong, wrong)):
                self._set(coordinator, state)
                await self._cycles(coordinator, step, 1)

        assert [call.args[0] for call in log.call_args_list] == ["warning", "warning"]
        # Only the interval between the two good readings is booked.
        entry = coordinator._energy_tracker.daily_log["2026-06-10"]
        assert entry.ac_charged_kwh == pytest.approx(0.2)

    @pytest.mark.asyncio
    async def test_a_sensor_that_is_not_configured_is_no_warning(
        self, tmp_path: Path
    ) -> None:
        coordinator = _state(tmp_path)

        with patch.object(coordinator_energy, "async_log") as log:
            await async_track_energy(coordinator, _T0, _live(), _CFG)

        log.assert_not_called()
        assert (
            coordinator._energy_tracker.ac_sensor_reason
            == REASON_AC_BATTERY_POWER_NOT_CONFIGURED
        )

    @pytest.mark.asyncio
    async def test_a_frozen_sensor_stops_being_booked(self, tmp_path: Path) -> None:
        """The sensor reported 2.4 kW at 12:00 and never again."""
        coordinator = _state(tmp_path)
        self._set(
            coordinator,
            SimpleNamespace(
                state="2400",
                attributes={"unit_of_measurement": "W"},
                last_reported=_T0,
            ),
        )

        await self._cycles(coordinator, 0, 7)

        tracker = coordinator._energy_tracker
        entry = tracker.daily_log["2026-06-10"]
        # Booked while the report is at most 10 minutes old: two intervals.
        assert entry.observed_hours == pytest.approx(0.5)
        assert entry.ac_power_hours == pytest.approx(10.0 / 60.0)
        assert entry.ac_charged_kwh == pytest.approx(0.4)
        assert tracker.ac_sensor_reason == REASON_AC_BATTERY_POWER_NO_READINGS


class TestBuildSite:
    def test_the_cycle_cost_is_the_planners_depreciation(self) -> None:
        site = build_battery_site(_live(), _CFG)

        assert site.rated_capacity_kwh == pytest.approx(10.0)
        assert site.usable_capacity_kwh == pytest.approx(9.0)
        assert site.charge_efficiency == pytest.approx(0.95)
        assert site.discharge_efficiency == pytest.approx(0.90)
        # 36000 * 30 % / (2 * 9 kWh * 6000 cycles), above the 0.02 margin.
        assert site.cycle_cost_per_kwh == pytest.approx(0.1)

    def test_the_configured_cycle_cost_is_the_floor(self) -> None:
        cfg = SensorConfig(batteries_cycle_cost=0.25)

        site = build_battery_site(_live(), cfg)

        assert site.cycle_cost_per_kwh == pytest.approx(0.25)

    def test_unknown_capacities_are_zero_and_the_cycle_cost_is_unknown(self) -> None:
        site = build_battery_site(LiveState(), SensorConfig())

        assert site.rated_capacity_kwh == pytest.approx(0.0)
        assert site.usable_capacity_kwh == pytest.approx(0.0)
        assert site.cycle_cost_per_kwh is None

    def test_without_a_purchase_price_the_cycle_cost_is_unknown(self) -> None:
        site = build_battery_site(_live(), SensorConfig(batteries_expected_cycles=6000))

        assert site.usable_capacity_kwh == pytest.approx(9.0)
        assert site.cycle_cost_per_kwh is None


class TestAvailability:
    """The live state holds 0 W for a power sensor that gives no reading."""

    @pytest.mark.parametrize("state", ["unavailable", "unknown", ""])
    def test_a_house_sensor_without_a_reading_is_missing(self, state: str) -> None:
        live = _live()
        live.house_consumption_power_w = 0.0
        live.solar_production_power_w = 0.0

        sample = build_energy_sample(
            _hass(**{_HOUSE: state, _PV: state}), _T0, live, _CFG
        )

        assert sample.house_power_w is None
        assert sample.pv_power_w is None

    def test_a_sensor_that_does_not_exist_or_is_not_configured_is_missing(
        self,
    ) -> None:
        hass = make_fake_hass({})

        assert build_energy_sample(hass, _T0, _live(), _CFG).house_power_w is None
        assert (
            build_energy_sample(hass, _T0, _live(), SensorConfig()).house_power_w
            is None
        )

    def test_a_reading_of_zero_is_a_reading(self) -> None:
        live = _live()
        live.house_consumption_power_w = 0.0

        sample = build_energy_sample(_hass(**{_HOUSE: "0"}), _T0, live, _CFG)

        assert sample.house_power_w == pytest.approx(0.0)


class TestUnit:
    """The live state holds the raw number of a sensor in another kind of unit."""

    @staticmethod
    def _hass(unit: str | None) -> Any:
        attributes = {} if unit is None else {"unit_of_measurement": unit}
        return make_fake_hass(
            {
                _HOUSE: {"state": "1200", "attributes": attributes},
                _PV: {"state": "2400", "attributes": attributes},
            }
        )

    @pytest.mark.parametrize("unit", ["kVA", "kWh", "%", "foo"])
    def test_a_reading_in_a_unit_that_is_not_power_is_missing(self, unit: str) -> None:
        sample = build_energy_sample(self._hass(unit), _T0, _live(), _CFG)

        assert sample.house_power_w is None
        assert sample.pv_power_w is None

    @pytest.mark.parametrize("unit", [None, "", "W", "kW"])
    def test_a_reading_in_a_power_unit_or_without_a_unit_is_taken(
        self, unit: str | None
    ) -> None:
        sample = build_energy_sample(self._hass(unit), _T0, _live(), _CFG)

        assert sample.house_power_w == pytest.approx(1200.0)
        assert sample.pv_power_w == pytest.approx(2400.0)

    def test_a_unit_that_is_not_a_string_is_no_unit(self) -> None:
        hass = make_fake_hass(
            {_HOUSE: {"state": "1200", "attributes": {"unit_of_measurement": 5}}}
        )

        sample = build_energy_sample(hass, _T0, _live(), _CFG)

        assert sample.house_power_w == pytest.approx(1200.0)
        assert power_sensors_in_another_unit(hass, _CFG) == ()

    def test_the_sensors_left_out_are_named(self) -> None:
        assert power_sensors_in_another_unit(self._hass("kVA"), _CFG) == (_HOUSE, _PV)
        assert power_sensors_in_another_unit(self._hass("kW"), _CFG) == ()
        assert power_sensors_in_another_unit(self._hass(None), _CFG) == ()
        assert power_sensors_in_another_unit(self._hass("kVA"), SensorConfig()) == ()

    def test_one_sensor_set_up_as_both_is_named_once(self) -> None:
        cfg = SensorConfig(house_consumption_power=_PV, solar_production_power=_PV)

        assert power_sensors_in_another_unit(self._hass("kVA"), cfg) == (_PV,)

    def test_a_sensor_without_a_reading_keeps_its_verdict(self) -> None:
        """An unavailable state has often dropped its unit."""
        hass = make_fake_hass({_HOUSE: "unavailable"})

        assert power_sensors_in_another_unit(hass, _CFG) == ()
        assert power_sensors_in_another_unit(hass, _CFG, (_HOUSE, _PV)) == (
            _HOUSE,
            _PV,
        )


class TestUnitWarning:
    """A cycle names the sensors it leaves out, and warns once (issue #1449)."""

    @staticmethod
    def _set(coordinator: CoordinatorSharedState, state: str, unit: str | None) -> None:
        attributes = {} if unit is None else {"unit_of_measurement": unit}
        states: Any = coordinator.hass.states
        states._states[_HOUSE] = FakeState(_HOUSE, state, attributes)

    @staticmethod
    async def _cycle(coordinator: CoordinatorSharedState, step: int) -> None:
        await async_track_energy(
            coordinator, _T0 + timedelta(minutes=5 * step), _live(), _CFG
        )

    @pytest.mark.asyncio
    async def test_a_wrong_unit_is_warned_about_once(self, tmp_path: Path) -> None:
        coordinator = _state(tmp_path)
        self._set(coordinator, "1200", "kVA")

        with patch.object(coordinator_energy, "async_log") as log:
            for step in range(4):
                await self._cycle(coordinator, step)

        tracker = coordinator._energy_tracker
        assert tracker.unit_not_power == (_HOUSE,)
        assert len(log.call_args_list) == 1
        assert log.call_args.args[0] == "warning"
        assert log.call_args.args[2] == _HOUSE
        entry = tracker.daily_log["2026-06-10"]
        assert entry.house_hours == pytest.approx(0.0)
        # The PV sensor has a good unit and is booked all along.
        assert entry.pv_hours == pytest.approx(15.0 / 60.0)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("unit", [None, "", "W", "kW"])
    async def test_a_power_unit_or_no_unit_is_no_warning(
        self, tmp_path: Path, unit: str | None
    ) -> None:
        coordinator = _state(tmp_path)
        self._set(coordinator, "1200", unit)

        with patch.object(coordinator_energy, "async_log") as log:
            for step in range(3):
                await self._cycle(coordinator, step)

        log.assert_not_called()
        tracker = coordinator._energy_tracker
        assert tracker.unit_not_power == ()
        entry = tracker.daily_log["2026-06-10"]
        assert entry.house_hours == pytest.approx(10.0 / 60.0)
        assert entry.house_consumption_kwh == pytest.approx(0.2)

    @pytest.mark.asyncio
    async def test_an_outage_of_the_sensor_is_no_second_warning(
        self, tmp_path: Path
    ) -> None:
        coordinator = _state(tmp_path)
        steps = (
            ("1200", "kVA"),
            ("unavailable", None),
            ("unavailable", None),
            ("1200", "kVA"),
        )

        with patch.object(coordinator_energy, "async_log") as log:
            for step, (state, unit) in enumerate(steps):
                self._set(coordinator, state, unit)
                await self._cycle(coordinator, step)
                assert coordinator._energy_tracker.unit_not_power == (_HOUSE,)

        assert len(log.call_args_list) == 1

    @pytest.mark.asyncio
    async def test_a_corrected_unit_clears_it_and_is_booked(
        self, tmp_path: Path
    ) -> None:
        coordinator = _state(tmp_path)
        tracker = coordinator._energy_tracker

        with patch.object(coordinator_energy, "async_log") as log:
            for step, unit in enumerate(("kVA", "kVA", "W", "W", "kVA")):
                self._set(coordinator, "1200", unit)
                await self._cycle(coordinator, step)
                assert tracker.unit_not_power == (() if unit == "W" else (_HOUSE,))

        # Once for each time the unit broke; correcting it is not a warning.
        assert [call.args[0] for call in log.call_args_list] == ["warning", "warning"]
        # Only the interval between the two good readings is booked.
        assert tracker.daily_log["2026-06-10"].house_hours == pytest.approx(5.0 / 60.0)


class TestEvConsumption:
    """The grid meter counts the EV; the house sensor may not."""

    @staticmethod
    def _cfg(*, includes_ev: bool) -> SensorConfig:
        cfg = SensorConfig(
            house_consumption_power=_HOUSE,
            house_power_includes_ev_charger_power=includes_ev,
        )
        cfg.ev.power_entity = _EV
        return cfg

    @staticmethod
    def _live(ev_power_w: float | None, second_w: float | None = None) -> LiveState:
        live = _live()
        live.ev = EVLiveState(power_w=ev_power_w)
        live.ev_second = EVLiveState(power_w=second_w)
        return live

    def test_ev_power_is_added_when_the_house_sensor_leaves_it_out(self) -> None:
        power = consumption_power_w(
            _hass(), self._live(7000.0), self._cfg(includes_ev=False)
        )

        assert power == pytest.approx(8200.0)

    def test_ev_power_is_not_added_twice(self) -> None:
        power = consumption_power_w(
            _hass(), self._live(7000.0), self._cfg(includes_ev=True)
        )

        assert power == pytest.approx(1200.0)

    def test_both_chargers_count_and_a_feeding_one_counts_as_nothing(self) -> None:
        cfg = self._cfg(includes_ev=False)
        cfg.ev_second.power_entity = "sensor.ev_second_charger_power"

        power = consumption_power_w(_hass(), self._live(-500.0, 3000.0), cfg)

        assert power == pytest.approx(4200.0)

    def test_a_charger_without_a_reading_leaves_the_consumption_unknown(self) -> None:
        power = consumption_power_w(
            _hass(), self._live(None), self._cfg(includes_ev=False)
        )

        assert power is None

    def test_a_charger_that_is_not_configured_is_no_charger(self) -> None:
        cfg = SensorConfig(house_consumption_power=_HOUSE)

        assert consumption_power_w(_hass(), self._live(None), cfg) == pytest.approx(
            1200.0
        )

    @pytest.mark.asyncio
    async def test_a_month_with_an_ev_on_the_grid_meter(self, tmp_path: Path) -> None:
        """House 300, EV 400, import 550 kWh: 21 % and not a clamped 0 %."""
        coordinator = _state(tmp_path)
        cfg = self._cfg(includes_ev=False)
        cfg.update_interval = 5
        hours = 100.0
        steps = int(hours * 12)

        for step in range(steps + 1):
            live = self._live(4000.0)
            live.house_consumption_power_w = 3000.0
            live.grid_import_energy_kwh = 1000.0 + 5.5 * step / 12.0
            live.huawei_batteries_charge_discharge_power_w = 0.0
            await async_track_energy(
                coordinator, _T0 + timedelta(minutes=5 * step), live, cfg
            )

        summary = coordinator._energy_tracker.month_summary("2026-06")
        assert summary["house_consumption_kwh"] == pytest.approx(700.0)
        assert summary["grid_import_kwh"] == pytest.approx(550.0)
        assert summary["self_sufficiency"] == pytest.approx(21.4, abs=0.05)


class TestTracking:
    @pytest.mark.asyncio
    async def test_the_history_file_is_set_and_loaded_once(
        self, tmp_path: Path
    ) -> None:
        coordinator = _state(tmp_path)
        tracker = coordinator._energy_tracker

        await init_energy_tracker(tracker, coordinator.hass)
        path = Path(tracker.history_file)
        assert path == tmp_path / ".storage" / HISTORY_FILE

        with patch.object(EnergyTracker, "load_history") as load:
            await init_energy_tracker(tracker, coordinator.hass)
        load.assert_not_called()

    @pytest.mark.asyncio
    async def test_two_cycles_book_the_energy_between_them(
        self, tmp_path: Path
    ) -> None:
        coordinator = _state(tmp_path)

        await async_track_energy(coordinator, _T0, _live(), _CFG)
        await async_track_energy(
            coordinator,
            _T0 + timedelta(minutes=6),
            _live(grid_import=100.4, pv=50.25, soc=53.0),
            _CFG,
        )

        entry = coordinator._energy_tracker.daily_log["2026-06-10"]
        assert entry.grid_import_kwh == pytest.approx(0.4)
        assert entry.pv_kwh == pytest.approx(0.25)
        assert entry.house_consumption_kwh == pytest.approx(0.12)
        assert entry.battery_charged_kwh == pytest.approx(0.3)
        assert entry.measured_stored_delta_kwh == pytest.approx(0.3)
        assert entry.wear_cost == pytest.approx(0.3 * 0.1)

    @pytest.mark.asyncio
    async def test_the_first_sample_is_saved_so_a_restart_can_continue(
        self, tmp_path: Path
    ) -> None:
        coordinator = _state(tmp_path)

        await async_track_energy(coordinator, _T0, _live(), _CFG)

        saved = json.loads((tmp_path / ".storage" / HISTORY_FILE).read_text())
        assert saved["last_sample"]["grid_import_energy_kwh"] == pytest.approx(100.0)

        restarted = _state(tmp_path)
        await async_track_energy(
            restarted, _T0 + timedelta(minutes=5), _live(grid_import=100.5), _CFG
        )
        assert restarted._energy_tracker.daily_log[
            "2026-06-10"
        ].grid_import_kwh == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_a_coordinator_without_the_tracker_is_skipped(self) -> None:
        coordinator = cast(CoordinatorSharedState, SimpleNamespace())

        await async_track_energy(coordinator, _T0, _live(), _CFG)

    @pytest.mark.asyncio
    async def test_a_failure_is_logged_and_does_not_fail_the_cycle(
        self, tmp_path: Path
    ) -> None:
        coordinator = _state(tmp_path)
        log = MagicMock()

        with (
            patch.object(coordinator_energy, "async_log", log),
            patch.object(
                coordinator_energy, "build_energy_sample", side_effect=ValueError("x")
            ),
        ):
            await async_track_energy(coordinator, _T0, _live(), _CFG)

        log.assert_called_once()
        assert log.call_args.args[0] == "error"
        assert coordinator._energy_tracker.daily_log == {}

    @pytest.mark.asyncio
    async def test_the_meter_trackers_feed_it_every_cycle(self, tmp_path: Path) -> None:
        """Without a plan and without the other trackers, energy still counts."""
        coordinator = _state(tmp_path)

        for minutes, meter in ((0, 100.0), (5, 100.5)):
            await async_accumulate_meter_trackers(
                coordinator,
                _T0 + timedelta(minutes=minutes),
                _live(grid_import=meter),
                _CFG,
                None,
            )

        assert coordinator._energy_tracker.daily_log[
            "2026-06-10"
        ].grid_import_kwh == pytest.approx(0.5)


class TestUpgrade:
    """An installation of the current release has no energy history file."""

    @pytest.mark.asyncio
    async def test_the_financial_history_of_the_current_release_still_loads(
        self, tmp_path: Path
    ) -> None:
        storage = tmp_path / ".storage"
        storage.mkdir()
        # The file as the current release writes it (indent 2, these keys).
        (storage / "hsem_financial_history.json").write_text(
            json.dumps(
                {
                    "import_cost_total": 1234.5,
                    "export_income_total": 321.0,
                    "_today_start_import_cost": 1230.0,
                    "_today_start_export_income": 320.0,
                    "_last_import_energy_kwh": 8765.432,
                    "_last_export_energy_kwh": 2345.678,
                    "_last_import_sample_at": "2026-06-10T09:55:00+00:00",
                    "_last_export_sample_at": "2026-06-10T09:55:00+00:00",
                    "_last_import_price": 2.1,
                    "_last_export_price": 0.4,
                    "_last_import_price_available": True,
                    "_last_export_price_available": True,
                    "today": "2026-06-10",
                    "daily_log": [
                        {
                            "date": "2026-06-08",
                            "import_cost": 12.5,
                            "export_income": 3.0,
                        },
                        {
                            "date": "2026-06-09",
                            "import_cost": 10.0,
                            "export_income": 4.5,
                        },
                    ],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        hass = SimpleNamespace(config=SimpleNamespace(config_dir=str(tmp_path)))
        financial = FinancialTracker(today="2026-06-10")
        energy = EnergyTracker()

        await init_financial_tracker(financial, hass)  # type: ignore[arg-type]
        await init_energy_tracker(energy, hass)  # type: ignore[arg-type]

        assert financial.import_cost_total == pytest.approx(1234.5)
        assert financial.import_cost_today == pytest.approx(4.5)
        assert sorted(financial.daily_log) == ["2026-06-08", "2026-06-09"]
        assert financial.daily_log["2026-06-09"].export_income == pytest.approx(4.5)
        assert financial._last_import_energy_kwh == pytest.approx(8765.432)
        # The energy record starts empty and says so.
        assert energy.daily_log == {}
        summary = energy.month_summary("2026-06")
        assert summary["self_sufficiency_reason"] == REASON_NO_DATA


class TestThroughTheRealCycle:
    @pytest.mark.asyncio
    async def test_the_sensors_report_what_the_cycle_recorded(self) -> None:
        """The sensors' data comes from the real update cycle (issue #1398)."""
        coordinator, _published = _coordinator(
            {
                "hsem_grid_import_energy_entity": "sensor.grid_import_energy",
                "hsem_grid_export_energy_entity": "sensor.grid_export_energy",
            }
        )
        states: Any = coordinator.hass.states._states

        for minutes, grid_import in ((0, 100.0), (5, 100.5)):
            for entity_id, value in (
                ("sensor.grid_import_energy", grid_import),
                ("sensor.grid_export_energy", 20.0),
            ):
                states[entity_id] = FakeState(
                    entity_id, str(value), {"unit_of_measurement": "kWh"}
                )
            with (
                patch(
                    f"{_CYCLE}.hsem_now", return_value=_T0 + timedelta(minutes=minutes)
                ),
                patch(f"{_TRACKING}.async_track_time_change"),
            ):
                await _run_cycle(coordinator)

        tracker = coordinator._energy_tracker
        # The cycle read the house power from a sensor that gave a reading.
        assert tracker.daily_log["2026-06-10"].house_consumption_kwh == pytest.approx(
            0.1
        )
        assert tracker.history_file.endswith(HISTORY_FILE)
        assert tracker.daily_log["2026-06-10"].grid_import_kwh == pytest.approx(0.5)

        sensors = build_monthly_energy_sensors(coordinator._config_entry, coordinator)
        with patch(f"{_SENSORS}.hsem_now", return_value=_T0):
            attributes = sensors[0].extra_state_attributes
        assert attributes is not None
        assert attributes["this_month"]["days"] == 1
        assert attributes["this_month"]["grid_import_kwh"] == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_the_ac_side_sensor_is_read_by_the_cycle(self) -> None:
        """3 kW into the battery on the AC side for five minutes (issue #1450).

        The option goes through the config reader and the real update cycle
        to the AC-side sensor's attributes (issue #1398).
        """
        coordinator, _published = _coordinator({"hsem_battery_ac_power_entity": _AC})
        states: Any = coordinator.hass.states._states
        states[_AC] = FakeState(_AC, "3", {"unit_of_measurement": "kW"})

        for minutes in (0, 5):
            with (
                patch(
                    f"{_CYCLE}.hsem_now", return_value=_T0 + timedelta(minutes=minutes)
                ),
                patch(f"{_TRACKING}.async_track_time_change"),
            ):
                await _run_cycle(coordinator)

        assert coordinator._cfg is not None
        assert coordinator._cfg.battery_ac_power_entity == _AC
        entry = coordinator._energy_tracker.daily_log["2026-06-10"]
        assert entry.ac_charged_kwh == pytest.approx(0.25)
        assert entry.ac_discharged_kwh == pytest.approx(0.0)

        sensors = {
            sensor._description.key: sensor
            for sensor in build_monthly_energy_sensors(
                coordinator._config_entry, coordinator
            )
        }
        with patch(f"{_SENSORS}.hsem_now", return_value=_T0):
            attributes = sensors["ac_round_trip_loss"].extra_state_attributes
            value = sensors["ac_round_trip_loss"].native_value
        assert attributes is not None
        assert attributes["this_month"]["ac_round_trip_charged_kwh"] == pytest.approx(
            0.25
        )
        # Measured, but a quarter of a kWh is too little to give a loss.
        assert value is None
        assert attributes["reason"] == REASON_THROUGHPUT_BELOW_MINIMUM

    @pytest.mark.asyncio
    async def test_without_the_ac_side_sensor_the_figure_is_unknown(self) -> None:
        """The reason says the AC side was not measured (issue #1450)."""
        coordinator, _published = _coordinator()

        for minutes in (0, 5):
            with (
                patch(
                    f"{_CYCLE}.hsem_now", return_value=_T0 + timedelta(minutes=minutes)
                ),
                patch(f"{_TRACKING}.async_track_time_change"),
            ):
                await _run_cycle(coordinator)

        assert coordinator._cfg is not None
        assert coordinator._cfg.battery_ac_power_entity is None
        assert "2026-06-10" in coordinator._energy_tracker.daily_log
        sensors = build_monthly_energy_sensors(coordinator._config_entry, coordinator)
        sensor = next(s for s in sensors if s._description.key == "ac_round_trip_loss")
        with patch(f"{_SENSORS}.hsem_now", return_value=_T0):
            attributes = sensor.extra_state_attributes
            value = sensor.native_value
        assert value is None
        assert attributes is not None
        assert attributes["reason"] == REASON_AC_BATTERY_POWER_NOT_CONFIGURED

    @pytest.mark.asyncio
    async def test_a_sensor_in_another_kind_of_unit_says_so(self) -> None:
        """2400 kVA is not 2400 W: nothing is booked, and the reason names it."""
        coordinator, _published = _coordinator({"hsem_battery_ac_power_entity": _AC})
        states: Any = coordinator.hass.states._states
        states[_AC] = FakeState(_AC, "2400", {"unit_of_measurement": "kVA"})

        for minutes in (0, 5):
            with (
                patch(
                    f"{_CYCLE}.hsem_now", return_value=_T0 + timedelta(minutes=minutes)
                ),
                patch(f"{_TRACKING}.async_track_time_change"),
            ):
                await _run_cycle(coordinator)

        entry = coordinator._energy_tracker.daily_log["2026-06-10"]
        assert entry.ac_charged_kwh == pytest.approx(0.0)
        sensors = build_monthly_energy_sensors(coordinator._config_entry, coordinator)
        sensor = next(s for s in sensors if s._description.key == "ac_round_trip_loss")
        with patch(f"{_SENSORS}.hsem_now", return_value=_T0):
            attributes = sensor.extra_state_attributes
            value = sensor.native_value
        assert value is None
        assert attributes is not None
        assert attributes["reason"] == REASON_AC_BATTERY_POWER_UNIT_NOT_POWER

    @pytest.mark.asyncio
    async def test_an_unavailable_house_sensor_is_missing_not_zero(self) -> None:
        """2 kW for ten minutes, then no reading, 0.09 kWh imported per cycle.

        The state collector hands the planner 0 W for the unavailable sensor.
        The energy record must hold the ten measured minutes only.
        """
        coordinator, _published = _coordinator(
            {"hsem_grid_import_energy_entity": "sensor.grid_import_energy"},
            states={_HOUSE: "2000"},
        )
        states: Any = coordinator.hass.states._states

        for step in range(5):
            states["sensor.grid_import_energy"] = FakeState(
                "sensor.grid_import_energy",
                str(round(100.0 + 0.09 * step, 2)),
                {"unit_of_measurement": "kWh"},
            )
            if step == 3:
                states[_HOUSE] = FakeState(_HOUSE, "unavailable", {})
            with (
                patch(
                    f"{_CYCLE}.hsem_now", return_value=_T0 + timedelta(minutes=5 * step)
                ),
                patch(f"{_TRACKING}.async_track_time_change"),
            ):
                await _run_cycle(coordinator)

        assert coordinator._live is not None
        assert coordinator._live.house_consumption_power_w == pytest.approx(0.0)
        entry = coordinator._energy_tracker.daily_log["2026-06-10"]
        assert entry.observed_hours == pytest.approx(20.0 / 60.0)
        assert entry.house_hours == pytest.approx(10.0 / 60.0)
        assert entry.house_consumption_kwh == pytest.approx(2.0 / 6.0)
        assert entry.grid_import_kwh == pytest.approx(0.36)
        # Of the import, only what fell in the measured minutes is paired.
        assert entry.paired_consumption_kwh == pytest.approx(2.0 / 6.0)
        assert entry.paired_import_kwh == pytest.approx(0.18)

    @pytest.mark.asyncio
    async def test_a_house_sensor_in_another_kind_of_unit_is_not_booked(self) -> None:
        """A house sensor that reports 2000 kVA, 0.09 kWh imported per cycle.

        The state collector hands the planner the raw 2000 as watts.  The
        energy record must hold no house consumption, and say why (issue
        #1449).
        """
        coordinator, _published = _coordinator(
            {"hsem_grid_import_energy_entity": "sensor.grid_import_energy"},
        )
        states: Any = coordinator.hass.states._states
        states[_HOUSE] = FakeState(_HOUSE, "2000", {"unit_of_measurement": "kVA"})

        for step in range(3):
            states["sensor.grid_import_energy"] = FakeState(
                "sensor.grid_import_energy",
                str(round(100.0 + 0.09 * step, 2)),
                {"unit_of_measurement": "kWh"},
            )
            with (
                patch(
                    f"{_CYCLE}.hsem_now", return_value=_T0 + timedelta(minutes=5 * step)
                ),
                patch(f"{_TRACKING}.async_track_time_change"),
            ):
                await _run_cycle(coordinator)

        # The planner's input is unchanged: the raw number, as watts.
        assert coordinator._live is not None
        assert coordinator._live.house_consumption_power_w == pytest.approx(2000.0)
        tracker = coordinator._energy_tracker
        entry = tracker.daily_log["2026-06-10"]
        assert entry.observed_hours == pytest.approx(10.0 / 60.0)
        assert entry.house_hours == pytest.approx(0.0)
        assert entry.house_consumption_kwh == pytest.approx(0.0)
        assert entry.grid_import_kwh == pytest.approx(0.18)
        summary = tracker.month_summary("2026-06")
        assert summary["house_consumption_kwh"] is None
        assert summary["self_sufficiency"] is None
        assert summary["self_sufficiency_reason"] == REASON_CONSUMPTION_NOT_MEASURED
        # The sensor is named for the user, on every monthly sensor.
        assert tracker.unit_not_power == (_HOUSE,)
        sensors = build_monthly_energy_sensors(coordinator._config_entry, coordinator)
        with patch(f"{_SENSORS}.hsem_now", return_value=_T0):
            attributes = sensors[0].extra_state_attributes
        assert attributes is not None
        assert attributes["unit_not_power"] == _HOUSE
