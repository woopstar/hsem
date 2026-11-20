"""Tests for the per-cycle feeding of the energy tracker (issue #1435)."""

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
)
from custom_components.hsem.coordinator_meter_tracking import (
    async_accumulate_meter_trackers,
)
from custom_components.hsem.coordinator_state import CoordinatorSharedState
from custom_components.hsem.coordinator_tracking import init_financial_tracker
from custom_components.hsem.custom_sensors.monthly_energy_sensors import (
    build_monthly_energy_sensors,
)
from custom_components.hsem.models.energy_tracker import REASON_NO_DATA, EnergyTracker
from custom_components.hsem.models.financial_tracker import FinancialTracker
from custom_components.hsem.models.live_state import EVLiveState, LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from tests.test_coordinator_cycle_paths import _coordinator, _run_cycle
from tests.test_ha_mock_integration import FakeState, make_fake_hass

_T0 = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
_HOUSE = "sensor.power_house_load"
_PV = "sensor.power_inverter_input_total"
_EV = "sensor.ev_charger_power"
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
