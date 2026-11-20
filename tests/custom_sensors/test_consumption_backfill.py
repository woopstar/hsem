"""Hour-block averages are backfilled from long-term statistics (issue #1433).

A new install has empty average windows.  The days HSEM did not observe are
taken from the hourly statistics of the house power source, built the way the
live path measures it.  Only the recorder's statistics are faked here: the
profile, the average sensor and its attributes are the real ones.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
import voluptuous as vol

from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.custom_sensors import consumption_backfill
from custom_components.hsem.custom_sensors.avg_sensor import HSEMAvgSensor
from custom_components.hsem.custom_sensors.consumption_backfill import (
    BACKFILL_DAYS,
    PROFILE_REFRESH,
    async_consumption_profile,
    missing_days,
)
from tests.test_avg_sensor_negative_guard import _avg_sensor

COPENHAGEN = ZoneInfo("Europe/Copenhagen")
KOLKATA = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 8, 12, 8, 0, tzinfo=UTC)  # 10:00 in Copenhagen
HOUSE = "sensor.house_power"
EV = "sensor.ev_power"
EV_2 = "sensor.ev_2_power"

_BACKFILL = "custom_components.hsem.custom_sensors.consumption_backfill"
_AVG = "custom_components.hsem.custom_sensors.avg_sensor"


class _Statistics:
    """Hourly mean statistics per entity, standing in for the recorder."""

    def __init__(self, means: dict[str, dict[datetime, float]]) -> None:
        self.means = means
        self.calls: list[tuple[str, datetime, datetime, str | None]] = []

    def reader(self, _hass: Any) -> _Statistics:
        return self

    async def read_hourly_mean(
        self, entity_id: str, start: datetime, end: datetime, unit: str | None = None
    ) -> dict[datetime, float]:
        self.calls.append((entity_id, start, end, unit))
        return dict(self.means.get(entity_id, {}))


@pytest.fixture(autouse=True)
def _clear_profile_cache() -> Generator[None]:
    consumption_backfill._profile_cache.clear()
    consumption_backfill._profile_locks.clear()
    yield
    consumption_backfill._profile_cache.clear()
    consumption_backfill._profile_locks.clear()


@pytest.fixture
def clock() -> Generator[SimpleNamespace]:
    """Pin Home Assistant's clock and time zone; both can be changed."""
    state = SimpleNamespace(now=NOW, zone=COPENHAGEN)
    with (
        patch("homeassistant.util.dt.utcnow", side_effect=lambda: state.now),
        patch(
            "homeassistant.util.dt.now",
            side_effect=lambda: state.now.astimezone(state.zone),
        ),
        patch(
            "homeassistant.util.dt.as_local",
            side_effect=lambda value: value.astimezone(state.zone),
        ),
    ):
        yield state


def _hass(*, recorder: bool = True) -> MagicMock:
    hass = MagicMock()
    hass.config.components = {"recorder"} if recorder else set()
    return hass


def _entry(**options: Any) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.options = {
        "hsem_house_consumption_power": HOUSE,
        "hsem_ev_charger_power": vol.UNDEFINED,
        "hsem_ev_second_charger_power": vol.UNDEFINED,
        "hsem_house_power_includes_ev_charger_power": False,
        **options,
    }
    entry.data = {}
    return entry


def _local(day: int, hour: int, month: int = 8) -> datetime:
    """A Copenhagen wall-clock hour of 2026 as the UTC key statistics use."""
    return datetime(2026, month, day, hour, tzinfo=COPENHAGEN).astimezone(UTC)


async def _profile(
    statistics: _Statistics, entry: MagicMock, hass: MagicMock | None = None
) -> dict[tuple[str, int], float]:
    with patch(f"{_BACKFILL}.StatisticsReader", statistics.reader):
        return await async_consumption_profile(hass or _hass(), entry)


# ---------------------------------------------------------------------------
# The profile
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mean_house_power_becomes_energy_per_local_hour_block(
    clock: SimpleNamespace,
) -> None:
    statistics = _Statistics({HOUSE: {_local(10, 14): 500.0, _local(11, 23): 1250.0}})

    profile = await _profile(statistics, _entry())

    assert profile == {
        ("2026-08-10", 14): pytest.approx(0.5),
        ("2026-08-11", 23): pytest.approx(1.25),
    }
    assert statistics.calls == [(HOUSE, NOW - timedelta(days=BACKFILL_DAYS), NOW, "W")]


@pytest.mark.asyncio
async def test_ev_chargers_are_subtracted_when_house_power_includes_them(
    clock: SimpleNamespace,
) -> None:
    """Same rule as the live path; an hour lacking a charger's data is skipped."""
    statistics = _Statistics(
        {
            HOUSE: {
                _local(10, 14): 3000.0,
                _local(10, 15): 3000.0,
                _local(10, 16): 100.0,
            },
            EV: {_local(10, 14): 2000.0, _local(10, 15): 2000.0, _local(10, 16): 400.0},
            EV_2: {_local(10, 14): 500.0, _local(10, 16): 0.0},
        }
    )
    entry = _entry(
        hsem_ev_charger_power=EV,
        hsem_ev_second_charger_power=EV_2,
        hsem_house_power_includes_ev_charger_power=True,
    )

    profile = await _profile(statistics, entry)

    # 15:00 has no second-charger statistics and 16:00 would be negative.
    assert profile == {("2026-08-10", 14): pytest.approx(0.5)}
    assert [call[0] for call in statistics.calls] == [HOUSE, EV, EV_2]


@pytest.mark.asyncio
async def test_ev_chargers_are_ignored_when_house_power_excludes_them(
    clock: SimpleNamespace,
) -> None:
    statistics = _Statistics(
        {HOUSE: {_local(10, 14): 3000.0}, EV: {_local(10, 14): 2000.0}}
    )
    entry = _entry(hsem_ev_charger_power=EV)

    profile = await _profile(statistics, entry)

    assert profile == {("2026-08-10", 14): pytest.approx(3.0)}
    assert [call[0] for call in statistics.calls] == [HOUSE]


@pytest.mark.asyncio
async def test_both_folds_of_the_autumn_repeated_hour_share_one_block(
    clock: SimpleNamespace,
) -> None:
    """02:00 happens twice on 25 October; the utility meter counts both."""
    first = datetime(2026, 10, 25, 0, tzinfo=UTC)
    statistics = _Statistics({HOUSE: {first: 400.0, first + timedelta(hours=1): 600.0}})

    profile = await _profile(statistics, _entry())

    assert profile == {("2026-10-25", 2): pytest.approx(1.0)}


@pytest.mark.asyncio
async def test_a_repeated_hour_with_one_fold_incomplete_is_skipped(
    clock: SimpleNamespace,
) -> None:
    first = datetime(2026, 10, 25, 0, tzinfo=UTC)
    second = first + timedelta(hours=1)
    statistics = _Statistics({HOUSE: {first: 400.0, second: 600.0}, EV: {first: 100.0}})
    entry = _entry(
        hsem_ev_charger_power=EV, hsem_house_power_includes_ev_charger_power=True
    )

    assert await _profile(statistics, entry) == {}


@pytest.mark.asyncio
async def test_a_time_zone_off_the_hour_is_not_backfilled(
    clock: SimpleNamespace,
) -> None:
    """UTC statistics hours do not line up with hour blocks at UTC+5:30."""
    clock.zone = KOLKATA
    statistics = _Statistics({HOUSE: {_local(10, 14): 500.0}})

    assert await _profile(statistics, _entry()) == {}


@pytest.mark.asyncio
async def test_no_house_statistics_means_no_profile(clock: SimpleNamespace) -> None:
    statistics = _Statistics({EV: {_local(10, 14): 500.0}})
    entry = _entry(
        hsem_ev_charger_power=EV, hsem_house_power_includes_ev_charger_power=True
    )

    assert await _profile(statistics, entry) == {}
    assert [call[0] for call in statistics.calls] == [HOUSE]


@pytest.mark.asyncio
async def test_nothing_is_read_without_a_recorder_or_a_house_source(
    clock: SimpleNamespace,
) -> None:
    statistics = _Statistics({HOUSE: {_local(10, 14): 500.0}})

    assert await _profile(statistics, _entry(), _hass(recorder=False)) == {}
    with patch(f"{_BACKFILL}.entry_house_power_entity", return_value=None):
        assert await _profile(statistics, _entry()) == {}
    assert statistics.calls == []


@pytest.mark.asyncio
async def test_one_read_is_shared_for_an_hour(clock: SimpleNamespace) -> None:
    """All 96 average sensors of an entry use the same read."""
    statistics = _Statistics({HOUSE: {_local(10, 14): 500.0}})
    entry = _entry()

    first = await _profile(statistics, entry)
    clock.now = NOW + PROFILE_REFRESH - timedelta(seconds=1)
    second = await _profile(statistics, entry)
    assert second is first
    assert len(statistics.calls) == 1

    clock.now = NOW + PROFILE_REFRESH
    await _profile(statistics, entry)
    assert len(statistics.calls) == 2


@pytest.mark.asyncio
async def test_a_changed_source_is_read_at_once(clock: SimpleNamespace) -> None:
    statistics = _Statistics({HOUSE: {_local(10, 14): 500.0}})

    await _profile(statistics, _entry())
    await _profile(statistics, _entry(hsem_house_consumption_power="sensor.other"))

    assert [call[0] for call in statistics.calls] == [HOUSE, "sensor.other"]


def test_missing_days_are_the_newest_days_hsem_has_no_sample_for() -> None:
    profile = {
        ("2026-08-08", 14): 0.8,
        ("2026-08-09", 14): 0.9,
        ("2026-08-10", 14): 1.0,
        ("2026-08-11", 14): 1.1,
        ("2026-08-11", 15): 5.0,
    }

    assert missing_days(profile, 14, {"2026-08-10"}, 2) == {
        "2026-08-11": 1.1,
        "2026-08-09": 0.9,
    }
    assert missing_days(profile, 14, set(), 0) == {}
    assert missing_days(profile, 3, set(), 5) == {}


# ---------------------------------------------------------------------------
# The average sensor
# ---------------------------------------------------------------------------


def _sensor(average: int) -> HSEMAvgSensor:
    """A real 14-15 average sensor on a config entry with a house source."""
    sensor = _avg_sensor(average=average)
    sensor._config_entry = _entry()
    sensor.hass = _hass()
    sensor._async_track_entities = AsyncMock()  # type: ignore[method-assign]
    sensor.async_write_ha_state = MagicMock()  # type: ignore[method-assign,misc]
    return sensor


def _block_14(days: dict[int, float]) -> _Statistics:
    """Mean house power in W for the 14-15 block of the given August days."""
    return _Statistics({HOUSE: {_local(day, 14): watts for day, watts in days.items()}})


async def _update(sensor: HSEMAvgSensor, statistics: _Statistics) -> None:
    """Run one update with no completed utility-meter block to store."""
    with (
        patch(f"{_BACKFILL}.StatisticsReader", statistics.reader),
        patch(f"{_AVG}.ha_get_entity_state_and_convert", return_value=None),
    ):
        await sensor._async_handle_update()


@pytest.mark.asyncio
async def test_an_empty_window_is_seeded_on_the_first_update(
    clock: SimpleNamespace,
) -> None:
    """A fresh install has a load value for the block after one update."""
    sensor = _sensor(average=3)
    statistics = _block_14({8: 800.0, 9: 900.0, 10: 1000.0, 11: 1100.0})

    await _update(sensor, statistics)

    assert sensor._measurements == {
        "2026-08-09": pytest.approx(0.9),
        "2026-08-10": pytest.approx(1.0),
        "2026-08-11": pytest.approx(1.1),
    }
    assert sensor.state == pytest.approx(1.0)
    assert sensor.available is True
    attributes = sensor.extra_state_attributes
    assert attributes["backfilled_days"] == ["2026-08-09", "2026-08-10", "2026-08-11"]
    assert "backfilled_days" in HSEMAvgSensor._unrecorded_attributes
    sensor.async_write_ha_state.assert_called_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_day_hsem_measured_is_never_replaced_or_pushed_out(
    clock: SimpleNamespace,
) -> None:
    sensor = _sensor(average=3)
    sensor._measurements = {"2026-07-01": 7.0, "2026-08-10": 9.0}
    statistics = _block_14({8: 800.0, 9: 900.0, 10: 1000.0, 11: 1100.0})

    await _update(sensor, statistics)

    # One free place: the newest unobserved day fills it.  The old July day
    # and the measured 10 August stay as they were.
    assert sensor._measurements == {
        "2026-07-01": pytest.approx(7.0),
        "2026-08-10": pytest.approx(9.0),
        "2026-08-11": pytest.approx(1.1),
    }
    assert sensor.extra_state_attributes["backfilled_days"] == ["2026-08-11"]


@pytest.mark.asyncio
async def test_a_full_window_does_not_read_statistics(clock: SimpleNamespace) -> None:
    sensor = _sensor(average=1)
    sensor._measurements = {"2026-08-11": 2.0}
    statistics = _block_14({10: 1000.0})

    await _update(sensor, statistics)

    assert statistics.calls == []
    assert sensor._measurements == {"2026-08-11": pytest.approx(2.0)}
    assert sensor.extra_state_attributes["backfilled_days"] == []


@pytest.mark.asyncio
async def test_without_statistics_the_sensor_stays_unavailable(
    clock: SimpleNamespace,
) -> None:
    """A source without long-term statistics behaves as before."""
    sensor = _sensor(average=14)

    await _update(sensor, _Statistics({}))

    assert sensor._measurements == {}
    assert sensor.state is None
    assert sensor.available is False
    assert sensor.extra_state_attributes["backfilled_days"] == []


@pytest.mark.asyncio
async def test_a_measurement_replaces_the_backfilled_value_of_the_same_day(
    clock: SimpleNamespace,
) -> None:
    """HSEM started mid-day: today's block is backfilled, then measured."""
    clock.now = datetime(2026, 8, 12, 13, 5, tzinfo=UTC)  # 15:05 in Copenhagen
    sensor = _sensor(average=3)
    await _update(sensor, _block_14({12: 500.0}))
    assert sensor._measurements == {"2026-08-12": pytest.approx(0.5)}
    assert sensor.extra_state_attributes["backfilled_days"] == ["2026-08-12"]

    with (
        patch(f"{_AVG}.ha_get_entity_state_and_convert", return_value=0.9),
        patch(f"{_AVG}._block_observed", return_value=True),
    ):
        await sensor._async_store_utility_meter_value()

    assert sensor._measurements == {"2026-08-12": pytest.approx(0.9)}
    assert sensor.extra_state_attributes["backfilled_days"] == []


@pytest.mark.asyncio
async def test_a_backfilled_day_that_rolls_out_of_the_window_loses_its_mark() -> None:
    sensor = _sensor(average=2)
    sensor._measurements = {"2026-08-09": 0.9, "2026-08-10": 1.0, "2026-08-11": 1.1}
    sensor._backfilled = {"2026-08-09", "2026-08-10"}

    await sensor._async_cleanup_old_measurements()

    assert sensor._measurements == {"2026-08-10": 1.0, "2026-08-11": 1.1}
    assert sensor._backfilled == {"2026-08-10"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (["2026-08-10", "2026-08-01"], {"2026-08-10"}),
        (None, set()),
    ],
)
async def test_backfilled_days_survive_a_restart(
    stored: list[str] | None, expected: set[str]
) -> None:
    """The mark is restored with the measurements it belongs to."""
    sensor = _sensor(average=3)
    attributes: dict[str, Any] = {
        "measurements": {"2026-08-10": 1.0, "2026-08-11": 1.1}
    }
    if stored is not None:
        attributes["backfilled_days"] = stored
    sensor.async_get_last_state = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(state="1.05", attributes=attributes)
    )
    sensor.async_get_last_extra_data = AsyncMock(return_value=None)  # type: ignore[method-assign]
    sensor._async_handle_update = AsyncMock()  # type: ignore[method-assign]
    with (
        patch(f"{_AVG}.async_track_time_interval", return_value=MagicMock()),
        patch.object(RestoreEntity, "async_added_to_hass", new_callable=AsyncMock),
    ):
        await sensor.async_added_to_hass()

    assert sensor._backfilled == expected
