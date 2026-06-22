"""Tests for MILP re-solve gating in HSEMDataUpdateCoordinator (issue #582).

The MILP optimizer is a global optimiser.  Re-solving it every coordinator
cycle (default: 1 minute) with noisy live PV/load/SoC inputs makes the EV
charger power oscillate on/off, amplified by the ``charger_min_power_w`` floor.

These tests verify:

- ``_should_rerun_milp`` returns ``True`` on each documented trigger and
  ``False`` when inputs are unchanged within the staleness window.
- The current-slot EV charger power is recomputed (smoothed) from the cached
  plan's energy allocation so it stays stable across multiple cycles within
  the same slot — no oscillation.
- ``planner_min_resolve_interval_minutes = 0`` preserves the legacy behaviour
  (re-solve every cycle).

Implementation note
-------------------
``HSEMDataUpdateCoordinator.__init__`` calls ``DataUpdateCoordinator.__init__``
which needs a bootstrapped HA runtime.  These tests bypass ``__init__`` with
``object.__new__`` and set only the attributes the methods under test read.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.hsem.coordinator import HSEMDataUpdateCoordinator
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.models.price_point import PricePoint
from custom_components.hsem.models.solcast_slot import SolcastSlot
from custom_components.hsem.utils.prices import SlotPrice

TZ = timezone(timedelta(hours=2))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_coordinator() -> HSEMDataUpdateCoordinator:
    """Return a bare coordinator with only the gating attributes set."""
    coord = object.__new__(HSEMDataUpdateCoordinator)
    coord._last_milp_planner_input = None
    coord._last_milp_solve_ts = None
    coord._last_milp_current_slot_start = None
    coord._last_planner_output = None
    coord._force_milp_rerun = False
    return coord


def _base_input(**overrides: object) -> PlannerInput:
    """Return a PlannerInput with prices, solcast, and a 15-min slot grid."""
    price_points = [
        PricePoint(hour=h, import_price=2.0, export_price=0.5, day_offset=0)
        for h in range(24)
    ]
    solcast_slots = [
        SolcastSlot(hour=h, pv_estimate=1.0, day_offset=0) for h in range(24)
    ]
    defaults: dict[str, object] = {
        "now_iso": "2024-06-15T12:07:00+02:00",
        "interval_minutes": 15,
        "interval_length_hours": 24,
        "planner_min_resolve_interval_minutes": 15,
        "price_points": price_points,
        "solcast_slots": solcast_slots,
        "ev_planned_load_connected": True,
        "ev_planned_load_current_soc_pct": 50.0,
        "ev_planned_load_smart_charging_enabled": True,
        "ev_planned_load_charger_power_kw": 11.0,
        "ev_planned_load_charger_min_power_w": 1380.0,
    }
    defaults.update(overrides)
    return PlannerInput(**defaults)  # type: ignore[arg-type]


def _prime_last_solve(
    coord: HSEMDataUpdateCoordinator,
    inp: PlannerInput,
    now: datetime,
    *,
    output: PlannerOutput | None = None,
) -> None:
    """Record *inp*/*now* as the last MILP solve so the gate can compare."""
    coord._last_milp_planner_input = inp
    coord._last_milp_solve_ts = now
    coord._last_milp_current_slot_start = coord._current_slot_start(inp, now)
    coord._last_planner_output = output if output is not None else PlannerOutput()
    coord._force_milp_rerun = False


# ---------------------------------------------------------------------------
# _should_rerun_milp — trigger conditions
# ---------------------------------------------------------------------------


class TestShouldRerunMilpTriggers:
    """Each documented trigger must force a re-solve; no change must not."""

    def test_first_cycle_returns_true(self) -> None:
        """No previous MILP — always solve."""
        coord = _make_coordinator()
        now = datetime(2024, 6, 15, 12, 7, tzinfo=TZ)
        assert coord._should_rerun_milp(_base_input(), now) is True

    def test_unchanged_inputs_within_window_returns_false(self) -> None:
        """Same inputs, 1 minute later, within staleness window — reuse plan."""
        coord = _make_coordinator()
        solve_time = datetime(2024, 6, 15, 12, 6, tzinfo=TZ)
        inp = _base_input()
        _prime_last_solve(coord, inp, solve_time)

        now = solve_time + timedelta(minutes=1)
        assert coord._should_rerun_milp(_base_input(), now) is False

    def test_slot_boundary_crossing_returns_true(self) -> None:
        """Crossing into a new 15-min slot must re-solve."""
        coord = _make_coordinator()
        solve_time = datetime(2024, 6, 15, 12, 14, tzinfo=TZ)
        inp = _base_input()
        _prime_last_solve(coord, inp, solve_time)

        # 12:14 is in the 12:00 slot; 12:16 is in the 12:15 slot.
        now = datetime(2024, 6, 15, 12, 16, tzinfo=TZ)
        assert coord._should_rerun_milp(_base_input(), now) is True

    def test_price_change_returns_true(self) -> None:
        """A changed import price must re-solve."""
        coord = _make_coordinator()
        solve_time = datetime(2024, 6, 15, 12, 6, tzinfo=TZ)
        _prime_last_solve(coord, _base_input(), solve_time)

        changed = _base_input()
        changed.price_points[5].import_price = 3.5
        now = solve_time + timedelta(minutes=1)
        assert coord._should_rerun_milp(changed, now) is True

    def test_solcast_change_returns_true(self) -> None:
        """A changed PV forecast for a future slot must re-solve."""
        coord = _make_coordinator()
        solve_time = datetime(2024, 6, 15, 12, 6, tzinfo=TZ)
        _prime_last_solve(coord, _base_input(), solve_time)

        changed = _base_input()
        changed.solcast_slots[18].pv_estimate = 4.2
        now = solve_time + timedelta(minutes=1)
        assert coord._should_rerun_milp(changed, now) is True

    def test_ev_connected_change_returns_true(self) -> None:
        """EV plugging in / unplugging must re-solve."""
        coord = _make_coordinator()
        solve_time = datetime(2024, 6, 15, 12, 6, tzinfo=TZ)
        _prime_last_solve(
            coord, _base_input(ev_planned_load_connected=True), solve_time
        )

        changed = _base_input(ev_planned_load_connected=False)
        now = solve_time + timedelta(minutes=1)
        assert coord._should_rerun_milp(changed, now) is True

    def test_ev_soc_change_above_threshold_returns_true(self) -> None:
        """EV SoC change > 2 percentage points must re-solve."""
        coord = _make_coordinator()
        solve_time = datetime(2024, 6, 15, 12, 6, tzinfo=TZ)
        _prime_last_solve(
            coord, _base_input(ev_planned_load_current_soc_pct=50.0), solve_time
        )

        changed = _base_input(ev_planned_load_current_soc_pct=53.0)
        now = solve_time + timedelta(minutes=1)
        assert coord._should_rerun_milp(changed, now) is True

    def test_ev_soc_change_below_threshold_returns_false(self) -> None:
        """EV SoC change <= 2 percentage points must not re-solve."""
        coord = _make_coordinator()
        solve_time = datetime(2024, 6, 15, 12, 6, tzinfo=TZ)
        _prime_last_solve(
            coord, _base_input(ev_planned_load_current_soc_pct=50.0), solve_time
        )

        changed = _base_input(ev_planned_load_current_soc_pct=51.5)
        now = solve_time + timedelta(minutes=1)
        assert coord._should_rerun_milp(changed, now) is False

    def test_smart_charging_toggle_returns_true(self) -> None:
        """Toggling smart charging must re-solve."""
        coord = _make_coordinator()
        solve_time = datetime(2024, 6, 15, 12, 6, tzinfo=TZ)
        _prime_last_solve(
            coord,
            _base_input(ev_planned_load_smart_charging_enabled=True),
            solve_time,
        )

        changed = _base_input(ev_planned_load_smart_charging_enabled=False)
        now = solve_time + timedelta(minutes=1)
        assert coord._should_rerun_milp(changed, now) is True

    def test_user_action_force_flag_returns_true(self) -> None:
        """A user action (config/switch change) must force a re-solve."""
        coord = _make_coordinator()
        solve_time = datetime(2024, 6, 15, 12, 6, tzinfo=TZ)
        _prime_last_solve(coord, _base_input(), solve_time)
        coord._force_milp_rerun = True

        now = solve_time + timedelta(minutes=1)
        assert coord._should_rerun_milp(_base_input(), now) is True

    def test_staleness_timeout_returns_true(self) -> None:
        """Exceeding planner_min_resolve_interval_minutes must re-solve."""
        coord = _make_coordinator()
        solve_time = datetime(2024, 6, 15, 12, 6, tzinfo=TZ)
        _prime_last_solve(coord, _base_input(), solve_time)

        # 15 minutes later — but stay in the same slot to isolate staleness.
        # 12:06 is in the 12:00 slot; advance the recorded slot start so the
        # boundary check does not also fire.
        coord._last_milp_current_slot_start = datetime(2024, 6, 15, 12, 15, tzinfo=TZ)
        now = datetime(2024, 6, 15, 12, 21, tzinfo=TZ)
        assert coord._should_rerun_milp(_base_input(), now) is True


class TestShouldRerunMilpBackwardCompat:
    """planner_min_resolve_interval_minutes = 0 preserves legacy behaviour."""

    def test_zero_interval_always_resolves(self) -> None:
        """With the interval set to 0, every cycle re-solves."""
        coord = _make_coordinator()
        solve_time = datetime(2024, 6, 15, 12, 6, tzinfo=TZ)
        inp = _base_input(planner_min_resolve_interval_minutes=0)
        _prime_last_solve(coord, inp, solve_time)

        now = solve_time + timedelta(seconds=10)
        same = _base_input(planner_min_resolve_interval_minutes=0)
        assert coord._should_rerun_milp(same, now) is True


# ---------------------------------------------------------------------------
# _smoothed_power_w — power formula, cap, and floor
# ---------------------------------------------------------------------------


class TestSmoothedPowerW:
    """The smoothing helper must cap at rated power and floor at the minimum."""

    def test_power_formula(self) -> None:
        """1.0 kWh over 0.25 h => 4000 W."""
        result = HSEMDataUpdateCoordinator._smoothed_power_w(1.0, 0.25, 11000.0, 1380.0)
        assert result == pytest.approx(4000.0)

    def test_caps_at_rated_power(self) -> None:
        """A high energy / short remaining time is capped at the rated power."""
        result = HSEMDataUpdateCoordinator._smoothed_power_w(5.0, 0.05, 11000.0, 1380.0)
        assert result == pytest.approx(11000.0)

    def test_floors_to_zero_below_minimum(self) -> None:
        """Below the minimum operating power the charger cannot start."""
        result = HSEMDataUpdateCoordinator._smoothed_power_w(0.1, 0.25, 11000.0, 1380.0)
        assert result == pytest.approx(0.0)

    def test_uncapped_when_max_zero(self) -> None:
        """A zero rated power means uncapped (only the floor applies)."""
        result = HSEMDataUpdateCoordinator._smoothed_power_w(5.0, 0.05, 0.0, 1380.0)
        assert result == pytest.approx(100000.0)


# ---------------------------------------------------------------------------
# _smooth_current_slot_ev_power — stable output across cycles in one slot
# ---------------------------------------------------------------------------


def _slot_with_ev_load(
    start: datetime, end: datetime, total_ev_kwh: float, power_w: float
) -> PlannedSlot:
    """Return a PlannedSlot carrying an EV load and a charger power."""
    return PlannedSlot(
        start=start,
        end=end,
        price=SlotPrice(2.0, 0.5),
        ev_total_planned_load_kwh=total_ev_kwh,
        ev_charger_calculated_power=power_w,
    )


class TestSmoothCurrentSlotEvPower:
    """Current-slot power must be recomputed from the cached energy allocation."""

    def test_power_rises_smoothly_as_slot_progresses(self) -> None:
        """Same cached energy → power scales up smoothly as time elapses.

        The cached plan allocates 1.0 kWh AC to the 12:00–12:15 slot.  As the
        slot progresses, dividing by the shrinking remaining time yields a
        smoothly rising power — never a 0↔max toggle.
        """
        coord = _make_coordinator()
        slot_start = datetime(2024, 6, 15, 12, 0, tzinfo=TZ)
        slot_end = datetime(2024, 6, 15, 12, 15, tzinfo=TZ)
        coord._last_milp_planner_input = _base_input(
            ev_planned_load_charger_power_kw=11.0,
            ev_planned_load_charger_min_power_w=1380.0,
        )

        powers: list[float] = []
        for minute in (1, 5, 10):
            output = PlannerOutput(
                slots=[_slot_with_ev_load(slot_start, slot_end, 1.0, 4000.0)]
            )
            now = slot_start + timedelta(minutes=minute)
            coord._smooth_current_slot_ev_power(output, now)
            powers.append(output.slots[0].ev_charger_calculated_power)

        # Monotonically non-decreasing, all positive, none toggled to zero.
        assert all(p > 0 for p in powers)
        assert powers[0] <= powers[1] <= powers[2]

    def test_power_capped_at_rated(self) -> None:
        """Near slot end, the smoothed power is capped at the charger rating."""
        coord = _make_coordinator()
        slot_start = datetime(2024, 6, 15, 12, 0, tzinfo=TZ)
        slot_end = datetime(2024, 6, 15, 12, 15, tzinfo=TZ)
        coord._last_milp_planner_input = _base_input(
            ev_planned_load_charger_power_kw=11.0
        )
        output = PlannerOutput(
            slots=[_slot_with_ev_load(slot_start, slot_end, 2.0, 8000.0)]
        )
        # 1 minute remaining: 2 kWh / (1/60 h) = 120000 W → capped at 11000.
        now = datetime(2024, 6, 15, 12, 14, tzinfo=TZ)
        coord._smooth_current_slot_ev_power(output, now)
        assert output.slots[0].ev_charger_calculated_power == pytest.approx(11000.0)

    def test_no_ev_load_leaves_power_untouched(self) -> None:
        """A slot with no EV load keeps its cached power value."""
        coord = _make_coordinator()
        slot_start = datetime(2024, 6, 15, 12, 0, tzinfo=TZ)
        slot_end = datetime(2024, 6, 15, 12, 15, tzinfo=TZ)
        coord._last_milp_planner_input = _base_input()
        output = PlannerOutput(
            slots=[_slot_with_ev_load(slot_start, slot_end, 0.0, 1234.0)]
        )
        now = slot_start + timedelta(minutes=5)
        coord._smooth_current_slot_ev_power(output, now)
        assert output.slots[0].ev_charger_calculated_power == pytest.approx(1234.0)

    def test_zero_power_slot_not_resurrected(self) -> None:
        """A slot whose cached power is 0 (below min) stays at 0."""
        coord = _make_coordinator()
        slot_start = datetime(2024, 6, 15, 12, 0, tzinfo=TZ)
        slot_end = datetime(2024, 6, 15, 12, 15, tzinfo=TZ)
        coord._last_milp_planner_input = _base_input()
        output = PlannerOutput(
            slots=[_slot_with_ev_load(slot_start, slot_end, 1.0, 0.0)]
        )
        now = slot_start + timedelta(minutes=5)
        coord._smooth_current_slot_ev_power(output, now)
        assert output.slots[0].ev_charger_calculated_power == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Integration: no oscillation across cycles with fluctuating live inputs
# ---------------------------------------------------------------------------


class TestNoOscillationIntegration:
    """Live PV/load fluctuation within a slot must not toggle the charger."""

    def test_charger_power_does_not_oscillate(self) -> None:
        """Across cycles within one slot the gate reuses the plan and smooths.

        Live PV/load fluctuate every cycle (they do not feed the gate's price,
        solcast, or EV checks), so ``_should_rerun_milp`` keeps returning
        ``False`` and the EV power is smoothed rather than re-solved to a value
        that could toggle between 0 and max.
        """
        coord = _make_coordinator()
        slot_start = datetime(2024, 6, 15, 12, 0, tzinfo=TZ)
        slot_end = datetime(2024, 6, 15, 12, 15, tzinfo=TZ)
        inp = _base_input()
        _prime_last_solve(coord, inp, slot_start)
        coord._last_milp_current_slot_start = slot_start

        powers: list[float] = []
        toggled_decisions: list[bool] = []
        for minute in range(1, 14):
            now = slot_start + timedelta(minutes=minute)
            # Live readings change every cycle but are not gate inputs.
            current = _base_input(
                now_iso=now.isoformat(),
                live_solar_production_w=1000.0 + minute * 137.0,
                live_house_consumption_w=500.0 + (minute % 3) * 800.0,
            )
            rerun = coord._should_rerun_milp(current, now)
            toggled_decisions.append(rerun)
            if not rerun:
                output = PlannerOutput(
                    slots=[_slot_with_ev_load(slot_start, slot_end, 1.0, 4000.0)]
                )
                coord._smooth_current_slot_ev_power(output, now)
                powers.append(output.slots[0].ev_charger_calculated_power)

        # The gate never re-solved during the slot.
        assert not any(toggled_decisions)
        # All power values are positive and non-decreasing (no 0↔max toggle).
        assert all(p > 0 for p in powers)
        assert powers == sorted(powers)
