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
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from tests.test_coordinator_cycle_paths import _coordinator, _run_cycle
from tests.test_ha_mock_integration import FakeState

_T0 = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
_CFG = SensorConfig(
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
    hass = SimpleNamespace(config=SimpleNamespace(config_dir=str(tmp_path)))
    return cast(
        CoordinatorSharedState,
        SimpleNamespace(hass=hass, _energy_tracker=EnergyTracker()),
    )


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
        sample = build_energy_sample(_T0, _live())

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

        sample = build_energy_sample(_T0, live)

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

    def test_unknown_capacities_are_zero(self) -> None:
        site = build_battery_site(LiveState(), SensorConfig())

        assert site.rated_capacity_kwh == pytest.approx(0.0)
        assert site.usable_capacity_kwh == pytest.approx(0.0)
        assert site.cycle_cost_per_kwh == pytest.approx(0.0)


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
        assert tracker.history_file.endswith(HISTORY_FILE)
        assert tracker.daily_log["2026-06-10"].grid_import_kwh == pytest.approx(0.5)

        sensors = build_monthly_energy_sensors(coordinator._config_entry, coordinator)
        with patch(f"{_SENSORS}.hsem_now", return_value=_T0):
            attributes = sensors[0].extra_state_attributes
        assert attributes is not None
        assert attributes["this_month"]["days"] == 1
        assert attributes["this_month"]["grid_import_kwh"] == pytest.approx(0.5)
