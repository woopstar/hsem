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

from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.custom_sensors import consumption_backfill
from custom_components.hsem.custom_sensors.avg_sensor import HSEMAvgSensor
from custom_components.hsem.custom_sensors.consumption_backfill import (
    BACKFILL_DAYS,
    CHARGER_LOOKBACK,
    PROFILE_REFRESH,
    async_consumption_profile,
    missing_days,
)
from custom_components.hsem.utils.schema import vol
from custom_components.hsem.utils.sensornames.energy import (
    get_house_load_power_sensor_entity_id,
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
        self.error: Exception | None = None
        self.calls: list[tuple[str, datetime, datetime, str | None]] = []

    def reader(self, _hass: Any) -> _Statistics:
        return self

    async def read_hourly_mean(
        self, entity_id: str, start: datetime, end: datetime, unit: str | None = None
    ) -> dict[datetime, float]:
        self.calls.append((entity_id, start, end, unit))
        if self.error is not None:
            raise self.error
        return {
            hour_start: mean
            for hour_start, mean in self.means.get(entity_id, {}).items()
            if start <= hour_start < end
        }


@pytest.fixture(autouse=True)
def _clear_profile_cache() -> Generator[None]:
    consumption_backfill._profile_cache.clear()
    consumption_backfill._profile_locks.clear()
    consumption_backfill._reported_chargers.clear()
    yield
    consumption_backfill._profile_cache.clear()
    consumption_backfill._profile_locks.clear()
    consumption_backfill._reported_chargers.clear()


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
    entry.created_at = NOW
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
    # No unit is requested: the values come in the sensor's own unit, as the
    # live path reads them, so a window never mixes two scales.
    assert statistics.calls == [(HOUSE, NOW - timedelta(days=BACKFILL_DAYS), NOW, None)]


@pytest.mark.asyncio
async def test_ev_chargers_are_subtracted_when_house_power_includes_them(
    clock: SimpleNamespace,
) -> None:
    """Same rule as the live path: a charger without data for an hour is 0 W."""
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

    # 15:00 has no second-charger statistics, which counts as 0 W there.
    # 16:00 would be negative and is left out.
    assert profile == {
        ("2026-08-10", 14): pytest.approx(0.5),
        ("2026-08-10", 15): pytest.approx(1.0),
    }
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
    clock.now = first + timedelta(days=1)
    statistics = _Statistics({HOUSE: {first: 400.0, first + timedelta(hours=1): 600.0}})

    profile = await _profile(statistics, _entry())

    assert profile == {("2026-10-25", 2): pytest.approx(1.0)}


@pytest.mark.asyncio
async def test_a_charger_that_is_unavailable_when_idle_does_not_empty_the_profile(
    clock: SimpleNamespace,
) -> None:
    """Statistics only for the hours it charged; every other hour is kept.

    Its first statistics in the read window are from 5 August, but it also
    charged in July, so it existed for the whole window.
    """
    house = {_local(day, 14): 800.0 for day in range(1, 12)}
    statistics = _Statistics(
        {HOUSE: house, EV: {_local(5, 14): 300.0, _local(10, 14, month=7): 7000.0}}
    )
    entry = _entry(
        hsem_ev_charger_power=EV, hsem_house_power_includes_ev_charger_power=True
    )

    profile = await _profile(statistics, entry)

    assert len(profile) == 11
    assert profile[("2026-08-01", 14)] == pytest.approx(0.8)
    assert profile[("2026-08-05", 14)] == pytest.approx(0.5)
    assert profile[("2026-08-06", 14)] == pytest.approx(0.8)
    # The charger was looked for before the window, once.
    start = NOW - timedelta(days=BACKFILL_DAYS)
    assert statistics.calls == [
        (HOUSE, start, NOW, None),
        (EV, start, NOW, None),
        (EV, start - CHARGER_LOOKBACK, start, None),
    ]


@pytest.mark.asyncio
async def test_hours_before_a_chargers_first_statistics_are_left_out(
    clock: SimpleNamespace,
) -> None:
    """The charger sensor was added on 7 August; the car charged on the 4th.

    The house power of the 4th holds 7 kW of charging that no statistics can
    subtract.  Counting the charger as 0 W there would backfill it as house
    load.
    """
    house = {_local(day, 14): 800.0 for day in range(1, 12)}
    house[_local(4, 14)] = 7800.0
    statistics = _Statistics({HOUSE: house, EV: {_local(7, 14): 0.0}})
    entry = _entry(
        hsem_ev_charger_power=EV, hsem_house_power_includes_ev_charger_power=True
    )

    profile = await _profile(statistics, entry)

    assert sorted(day for day, _hour in profile) == [
        f"2026-08-{day:02d}" for day in range(7, 12)
    ]
    assert list(profile.values()) == [pytest.approx(0.8)] * 5


@pytest.mark.asyncio
async def test_a_second_charger_added_later_limits_the_first(
    clock: SimpleNamespace,
) -> None:
    """Every charger must be known for an hour to be backfilled."""
    house = {_local(day, 14): 800.0 for day in range(1, 12)}
    statistics = _Statistics(
        {HOUSE: house, EV: {_local(1, 14): 0.0}, EV_2: {_local(9, 14): 300.0}}
    )
    entry = _entry(
        hsem_ev_charger_power=EV,
        hsem_ev_second_charger_power=EV_2,
        hsem_house_power_includes_ev_charger_power=True,
    )

    profile = await _profile(statistics, entry)

    assert profile == {
        ("2026-08-09", 14): pytest.approx(0.5),
        ("2026-08-10", 14): pytest.approx(0.8),
        ("2026-08-11", 14): pytest.approx(0.8),
    }


@pytest.mark.asyncio
async def test_a_charger_without_any_statistics_stops_the_backfill(
    clock: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    """EV energy cannot be told from house load, so nothing is backfilled."""
    statistics = _Statistics({HOUSE: {_local(10, 14): 3000.0}})
    entry = _entry(
        hsem_ev_charger_power=EV, hsem_house_power_includes_ev_charger_power=True
    )

    with caplog.at_level("INFO", logger="custom_components.hsem"):
        assert await _profile(statistics, entry) == {}
        # The next hourly read finds the same and does not log it again.
        clock.now = NOW + PROFILE_REFRESH
        assert await _profile(statistics, entry) == {}

    assert caplog.text.count(f"{EV} has no long-term statistics") == 1


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
    with patch(f"{_BACKFILL}.configured_house_power_entity", return_value=None):
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


@pytest.mark.asyncio
async def test_the_source_comes_from_the_config_not_from_current_states(
    clock: SimpleNamespace,
) -> None:
    """At start-up the house sensor has no state yet; it is still the source."""
    hass = _hass()
    hass.states.get.return_value = None
    derived_inputs = {
        "hsem_huawei_solar_inverter_active_power": "sensor.inverter_power",
        "hsem_huawei_solar_power_meter_active_power": "sensor.meter_power",
    }
    statistics = _Statistics({HOUSE: {_local(10, 14): 500.0}})

    await _profile(statistics, _entry(**derived_inputs), hass)
    # Without a configured sensor the derived house load is the source.
    await _profile(
        statistics,
        _entry(hsem_house_consumption_power=vol.UNDEFINED, **derived_inputs),
        hass,
    )

    assert [call[0] for call in statistics.calls] == [
        HOUSE,
        get_house_load_power_sensor_entity_id(),
    ]


@pytest.mark.asyncio
async def test_a_failed_read_is_logged_once_and_not_repeated_within_the_hour(
    clock: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    """96 sensors share the failure instead of asking the recorder 96 times."""
    statistics = _Statistics({HOUSE: {_local(10, 14): 500.0}})
    statistics.error = RuntimeError("database is locked")
    entry = _entry()

    with caplog.at_level("WARNING"):
        assert await _profile(statistics, entry) == {}
        assert await _profile(statistics, entry) == {}

    assert len(statistics.calls) == 1
    assert "Traceback" in caplog.text
    assert caplog.text.count("reading statistics of sensor.house_power failed") == 1

    statistics.error = None
    clock.now = NOW + PROFILE_REFRESH
    assert await _profile(statistics, entry) == {("2026-08-10", 14): pytest.approx(0.5)}


def test_missing_days_are_the_newest_days_hsem_has_no_sample_for() -> None:
    profile = {
        ("2026-08-08", 14): 0.8,
        ("2026-08-09", 14): 0.9,
        ("2026-08-10", 14): 1.0,
        ("2026-08-11", 14): 1.1,
        ("2026-08-11", 15): 5.0,
    }

    assert missing_days(profile, 14, {"2026-08-10"}, 2, "2026-08-12") == {
        "2026-08-11": 1.1,
        "2026-08-09": 0.9,
    }
    assert missing_days(profile, 14, set(), 5, "2026-08-10") == {
        "2026-08-09": 0.9,
        "2026-08-08": 0.8,
    }
    assert missing_days(profile, 14, set(), 0, "2026-08-12") == {}
    assert missing_days(profile, 3, set(), 5, "2026-08-12") == {}


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


async def _update(
    sensor: HSEMAvgSensor, statistics: _Statistics, meter: float | None = None
) -> None:
    """Run one update.  *meter* is the utility meter's reading, if it has one."""
    with (
        patch(f"{_BACKFILL}.StatisticsReader", statistics.reader),
        patch(f"{_AVG}.ha_get_entity_state_and_convert", return_value=meter),
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
    # HSEM measures the block from today on; only earlier days are backfilled.
    assert attributes["backfill_before"] == "2026-08-12"
    assert {
        "backfilled_days",
        "backfill_before",
    } <= HSEMAvgSensor._unrecorded_attributes
    sensor.async_write_ha_state.assert_called_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_day_hsem_measured_is_never_replaced_or_pushed_out(
    clock: SimpleNamespace,
) -> None:
    sensor = _sensor(average=3)
    sensor._measurements = {"2026-08-10": 9.0, "2026-08-11": 9.5}
    statistics = _block_14({8: 800.0, 9: 900.0, 10: 1000.0, 11: 1100.0})

    await _update(sensor, statistics)

    # One free place: the newest day before HSEM's first measurement fills
    # it.  The two measured days stay as they were.
    assert sensor._measurements == {
        "2026-08-09": pytest.approx(0.9),
        "2026-08-10": pytest.approx(9.0),
        "2026-08-11": pytest.approx(9.5),
    }
    assert sensor.extra_state_attributes["backfilled_days"] == ["2026-08-09"]
    assert sensor.extra_state_attributes["backfill_before"] == "2026-08-10"


@pytest.mark.asyncio
async def test_a_block_the_observation_guard_rejected_is_not_backfilled(
    clock: SimpleNamespace,
) -> None:
    """Issues #1101 and #1110: Home Assistant was down 14:10-14:50 today.

    The utility-meter sample is rejected.  The statistics mean of that hour
    covers only the 20 minutes Home Assistant was up, so it must not come
    back in as today's sample.
    """
    clock.now = datetime(2026, 8, 12, 13, 5, tzinfo=UTC)  # 15:05 in Copenhagen
    sensor = _sensor(average=3)
    sensor._measurements = {"2026-08-10": 1.0}
    sensor._backfill_before = "2026-08-10"
    statistics = _block_14({11: 1100.0, 12: 150.0})

    with patch(f"{_AVG}._block_observed", return_value=False):
        await _update(sensor, statistics, meter=0.05)

    assert sensor._measurements == {"2026-08-10": pytest.approx(1.0)}
    assert sensor.extra_state_attributes["backfilled_days"] == []


@pytest.mark.asyncio
async def test_the_current_day_is_not_backfilled_on_a_new_install(
    clock: SimpleNamespace,
) -> None:
    """Installed at 15:05: today's 14-15 block waits for a real measurement."""
    clock.now = datetime(2026, 8, 12, 13, 5, tzinfo=UTC)  # 15:05 in Copenhagen
    sensor = _sensor(average=3)

    await _update(sensor, _block_14({11: 1100.0, 12: 500.0}))

    assert sensor._measurements == {"2026-08-11": pytest.approx(1.1)}
    assert sensor.extra_state_attributes["backfill_before"] == "2026-08-12"

    # The next day the guard has rejected the 12th.  It stays out, although
    # the window has room and the statistics have the hour.
    clock.now += timedelta(days=1)
    consumption_backfill._profile_cache.clear()
    await _update(sensor, _block_14({11: 1100.0, 12: 500.0, 13: 700.0}))

    assert sensor._measurements == {"2026-08-11": pytest.approx(1.1)}
    assert sensor.extra_state_attributes["backfill_before"] == "2026-08-12"


@pytest.mark.asyncio
async def test_an_old_install_with_an_empty_window_is_not_backfilled(
    clock: SimpleNamespace,
) -> None:
    """HSEM has run since June; the window is empty because state was lost.

    Every day in the statistics is a day HSEM was running, so a missing day
    is a rejected or unobserved block and none of them is taken.
    """
    sensor = _sensor(average=3)
    sensor._config_entry.created_at = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

    await _update(sensor, _block_14({9: 900.0, 10: 1000.0, 11: 1100.0}))

    assert sensor._measurements == {}
    assert sensor.extra_state_attributes["backfill_before"] == "2026-06-01"


@pytest.mark.asyncio
async def test_an_install_from_last_week_is_backfilled_up_to_its_first_day(
    clock: SimpleNamespace,
) -> None:
    """Installed on 10 August at 23:30 local time; that day stays out."""
    sensor = _sensor(average=3)
    sensor._config_entry.created_at = datetime(2026, 8, 10, 21, 30, tzinfo=UTC)

    await _update(sensor, _block_14({8: 800.0, 9: 900.0, 10: 1000.0, 11: 1100.0}))

    assert sensor._measurements == {
        "2026-08-08": pytest.approx(0.8),
        "2026-08-09": pytest.approx(0.9),
    }
    assert sensor.extra_state_attributes["backfill_before"] == "2026-08-10"


@pytest.mark.asyncio
async def test_a_recorder_error_does_not_stop_the_sensor_update(
    clock: SimpleNamespace,
) -> None:
    """The update runs before the entity is added; it must not raise."""
    sensor = _sensor(average=3)
    sensor._measurements = {"2026-08-11": 2.0}
    statistics = _block_14({10: 1000.0})
    statistics.error = RuntimeError("database is locked")

    await _update(sensor, statistics)

    assert sensor._measurements == {"2026-08-11": pytest.approx(2.0)}
    assert sensor.state == pytest.approx(2.0)
    sensor.async_write_ha_state.assert_called_once()  # type: ignore[attr-defined]


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
async def test_a_measurement_replaces_a_backfilled_value_of_the_same_day(
    clock: SimpleNamespace,
) -> None:
    """A measurement always wins and clears the mark."""
    clock.now = datetime(2026, 8, 12, 13, 5, tzinfo=UTC)  # 15:05 in Copenhagen
    sensor = _sensor(average=3)
    sensor._measurements = {"2026-08-12": 0.5}
    sensor._backfilled = {"2026-08-12"}

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
    ("stored", "horizon", "expected", "expected_horizon"),
    [
        (["2026-08-10", "2026-08-01"], "2026-08-11", {"2026-08-10"}, "2026-08-11"),
        (None, None, set(), None),
        (None, "not a date", set(), None),
    ],
)
async def test_backfilled_days_survive_a_restart(
    stored: list[str] | None,
    horizon: str | None,
    expected: set[str],
    expected_horizon: str | None,
) -> None:
    """The mark and the backfill limit are restored with the measurements."""
    sensor = _sensor(average=3)
    attributes: dict[str, Any] = {
        "measurements": {"2026-08-10": 1.0, "2026-08-11": 1.1}
    }
    if stored is not None:
        attributes["backfilled_days"] = stored
    if horizon is not None:
        attributes["backfill_before"] = horizon
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
    assert sensor._backfill_before == expected_horizon


@pytest.mark.asyncio
async def test_nothing_is_backfilled_before_the_measurements_exist() -> None:
    """Before the first update there is no window to fill and no limit to set."""
    sensor = _sensor(average=3)
    statistics = _block_14({11: 1100.0})

    with patch(f"{_BACKFILL}.StatisticsReader", statistics.reader):
        await sensor._async_backfill_from_statistics()

    assert sensor._measurements is None
    assert sensor._backfill_before is None
    assert statistics.calls == []
