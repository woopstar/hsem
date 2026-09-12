"""Consolidated regression test suite for HSEM P0 bugs (issue #274).

Each section maps to one P0 issue and contains:
- A short comment describing the *old* (broken) behaviour.
- Deterministic, pure-Python tests that would have *failed* before the fix.
- Tests that *pass* with the current, correct implementation.

Covered bugs
------------
P0-01  Month matching (issue #265) — string-containment false positive
P0-05  Invalid sensor values (issue #269) — "unknown"/"unavailable" → 0
P0-06  Concurrent updates (issue #270) — parallel update cycles not locked
P0-07  Version comparison (issue #271) — "1.10" < "1.9" (lexicographic)
P0-09  Exception handling (issue #273) — broad ``except Exception`` swallowed errors

CI compatibility
----------------
All tests are pure-Python; no running Home Assistant instance is required.
Async tests use ``pytest-asyncio`` with the ``asyncio`` mark.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.exceptions import HomeAssistantError, ServiceNotFound

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
        from custom_components.hsem.utils.conversion import convert_months_to_int

        result = convert_months_to_int(["10", "11", "12"])
        assert 1 not in result, (
            "January (1) must not be present after converting ['10','11','12']"
        )

    def test_january_only_matches_january(self) -> None:
        """Converting ['1'] must yield exactly [1]."""
        from custom_components.hsem.utils.conversion import convert_months_to_int

        result = convert_months_to_int(["1"])
        assert result == [1]
        for ghost in (10, 11, 12):
            assert ghost not in result, (
                f"Month {ghost} must not appear after converting ['1']"
            )

    def test_all_winter_months_correct(self) -> None:
        """Standard winter set [1,2,3,4,10,11,12] must survive round-trip."""
        from custom_components.hsem.utils.conversion import convert_months_to_int

        raw = ["1", "2", "3", "4", "10", "11", "12"]
        result = convert_months_to_int(raw)
        assert set(result) == {1, 2, 3, 4, 10, 11, 12}
        # Summer months must be absent
        for summer in (5, 6, 7, 8, 9):
            assert summer not in result

    def test_summer_months_not_in_winter_set(self) -> None:
        """May through September must not appear in the default winter set."""
        from custom_components.hsem.utils.conversion import convert_months_to_int

        winter = convert_months_to_int(["1", "2", "3", "4", "10", "11", "12"])
        for month in range(5, 10):
            assert month not in winter, f"Month {month} must not be in the winter set"

    def test_invalid_month_zero_raises(self) -> None:
        """Month 0 is out-of-range and must raise ``ValueError``."""
        from custom_components.hsem.utils.conversion import convert_months_to_int

        with pytest.raises(ValueError, match="Month must be between 1 and 12"):
            convert_months_to_int(["0"])

    def test_invalid_month_thirteen_raises(self) -> None:
        """Month 13 is out-of-range and must raise ``ValueError``."""
        from custom_components.hsem.utils.conversion import convert_months_to_int

        with pytest.raises(ValueError, match="Month must be between 1 and 12"):
            convert_months_to_int(["13"])


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
        from custom_components.hsem.utils.conversion import convert_to_float

        result = convert_to_float("unknown")
        assert result is None, f"Expected None, got {result!r}"

    def test_unavailable_returns_none_not_zero(self) -> None:
        """'unavailable' is a sentinel — must not become 0."""
        from custom_components.hsem.utils.conversion import convert_to_float

        result = convert_to_float("unavailable")
        assert result is None

    def test_empty_string_returns_none(self) -> None:
        """An empty string has no numeric meaning — must return None."""
        from custom_components.hsem.utils.conversion import convert_to_float

        assert convert_to_float("") is None

    def test_none_input_returns_none(self) -> None:
        """Python None must pass through as None."""
        from custom_components.hsem.utils.conversion import convert_to_float

        assert convert_to_float(None) is None

    def test_real_zero_is_not_none(self) -> None:
        """Numeric 0 / '0' is a valid measurement and must NOT become None."""
        from custom_components.hsem.utils.conversion import convert_to_float

        assert convert_to_float(0) == pytest.approx(0.0)
        assert convert_to_float("0") == pytest.approx(0.0)
        assert convert_to_float("0.0") == pytest.approx(0.0)

    def test_valid_positive_float_round_trips(self) -> None:
        """A valid float string converts cleanly."""
        from custom_components.hsem.utils.conversion import convert_to_float

        assert convert_to_float("75.5") == pytest.approx(75.5)

    def test_valid_negative_float_round_trips(self) -> None:
        """Negative values (e.g. export power) must convert correctly."""
        from custom_components.hsem.utils.conversion import convert_to_float

        assert convert_to_float("-3.14") == pytest.approx(-3.14)

    def test_unavailable_soc_sets_missing_entities_flag(self) -> None:
        """A None battery SoC (from unavailable sensor) must set missing_entities."""
        from custom_components.hsem.models.live_state import LiveState
        from custom_components.hsem.utils.conversion import convert_to_float

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
        from custom_components.hsem.utils.conversion import convert_to_float

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

        async def _async_handle_update(self, event: Any = None) -> None:
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

        class _WriteTracking(TestP006ConcurrentUpdates._Sensor):
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

    def test_production_coordinator_has_update_lock(self) -> None:
        """The HSEMDataUpdateCoordinator.__init__ must create ``_update_lock``.

        The lock was moved from HSEMWorkingModeSensor to the coordinator as part
        of the DataUpdateCoordinator refactor (issue #283).  The coordinator now
        owns the single update pipeline, so the concurrent-update guard lives there.
        """
        import inspect

        from custom_components.hsem.coordinator import HSEMDataUpdateCoordinator

        source = inspect.getsource(HSEMDataUpdateCoordinator.__init__)
        assert "_update_lock = asyncio.Lock()" in source, (
            "HSEMDataUpdateCoordinator.__init__ must contain self._update_lock = asyncio.Lock()"
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

        v_1_10 = _parse_version("1.10")
        v_1_9 = _parse_version("1.9")
        assert v_1_10 is not None and v_1_9 is not None
        assert v_1_10 > v_1_9

    def test_pre_release_less_than_release(self) -> None:
        """Pre-release 1.5.0a1 must sort below the full release 1.5.0."""
        from custom_components.hsem import _parse_version

        v_pre = _parse_version("1.5.0a1")
        v_rel = _parse_version("1.5.0")
        assert v_pre is not None and v_rel is not None
        assert v_pre < v_rel

    def test_patch_version_ordering(self) -> None:
        """1.10.1 must compare as greater than 1.10."""
        from custom_components.hsem import _parse_version

        v_patch = _parse_version("1.10.1")
        v_base = _parse_version("1.10")
        assert v_patch is not None and v_base is not None
        assert v_patch > v_base

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
        from custom_components.hsem.utils.ha_helpers import EntityNotFoundError

        assert issubclass(EntityNotFoundError, HomeAssistantError)

    def test_unknown_state_returns_none(self) -> None:
        """'unknown' entity state must return None for float reads."""
        from custom_components.hsem.utils.ha_helpers import (
            ha_get_entity_state_and_convert,
        )

        hass = MagicMock()
        state_mock = MagicMock()
        state_mock.state = "unknown"
        hass.states.get.return_value = state_mock

        sensor = MagicMock()
        sensor.hass = hass
        sensor.entity_id = "sensor.hsem_test"

        result = ha_get_entity_state_and_convert(sensor, "sensor.battery_soc", "float")
        assert result is None

    def test_unavailable_state_returns_none(self) -> None:
        """'unavailable' entity state must return None for float reads."""
        from custom_components.hsem.utils.ha_helpers import (
            ha_get_entity_state_and_convert,
        )

        hass = MagicMock()
        state_mock = MagicMock()
        state_mock.state = "unavailable"
        hass.states.get.return_value = state_mock

        sensor = MagicMock()
        sensor.hass = hass
        sensor.entity_id = "sensor.hsem_test"

        result = ha_get_entity_state_and_convert(sensor, "sensor.battery_soc", "float")
        assert result is None

    def test_missing_entity_raises_entity_not_found(self) -> None:
        """A completely absent entity must raise ``EntityNotFoundError``."""
        from custom_components.hsem.utils.ha_helpers import (
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
        from custom_components.hsem.utils.ha_helpers import (
            ha_get_entity_state_and_convert,
        )

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
        from custom_components.hsem.utils.ha_helpers import async_set_number_value

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
            patch("custom_components.hsem.utils.ha_helpers._LOGGER"),
            pytest.raises(ServiceNotFound),
        ):
            await async_set_number_value(sensor, "number.inverter_charge_power", 2500)

    @pytest.mark.asyncio
    async def test_async_set_select_option_propagates_homeassistant_error(self) -> None:
        """``async_set_select_option`` must re-raise ``HomeAssistantError``."""
        from custom_components.hsem.utils.ha_helpers import async_set_select_option

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
            patch("custom_components.hsem.utils.ha_helpers._LOGGER"),
            pytest.raises(HomeAssistantError),
        ):
            await async_set_select_option(
                sensor, "select.inverter_working_mode", "Manual"
            )
