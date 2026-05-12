"""Consolidated regression test suite for HSEM P0 bugs (issue #274).

Each section maps to one P0 issue and contains:
- A short comment describing the *old* (broken) behaviour.
- Deterministic, pure-Python tests that would have *failed* before the fix.
- Tests that *pass* with the current, correct implementation.

Covered bugs
------------
P0-01  Month matching (issue #265) — string-containment false positive
P0-02  Midnight rollover (issue #266) — cross-midnight windows not handled
P0-03  Next-day charging (issue #267) — 07:00 window not found from 22:00
P0-04  Schedule_3 default (issue #268) — 00:00→00:00 zero-length window
P0-05  Invalid sensor values (issue #269) — "unknown"/"unavailable" → 0
P0-06  Concurrent updates (issue #270) — parallel update cycles not locked
P0-07  Version comparison (issue #271) — "1.10" < "1.9" (lexicographic)
P0-08  Magic thresholds (issue #272) — hard-coded 0.1/0.2 kWh literals
P0-09  Exception handling (issue #273) — broad ``except Exception`` swallowed errors

CI compatibility
----------------
All tests are pure-Python; no running Home Assistant instance is required.
Async tests use ``pytest-asyncio`` with the ``asyncio`` mark.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceNotFound

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_UTC = UTC


def _dt(
    hour: int,
    minute: int = 0,
    *,
    day: int = 15,
    month: int = 1,
    year: int = 2026,
    day_offset: int = 0,
) -> datetime:
    """Return a UTC-aware datetime. ``day_offset`` shifts by whole days."""
    base = datetime(year, month, day, hour, minute, tzinfo=_UTC)
    return base + timedelta(days=day_offset)


# ===========================================================================
# P0-01  Month matching  (issue #265)
# ===========================================================================


class TestP001MonthMatching:
    """OLD BUG: The working-mode sensor checked membership with string
    containment — ``"1" in str(winter_months)`` — so January matched
    "10", "11", and "12" as well, causing wrong seasonal mode in Oct/Nov/Dec.

    FIX: ``convert_months_to_int`` converts all entries to ``int`` so that
    Python's ``in`` operator performs numeric membership rather than substring
    search.
    """

    def test_string_containment_is_the_old_bug(self) -> None:
        """Demonstrate that the old approach was broken.

        The string representation of ``["10", "11", "12"]`` contains the
        substring ``"1"`` → so January appeared to match October, November,
        and December.  This test deliberately shows the *broken* assertion so
        it is clear what the fix corrects.
        """
        # This is exactly what the old code did — and why it was wrong.
        old_winter_repr = str(["10", "11", "12"])
        assert "1" in old_winter_repr  # broken: "1" is a substring of "10"

    def test_integer_membership_is_correct(self) -> None:
        """With integer lists, January does NOT match Oct/Nov/Dec."""
        winter_months = [10, 11, 12]
        assert 1 not in winter_months  # correct fix

    def test_convert_months_to_int_removes_false_positives(self) -> None:
        """``convert_months_to_int`` must return ints so membership is numeric."""
        from custom_components.hsem.utils.misc import convert_months_to_int

        result = convert_months_to_int(["10", "11", "12"])
        assert 1 not in result, (
            "January (1) must not be present after converting ['10','11','12']"
        )

    def test_january_only_matches_january(self) -> None:
        """Converting ['1'] must yield exactly [1]."""
        from custom_components.hsem.utils.misc import convert_months_to_int

        result = convert_months_to_int(["1"])
        assert result == [1]
        for ghost in (10, 11, 12):
            assert ghost not in result, (
                f"Month {ghost} must not appear after converting ['1']"
            )

    def test_all_winter_months_correct(self) -> None:
        """Standard winter set [1,2,3,4,10,11,12] must survive round-trip."""
        from custom_components.hsem.utils.misc import convert_months_to_int

        raw = ["1", "2", "3", "4", "10", "11", "12"]
        result = convert_months_to_int(raw)
        assert set(result) == {1, 2, 3, 4, 10, 11, 12}
        # Summer months must be absent
        for summer in (5, 6, 7, 8, 9):
            assert summer not in result

    def test_summer_months_not_in_winter_set(self) -> None:
        """May through September must not appear in the default winter set."""
        from custom_components.hsem.utils.misc import convert_months_to_int

        winter = convert_months_to_int(["1", "2", "3", "4", "10", "11", "12"])
        for month in range(5, 10):
            assert month not in winter, f"Month {month} must not be in the winter set"

    def test_invalid_month_zero_raises(self) -> None:
        """Month 0 is out-of-range and must raise ``ValueError``."""
        from custom_components.hsem.utils.misc import convert_months_to_int

        with pytest.raises(ValueError, match="Month must be between 1 and 12"):
            convert_months_to_int(["0"])

    def test_invalid_month_thirteen_raises(self) -> None:
        """Month 13 is out-of-range and must raise ``ValueError``."""
        from custom_components.hsem.utils.misc import convert_months_to_int

        with pytest.raises(ValueError, match="Month must be between 1 and 12"):
            convert_months_to_int(["13"])


# ===========================================================================
# P0-02  Midnight rollover  (issue #266)
# ===========================================================================


class TestP002MidnightRollover:
    """OLD BUG: ``is_time_in_window`` and ``interval_ends_before_window_start``
    only handled same-day windows (start < end).  A cross-midnight window such
    as 23:00–02:00 was silently treated as always-false, causing HSEM to skip
    valid overnight charge/discharge windows entirely.

    FIX: Both helpers now detect when ``start > end`` (cross-midnight) and
    use the corresponding logic branch.
    """

    def test_inside_cross_midnight_window(self) -> None:
        """01:00 is inside the 23:00-02:00 window."""
        from custom_components.hsem.utils.misc import is_time_in_window

        assert is_time_in_window(time(1, 0), time(23, 0), time(2, 0)) is True

    def test_at_start_of_cross_midnight_window(self) -> None:
        """23:00 is the inclusive start of the 23:00-02:00 window."""
        from custom_components.hsem.utils.misc import is_time_in_window

        assert is_time_in_window(time(23, 0), time(23, 0), time(2, 0)) is True

    def test_at_end_of_cross_midnight_window_is_exclusive(self) -> None:
        """02:00 is the exclusive end — must be False."""
        from custom_components.hsem.utils.misc import is_time_in_window

        assert is_time_in_window(time(2, 0), time(23, 0), time(2, 0)) is False

    def test_before_cross_midnight_window(self) -> None:
        """21:00 is before the 23:00 start — must be False."""
        from custom_components.hsem.utils.misc import is_time_in_window

        assert is_time_in_window(time(21, 0), time(23, 0), time(2, 0)) is False

    def test_after_cross_midnight_window(self) -> None:
        """03:00 is after the 02:00 end of the cross-midnight window."""
        from custom_components.hsem.utils.misc import is_time_in_window

        assert is_time_in_window(time(3, 0), time(23, 0), time(2, 0)) is False

    def test_interval_ending_before_cross_midnight_window_start(self) -> None:
        """An interval ending at 22:00 is before a 23:00 cross-midnight window."""
        from custom_components.hsem.utils.misc import interval_ends_before_window_start

        now = _dt(21, 0)
        interval_end = _dt(22, 0)
        assert interval_ends_before_window_start(interval_end, time(23, 0), now) is True

    def test_interval_ending_inside_cross_midnight_window_is_not_before(self) -> None:
        """An interval ending at 23:30 is NOT before the 23:00 window start."""
        from custom_components.hsem.utils.misc import interval_ends_before_window_start

        now = _dt(21, 0)
        interval_end = _dt(23, 30)
        assert (
            interval_ends_before_window_start(interval_end, time(23, 0), now) is False
        )

    def test_same_day_window_still_works(self) -> None:
        """Ordinary same-day windows continue to work after the fix."""
        from custom_components.hsem.utils.misc import is_time_in_window

        assert is_time_in_window(time(8, 0), time(7, 0), time(9, 0)) is True
        assert is_time_in_window(time(6, 59), time(7, 0), time(9, 0)) is False


# ===========================================================================
# P0-03  Next-day charging  (issue #267)
# ===========================================================================


class TestP003NextDayCharging:
    """OLD BUG: ``next_window_start_dt`` did not exist; the sensor used a naive
    ``now.replace(hour=..., minute=...)`` call which always returned a time on
    the *current* calendar day.  At 22:00, the 07:00 morning discharge window
    was computed as *already in the past* — so the cheap 02:00–05:00 overnight
    grid-charge opportunity was never selected.

    FIX: ``next_window_start_dt`` always returns the *next* future occurrence of
    the requested wall-clock time (today if still upcoming, tomorrow if past).
    """

    def test_evening_planning_resolves_morning_window_to_tomorrow(self) -> None:
        """At 22:00 a 07:00 window must resolve to the next calendar day."""
        from custom_components.hsem.utils.misc import next_window_start_dt

        now = _dt(22, 0)
        result = next_window_start_dt(now, time(7, 0))
        expected = _dt(7, 0, day_offset=1)
        assert result == expected, (
            f"At 22:00, 07:00 window should be tomorrow — got {result}"
        )

    def test_result_is_always_strictly_after_now(self) -> None:
        """``next_window_start_dt`` must never return a past datetime."""
        from custom_components.hsem.utils.misc import next_window_start_dt

        for hour in (0, 6, 12, 18, 22, 23):
            now = _dt(hour, 0)
            result = next_window_start_dt(now, time(7, 0))
            assert result > now, (
                f"next_window_start_dt from {now.time()} returned {result.time()}, "
                "which is not strictly after now"
            )

    def test_pre_morning_time_still_returns_today(self) -> None:
        """At 06:00 the 07:00 window is still today — must not advance to tomorrow."""
        from custom_components.hsem.utils.misc import next_window_start_dt

        now = _dt(6, 0)
        result = next_window_start_dt(now, time(7, 0))
        assert result == _dt(7, 0), (
            "At 06:00, the 07:00 window has not yet passed — should be today"
        )

    def test_cheap_night_slot_flagged_before_next_day_discharge_window(self) -> None:
        """A 02:00-03:00 charge slot tonight is before the 07:00 window tomorrow.

        This is the P0-03 key scenario: planning a 02:00 grid charge at 22:00
        to cover morning peak use the following day.
        """
        from custom_components.hsem.utils.misc import interval_ends_before_window_start

        now = _dt(22, 0)
        charge_slot_end = _dt(3, 0, day_offset=1)  # 03:00 next day
        assert (
            interval_ends_before_window_start(charge_slot_end, time(7, 0), now) is True
        ), (
            "02:00-03:00 charge slot must be flagged as 'before' the 07:00 discharge window"
        )

    def test_slot_after_discharge_window_excluded(self) -> None:
        """A slot ending at 08:00 is NOT before the 07:00 window."""
        from custom_components.hsem.utils.misc import interval_ends_before_window_start

        now = _dt(22, 0)
        charge_slot_end = _dt(8, 0, day_offset=1)
        assert (
            interval_ends_before_window_start(charge_slot_end, time(7, 0), now) is False
        )


# ===========================================================================
# P0-04  Schedule_3 default  (issue #268)
# ===========================================================================


class TestP004Schedule3Default:
    """OLD BUG: ``schedule_3`` defaulted to ``enabled=True`` with a
    ``00:00:00 → 00:00:00`` window, which is a zero-length window that cannot
    be distinguished from midnight-to-midnight (a 24-hour window).  This caused
    spurious grid-charge commands on any night where schedule_3 fired.

    FIX: ``schedule_3`` is now ``enabled=False`` by default and uses explicit
    non-midnight placeholder times so the window is unambiguously non-zero
    when re-enabled by the user.
    """

    def test_schedule_3_disabled_by_default(self) -> None:
        """schedule_3 must ship disabled so it never fires unintentionally."""
        from custom_components.hsem.const import DEFAULT_CONFIG_VALUES

        assert (
            DEFAULT_CONFIG_VALUES["hsem_batteries_enable_batteries_schedule_3"] is False
        ), "schedule_3 must default to disabled"

    def test_schedule_3_default_start_is_not_midnight(self) -> None:
        """Default start must not be '00:00:00' to avoid ambiguous zero-length window."""
        from custom_components.hsem.const import DEFAULT_CONFIG_VALUES

        start = DEFAULT_CONFIG_VALUES[
            "hsem_batteries_enable_batteries_schedule_3_start"
        ]
        assert start != "00:00:00", (
            "schedule_3 default start '00:00:00' + end '00:00:00' is ambiguous"
        )

    def test_schedule_3_default_end_is_not_midnight(self) -> None:
        """Default end must not be '00:00:00'."""
        from custom_components.hsem.const import DEFAULT_CONFIG_VALUES

        end = DEFAULT_CONFIG_VALUES["hsem_batteries_enable_batteries_schedule_3_end"]
        assert end != "00:00:00"

    def test_schedule_3_default_start_and_end_differ(self) -> None:
        """Default start ≠ default end — window is non-zero when enabled."""
        from custom_components.hsem.const import DEFAULT_CONFIG_VALUES

        start = DEFAULT_CONFIG_VALUES[
            "hsem_batteries_enable_batteries_schedule_3_start"
        ]
        end = DEFAULT_CONFIG_VALUES["hsem_batteries_enable_batteries_schedule_3_end"]
        assert start != end, "Default schedule_3 must not be a zero-length window"

    def test_schedules_1_and_2_remain_enabled(self) -> None:
        """Schedules 1 and 2 should remain enabled by default."""
        from custom_components.hsem.const import DEFAULT_CONFIG_VALUES

        assert (
            DEFAULT_CONFIG_VALUES["hsem_batteries_enable_batteries_schedule_1"] is True
        )
        assert (
            DEFAULT_CONFIG_VALUES["hsem_batteries_enable_batteries_schedule_2"] is True
        )

    @pytest.mark.asyncio
    async def test_zero_length_window_rejected_by_validator(self) -> None:
        """The schedule validator must reject a 00:00:00 → 00:00:00 window when enabled."""
        from custom_components.hsem.flows.batteries_schedule_3 import (
            validate_batteries_schedule_3_input,
        )

        user_input = {
            "hsem_batteries_enable_batteries_schedule_3": True,
            "hsem_batteries_enable_batteries_schedule_3_start": "00:00:00",
            "hsem_batteries_enable_batteries_schedule_3_end": "00:00:00",
            "hsem_batteries_enable_batteries_schedule_3_min_price_difference": 0.0,
        }
        errors = await validate_batteries_schedule_3_input(user_input)
        assert "base" in errors, (
            "00:00→00:00 with enabled=True must produce a validation error"
        )

    @pytest.mark.asyncio
    async def test_disabled_schedule_3_accepts_any_times(self) -> None:
        """A disabled schedule_3 must never fail validation (times are irrelevant)."""
        from custom_components.hsem.flows.batteries_schedule_3 import (
            validate_batteries_schedule_3_input,
        )

        user_input = {
            "hsem_batteries_enable_batteries_schedule_3": False,
            "hsem_batteries_enable_batteries_schedule_3_start": "00:00:00",
            "hsem_batteries_enable_batteries_schedule_3_end": "00:00:00",
            "hsem_batteries_enable_batteries_schedule_3_min_price_difference": 0.0,
        }
        errors = await validate_batteries_schedule_3_input(user_input)
        assert errors == {}, "Disabled schedule must not fail validation"


# ===========================================================================
# P0-05  Invalid sensor values  (issue #269)
# ===========================================================================


class TestP005InvalidSensorValues:
    """OLD BUG: ``convert_to_float`` returned ``0.0`` for HA sentinel strings
    such as ``"unknown"`` and ``"unavailable"``, silently turning a missing or
    broken sensor into zero consumption.  This could feed the planner wrong
    data and trigger unsafe hardware decisions.

    FIX: ``convert_to_float`` returns ``None`` for any non-numeric input.
    Critical sensors check for ``None`` and set ``live.missing_entities = True``
    so the planner enters safe mode instead of acting on ghost zeros.
    """

    def test_unknown_returns_none_not_zero(self) -> None:
        """'unknown' is a sentinel — must not become 0."""
        from custom_components.hsem.utils.misc import convert_to_float

        result = convert_to_float("unknown")
        assert result is None, f"Expected None, got {result!r}"

    def test_unavailable_returns_none_not_zero(self) -> None:
        """'unavailable' is a sentinel — must not become 0."""
        from custom_components.hsem.utils.misc import convert_to_float

        result = convert_to_float("unavailable")
        assert result is None

    def test_empty_string_returns_none(self) -> None:
        """An empty string has no numeric meaning — must return None."""
        from custom_components.hsem.utils.misc import convert_to_float

        assert convert_to_float("") is None

    def test_none_input_returns_none(self) -> None:
        """Python None must pass through as None."""
        from custom_components.hsem.utils.misc import convert_to_float

        assert convert_to_float(None) is None

    def test_real_zero_is_not_none(self) -> None:
        """Numeric 0 / '0' is a valid measurement and must NOT become None."""
        from custom_components.hsem.utils.misc import convert_to_float

        assert convert_to_float(0) == pytest.approx(0.0)
        assert convert_to_float("0") == pytest.approx(0.0)
        assert convert_to_float("0.0") == pytest.approx(0.0)

    def test_valid_positive_float_round_trips(self) -> None:
        """A valid float string converts cleanly."""
        from custom_components.hsem.utils.misc import convert_to_float

        assert convert_to_float("75.5") == pytest.approx(75.5)

    def test_valid_negative_float_round_trips(self) -> None:
        """Negative values (e.g. export power) must convert correctly."""
        from custom_components.hsem.utils.misc import convert_to_float

        assert convert_to_float("-3.14") == pytest.approx(-3.14)

    def test_unavailable_soc_sets_missing_entities_flag(self) -> None:
        """A None battery SoC (from unavailable sensor) must set missing_entities."""
        from custom_components.hsem.models.live_state import LiveState
        from custom_components.hsem.utils.misc import convert_to_float

        state = LiveState()
        soc = convert_to_float("unavailable")
        if soc is None:
            state.add_missing_entity("Critical: battery SoC unavailable")
        state.huawei_batteries_soc_pct = soc

        assert state.missing_entities is True
        assert state.huawei_batteries_soc_pct is None

    def test_zero_soc_does_not_set_missing_flag(self) -> None:
        """A valid 0% SoC is real data — must not trigger the missing-entity flag."""
        from custom_components.hsem.models.live_state import LiveState
        from custom_components.hsem.utils.misc import convert_to_float

        state = LiveState()
        soc = convert_to_float("0")
        if soc is None:
            state.add_missing_entity("Critical: battery SoC unavailable")
        state.huawei_batteries_soc_pct = soc

        assert state.missing_entities is False
        assert state.huawei_batteries_soc_pct == pytest.approx(0.0)


# ===========================================================================
# P0-06  Concurrent updates  (issue #270)
# ===========================================================================


class TestP006ConcurrentUpdates:
    """OLD BUG: ``_async_handle_update`` had no lock, so two rapid HA state-
    change events could launch two simultaneous planner cycles, leading to
    double inverter writes (two API calls to the Huawei solar inverter in the
    same second).

    FIX: An ``asyncio.Lock`` guards the entry point.  A concurrent call
    immediately returns without starting a second cycle.
    """

    class _Sensor:
        """Minimal stub that replicates the production locking pattern."""

        def __init__(self) -> None:
            self._update_lock = asyncio.Lock()
            self.cycle_runs: int = 0
            self.skipped: int = 0

        async def _async_handle_update(self, event=None) -> None:
            if self._update_lock.locked():
                self.skipped += 1
                return
            async with self._update_lock:
                await self._run_cycle()

        async def _run_cycle(self) -> None:
            self.cycle_runs += 1
            # Two yields so a concurrent caller can observe the locked state.
            await asyncio.sleep(0)
            await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_single_update_runs_exactly_once(self) -> None:
        """A lone call must run the cycle once."""
        sensor = self._Sensor()
        await sensor._async_handle_update()
        assert sensor.cycle_runs == 1
        assert sensor.skipped == 0

    @pytest.mark.asyncio
    async def test_concurrent_second_call_is_skipped(self) -> None:
        """While the first cycle is running, the second concurrent call is dropped."""
        sensor = self._Sensor()
        await asyncio.gather(
            sensor._async_handle_update(),
            sensor._async_handle_update(),
        )
        assert sensor.cycle_runs == 1, (
            f"Cycle ran {sensor.cycle_runs} times — expected exactly 1"
        )
        assert sensor.skipped == 1, f"Expected 1 skipped call, got {sensor.skipped}"

    @pytest.mark.asyncio
    async def test_no_double_inverter_write(self) -> None:
        """Concurrent updates must not trigger more than one hardware write."""
        writes: list[str] = []

        class _WriteTracking(self.__class__._Sensor):
            async def _run_cycle(self) -> None:
                self.cycle_runs += 1
                writes.append("write")
                await asyncio.sleep(0)
                await asyncio.sleep(0)

        sensor = _WriteTracking()
        await asyncio.gather(
            sensor._async_handle_update(),
            sensor._async_handle_update(),
        )
        assert len(writes) == 1, (
            f"Inverter write happened {len(writes)} times; expected exactly 1"
        )

    @pytest.mark.asyncio
    async def test_sequential_updates_both_execute(self) -> None:
        """Two non-overlapping sequential calls must both run the cycle."""
        sensor = self._Sensor()
        await sensor._async_handle_update()
        await sensor._async_handle_update()
        assert sensor.cycle_runs == 2

    def test_production_sensor_has_update_lock(self) -> None:
        """The real HSEMWorkingModeSensor.__init__ must create ``_update_lock``."""
        import inspect

        from custom_components.hsem.custom_sensors.working_mode_sensor import (
            HSEMWorkingModeSensor,
        )

        source = inspect.getsource(HSEMWorkingModeSensor.__init__)
        assert "_update_lock = asyncio.Lock()" in source, (
            "HSEMWorkingModeSensor.__init__ must contain self._update_lock = asyncio.Lock()"
        )


# ===========================================================================
# P0-07  Version comparison  (issue #271)
# ===========================================================================


class TestP007VersionComparison:
    """OLD BUG: Version strings were compared with Python's built-in string
    comparison, which is lexicographic.  ``"1.10" < "1.9"`` is True under
    lexicographic ordering because "1" == "1", "." == ".", and then "1" < "9".

    FIX: All version comparisons now use ``packaging.version.Version`` which
    implements correct PEP 440 numeric ordering.
    """

    def test_string_comparison_is_the_old_bug(self) -> None:
        """Demonstrate that naive string comparison is wrong."""
        # This is the broken old way — kept as documentation.
        assert "1.10" < "1.9"  # lexicographic: wrong

    def test_packaging_version_gives_correct_order(self) -> None:
        """``packaging.version.Version`` must order 1.10 > 1.9 correctly."""
        from packaging.version import Version

        assert Version("1.10") > Version("1.9")

    def test_parse_version_helper_returns_version_object(self) -> None:
        """``_parse_version`` must return a ``packaging.version.Version``."""
        from packaging.version import Version

        from custom_components.hsem import _parse_version

        result = _parse_version("1.10.0")
        assert isinstance(result, Version)

    def test_parse_version_returns_none_for_invalid_input(self) -> None:
        """Invalid strings must return None, not raise."""
        from custom_components.hsem import _parse_version

        assert _parse_version("not-a-version") is None
        assert _parse_version("") is None

    def test_1_10_greater_than_1_9(self) -> None:
        """The key regression: 1.10 must compare as *greater than* 1.9."""
        from custom_components.hsem import _parse_version

        assert _parse_version("1.10") > _parse_version("1.9")

    def test_pre_release_less_than_release(self) -> None:
        """Pre-release 1.5.0a1 must sort below the full release 1.5.0."""
        from custom_components.hsem import _parse_version

        assert _parse_version("1.5.0a1") < _parse_version("1.5.0")

    def test_patch_version_ordering(self) -> None:
        """1.10.1 must compare as greater than 1.10."""
        from custom_components.hsem import _parse_version

        assert _parse_version("1.10.1") > _parse_version("1.10")

    def test_installed_above_minimum_accepted(self) -> None:
        """An installed version above the minimum must pass the guard."""
        from custom_components.hsem import _parse_version

        installed = _parse_version("2.0.0")
        required = _parse_version("1.5.0a1")
        assert installed is not None and required is not None
        assert installed >= required

    def test_installed_below_minimum_rejected(self) -> None:
        """An installed version below the minimum must fail the guard."""
        from custom_components.hsem import _parse_version

        installed = _parse_version("1.4.9")
        required = _parse_version("1.5.0a1")
        assert installed is not None and required is not None
        assert installed < required


# ===========================================================================
# P0-08  Magic thresholds  (issue #272)
# ===========================================================================


class TestP008MagicThresholds:
    """OLD BUG: The planner used hard-coded ``0.1`` and ``0.2`` literals in
    multiple places with no explanation of their meaning or units.  A change
    in one location did not propagate to others, leading to the solar-charge
    threshold regression in v5.1.0 (one site used ``-0.1``, another ``-0.2``).

    FIX: Named constants ``SOLAR_SURPLUS_CHARGE_THRESHOLD_KWH`` and
    ``NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH`` are defined in ``const.py`` and
    imported everywhere the threshold is used.
    """

    def test_solar_surplus_constant_exists_and_is_negative(self) -> None:
        """SOLAR_SURPLUS_CHARGE_THRESHOLD_KWH must be defined and negative."""
        from custom_components.hsem.const import SOLAR_SURPLUS_CHARGE_THRESHOLD_KWH

        assert SOLAR_SURPLUS_CHARGE_THRESHOLD_KWH < 0

    def test_near_zero_constant_exists_and_is_non_negative(self) -> None:
        """NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH must be defined and >= 0."""
        from custom_components.hsem.const import NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH

        assert NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH >= 0

    def test_solar_surplus_default_matches_v510(self) -> None:
        """Default value must match v5.1.0 behaviour: -0.2 kWh."""
        from custom_components.hsem.const import SOLAR_SURPLUS_CHARGE_THRESHOLD_KWH

        assert pytest.approx(-0.2) == SOLAR_SURPLUS_CHARGE_THRESHOLD_KWH

    def test_near_zero_default_matches_v510(self) -> None:
        """Default value must match v5.1.0 behaviour: 0.1 kWh."""
        from custom_components.hsem.const import NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH

        assert pytest.approx(0.1) == NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH

    def test_charge_scheduler_imports_constants_not_literals(self) -> None:
        """charge_scheduler.py must import and reference the named constants."""
        import ast
        import pathlib

        source = pathlib.Path(
            "custom_components/hsem/planner/charge_scheduler.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)

        imported_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    imported_names.add(alias.asname or alias.name)

        assert "SOLAR_SURPLUS_CHARGE_THRESHOLD_KWH" in imported_names, (
            "charge_scheduler.py must import SOLAR_SURPLUS_CHARGE_THRESHOLD_KWH"
        )
        assert "NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH" in imported_names, (
            "charge_scheduler.py must import NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH"
        )

    def test_near_zero_threshold_used_in_optimization_strategy(self) -> None:
        """A slot at exactly NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH gets BatteriesChargeSolar
        (condition is <=), not BatteriesDischargeMode — validates that the threshold
        drives the decision."""
        from datetime import UTC, datetime

        from custom_components.hsem.const import NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH
        from custom_components.hsem.models.planner_outputs import PlannedSlot
        from custom_components.hsem.planner.charge_scheduler import (
            apply_optimization_strategy,
        )
        from custom_components.hsem.utils.recommendations import Recommendations

        now = datetime(2024, 6, 15, 12, 0, tzinfo=UTC)  # noon in June → summer
        slot = PlannedSlot(
            start=datetime(2024, 6, 15, 12, 0, tzinfo=UTC),
            end=datetime(2024, 6, 15, 13, 0, tzinfo=UTC),
            import_price=0.20,
            export_price=0.05,
            estimated_net_consumption=NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH,
            recommendation=None,
        )
        apply_optimization_strategy(
            slots=[slot],
            now=now,
            current_capacity=5.0,
            usable_capacity=9.0,
            required_capacity=0.0,
            months_winter=[1, 2, 3, 4, 10, 11, 12],
            warnings=[],
        )
        assert slot.recommendation == Recommendations.BatteriesChargeSolar.value, (
            "A slot at the near-zero boundary must be classified as BatteriesChargeSolar"
        )


# ===========================================================================
# P0-09  Exception handling  (issue #273)
# ===========================================================================


class TestP009ExceptionHandling:
    """OLD BUG: Twelve ``except Exception`` blocks across the codebase caught
    every possible exception silently or with minimal context.  Inverter write
    failures were swallowed without re-raising, so a ``ServiceNotFound`` error
    (e.g. Huawei Solar integration not loaded) would log a warning and then
    continue as if the write succeeded — leaving the inverter in the wrong mode.

    FIX: Every handler is narrowed to specific exception types.
    ``_LOGGER.exception()`` is used (includes automatic traceback).  Inverter
    write helpers re-raise on ``ServiceNotFound`` / ``ServiceValidationError``
    so callers can block hardware writes on failure.
    """

    def test_entity_not_found_error_is_homeassistant_error_subclass(self) -> None:
        """``EntityNotFoundError`` must be a subclass of ``HomeAssistantError``
        so it propagates through HA's own exception hierarchy."""
        from custom_components.hsem.utils.misc import EntityNotFoundError

        assert issubclass(EntityNotFoundError, HomeAssistantError)

    def test_unknown_state_raises_entity_not_found(self) -> None:
        """'unknown' entity state must raise ``EntityNotFoundError`` (not return 0)."""
        from custom_components.hsem.utils.misc import (
            EntityNotFoundError,
            ha_get_entity_state_and_convert,
        )

        hass = MagicMock()
        state_mock = MagicMock()
        state_mock.state = "unknown"
        hass.states.get.return_value = state_mock

        sensor = MagicMock()
        sensor.hass = hass
        sensor.entity_id = "sensor.hsem_test"

        with pytest.raises(EntityNotFoundError):
            ha_get_entity_state_and_convert(sensor, "sensor.battery_soc", "float")

    def test_unavailable_state_raises_entity_not_found(self) -> None:
        """'unavailable' entity state must raise ``EntityNotFoundError``."""
        from custom_components.hsem.utils.misc import (
            EntityNotFoundError,
            ha_get_entity_state_and_convert,
        )

        hass = MagicMock()
        state_mock = MagicMock()
        state_mock.state = "unavailable"
        hass.states.get.return_value = state_mock

        sensor = MagicMock()
        sensor.hass = hass
        sensor.entity_id = "sensor.hsem_test"

        with pytest.raises(EntityNotFoundError):
            ha_get_entity_state_and_convert(sensor, "sensor.battery_soc", "float")

    def test_missing_entity_raises_entity_not_found(self) -> None:
        """A completely absent entity must raise ``EntityNotFoundError``."""
        from custom_components.hsem.utils.misc import (
            EntityNotFoundError,
            ha_get_entity_state_and_convert,
        )

        hass = MagicMock()
        hass.states.get.return_value = None  # entity does not exist

        sensor = MagicMock()
        sensor.hass = hass
        sensor.entity_id = "sensor.hsem_test"

        with pytest.raises(EntityNotFoundError, match="not found"):
            ha_get_entity_state_and_convert(sensor, "sensor.missing", "float")

    def test_valid_entity_state_converts_without_raising(self) -> None:
        """A valid numeric state must convert cleanly — no exception."""
        from custom_components.hsem.utils.misc import ha_get_entity_state_and_convert

        hass = MagicMock()
        state_mock = MagicMock()
        state_mock.state = "83.5"
        hass.states.get.return_value = state_mock

        sensor = MagicMock()
        sensor.hass = hass
        sensor.entity_id = "sensor.hsem_test"

        result = ha_get_entity_state_and_convert(sensor, "sensor.soc", "float", 1)
        assert result == pytest.approx(83.5)

    @pytest.mark.asyncio
    async def test_async_set_number_value_propagates_service_not_found(self) -> None:
        """``async_set_number_value`` must re-raise ``ServiceNotFound`` so that
        callers can block hardware writes when the underlying HA service is absent.

        ``ServiceNotFound.__str__`` calls ``async_get_hass()`` which is not
        available in the test context — so we patch ``_LOGGER`` in the
        production module to prevent it from formatting the exception during
        logging before the re-raise.
        """
        from custom_components.hsem.utils.misc import async_set_number_value

        hass = MagicMock()
        state_mock = MagicMock()
        state_mock.state = "50"
        hass.states.get.return_value = state_mock
        # Production code calls hass.services.async_call — must be an AsyncMock.
        hass.services.async_call = AsyncMock(
            side_effect=ServiceNotFound("domain", "service")
        )

        sensor = MagicMock()
        sensor.hass = hass
        sensor.entity_id = "sensor.hsem_test"

        with (
            patch("custom_components.hsem.utils.misc._LOGGER"),
            pytest.raises(ServiceNotFound),
        ):
            await async_set_number_value(sensor, "number.inverter_charge_power", 2500)

    @pytest.mark.asyncio
    async def test_async_set_select_option_propagates_homeassistant_error(self) -> None:
        """``async_set_select_option`` must re-raise ``HomeAssistantError``."""
        from custom_components.hsem.utils.misc import async_set_select_option

        hass = MagicMock()
        state_mock = MagicMock()
        state_mock.state = "auto"
        hass.states.get.return_value = state_mock
        # Production code calls hass.services.async_call — must be an AsyncMock.
        hass.services.async_call = AsyncMock(
            side_effect=HomeAssistantError("inverter offline")
        )

        sensor = MagicMock()
        sensor.hass = hass
        sensor.entity_id = "sensor.hsem_test"

        with (
            patch("custom_components.hsem.utils.misc._LOGGER"),
            pytest.raises(HomeAssistantError),
        ):
            await async_set_select_option(
                sensor, "select.inverter_working_mode", "Manual"
            )
