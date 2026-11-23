"""The statistics reader against a real recorder (issue #1433).

Every other test of the backfill fakes ``statistics_during_period`` or the
reader, so a wrong argument order, row shape or unit would pass there.  Here
a real Home Assistant recorder runs on a temporary SQLite database:
statistics are written through Home Assistant's own import API and read back
through :class:`StatisticsReader`.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

import homeassistant.util.dt as dt_util
from homeassistant import bootstrap, loader
from homeassistant.components.recorder import (
    get_instance,  # pyright: ignore[reportPrivateImportUsage] — HA public API, not in stubs
)
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import recorder as recorder_helper
from homeassistant.setup import async_setup_component

from custom_components.hsem.custom_sensors import consumption_backfill
from custom_components.hsem.custom_sensors.consumption_backfill import (
    async_consumption_profile,
)
from custom_components.hsem.ml.statistics_reader import StatisticsReader
from custom_components.hsem.utils.schema import vol

HOUSE_POWER = "sensor.house_power"
HOUSE_ENERGY = "sensor.house_energy"
START = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


@pytest.fixture(autouse=True)
def long_term_statistics() -> None:
    """Use the real reader here instead of the empty one from ``conftest``."""


@pytest_asyncio.fixture
async def hass(tmp_path: Path) -> AsyncGenerator[HomeAssistant]:
    """A Home Assistant core with a real recorder on a temporary database."""
    instance = HomeAssistant(str(tmp_path))
    loader.async_setup(instance)
    instance.config_entries = ConfigEntries(instance, {})
    assert await bootstrap.async_load_base_functionality(instance)
    await instance.config.async_set_time_zone("Europe/Copenhagen")
    # No custom integrations are needed, and the editable install of this
    # repository is not a directory the loader could scan.
    no_custom_integrations: dict[str, loader.Integration] = {}
    instance.data[loader.DATA_CUSTOM_COMPONENTS] = no_custom_integrations
    # The recorder only starts working once Home Assistant is running.
    instance.set_state(CoreState.running)
    recorder_helper.async_initialize_recorder(instance)
    assert await async_setup_component(
        instance, "recorder", {"recorder": {"db_url": f"sqlite:///{tmp_path}/hsem.db"}}
    )
    await instance.async_block_till_done()
    try:
        yield instance
    finally:
        await instance.async_stop(force=True)
        dt_util.set_default_time_zone(UTC)


async def _import(
    hass: HomeAssistant,
    statistic_id: str,
    unit: str,
    unit_class: str,
    rows: list[dict[str, Any]],
) -> None:
    """Write hourly statistics the way the recorder stores a sensor's own."""
    has_sum = "sum" in rows[0]
    async_import_statistics(
        hass,
        {
            "has_mean": not has_sum,
            "mean_type": (
                StatisticMeanType.NONE if has_sum else StatisticMeanType.ARITHMETIC
            ),
            "has_sum": has_sum,
            "name": None,
            "source": "recorder",
            "statistic_id": statistic_id,
            "unit_of_measurement": unit,
            "unit_class": unit_class,
        },
        rows,  # type: ignore[arg-type]
    )
    await get_instance(hass).async_block_till_done()


@pytest.mark.asyncio
async def test_hourly_mean_is_read_back_from_the_recorder(hass: HomeAssistant) -> None:
    await _import(
        hass,
        HOUSE_POWER,
        "kW",
        "power",
        [{"start": START + i * HOUR, "mean": 0.5 + i} for i in range(3)],
    )
    reader = StatisticsReader(hass)

    converted = await reader.read_hourly_mean(HOUSE_POWER, START, START + 2 * HOUR, "W")
    stored = await reader.read_hourly_mean(HOUSE_POWER, START, START + 3 * HOUR)

    # The end is exclusive, the keys are UTC hour starts and W is converted.
    assert converted == {
        START: pytest.approx(500.0),
        START + HOUR: pytest.approx(1500.0),
    }
    assert stored == {
        START: pytest.approx(0.5),
        START + HOUR: pytest.approx(1.5),
        START + 2 * HOUR: pytest.approx(2.5),
    }


@pytest.mark.asyncio
async def test_hourly_energy_is_read_back_from_the_recorder(
    hass: HomeAssistant,
) -> None:
    sums = [1000.0, 1400.0, 2600.0, 2600.0]  # Wh at the end of each hour
    await _import(
        hass,
        HOUSE_ENERGY,
        "Wh",
        "energy",
        [
            {"start": START + (i - 1) * HOUR, "state": total, "sum": total}
            for i, total in enumerate(sums)
        ],
    )

    hours = await StatisticsReader(hass).read_hourly_energy(
        HOUSE_ENERGY, START, START + 3 * HOUR, "kWh"
    )

    assert hours == {
        START: pytest.approx(0.4),
        START + HOUR: pytest.approx(1.2),
        START + 2 * HOUR: pytest.approx(0.0),
    }
    assert (
        await StatisticsReader(hass).read_hourly_energy(
            "sensor.unknown", START, START + 3 * HOUR, "kWh"
        )
        == {}
    )


@pytest.mark.asyncio
async def test_the_consumption_profile_is_built_from_recorder_statistics(
    hass: HomeAssistant,
) -> None:
    """End to end: recorder rows become kWh per local hour block."""
    first = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) - 3 * HOUR
    await _import(
        hass,
        HOUSE_POWER,
        "W",
        "power",
        [{"start": first + i * HOUR, "mean": 400.0 + 100.0 * i} for i in range(2)],
    )
    hass.states.async_set(HOUSE_POWER, "450", {"unit_of_measurement": "W"})
    entry = MagicMock()
    entry.entry_id = "recorder_entry"
    entry.options = {
        "hsem_house_consumption_power": HOUSE_POWER,
        "hsem_ev_charger_power": vol.UNDEFINED,
        "hsem_ev_second_charger_power": vol.UNDEFINED,
        "hsem_house_power_includes_ev_charger_power": False,
    }
    entry.data = {}

    try:
        profile = await async_consumption_profile(hass, entry)
    finally:
        consumption_backfill._profile_cache.clear()
        consumption_backfill._profile_locks.clear()

    def block(hour_start: datetime) -> tuple[str, int]:
        local = dt_util.as_local(hour_start)
        return local.date().isoformat(), local.hour

    assert profile == {
        block(first): pytest.approx(0.4),
        block(first + HOUR): pytest.approx(0.5),
    }


@pytest.mark.asyncio
async def test_nothing_is_backfilled_until_the_sensor_reports_its_unit(
    hass: HomeAssistant,
) -> None:
    """Statistics stored in kW for a sensor that reports W now (issue #1451).

    Without a unit requested, Home Assistant converts to the unit of the
    current state and returns the stored unit when there is no state.  At
    start-up that would make the days 1000 times too small.
    """
    first = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) - 3 * HOUR
    await _import(
        hass,
        HOUSE_POWER,
        "kW",
        "power",
        [{"start": first + i * HOUR, "mean": 0.4 + 0.1 * i} for i in range(2)],
    )
    entry = MagicMock()
    entry.entry_id = "recorder_entry"
    entry.options = {
        "hsem_house_consumption_power": HOUSE_POWER,
        "hsem_ev_charger_power": vol.UNDEFINED,
        "hsem_ev_second_charger_power": vol.UNDEFINED,
        "hsem_house_power_includes_ev_charger_power": False,
    }
    entry.data = {}
    reader = StatisticsReader(hass)
    end = first + 2 * HOUR

    try:
        # No state: the recorder answers in the stored unit, kW.
        assert hass.states.get(HOUSE_POWER) is None
        stored = await reader.read_hourly_mean(HOUSE_POWER, first, end)
        before = await async_consumption_profile(hass, entry)

        # With a state in W the same read is converted to W.
        hass.states.async_set(HOUSE_POWER, "450", {"unit_of_measurement": "W"})
        current = await reader.read_hourly_mean(HOUSE_POWER, first, end)
        after = await async_consumption_profile(hass, entry)
    finally:
        consumption_backfill._profile_cache.clear()
        consumption_backfill._profile_locks.clear()

    def block(hour_start: datetime) -> tuple[str, int]:
        local = dt_util.as_local(hour_start)
        return local.date().isoformat(), local.hour

    assert stored == {first: pytest.approx(0.4), first + HOUR: pytest.approx(0.5)}
    assert current == {first: pytest.approx(400.0), first + HOUR: pytest.approx(500.0)}
    assert before == {}
    assert after == {
        block(first): pytest.approx(0.4),
        block(first + HOUR): pytest.approx(0.5),
    }
