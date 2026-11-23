"""The energy history backfill against a real recorder (issue #1450).

Statistics are written through Home Assistant's own import API, read through
the recorder executor by the backfill, and published by the monthly energy
sensors.  Nothing between the recorder and the sensor attribute is faked.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

import homeassistant.util.dt as dt_util
from homeassistant.core import HomeAssistant

from custom_components.hsem import coordinator_energy_backfill as backfill
from custom_components.hsem.coordinator_energy import async_track_energy
from custom_components.hsem.coordinator_state import CoordinatorSharedState
from custom_components.hsem.custom_sensors.monthly_energy_sensors import (
    build_monthly_energy_sensors,
)
from custom_components.hsem.models.energy_tracker import EnergyTracker
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from tests.ml.test_statistics_reader_recorder import (
    _import,
    hass,  # noqa: F401 — the real-recorder fixture
)

_HOUSE = "sensor.house_power"
_IMPORT = "sensor.grid_import_energy"
_EXPORT = "sensor.grid_export_energy"
_BATTERY = "sensor.battery_power"
_HOUR = timedelta(hours=1)
_MINUTE = timedelta(minutes=1)
_SENSORS = "custom_components.hsem.custom_sensors.monthly_energy_sensors"


@pytest.mark.asyncio
async def test_recorder_statistics_reach_the_monthly_sensors(
    hass: HomeAssistant,  # noqa: F811
) -> None:
    now = dt_util.now()
    # 10:00 local time three days ago, as a UTC hour start.
    first = dt_util.as_utc(
        dt_util.start_of_local_day(now - timedelta(days=3)) + 10 * _HOUR
    )
    day = dt_util.as_local(first).date().isoformat()
    # Stored in kW and Wh: the backfill must convert to W and kWh.
    await _import(
        hass,
        _HOUSE,
        "kW",
        "power",
        [{"start": first + i * _HOUR, "mean": 2.0 + i} for i in range(2)],
    )
    await _import(
        hass,
        _IMPORT,
        "Wh",
        "energy",
        [
            {"start": first + (i - 1) * _HOUR, "state": total, "sum": total}
            for i, total in enumerate([1000.0, 1500.0, 2500.0])
        ],
    )
    # The export meter reported 0 and came back: the sum jumps by its reading.
    await _import(
        hass,
        _EXPORT,
        "kWh",
        "energy",
        [
            {"start": first + (i - 1) * _HOUR, "state": total, "sum": total}
            for i, total in enumerate([100.0, 100.25, 48100.25])
        ],
    )
    hass.states.async_set(_HOUSE, "1200", {"unit_of_measurement": "W"})
    hass.states.async_set(_EXPORT, "48000.5", {"unit_of_measurement": "kWh"})
    hass.states.async_set(_IMPORT, "5000", {"unit_of_measurement": "kWh"})
    # The battery power sensor has no statistics, and no state at first.
    cfg = SensorConfig(
        house_consumption_power=_HOUSE,
        grid_import_energy_entity=_IMPORT,
        grid_export_energy_entity=_EXPORT,
        huawei_solar_batteries_charge_discharge_power=_BATTERY,
        house_power_includes_ev_charger_power=True,
        update_interval=5,
    )
    tracker = EnergyTracker()
    coordinator = cast(
        CoordinatorSharedState, SimpleNamespace(hass=hass, _energy_tracker=tracker)
    )

    try:
        await async_track_energy(coordinator, now, LiveState(), cfg)
        # The battery power sensor has no state: the backfill waits for it.
        assert tracker.daily_log == {}
        assert tracker.backfill_complete is False
        hass.states.async_set(_BATTERY, "0", {"unit_of_measurement": "W"})
        await async_track_energy(coordinator, now + 5 * _MINUTE, LiveState(), cfg)
    finally:
        backfill._attempts.clear()

    entry = tracker.daily_log[day]
    assert entry.backfilled is True
    assert entry.house_consumption_kwh == pytest.approx(5.0)
    assert entry.grid_import_kwh == pytest.approx(1.5)
    # The hour of the jump is unknown, not 48 000 kWh.
    assert entry.grid_export_kwh == pytest.approx(0.25)
    assert entry.grid_export_hours == pytest.approx(1.0)
    assert entry.battery_hours == pytest.approx(0.0)

    sensor_coordinator = MagicMock()
    sensor_coordinator._energy_tracker = tracker
    sensors = build_monthly_energy_sensors(MagicMock(), sensor_coordinator)
    with patch(f"{_SENSORS}.hsem_now", return_value=now):
        attributes = sensors[0].extra_state_attributes or {}

    assert attributes["backfilled_days"] == 1
    assert attributes["backfilled_from"] == day
    assert attributes["backfilled_until"] == day
    month = next(row for row in attributes["monthly"] if row["month"] == day[:7])
    assert month["backfilled_days"] == 1
    assert month["house_consumption_kwh"] == pytest.approx(5.0)
    assert month["grid_import_kwh"] == pytest.approx(1.5)
    assert month["battery_charged_kwh"] is None
    assert month["self_sufficiency"] == pytest.approx(70.0)
