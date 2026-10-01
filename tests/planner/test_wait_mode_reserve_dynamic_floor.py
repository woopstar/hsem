"""Tests for issue #1200 — the wait-mode reserve must respect the dynamic floor.

``wait_mode_reserve_kwh`` gates ``batteries_wait_mode`` self-consumption
(``self_consumption_with_reserve``): the applier lets the house use the battery
down to that reserve.  It was derived from the selected plan's SoC trajectory
alone (issues #914, #954).  When the dynamic discharge floor holds the battery
the trajectory is flat, the reserve is close to zero, and the applier released
the energy the floor had set aside.

The reserve is now at least the live slot's ``discharge_reserve_kwh`` (issue
#1188), which has the same origin as the applier's live capacity: kWh above
the hardware floor.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.custom_sensors.applier import (
    async_apply_battery_settings,
)
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.planner.discharge_reserve import (
    wait_mode_reserve_with_floor,
)
from custom_components.hsem.planner.discharge_scheduler import (
    calculate_required_battery_for_plan,
)
from custom_components.hsem.planner.milp_optimizer import is_scipy_available
from custom_components.hsem.utils.misc import get_max_discharge_power
from custom_components.hsem.utils.prices import SlotPrice
from custom_components.hsem.utils.recommendations import Recommendations
from custom_components.hsem.utils.workingmodes import WorkingModes
from tests.planner.test_dynamic_floor_reserve_profile import _future, _replan
from tests.test_batteries_wait_mode import (
    _LOGGER_PATCH,
    _cfg,
    _live,
    _sensor,
    _wait_rec,
    _write_and_verify_ok,
)
from tests.test_dynamic_floor_reference_plan import _NOW

_WAIT = Recommendations.BatteriesWaitMode.value
_APPLIER = "custom_components.hsem.custom_sensors.applier"
_MODE = "select.wm"
_CAP = "number.maxdis"
#: ``get_max_discharge_power`` of the 10 kWh pack the applier tests use.
_RATED_DISCHARGE_W = get_max_discharge_power(10_000)
_T0 = datetime(2026, 9, 28, 21, 0, tzinfo=UTC)


def _slot(offset_hours: int, reserve_kwh: float) -> PlannedSlot:
    start = _T0 + timedelta(hours=offset_hours)
    slot = PlannedSlot(
        start=start,
        end=start + timedelta(hours=1),
        price=SlotPrice(import_price=0.20, export_price=0.05),
    )
    slot.discharge_reserve_kwh = reserve_kwh
    return slot


class TestWaitModeReserveWithFloor:
    """``wait_mode_reserve_with_floor``: the larger of the two requirements."""

    _NOW = _T0 + timedelta(minutes=30)

    def test_floor_reserve_above_the_plan_reserve_wins(self) -> None:
        slots = [_slot(0, 6.3), _slot(1, 5.5)]

        assert wait_mode_reserve_with_floor(0.542, slots, self._NOW) == pytest.approx(
            6.3
        )

    def test_plan_reserve_above_the_floor_reserve_is_kept(self) -> None:
        slots = [_slot(0, 1.2), _slot(1, 0.8)]

        assert wait_mode_reserve_with_floor(2.0, slots, self._NOW) == pytest.approx(2.0)

    def test_without_a_dynamic_floor_the_plan_reserve_is_unchanged(self) -> None:
        slots = [_slot(0, 0.0), _slot(1, 0.0)]

        assert wait_mode_reserve_with_floor(0.542, slots, self._NOW) == pytest.approx(
            0.542
        )

    def test_reads_the_live_slot_not_a_past_or_later_one(self) -> None:
        """The slot that contains *now*, whatever order the slots come in."""
        slots = [_slot(2, 1.0), _slot(-1, 9.0), _slot(0, 4.0), _slot(1, 3.0)]

        assert wait_mode_reserve_with_floor(0.0, slots, self._NOW) == pytest.approx(4.0)

    def test_unknown_plan_reserve_stays_unknown(self) -> None:
        """``None`` means strict Wait in the applier; a floor must not undo that."""
        assert wait_mode_reserve_with_floor(None, [_slot(0, 6.3)], self._NOW) is None

    def test_no_future_slot_keeps_the_plan_reserve(self) -> None:
        assert wait_mode_reserve_with_floor(
            0.5, [_slot(-3, 6.3)], self._NOW
        ) == pytest.approx(0.5)


pytestmark_planner = pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available in this environment"
)


def _plan_reserve(output: PlannerOutput, soc_pct: float) -> float | None:
    """Return the #914/#954 trajectory reserve of *output* on its own."""
    return calculate_required_battery_for_plan(
        output.slots, _NOW, 10.0 * (soc_pct - 5.0) / 100.0
    )


@pytestmark_planner
class TestPublishedReserveThroughRunPlanner:
    """The #1125 fixture: 10 kWh, 5 % hardware floor, 0.15 night, 21:30."""

    def test_floor_held_wait_slot_reserves_the_floor(self) -> None:
        """25 %: the floor (41.8 %) holds the battery; the reserve is all of it.

        The bridge to the planned 02:00 charge costs 0.19 throughout, so the
        battery is held in the live slot (issue #1222 only moves a shortfall
        between differently priced slots).
        """
        floor_pct, _profile, _reference, final = _replan(25.0)
        live_slot = _future(final)[0]

        assert floor_pct > 25.0
        assert live_slot.recommendation == _WAIT
        assert live_slot.discharge_reserve_kwh == pytest.approx(2.0)
        # The trajectory reserve alone would release part of it.
        plan_reserve = _plan_reserve(final, 25.0)
        assert plan_reserve is not None
        assert plan_reserve < 2.0 - 0.05
        assert final.wait_mode_reserve_kwh is not None
        assert final.wait_mode_reserve_kwh >= live_slot.discharge_reserve_kwh - 1e-9
        assert final.wait_mode_reserve_kwh == pytest.approx(2.0)

    def test_reserve_is_at_least_the_live_slots_floor_reserve(self) -> None:
        """40 % and 90 %: whatever the live slot does, the floor part is kept."""
        for soc_pct in (40.0, 90.0):
            _floor, _profile, _reference, final = _replan(soc_pct)
            live_slot = _future(final)[0]

            assert live_slot.discharge_reserve_kwh > 1.0
            assert final.wait_mode_reserve_kwh is not None
            assert final.wait_mode_reserve_kwh >= (
                live_slot.discharge_reserve_kwh - 5e-4
            )

    def test_without_the_dynamic_floor_the_reserve_is_unchanged(self) -> None:
        """The reference solve has no floor: the #954 reserve, decay included."""
        _floor, _profile, reference, _final = _replan(68.0)

        assert all(slot.discharge_reserve_kwh == 0.0 for slot in reference.slots)
        assert reference.wait_mode_reserve_kwh == _plan_reserve(reference, 68.0)


async def _apply(
    capacity_kwh: float, reserve_kwh: float | None, *, working_mode: WorkingModes
) -> tuple[list[str], list[int]]:
    """Apply a wait slot and return the working modes and discharge caps written.

    The battery starts in *working_mode* with a stale 380 W discharge cap, so
    both the mode the applier wants and its cap show up as writes.
    """
    live = _live(working_mode=working_mode.value)
    live.battery_current_capacity_kwh = capacity_kwh
    live.huawei_batteries_rated_capacity_wh = 10_000
    live.huawei_batteries_max_discharge_power_w = 380
    with (
        patch(_LOGGER_PATCH, new_callable=MagicMock),
        patch(f"{_APPLIER}.async_write_and_verify", side_effect=_write_and_verify_ok),
        patch(f"{_APPLIER}.async_set_select_option", new_callable=AsyncMock) as select,
        patch(f"{_APPLIER}.async_set_number_value", new_callable=AsyncMock) as number,
    ):
        await async_apply_battery_settings(
            _sensor(), _cfg(), live, _wait_rec(), 5.0, wait_mode_reserve_kwh=reserve_kwh
        )
    modes = [call.args[2] for call in select.await_args_list if call.args[1] == _MODE]
    caps = [call.args[2] for call in number.await_args_list if call.args[1] == _CAP]
    return modes, caps


@pytestmark_planner
class TestApplierUsesTheFloorReserve:
    """``self_consumption_with_reserve`` with the planner's published reserve."""

    @pytest.mark.asyncio
    async def test_battery_at_the_floor_reserve_is_held(self) -> None:
        """25 % = 2.0 kWh stored, 2.0 kWh reserved: TOU hold and a 0 W cap."""
        _floor, _profile, _reference, final = _replan(25.0)

        modes, caps = await _apply(
            2.0,
            final.wait_mode_reserve_kwh,
            working_mode=WorkingModes.MaximizeSelfConsumption,
        )

        assert modes == [WorkingModes.TimeOfUse.value]
        assert caps == [0]

    @pytest.mark.asyncio
    async def test_trajectory_reserve_alone_released_the_floor(self) -> None:
        """What happened before #1200: the floor's 2.0 kWh was not reserved."""
        _floor, _profile, _reference, final = _replan(25.0)

        modes, caps = await _apply(
            2.0, _plan_reserve(final, 25.0), working_mode=WorkingModes.TimeOfUse
        )

        assert modes == [WorkingModes.MaximizeSelfConsumption.value]
        assert caps == [_RATED_DISCHARGE_W]

    @pytest.mark.asyncio
    async def test_energy_above_the_floor_reserve_may_be_used(self) -> None:
        """1 kWh above the reserve: MSC at the full rate, down to the reserve."""
        _floor, _profile, _reference, final = _replan(68.0)
        assert final.wait_mode_reserve_kwh is not None

        modes, caps = await _apply(
            final.wait_mode_reserve_kwh + 1.0,
            final.wait_mode_reserve_kwh,
            working_mode=WorkingModes.TimeOfUse,
        )

        assert modes == [WorkingModes.MaximizeSelfConsumption.value]
        assert caps == [_RATED_DISCHARGE_W]
