"""Tests for issue #1200 — the wait-mode reserve must respect the dynamic floor.

``wait_mode_reserve_kwh`` gates ``batteries_wait_mode`` self-consumption
(``self_consumption_with_reserve``): the applier lets the house use the battery
down to that reserve, measured as live capacity above the **hardware** floor.
The planner derived it from the selected plan's SoC trajectory (issues #914,
#954), which is measured above the **effective** floor.  With the dynamic
discharge floor active the two origins differ, and the energy between them,
the energy the floor sets aside, counted as surplus the house may use.

The reserve is now published above the hardware floor.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.coordinator_dynamic_floor import (
    compute_dynamic_floor_from_plan,
)
from custom_components.hsem.custom_sensors.applier import (
    async_apply_battery_settings,
)
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.planner import run_planner
from custom_components.hsem.planner.milp_optimizer import is_scipy_available
from custom_components.hsem.utils.dynamic_floor import DynamicDischargeFloor
from custom_components.hsem.utils.misc import get_max_discharge_power
from custom_components.hsem.utils.recommendations import Recommendations
from custom_components.hsem.utils.soc_bounds import (
    wait_mode_reserve_above_hardware_floor,
)
from custom_components.hsem.utils.workingmodes import WorkingModes
from tests.test_batteries_wait_mode import (
    _LOGGER_PATCH,
    _cfg,
    _live as _applier_live,
    _sensor,
    _wait_rec,
    _write_and_verify_ok,
)
from tests.test_dynamic_floor_reference_plan import (
    _NOW,
    _hourly_recommendations,
    _live,
    _planner_input,
)

_WAIT = Recommendations.BatteriesWaitMode.value
_APPLIER = "custom_components.hsem.custom_sensors.applier"
_MODE = "select.wm"
_CAP = "number.maxdis"
_RATED_KWH = 10.0
_HARDWARE_FLOOR_PCT = 5.0
#: ``get_max_discharge_power`` of the 10 kWh pack the applier tests use.
_RATED_DISCHARGE_W = get_max_discharge_power(10_000)


def _stored_kwh(soc_pct: float) -> float:
    """Return the live capacity above the hardware floor, as the applier sees it."""
    return _RATED_KWH * (soc_pct - _HARDWARE_FLOOR_PCT) / 100.0


class TestWaitModeReserveAboveHardwareFloor:
    """``wait_mode_reserve_above_hardware_floor``: one origin for both sides."""

    def test_floor_energy_is_added_to_the_plan_reserve(self) -> None:
        """Effective floor 68 % on a 5 % hardware floor holds 6.3 kWh."""
        assert wait_mode_reserve_above_hardware_floor(
            0.5, 10.0, 5.0, 68.0
        ) == pytest.approx(6.8)

    def test_without_a_dynamic_floor_the_plan_reserve_is_unchanged(self) -> None:
        assert wait_mode_reserve_above_hardware_floor(
            0.542, 10.0, 5.0, 5.0
        ) == pytest.approx(0.542)

    def test_unknown_plan_reserve_stays_unknown(self) -> None:
        """``None`` means strict Wait in the applier; a floor must not undo that."""
        assert wait_mode_reserve_above_hardware_floor(None, 10.0, 5.0, 68.0) is None

    @pytest.mark.parametrize("rated", [None, float("nan"), -3.0, 0.0])
    def test_unusable_rated_capacity_adds_nothing(self, rated: float | None) -> None:
        assert wait_mode_reserve_above_hardware_floor(
            0.4, rated, 5.0, 68.0
        ) == pytest.approx(0.4)

    def test_effective_floor_below_the_hardware_floor_adds_nothing(self) -> None:
        assert wait_mode_reserve_above_hardware_floor(
            0.4, 10.0, 5.0, 3.0
        ) == pytest.approx(0.4)


pytestmark_planner = pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available in this environment"
)


def _replan(soc_pct: float) -> tuple[float, PlannerOutput, PlannerOutput]:
    """Run one replan as the coordinator does: reference, floor, final.

    The #1125 fixture: 10 kWh, 5 % hardware floor, 21:30, a 0.15 night that is
    no affordable refill, so the bridge runs to tomorrow's first solar surplus.
    """
    planner_input = replace(_planner_input(0.15), battery_soc_pct=soc_pct)
    reference = run_planner(planner_input)
    live = _live()
    live.huawei_batteries_soc_pct = soc_pct
    floor_pct, _diag = compute_dynamic_floor_from_plan(
        DynamicDischargeFloor(),
        _hourly_recommendations(),
        reference,
        planner_input,
        live,
        _NOW,
    )
    final = run_planner(replace(planner_input, dynamic_discharge_floor_pct=floor_pct))
    return floor_pct, reference, final


def _live_slot_recommendation(output: PlannerOutput) -> str | None:
    return next(slot for slot in output.slots if slot.end > _NOW).recommendation


@pytestmark_planner
class TestPublishedReserveThroughRunPlanner:
    """What ``run_planner`` publishes with and without the dynamic floor."""

    def test_battery_below_the_floor_reserves_everything_it_holds(self) -> None:
        """68 % under a 77.72 % floor: the origin is the live SoC, 6.3 kWh up."""
        floor_pct, _reference, final = _replan(68.0)

        assert floor_pct > 68.0
        assert _live_slot_recommendation(final) == _WAIT
        assert final.wait_mode_reserve_kwh is not None
        assert final.wait_mode_reserve_kwh >= _stored_kwh(68.0) - 1e-9
        assert final.wait_mode_reserve_kwh == pytest.approx(6.3)

    def test_battery_above_the_floor_reserves_the_floor(self) -> None:
        """90 % over a 77.72 % floor: 7.27 kWh held, 1.23 kWh usable."""
        floor_pct, _reference, final = _replan(90.0)
        floor_kwh = _RATED_KWH * (floor_pct - _HARDWARE_FLOOR_PCT) / 100.0

        assert 68.0 < floor_pct < 90.0
        assert final.wait_mode_reserve_kwh is not None
        assert final.wait_mode_reserve_kwh >= floor_kwh - 1e-3
        assert final.wait_mode_reserve_kwh < _stored_kwh(90.0)

    def test_without_the_dynamic_floor_the_reserve_is_unchanged(self) -> None:
        """The reference solve has no floor: both origins are the hardware floor."""
        _floor, reference, _final = _replan(68.0)

        assert reference.wait_mode_reserve_kwh is not None
        # Far below the 6.3 kWh the battery holds: nothing was added.
        assert reference.wait_mode_reserve_kwh < 1.0


async def _apply(
    capacity_kwh: float, reserve_kwh: float | None, *, working_mode: WorkingModes
) -> tuple[list[str], list[int]]:
    """Apply a wait slot and return the working modes and discharge caps written.

    The battery starts in *working_mode* with a stale 380 W discharge cap, so
    both the mode the applier wants and its cap show up as writes.
    """
    live = _applier_live(working_mode=working_mode.value)
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
    async def test_battery_below_the_floor_is_held(self) -> None:
        """68 % = 6.3 kWh stored, all of it reserved: TOU hold and a 0 W cap."""
        _floor, _reference, final = _replan(68.0)

        modes, caps = await _apply(
            _stored_kwh(68.0),
            final.wait_mode_reserve_kwh,
            working_mode=WorkingModes.MaximizeSelfConsumption,
        )

        assert modes == [WorkingModes.TimeOfUse.value]
        assert caps == [0]

    @pytest.mark.asyncio
    async def test_reserve_above_the_effective_floor_released_the_floor(self) -> None:
        """Before #1200 the reserve was 0.0 here and all 6.3 kWh were released."""
        modes, caps = await _apply(
            _stored_kwh(68.0), 0.0, working_mode=WorkingModes.TimeOfUse
        )

        assert modes == [WorkingModes.MaximizeSelfConsumption.value]
        assert caps == [_RATED_DISCHARGE_W]

    @pytest.mark.asyncio
    async def test_energy_above_the_floor_may_be_used(self) -> None:
        """90 %: the 1.23 kWh above the floor is served at the full rate."""
        _floor, _reference, final = _replan(90.0)

        modes, caps = await _apply(
            _stored_kwh(90.0),
            final.wait_mode_reserve_kwh,
            working_mode=WorkingModes.TimeOfUse,
        )

        assert modes == [WorkingModes.MaximizeSelfConsumption.value]
        assert caps == [_RATED_DISCHARGE_W]
