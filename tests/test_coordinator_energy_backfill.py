"""Tests for the one-time statistics backfill of the energy history (issue #1450)."""

from __future__ import annotations

import logging
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem import coordinator_energy_backfill as backfill
from custom_components.hsem.coordinator_energy import async_track_energy
from custom_components.hsem.coordinator_energy_backfill import (
    RETRY_INTERVAL_S,
    SOURCE_GRACE_S,
    async_backfill_energy_history,
    async_read_house_and_pv,
)
from custom_components.hsem.coordinator_state import CoordinatorSharedState
from custom_components.hsem.models.energy_tracker import (
    BatterySite,
    EnergyDayEntry,
    EnergyTracker,
)
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import EVChargerConfig, SensorConfig
from tests.test_ha_mock_integration import make_fake_hass

_CPH = ZoneInfo("Europe/Copenhagen")
_NOW = datetime(2026, 6, 10, 12, 0, tzinfo=_CPH)
_HOUR = timedelta(hours=1)
#: 12:00 local time on 8 June 2026.
_H0 = datetime(2026, 6, 8, 10, 0, tzinfo=UTC)
_HOUSE = "sensor.house_power"
_PV_POWER = "sensor.pv_power"
_PV_ENERGY = "sensor.pv_energy"
_IMPORT = "sensor.grid_import"
_EXPORT = "sensor.grid_export"
_BATTERY = "sensor.battery_power"
_EV = "sensor.ev_power"
_EV2 = "sensor.ev2_power"
_SITE = BatterySite(rated_capacity_kwh=10.0, usable_capacity_kwh=9.0)
_READER = "custom_components.hsem.ml.statistics_reader.StatisticsReader"


def _cfg(**overrides: Any) -> SensorConfig:
    return SensorConfig(
        **{
            "house_consumption_power": _HOUSE,
            "solar_production_power": _PV_POWER,
            "pv_energy_entity": _PV_ENERGY,
            "grid_import_energy_entity": _IMPORT,
            "grid_export_energy_entity": _EXPORT,
            "huawei_solar_batteries_charge_discharge_power": _BATTERY,
            "house_power_includes_ev_charger_power": True,
            "update_interval": 5,
            **overrides,
        }
    )


def _hass(*, recorder: bool = True, **states: str | None) -> Any:
    """Return a hass with a recorder in which every source gives a reading."""
    ready: dict[str, str | None] = {
        _HOUSE: "1200",
        _PV_POWER: "2400",
        _PV_ENERGY: "500",
        _IMPORT: "100",
        _EXPORT: "20",
        _BATTERY: "0",
        **{entity_id.replace("__", "."): state for entity_id, state in states.items()},
    }
    hass = make_fake_hass(
        {entity_id: state for entity_id, state in ready.items() if state is not None}
    )
    hass.config = SimpleNamespace(components={"recorder"} if recorder else set())
    return hass


def _tracker() -> EnergyTracker:
    return EnergyTracker(history_file="energy.json")


class _Statistics:
    """Stands in for the statistics reader: series per entity, calls recorded."""

    def __init__(
        self,
        mean: dict[str, dict[datetime, float]] | None = None,
        energy: dict[str, dict[datetime, float]] | None = None,
    ) -> None:
        self.mean = mean or {}
        self.energy = energy or {}
        self.calls: list[tuple[str, str, datetime, datetime, str | None]] = []
        self.error: Exception | None = None

    async def read_hourly_mean(
        self, entity_id: str, start: datetime, end: datetime, unit: str | None = None
    ) -> dict[datetime, float]:
        self.calls.append(("mean", entity_id, start, end, unit))
        if self.error is not None:
            raise self.error
        return dict(self.mean.get(entity_id, {}))

    async def read_hourly_energy(
        self, entity_id: str, start: datetime, end: datetime, unit: str | None = None
    ) -> dict[datetime, float]:
        self.calls.append(("energy", entity_id, start, end, unit))
        return dict(self.energy.get(entity_id, {}))


@pytest.fixture(autouse=True)
def _no_open_backfills() -> Generator[None]:
    backfill._attempts.clear()
    yield
    backfill._attempts.clear()


@pytest.fixture
def statistics() -> Generator[_Statistics]:
    stand_in = _Statistics(
        mean={
            _HOUSE: {_H0: 2000.0, _H0 + _HOUR: 1000.0},
            _PV_POWER: {_H0: 3000.0, _H0 + _HOUR: 500.0},
            _BATTERY: {_H0: 1500.0, _H0 + _HOUR: -500.0},
        },
        energy={
            _IMPORT: {_H0: 0.5},
            _EXPORT: {_H0: 0.25},
            _PV_ENERGY: {_H0: 2.75},
        },
    )
    with (
        patch(f"{_READER}.read_hourly_mean", stand_in.read_hourly_mean),
        patch(f"{_READER}.read_hourly_energy", stand_in.read_hourly_energy),
    ):
        yield stand_in


class TestBackfill:
    @pytest.mark.asyncio
    async def test_past_days_are_built_from_the_statistics(
        self, statistics: _Statistics
    ) -> None:
        tracker = _tracker()

        ran = await async_backfill_energy_history(_hass(), tracker, _NOW, _cfg(), _SITE)

        assert ran is True
        assert tracker.backfill_complete is True
        (day,) = tracker.daily_log.values()
        assert (day.date, day.backfilled) == ("2026-06-08", True)
        assert day.house_consumption_kwh == pytest.approx(3.0)
        assert day.grid_import_kwh == pytest.approx(0.5)
        assert day.grid_export_kwh == pytest.approx(0.25)
        # The PV meter where it has the hour, the PV power for the other.
        assert day.pv_kwh == pytest.approx(2.75 + 0.5)
        assert day.pv_hours == pytest.approx(2.0)
        assert day.battery_charged_kwh == pytest.approx(1.5)
        assert day.battery_discharged_kwh == pytest.approx(0.5)
        assert backfill._attempts == {}

    @pytest.mark.asyncio
    async def test_statistics_are_asked_in_the_units_the_live_path_uses(
        self, statistics: _Statistics
    ) -> None:
        await async_backfill_energy_history(_hass(), _tracker(), _NOW, _cfg(), _SITE)

        units = {
            (kind, entity_id): unit for kind, entity_id, *_, unit in statistics.calls
        }
        assert units == {
            ("mean", _HOUSE): "W",
            ("mean", _PV_POWER): "W",
            ("mean", _BATTERY): "W",
            ("energy", _PV_ENERGY): "kWh",
            ("energy", _IMPORT): "kWh",
            ("energy", _EXPORT): "kWh",
        }
        starts = {start for _kind, _entity, start, _end, _unit in statistics.calls}
        assert starts == {_NOW - timedelta(days=367)}
        assert {end for *_, end, _unit in statistics.calls} == {_NOW}

    @pytest.mark.asyncio
    async def test_a_measured_day_is_never_changed(
        self, statistics: _Statistics
    ) -> None:
        measured = EnergyDayEntry("2026-06-08", house_consumption_kwh=7.0)
        tracker = _tracker()
        tracker.daily_log["2026-06-08"] = measured
        statistics.mean[_HOUSE][_H0 - 24 * _HOUR] = 4000.0

        await async_backfill_energy_history(_hass(), tracker, _NOW, _cfg(), _SITE)

        assert tracker.daily_log["2026-06-08"] is measured
        assert measured.house_consumption_kwh == pytest.approx(7.0)
        assert tracker.daily_log["2026-06-07"].backfilled is True
        assert tracker.daily_log["2026-06-07"].house_consumption_kwh == pytest.approx(
            4.0
        )

    @pytest.mark.asyncio
    async def test_it_runs_once(self, statistics: _Statistics) -> None:
        tracker = _tracker()
        await async_backfill_energy_history(_hass(), tracker, _NOW, _cfg(), _SITE)
        statistics.calls.clear()

        ran = await async_backfill_energy_history(
            _hass(), tracker, _NOW + _HOUR, _cfg(), _SITE
        )

        assert ran is False
        assert statistics.calls == []

    @pytest.mark.asyncio
    async def test_without_statistics_nothing_changes(self) -> None:
        tracker = _tracker()
        empty = _Statistics()
        with (
            patch(f"{_READER}.read_hourly_mean", empty.read_hourly_mean),
            patch(f"{_READER}.read_hourly_energy", empty.read_hourly_energy),
        ):
            ran = await async_backfill_energy_history(
                _hass(), tracker, _NOW, _cfg(), _SITE
            )

        assert ran is True
        assert tracker.daily_log == {}
        assert tracker.as_sensor_attributes(_NOW.date())["backfilled_days"] == 0

    @pytest.mark.asyncio
    async def test_without_a_recorder_nothing_is_read(
        self, statistics: _Statistics
    ) -> None:
        tracker = _tracker()

        ran = await async_backfill_energy_history(
            _hass(recorder=False), tracker, _NOW, _cfg(), _SITE
        )

        assert ran is False
        assert tracker.backfill_complete is False
        assert statistics.calls == []

    @pytest.mark.asyncio
    async def test_nothing_is_configured(self, statistics: _Statistics) -> None:
        tracker = _tracker()

        ran = await async_backfill_energy_history(
            _hass(), tracker, _NOW, SensorConfig(), _SITE
        )

        assert ran is True
        assert tracker.daily_log == {}
        assert statistics.calls == []


class TestSourcesWithoutAState:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", [None, "unavailable", "unknown"])
    async def test_the_backfill_waits_for_a_source_without_a_state(
        self, statistics: _Statistics, state: str | None
    ) -> None:
        tracker = _tracker()
        hass = _hass(sensor__house_power=state)

        first = await async_backfill_energy_history(hass, tracker, _NOW, _cfg(), _SITE)
        later = await async_backfill_energy_history(
            hass, tracker, _NOW + timedelta(seconds=SOURCE_GRACE_S - 1), _cfg(), _SITE
        )

        assert (first, later) == (False, False)
        assert tracker.backfill_complete is False
        assert statistics.calls == []

    @pytest.mark.asyncio
    async def test_it_runs_as_soon_as_every_source_reports(
        self, statistics: _Statistics
    ) -> None:
        tracker = _tracker()
        await async_backfill_energy_history(
            _hass(sensor__house_power=None), tracker, _NOW, _cfg(), _SITE
        )

        ran = await async_backfill_energy_history(
            _hass(), tracker, _NOW + timedelta(minutes=5), _cfg(), _SITE
        )

        assert ran is True
        assert tracker.daily_log["2026-06-08"].house_hours == pytest.approx(2.0)

    @pytest.mark.asyncio
    async def test_after_the_wait_a_missing_source_keeps_the_backfill_open(
        self, statistics: _Statistics, caplog: pytest.LogCaptureFixture
    ) -> None:
        tracker = _tracker()
        hass = _hass(sensor__house_power="unavailable", sensor__grid_import=None)
        await async_backfill_energy_history(hass, tracker, _NOW, _cfg(), _SITE)
        after_wait = _NOW + timedelta(seconds=SOURCE_GRACE_S)

        with caplog.at_level(logging.INFO, logger="custom_components.hsem"):
            ran = await async_backfill_energy_history(
                hass, tracker, after_wait, _cfg(), _SITE
            )

        assert ran is False
        assert tracker.backfill_complete is False
        assert tracker.daily_log == {}
        assert statistics.calls == []
        assert f"waiting for a state from {_HOUSE}, {_IMPORT}" in caplog.text

    @pytest.mark.asyncio
    async def test_after_the_wait_it_looks_once_per_hour_and_logs_once(
        self, statistics: _Statistics, caplog: pytest.LogCaptureFixture
    ) -> None:
        tracker = _tracker()
        missing = _hass(sensor__house_power=None)
        await async_backfill_energy_history(missing, tracker, _NOW, _cfg(), _SITE)
        after_wait = _NOW + timedelta(seconds=SOURCE_GRACE_S)

        with caplog.at_level(logging.INFO, logger="custom_components.hsem"):
            await async_backfill_energy_history(
                missing, tracker, after_wait, _cfg(), _SITE
            )
            # The source is back, but the next look is an hour away.
            too_soon = await async_backfill_energy_history(
                _hass(),
                tracker,
                after_wait + timedelta(seconds=RETRY_INTERVAL_S - 1),
                _cfg(),
                _SITE,
            )
            still_missing = await async_backfill_energy_history(
                missing,
                tracker,
                after_wait + timedelta(seconds=RETRY_INTERVAL_S),
                _cfg(),
                _SITE,
            )

        assert (too_soon, still_missing) == (False, False)
        assert tracker.backfill_complete is False
        assert statistics.calls == []
        assert caplog.text.count("waiting for a state from") == 1

        ran = await async_backfill_energy_history(
            _hass(),
            tracker,
            after_wait + timedelta(seconds=2 * RETRY_INTERVAL_S),
            _cfg(),
            _SITE,
        )

        assert ran is True
        assert tracker.backfill_complete is True
        assert tracker.daily_log["2026-06-08"].house_hours == pytest.approx(2.0)
        assert backfill._attempts == {}

    @pytest.mark.asyncio
    async def test_a_source_that_is_not_configured_does_not_hold_it_back(
        self, statistics: _Statistics
    ) -> None:
        tracker = _tracker()
        cfg = _cfg(grid_import_energy_entity=None)

        ran = await async_backfill_energy_history(
            _hass(sensor__grid_import=None), tracker, _NOW, cfg, _SITE
        )

        assert ran is True
        assert tracker.backfill_complete is True
        assert tracker.daily_log["2026-06-08"].grid_import_hours == pytest.approx(0.0)
        assert _IMPORT not in {entity_id for _kind, entity_id, *_ in statistics.calls}

    @pytest.mark.asyncio
    async def test_overlapping_cycles_close_the_backfill_once(
        self, statistics: _Statistics
    ) -> None:
        tracker = _tracker()

        async def overlapping(*_args: Any, **_kwargs: Any) -> dict[datetime, float]:
            # Another cycle finished this backfill while the read was awaited.
            backfill._attempts.pop(tracker.history_file, None)
            return {}

        with patch(f"{_READER}.read_hourly_mean", overlapping):
            ran = await async_backfill_energy_history(
                _hass(), tracker, _NOW, _cfg(), _SITE
            )

        assert ran is True
        assert backfill._attempts == {}


class TestMeterGlitches:
    @pytest.mark.asyncio
    async def test_a_meter_that_reset_and_came_back_adds_no_energy(
        self, statistics: _Statistics, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The meter reported 0 and returned: the sum jumps by its reading.
        statistics.energy[_IMPORT] = {_H0: 0.5, _H0 + _HOUR: 48123.4}
        tracker = _tracker()

        with caplog.at_level(logging.INFO, logger="custom_components.hsem"):
            await async_backfill_energy_history(_hass(), tracker, _NOW, _cfg(), _SITE)

        day = tracker.daily_log["2026-06-08"]
        assert day.grid_import_kwh == pytest.approx(0.5)
        assert day.grid_import_hours == pytest.approx(1.0)
        assert "1 hourly values above 50 kWh dropped" in caplog.text


class TestEvChargers:
    @pytest.mark.asyncio
    async def test_charger_power_is_added_when_the_house_sensor_leaves_it_out(
        self, statistics: _Statistics
    ) -> None:
        statistics.mean[_EV] = {_H0: 4000.0, _H0 + _HOUR: -50.0}
        statistics.mean[_EV2] = {_H0: 1000.0}
        tracker = _tracker()
        cfg = _cfg(
            house_power_includes_ev_charger_power=False,
            ev=EVChargerConfig(power_entity=_EV),
            ev_second=EVChargerConfig(power_entity=_EV2),
        )
        hass = _hass(sensor__ev_power="0", sensor__ev2_power="0")

        await async_backfill_energy_history(hass, tracker, _NOW, cfg, _SITE)

        day = tracker.daily_log["2026-06-08"]
        # The second hour has no statistics of the second charger: unknown.
        assert day.house_hours == pytest.approx(1.0)
        assert day.house_consumption_kwh == pytest.approx(2.0 + 4.0 + 1.0)

    @pytest.mark.asyncio
    async def test_a_charger_in_the_house_power_is_not_read(
        self, statistics: _Statistics
    ) -> None:
        cfg = _cfg(ev=EVChargerConfig(power_entity=_EV))

        await async_backfill_energy_history(_hass(), _tracker(), _NOW, cfg, _SITE)

        assert _EV not in {entity_id for _kind, entity_id, *_ in statistics.calls}

    @pytest.mark.asyncio
    async def test_a_charger_without_a_state_keeps_the_backfill_open(
        self, statistics: _Statistics
    ) -> None:
        tracker = _tracker()
        cfg = _cfg(
            house_power_includes_ev_charger_power=False,
            ev=EVChargerConfig(power_entity=_EV),
        )
        hass = _hass()
        await async_backfill_energy_history(hass, tracker, _NOW, cfg, _SITE)

        ran = await async_backfill_energy_history(
            hass, tracker, _NOW + timedelta(seconds=SOURCE_GRACE_S), cfg, _SITE
        )

        assert ran is False
        assert tracker.backfill_complete is False
        assert statistics.calls == []


class TestRecorderErrors:
    @pytest.mark.asyncio
    async def test_a_failed_read_is_logged_and_tried_again_later(
        self, statistics: _Statistics, caplog: pytest.LogCaptureFixture
    ) -> None:
        tracker = _tracker()
        statistics.error = RuntimeError("database is locked")

        with caplog.at_level(logging.WARNING):
            failed = await async_backfill_energy_history(
                _hass(), tracker, _NOW, _cfg(), _SITE
            )
        calls = len(statistics.calls)
        too_soon = await async_backfill_energy_history(
            _hass(),
            tracker,
            _NOW + timedelta(seconds=RETRY_INTERVAL_S - 1),
            _cfg(),
            _SITE,
        )

        assert (failed, too_soon) == (False, False)
        assert len(statistics.calls) == calls
        assert tracker.backfill_complete is False
        assert tracker.daily_log == {}
        assert "database is locked" in caplog.text

        statistics.error = None
        ran = await async_backfill_energy_history(
            _hass(), tracker, _NOW + timedelta(seconds=RETRY_INTERVAL_S), _cfg(), _SITE
        )

        assert ran is True
        assert tracker.daily_log["2026-06-08"].backfilled is True


class TestThroughTheCycle:
    def _coordinator(self, tmp_path: Path) -> CoordinatorSharedState:
        hass = _hass()
        hass.config.config_dir = str(tmp_path)
        return cast(
            CoordinatorSharedState,
            SimpleNamespace(hass=hass, _energy_tracker=EnergyTracker()),
        )

    @pytest.mark.asyncio
    async def test_the_first_cycle_backfills_and_saves(
        self, tmp_path: Path, statistics: _Statistics
    ) -> None:
        coordinator = self._coordinator(tmp_path)
        live = LiveState()
        live.battery_usable_capacity_kwh = 9.0

        await async_track_energy(coordinator, _NOW, live, _cfg())

        reloaded = EnergyTracker(history_file=coordinator._energy_tracker.history_file)
        await reloaded.load_history()
        assert reloaded.backfill_complete is True
        assert reloaded.daily_log["2026-06-08"].backfilled is True
        assert reloaded.daily_log["2026-06-08"].usable_capacity_kwh == pytest.approx(
            9.0
        )

    @pytest.mark.asyncio
    async def test_a_backfill_that_did_not_run_does_not_save(
        self, tmp_path: Path
    ) -> None:
        coordinator = self._coordinator(tmp_path)
        coordinator._energy_tracker._saved_at = _NOW
        persist = AsyncMock()

        with (
            patch(
                "custom_components.hsem.coordinator_energy"
                ".async_backfill_energy_history",
                AsyncMock(return_value=False),
            ),
            patch(
                "custom_components.hsem.coordinator_energy.persist_all_trackers",
                persist,
            ),
        ):
            await async_track_energy(coordinator, _NOW, LiveState(), _cfg())

        persist.assert_not_called()


class TestHouseAndPv:
    """Issue #1455: ``hsem.what_if`` reads only what it rebuilds a day from."""

    @pytest.mark.asyncio
    async def test_grid_and_battery_statistics_are_not_read(
        self, statistics: _Statistics
    ) -> None:
        hourly = await async_read_house_and_pv(_hass(), _cfg(), _H0, _H0 + 2 * _HOUR)

        assert {call[1] for call in statistics.calls} == {_HOUSE, _PV_POWER, _PV_ENERGY}
        assert hourly.house_kwh == {
            _H0: pytest.approx(2.0),
            _H0 + _HOUR: pytest.approx(1.0),
        }
        # The PV meter where it has the hour, the PV power for the other.
        assert hourly.pv_kwh == {
            _H0: pytest.approx(2.75),
            _H0 + _HOUR: pytest.approx(0.5),
        }
        assert not hourly.grid_import_kwh
        assert not hourly.grid_export_kwh
        assert not hourly.battery_net_kwh
