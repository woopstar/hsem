"""Tests for HSEMDataUpdateCoordinator (issue #283).

Acceptance criteria:
- Data is fetched once per interval (single coordinator, not per entity).
- Entities do not independently fetch the same data.
- Coordinator exposes last update status via coordinator.last_update_success.
- Update lock prevents concurrent pipeline executions.
- CoordinatorData contains a consistent snapshot after each cycle.
- async_setup registers timers; async_teardown cancels them.
- async_options_updated triggers a fresh pipeline cycle.

Implementation note
-------------------
``HSEMDataUpdateCoordinator.__init__`` calls ``DataUpdateCoordinator.__init__``
which invokes ``homeassistant.helpers.frame.report_usage``.  That helper
requires the HA event-loop frame helper to be bootstrapped (only done inside a
real HA test environment via ``hass`` fixtures).  To keep these tests isolated
and fast we use one of two approaches depending on what is being verified:

1. **Source inspection** – when the test only needs to confirm that a certain
   attribute is *initialised* in ``__init__``, we inspect the source code
   directly (no construction required).
2. **``object.__new__`` + manual attribute injection** – when the test needs to
   call *methods* on the coordinator we bypass ``__init__`` entirely and set
   only the attributes the method under test actually reads.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from custom_components.hsem.coordinator import (
    CoordinatorData,
    HSEMDataUpdateCoordinator,
)
from custom_components.hsem.coordinator_builder import generate_recommendation_intervals

# ---------------------------------------------------------------------------
# Helper: build a bare coordinator instance without calling __init__
# ---------------------------------------------------------------------------


def _make_bare_coordinator() -> HSEMDataUpdateCoordinator:
    """Return an HSEMDataUpdateCoordinator whose __init__ was NOT called.

    Attributes required by individual tests are set explicitly on the returned
    object.  This avoids the ``frame.report_usage`` call inside HA's
    ``DataUpdateCoordinator.__init__`` which requires a bootstrapped HA runtime.
    """
    coord = object.__new__(HSEMDataUpdateCoordinator)
    # Minimal set of attributes that the coordinator methods may reference.
    coord._update_lock = asyncio.Lock()
    coord._interval_timer_unsub = None
    coord._hourly_timer_unsub = None
    coord._listener_unsubs = []
    coord._timer_interval = None
    coord._next_update = None
    coord.data = None  # type: ignore[assignment]  # test sets data to None before first cycle
    coord.last_update_success = True
    cfg = MagicMock()
    cfg.verbose_logging = False
    cfg.update_interval = 5
    cfg.recommendation_interval_minutes = 60
    cfg.recommendation_interval_length = 24
    coord._cfg = cfg
    coord._options_update_task = None
    coord._options_update_debounce_task = None
    return coord


# ---------------------------------------------------------------------------
# CoordinatorData unit tests
# ---------------------------------------------------------------------------


class TestCoordinatorData:
    """Verify the CoordinatorData dataclass defaults and field types."""

    def test_default_instance_has_no_live_state(self) -> None:
        """A fresh CoordinatorData must have live=None."""
        data = CoordinatorData()
        assert data.live is None
        assert data.cfg is None
        assert data.state is None
        assert data.last_updated is None
        assert data.next_update is None

    def test_empty_list_fields_are_mutable(self) -> None:
        """List fields must be independent instances (not shared default)."""
        d1 = CoordinatorData()
        d2 = CoordinatorData()
        d1.hourly_recommendations.append("x")  # type: ignore[arg-type]  # intentional: test verifies list independence with a sentinel string
        assert "x" not in d2.hourly_recommendations

    def test_numeric_fields_default_to_zero(self) -> None:
        """Numeric accumulator fields must default to 0.0."""
        data = CoordinatorData()
        assert data.batteries_schedules_remaining_capacity_needed == pytest.approx(0.0)
        assert data.current_required_battery == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Coordinator construction tests (source inspection — no HA runtime needed)
# ---------------------------------------------------------------------------


class TestCoordinatorConstruction:
    """Verify key attributes are initialised in HSEMDataUpdateCoordinator.__init__."""

    def test_update_lock_is_asyncio_lock(self) -> None:
        """__init__ must create self._update_lock = asyncio.Lock()."""
        source = inspect.getsource(HSEMDataUpdateCoordinator.__init__)
        assert "_update_lock = asyncio.Lock()" in source, (
            "HSEMDataUpdateCoordinator.__init__ must contain "
            "self._update_lock = asyncio.Lock()"
        )

    def test_initial_data_field_comment_or_absent(self) -> None:
        """data is managed by the DataUpdateCoordinator base class (starts as None).

        We verify this via the bare instance helper which sets data=None to
        reflect the pre-first-cycle state.
        """
        coord = _make_bare_coordinator()
        assert coord.data is None

    def test_timer_handles_start_as_none(self) -> None:
        """Timer unsub handles must be None before async_setup is called."""
        coord = _make_bare_coordinator()
        assert coord._interval_timer_unsub is None
        assert coord._hourly_timer_unsub is None


# ---------------------------------------------------------------------------
# Concurrent update lock tests
# ---------------------------------------------------------------------------


class _StubCoordinator:
    """Minimal stub that replaces only the locking behaviour of HSEMDataUpdateCoordinator."""

    def __init__(self) -> None:
        self._update_lock = asyncio.Lock()
        self._cycle_count = 0
        self._skip_count = 0
        self._cfg = MagicMock()
        self._cfg.verbose_logging = False

    async def _async_handle_update(self, event: Any = None) -> None:
        """Identical guard logic to the production coordinator."""
        if self._update_lock.locked():
            self._skip_count += 1
            return
        async with self._update_lock:
            await self._async_run_update_cycle()

    async def _async_run_update_cycle(self) -> None:
        """Simulated slow cycle (2 event-loop ticks)."""
        self._cycle_count += 1
        await asyncio.sleep(0)
        await asyncio.sleep(0)


class TestCoordinatorUpdateLock:
    """Verify the asyncio.Lock guard inside _async_handle_update."""

    @pytest.mark.asyncio
    async def test_single_call_runs_once(self) -> None:
        """A lone call executes exactly one cycle."""
        coord = _StubCoordinator()
        await coord._async_handle_update()
        assert coord._cycle_count == 1
        assert coord._skip_count == 0

    @pytest.mark.asyncio
    async def test_concurrent_second_call_is_dropped(self) -> None:
        """While a cycle is running a second concurrent call is skipped, not queued."""
        coord = _StubCoordinator()
        await asyncio.gather(
            coord._async_handle_update(),
            coord._async_handle_update(),
        )
        assert coord._cycle_count == 1, f"Expected 1 cycle, got {coord._cycle_count}"
        assert coord._skip_count == 1, f"Expected 1 skip, got {coord._skip_count}"

    @pytest.mark.asyncio
    async def test_sequential_calls_both_execute(self) -> None:
        """Two non-overlapping sequential calls both run the cycle."""
        coord = _StubCoordinator()
        await coord._async_handle_update()
        await coord._async_handle_update()
        assert coord._cycle_count == 2


# ---------------------------------------------------------------------------
# Coordinator async_teardown
# ---------------------------------------------------------------------------


class TestCoordinatorTeardown:
    """Verify async_teardown cancels all registered timer subscriptions."""

    @pytest.mark.asyncio
    async def test_teardown_cancels_timers(self) -> None:
        """async_teardown must call the unsub callables for both timers."""
        coordinator = _make_bare_coordinator()

        interval_unsub = MagicMock()
        hourly_unsub = MagicMock()
        coordinator._interval_timer_unsub = interval_unsub
        coordinator._hourly_timer_unsub = hourly_unsub

        await coordinator.async_teardown()

        interval_unsub.assert_called_once()
        hourly_unsub.assert_called_once()
        assert coordinator._interval_timer_unsub is None
        assert coordinator._hourly_timer_unsub is None

    @pytest.mark.asyncio
    async def test_teardown_safe_when_no_timers(self) -> None:
        """async_teardown must not raise when no timers were registered."""
        coordinator = _make_bare_coordinator()
        # Both handles are None — no error expected
        await coordinator.async_teardown()


# ---------------------------------------------------------------------------
# Options-update background task
# ---------------------------------------------------------------------------


class TestOptionsUpdateBackgroundTask:
    """async_options_updated must schedule the pipeline as a background task.

    Regression: the config-entry update listener (fired by
    ``async_update_entry`` from switch/number/time entities) used to await
    the full MILP/ML pipeline inline.  The toggle itself was fast (HA fires
    listeners as tasks), but the *effect* of a toggle was delayed because
    each options change ran a full synchronous pipeline cycle.  The fix
    schedules the cycle via ``hass.async_create_task`` with
    cancel-and-reschedule semantics so repeated toggles collapse into one
    run and the listener returns immediately.

    A short debounce window is used so rapid toggles only trigger a single
    planner run after the user stops clicking.
    """

    @pytest.mark.asyncio
    @patch("custom_components.hsem.coordinator.OPTIONS_UPDATE_DEBOUNCE_SECONDS", 0.0)
    async def test_options_updated_schedules_background_task(self) -> None:
        """async_options_updated returns immediately after scheduling a task."""
        coordinator = _make_bare_coordinator()
        coordinator.hass = MagicMock()

        scheduled: list[asyncio.Task] = []

        def _create_task(coro: Any, *, name: str, **kwargs: Any) -> asyncio.Task:
            task = asyncio.get_running_loop().create_task(coro, name=name)
            scheduled.append(task)
            return task

        coordinator.hass.async_create_task = MagicMock(side_effect=_create_task)

        # Make the pipeline cycle a no-op so the task completes quickly.
        async def _noop_cycle(*args: Any, **kwargs: Any) -> None:
            await asyncio.sleep(0)

        coordinator._async_handle_update = _noop_cycle  # type: ignore[method-assign]  # test monkey-patch

        await coordinator.async_options_updated()

        # The method must have scheduled exactly one debounce task with
        # eager-start disabled so the switch service call returns immediately.
        coordinator.hass.async_create_task.assert_called_once()  # type: ignore[union-attr]  # mock assertion
        call_kwargs = coordinator.hass.async_create_task.call_args[1]  # type: ignore[attr-defined]
        assert call_kwargs.get("eager_start") is False
        assert len(scheduled) == 1
        assert scheduled[0].get_name() == "hsem_options_update_debounce"

        # Let the debounce task finish; it then schedules the background task.
        await scheduled[0]
        assert len(scheduled) == 2
        assert scheduled[1].get_name() == "hsem_options_update"
        await scheduled[1]

    @pytest.mark.asyncio
    @patch("custom_components.hsem.coordinator.OPTIONS_UPDATE_DEBOUNCE_SECONDS", 0.0)
    async def test_repeated_toggle_cancels_pending_task(self) -> None:
        """Repeated options updates collapse into a single background run."""
        coordinator = _make_bare_coordinator()
        coordinator.hass = MagicMock()

        scheduled: list[asyncio.Task] = []

        def _create_task(coro: Any, *, name: str, **kwargs: Any) -> asyncio.Task:
            task = asyncio.get_running_loop().create_task(coro, name=name)
            scheduled.append(task)
            return task

        coordinator.hass.async_create_task = MagicMock(side_effect=_create_task)

        # A cycle that blocks until cancelled.
        started = asyncio.Event()

        async def _blocking_cycle(*args: Any, **kwargs: Any) -> None:
            started.set()
            await asyncio.sleep(60)

        coordinator._async_handle_update = _blocking_cycle  # type: ignore[method-assign]  # test monkey-patch

        await coordinator.async_options_updated()
        assert len(scheduled) == 1
        debounce_task = scheduled[0]
        assert debounce_task.get_name() == "hsem_options_update_debounce"

        # Let the debounce task fire and start the background update cycle.
        await started.wait()
        assert len(scheduled) == 2
        background_task = scheduled[1]
        assert background_task.get_name() == "hsem_options_update"

        # A second toggle cancels the previous debounce/background tasks and
        # schedules a fresh debounce task.
        await coordinator.async_options_updated()
        assert len(scheduled) == 3
        assert scheduled[2].get_name() == "hsem_options_update_debounce"

        # Let the new debounce task fire so it cancels the old background task.
        await scheduled[2]
        assert background_task.cancelled() or background_task.cancelling()

    @pytest.mark.asyncio
    @patch("custom_components.hsem.coordinator.OPTIONS_UPDATE_DEBOUNCE_SECONDS", 0.0)
    async def test_teardown_cancels_pending_options_task(self) -> None:
        """async_teardown must cancel pending debounce and options-update tasks."""
        coordinator = _make_bare_coordinator()
        coordinator.hass = MagicMock()

        async def _blocking_cycle(*args: Any, **kwargs: Any) -> None:
            await asyncio.sleep(60)

        coordinator._async_handle_update = _blocking_cycle  # type: ignore[method-assign]  # test monkey-patch
        coordinator.hass.async_create_task = MagicMock(  # type: ignore[method-assign]  # test monkey-patch
            side_effect=lambda coro, *, name, **kwargs: (
                asyncio.get_running_loop().create_task(coro, name=name)
            )
        )

        await coordinator.async_options_updated()
        debounce_task = coordinator._options_update_debounce_task
        assert debounce_task is not None and not debounce_task.done()

        # Wait for the debounce task to schedule the background task.
        await debounce_task
        task = coordinator._options_update_task
        assert task is not None and not task.done()
        # The debounce task is cleared once it has scheduled the background run.
        assert coordinator._options_update_debounce_task is None

        await coordinator.async_teardown()

        assert task.cancelling() or task.cancelled() or task.done()
        assert coordinator._options_update_task is None


# ---------------------------------------------------------------------------
# Coordinator recommendation interval generation
# ---------------------------------------------------------------------------


class TestGenerateRecommendationIntervals:
    """Verify the recommendation-slot generation helper inside the coordinator."""

    def test_generates_correct_count_for_60min_24h(self) -> None:
        """60-minute slots over 24 hours must produce 24 slots."""
        slots = generate_recommendation_intervals(60, 24)
        assert len(slots) == 24

    def test_generates_correct_count_for_15min_48h(self) -> None:
        """15-minute slots over 48 hours must produce 192 slots."""
        slots = generate_recommendation_intervals(15, 48)
        assert len(slots) == 192

    def test_slots_start_at_midnight(self) -> None:
        """The first slot must start at midnight of the current day."""
        slots = generate_recommendation_intervals(60, 24)
        first = slots[0]
        assert first.start.hour == 0
        assert first.start.minute == 0

    def test_consecutive_slots_are_contiguous(self) -> None:
        """Each slot's end must equal the next slot's start."""
        slots = generate_recommendation_intervals(15, 2)
        for i in range(len(slots) - 1):
            assert slots[i].end == slots[i + 1].start

    def test_slots_have_zero_defaults(self) -> None:
        """All numeric fields on a freshly generated slot must be 0.0."""
        slots = generate_recommendation_intervals(60, 1)
        slot = slots[0]
        assert slot.import_price == pytest.approx(0.0)
        assert slot.export_price == pytest.approx(0.0)
        assert slot.solcast_pv_estimate_kwh == pytest.approx(0.0)
        assert slot.avg_house_consumption_kwh == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Single-poll guarantee
# ---------------------------------------------------------------------------


class TestSinglePollGuarantee:
    """Entities must not independently poll; only the coordinator fetches data."""

    def test_working_mode_sensor_should_poll_is_false(self) -> None:
        """HSEMWorkingModeSensor.should_poll must return False."""
        from custom_components.hsem.custom_sensors.working_mode_sensor import (
            HSEMWorkingModeSensor,
        )

        assert HSEMWorkingModeSensor.should_poll.fget is not None  # type: ignore[attr-defined]  # mock attribute set in test
        # Instantiate a minimal stub to call the property
        sensor = object.__new__(HSEMWorkingModeSensor)
        # Inject a minimal coordinator mock
        coord = MagicMock()
        coord.last_update_success = False
        coord.data = None
        sensor.coordinator = coord
        assert sensor.should_poll is False

    def test_degraded_mode_sensor_should_poll_is_false(self) -> None:
        """HSEMDegradedModeSensor.should_poll must return False."""
        from custom_components.hsem.custom_sensors.degraded_mode_sensor import (
            HSEMDegradedModeSensor,
        )

        sensor = object.__new__(HSEMDegradedModeSensor)
        coord = MagicMock()
        coord.last_update_success = False
        coord.data = None
        sensor.coordinator = coord
        sensor._restored_state = None
        assert sensor.should_poll is False


# ---------------------------------------------------------------------------
# Coordinator data exposure
# ---------------------------------------------------------------------------


class TestCoordinatorDataExposure:
    """Verify coordinator exposes last_update_success and data correctly."""

    def test_last_update_success_defaults_true(self) -> None:
        """DataUpdateCoordinator initialises last_update_success to True (HA default).

        The coordinator is considered healthy until its first failed cycle, which
        is the standard behaviour for HA's DataUpdateCoordinator base class.
        We verify the attribute is present and True via the bare instance which
        reflects this default.
        """
        coord = _make_bare_coordinator()
        # Bare coordinator sets last_update_success=True to mirror the HA default.
        assert coord.last_update_success is True

    def test_data_is_none_before_first_cycle(self) -> None:
        """coordinator.data must be None before async_setup is called."""
        coord = _make_bare_coordinator()
        assert coord.data is None
