"""Regression tests for issue #1140 — the dynamic floor never saw a planned charge.

Every coordinator cycle regenerates ``_hourly_recommendations`` from scratch
(``batteries_charged_kwh = 0.0``, ``recommendation = None``) before the
planner phase, so ``compute_floor()``'s grid-charge refill branch never fired:
the bridge always ran to the next PV surplus.  After sunset that is the whole
night's house load, the floor exceeded the live SoC, and the #1094 live-SoC
cap then pinned the model at 0 kWh above the origin — the battery sat in
``batteries_wait_mode`` all evening (issue #1125).

The floor is now computed from a **floor-free reference solve in the same
replan**.  Reading the previous committed plan instead fed the floor back
into the plan it constrains: at moderate night prices a partial night charge
lowered the floor, the next plan charged less, the floor rose again, and the
two flipped on every replan.

Since #1138 a cash-correct reference plan no longer buys at a cheap night when
tomorrow's PV refills the battery anyway, so the scan also ends the bridge at
an *affordable* grid refill the plan does not schedule (issue #1156).

The coordinator tests go through the real regeneration in
``_async_collect_and_populate`` and the real ``compute_floor()``; only the HA
entity reads, the consumption/PV population (a deterministic profile), and
the planner executor job are faked.  ``TestRealPlanner`` runs the real
``run_planner`` for both solves.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem import coordinator_builder
from custom_components.hsem.coordinator import HSEMDataUpdateCoordinator
from custom_components.hsem.coordinator_dynamic_floor import (
    build_dynamic_floor_bridge_slots,
    compute_dynamic_floor_from_plan,
)
from custom_components.hsem.custom_sensors.hourly_data_populator.consumption import (
    ConsumptionPopulation,
)
from custom_components.hsem.custom_sensors.hourly_data_populator.prices_solcast import (
    _populate_from_attributes,
)
from custom_components.hsem.models.hourly_consumption_average import (
    HourlyConsumptionAverage,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.models.price_point import PricePoint
from custom_components.hsem.models.solcast_slot import SolcastSlot
from custom_components.hsem.planner import run_planner
from custom_components.hsem.utils.dynamic_floor import DynamicDischargeFloor
from custom_components.hsem.utils.prices import SlotPrice
from custom_components.hsem.utils.recommendations import Recommendations
from custom_components.hsem.utils.soc_bounds import resolve_soc_bounds_pct
from tests.coordinator_fixtures import make_real_coordinator
from tests.test_ha_mock_integration import make_fake_config_entry

_TZ = timezone(timedelta(hours=3))
_NOW = datetime(2026, 9, 28, 21, 30, tzinfo=_TZ)
_MIDNIGHT = _NOW.replace(hour=0, minute=0)
_SLOT = timedelta(minutes=15)

# The #1125/#1094 reporter: 9.5 kWh usable between 5 % and 100 % SoC.
_HARDWARE_FLOOR_PCT = 5.0
_RATED_WH = 10_000.0
_USABLE_KWH = 9.5
_LIVE_SOC_PCT = 68.0

_HOUSE_KWH_PER_SLOT = 0.15  # 0.6 kW house load
_PV_KWH_PER_SLOT = 0.5  # 2 kW PV during the day → surplus
_PV_FIRST_SURPLUS = datetime(2026, 9, 29, 9, 30, tzinfo=_TZ)
_PV_LAST_SURPLUS = datetime(2026, 9, 29, 16, 0, tzinfo=_TZ)
_CHARGE_START = datetime(2026, 9, 29, 2, 0, tzinfo=_TZ)
_CHARGE_END = datetime(2026, 9, 29, 2, 45, tzinfo=_TZ)
_CHARGE_KWH_PER_SLOT = 1.0  # 4 kW grid charge
# 21:30 → 02:00 is 18 slots × 0.15 kWh = 2.7 kWh.  The three planned charge
# slots cost the same, so their 3.0 kWh is credited at the first of them
# (issue #1198) and covers the bridge there.
_PLANNED_REFILL = _CHARGE_START
# An affordable refill is credited slot by slot at the charge power: 1.25 kWh
# per quarter-hour first covers the 2.7 kWh in the 02:30 slot.
_EXPECTED_REFILL = datetime(2026, 9, 29, 2, 30, tzinfo=_TZ)
# 21:30 → 09:30 is 12 h of 0.6 kW = 7.2 kWh; × 1.15 margin / 9.5 kWh.
# 7.2 kWh × margin on top of the 5 % hardware floor of a 10 kWh battery
# (issue #1221): 87.8 %.
_SOLAR_BRIDGE_FLOOR_PCT = 5.0 + 7.2 * 1.15 / _USABLE_KWH * 95.0

_CHARGE = Recommendations.BatteriesChargeGrid.value
_WAIT = Recommendations.BatteriesWaitMode.value

_CYCLE = "custom_components.hsem.coordinator_cycle"
_BUILDER = "custom_components.hsem.coordinator_builder"
_LOAD = "custom_components.hsem.coordinator_load_forecast"
_PHASE = "custom_components.hsem.coordinator_planner_phase"


def _is_pv_surplus(start: datetime) -> bool:
    """Return whether *start* lies in the daytime PV-surplus window."""
    return _PV_FIRST_SURPLUS <= start < _PV_LAST_SURPLUS


def _populate_consumption(
    recommendations: list[HourlyRecommendation], *_args: Any, **_kwargs: Any
) -> ConsumptionPopulation:
    """Fill every slot with a flat 0.6 kW house load."""
    for rec in recommendations:
        rec.avg_house_consumption_kwh = _HOUSE_KWH_PER_SLOT
        rec.avg_house_consumption_1d_kwh = _HOUSE_KWH_PER_SLOT
        rec.avg_house_consumption_3d_kwh = _HOUSE_KWH_PER_SLOT
        rec.avg_house_consumption_7d_kwh = _HOUSE_KWH_PER_SLOT
        rec.avg_house_consumption_14d_kwh = _HOUSE_KWH_PER_SLOT
    return ConsumptionPopulation(ok=True)


def _populate_pv(
    recommendations: list[HourlyRecommendation], *_args: Any, **_kwargs: Any
) -> None:
    """Forecast PV surplus from 09:30 tomorrow, nothing overnight."""
    for rec in recommendations:
        rec.solcast_pv_estimate_kwh = (
            _PV_KWH_PER_SLOT if _is_pv_surplus(rec.start) else 0.0
        )


def _live() -> LiveState:
    """Return the reporter's live battery state at 21:30."""
    return LiveState(
        force_working_mode_state="auto",
        huawei_batteries_soc_pct=_LIVE_SOC_PCT,
        huawei_batteries_rated_capacity_wh=_RATED_WH,
        huawei_batteries_end_of_discharge_soc_pct=_HARDWARE_FLOOR_PCT,
        huawei_batteries_charging_cutoff_capacity_pct=100.0,
        house_consumption_power_w=600.0,
    )


def _snapshot() -> MagicMock:
    """Return a state snapshot carrying the reporter's live state."""
    snapshot = MagicMock()
    snapshot.live = _live()
    snapshot.energy_average_values = {}
    return snapshot


def _plan(*, grid_charge: bool, cheap_night: bool = False) -> PlannerOutput:
    """Return a 48 h plan: grid charge 02:00-02:45 (or none), wait otherwise.

    Every slot carries the cycle's house load and PV forecast, as a real plan
    does; the bridge scan reads them from here (issue #1187).  With
    *cheap_night* the slots are priced 0.20, except 0.03 from 02:00 to 06:00
    each night; otherwise every price is the default 0.0 (flat).
    """
    slots: list[PlannedSlot] = []
    start = _MIDNIGHT
    while start < _MIDNIGHT + timedelta(hours=48):
        charging = grid_charge and _CHARGE_START <= start < _CHARGE_END
        price = SlotPrice(0.0, 0.0)
        if cheap_night:
            price = SlotPrice(0.03 if 2 <= start.hour < 6 else 0.20, 0.0)
        slots.append(
            PlannedSlot(
                start=start,
                end=start + _SLOT,
                price=price,
                avg_house_consumption_kwh=_HOUSE_KWH_PER_SLOT,
                solcast_pv_estimate_kwh=(
                    _PV_KWH_PER_SLOT if _is_pv_surplus(start) else 0.0
                ),
                recommendation=_CHARGE if charging else _WAIT,
                batteries_charged_kwh=_CHARGE_KWH_PER_SLOT if charging else 0.0,
            )
        )
        start += _SLOT
    return PlannerOutput(slots=slots)


def _coordinator(
    tmp_path: Path,
    *,
    reference: PlannerOutput,
    committed: PlannerOutput | None = None,
    floor_enabled: bool = True,
) -> tuple[HSEMDataUpdateCoordinator, MagicMock, list[PlannerInput]]:
    """Return a real coordinator whose planner job returns canned plans.

    The floor-free solve (``dynamic_discharge_floor_pct is None``) returns
    *reference*; the solve with a floor returns a plan without a charge.
    Every planner input is recorded, in order.
    """
    config_entry = make_fake_config_entry(
        {
            "hsem_dynamic_discharge_floor": floor_enabled,
            "hsem_recommendation_interval_minutes": 15,
            "hsem_recommendation_interval_length": 48,
        }
    )
    solved: list[PlannerInput] = []

    async def _solve(_fn: Any, planner_input: PlannerInput) -> PlannerOutput:
        solved.append(planner_input)
        if planner_input.dynamic_discharge_floor_pct is None:
            return reference
        return _plan(grid_charge=False)

    hass = MagicMock()
    hass.config.config_dir = str(tmp_path)
    hass.async_add_executor_job = AsyncMock(side_effect=_solve)
    coordinator = make_real_coordinator(hass=hass, config_entry=config_entry)
    coordinator._last_planner_output = committed
    coordinator._set_update_interval = AsyncMock()  # type: ignore[method-assign]
    build = MagicMock(return_value=PlannerInput())
    return coordinator, build, solved


@contextmanager
def _patched_cycle(build: MagicMock) -> Iterator[None]:
    """Patch HA reads, the clock, and forecast population for one cycle."""
    collected: tuple[MagicMock, None, list[Any]] = (_snapshot(), None, [])
    with (
        patch(f"{_CYCLE}.hsem_now", return_value=_NOW),
        patch(f"{_BUILDER}.hsem_now", return_value=_NOW),
        patch(
            f"{_CYCLE}.async_collect_all_states",
            AsyncMock(return_value=collected),
        ),
        patch(
            f"{_LOAD}.populate_avg_house_consumption_from_snapshot",
            side_effect=_populate_consumption,
        ),
        patch(
            f"{_CYCLE}.populate_price_and_solcast_from_snapshot",
            side_effect=_populate_pv,
        ),
        patch(f"{_PHASE}.build_planner_input", build),
    ):
        yield


async def _run_collect_then_plan(
    coordinator: HSEMDataUpdateCoordinator, build: MagicMock
) -> None:
    """Run the real regeneration, then the real planner phase, at 21:30."""
    with _patched_cycle(build):
        consumption_ok, state = await coordinator._async_collect_and_populate(_NOW)
        assert consumption_ok is True
        live = coordinator._live
        assert live is not None
        await coordinator._run_planner_phase(
            _NOW, live, coordinator._cfg, state, consumption_ok, 0
        )


def _model_origin_pct(dynamic_floor_pct: float) -> float:
    """Return the planner's effective floor after the #1094 live-SoC cap."""
    _hardware, effective, _maximum = resolve_soc_bounds_pct(
        _HARDWARE_FLOOR_PCT, 100.0, dynamic_floor_pct, _LIVE_SOC_PCT
    )
    return effective


class TestFloorReadsReferencePlan:
    """The bridge scan sees the grid charge in this replan's reference solve."""

    @pytest.mark.asyncio
    async def test_reference_overnight_charge_is_the_refill(
        self, tmp_path: Path
    ) -> None:
        """At 21:30 the 02:00 grid charge is the refill, so the floor is released.

        The plan is priced: the charge sits in the first slots of a cheap
        night, where the scan credits it (issue #1198).
        """
        coordinator, build, solved = _coordinator(
            tmp_path, reference=_plan(grid_charge=True, cheap_night=True)
        )

        await _run_collect_then_plan(coordinator, build)

        diag = coordinator._effective_discharge_floor_diag
        assert diag is not None
        assert diag["refill_type"] == "grid_charge"
        assert diag["next_refill_slot"] == _PLANNED_REFILL.isoformat()
        # 4.5 h of 0.6 kW load (2.7 kWh) is covered by the planned charge, so
        # the bridge reserve is zero (documented in planner-spec.md, #1140).
        assert diag["reserve_kwh"] == pytest.approx(0.0)
        assert diag["bridge_duration_hours"] == pytest.approx(4.5)

        floor_pct = coordinator._effective_discharge_floor_pct
        assert floor_pct == pytest.approx(_HARDWARE_FLOOR_PCT)
        # Two solves: the floor-free reference, then the plan with the floor.
        assert [i.dynamic_discharge_floor_pct for i in solved] == [
            None,
            pytest.approx(_HARDWARE_FLOOR_PCT),
        ]
        assert coordinator._last_planner_input is solved[1]
        build.assert_called_once()
        assert build.call_args.kwargs["dynamic_discharge_floor_pct"] is None
        # Not pinned at the live SoC: the model has 68 % → 5 % to spend on
        # evening load, so batteries_wait_mode is no longer forced.
        origin = _model_origin_pct(_HARDWARE_FLOOR_PCT)
        dischargeable_kwh = (_LIVE_SOC_PCT - origin) / 100.0 * _RATED_WH / 1000.0
        assert dischargeable_kwh == pytest.approx(6.3)

    @pytest.mark.asyncio
    async def test_reference_without_charge_bridges_to_solar(
        self, tmp_path: Path
    ) -> None:
        """No planned grid charge: the floor bridges the night to PV (#600)."""
        coordinator, build, solved = _coordinator(
            tmp_path, reference=_plan(grid_charge=False)
        )

        await _run_collect_then_plan(coordinator, build)

        diag = coordinator._effective_discharge_floor_diag
        assert diag is not None
        assert diag["refill_type"] == "solar_surplus"
        assert diag["next_refill_slot"] == _PV_FIRST_SURPLUS.isoformat()
        assert diag["reserve_kwh"] == pytest.approx(7.2)
        floor_pct = coordinator._effective_discharge_floor_pct
        assert floor_pct is not None
        assert floor_pct == pytest.approx(_SOLAR_BRIDGE_FLOOR_PCT)
        assert solved[1].dynamic_discharge_floor_pct == pytest.approx(floor_pct)
        assert _model_origin_pct(floor_pct) == pytest.approx(_LIVE_SOC_PCT)

    @pytest.mark.asyncio
    async def test_cheap_night_without_a_charge_is_the_refill(
        self, tmp_path: Path
    ) -> None:
        """#1156: an unplanned 0.03 night refill releases the floor.

        The reference plan does not charge, but 02:00-06:00 is the
        look-ahead's cheapest price.  At the 5 kW default charge power each
        quarter-hour can take 1.25 kWh, so the third one covers the 2.7 kWh
        bridged by 02:00.
        """
        coordinator, build, solved = _coordinator(
            tmp_path, reference=_plan(grid_charge=False, cheap_night=True)
        )

        await _run_collect_then_plan(coordinator, build)

        diag = coordinator._effective_discharge_floor_diag
        assert diag is not None
        assert diag["refill_type"] == "grid_available"
        assert diag["next_refill_slot"] == _EXPECTED_REFILL.isoformat()
        assert diag["reserve_kwh"] == pytest.approx(0.0)
        assert diag["cheap_refill_price"] == pytest.approx(0.03)
        assert coordinator._effective_discharge_floor_pct == pytest.approx(
            _HARDWARE_FLOOR_PCT
        )
        assert solved[1].dynamic_discharge_floor_pct == pytest.approx(
            _HARDWARE_FLOOR_PCT
        )

    @pytest.mark.asyncio
    async def test_committed_plan_does_not_feed_the_floor(self, tmp_path: Path) -> None:
        """A charge in the previous plan is ignored — no plan→floor feedback."""
        coordinator, build, _solved = _coordinator(
            tmp_path,
            reference=_plan(grid_charge=False),
            committed=_plan(grid_charge=True),
        )

        await _run_collect_then_plan(coordinator, build)

        diag = coordinator._effective_discharge_floor_diag
        assert diag is not None
        assert diag["refill_type"] == "solar_surplus"
        assert coordinator._effective_discharge_floor_pct == pytest.approx(
            _SOLAR_BRIDGE_FLOOR_PCT
        )

    @pytest.mark.asyncio
    async def test_reused_plan_keeps_the_floor_in_force(self, tmp_path: Path) -> None:
        """Between replans no solve runs and the margin sees the floor in force."""
        coordinator, build, solved = _coordinator(
            tmp_path,
            reference=_plan(grid_charge=True),
            committed=_plan(grid_charge=False),
        )
        coordinator._effective_discharge_floor_pct = 42.0
        coordinator._effective_discharge_floor_diag = {"refill_type": "kept"}
        correct_margin = MagicMock()

        with (
            patch.object(coordinator, "_should_replan", return_value=False),
            patch.object(coordinator._dynamic_floor, "correct_margin", correct_margin),
        ):
            await _run_collect_then_plan(coordinator, build)

        assert solved == []
        build.assert_not_called()
        assert coordinator._effective_discharge_floor_pct == pytest.approx(42.0)
        assert coordinator._effective_discharge_floor_diag == {"refill_type": "kept"}
        correct_margin.assert_called_once_with(_LIVE_SOC_PCT, 42.0, now=_NOW)

    @pytest.mark.asyncio
    async def test_enabled_floor_without_a_value_forces_a_replan(
        self, tmp_path: Path
    ) -> None:
        """Switching the floor on mid-plan solves at once instead of waiting."""
        coordinator, build, solved = _coordinator(
            tmp_path,
            reference=_plan(grid_charge=True),
            committed=_plan(grid_charge=False),
        )

        with patch.object(coordinator, "_should_replan", return_value=False):
            await _run_collect_then_plan(coordinator, build)

        assert len(solved) == 2
        assert coordinator._effective_discharge_floor_pct == pytest.approx(
            _HARDWARE_FLOOR_PCT
        )

    @pytest.mark.asyncio
    async def test_disabled_floor_stays_a_no_op(self, tmp_path: Path) -> None:
        """The opt-in switch off: one solve, no floor, no margin learning."""
        coordinator, build, solved = _coordinator(
            tmp_path, reference=_plan(grid_charge=True), floor_enabled=False
        )
        coordinator._effective_discharge_floor_pct = 42.0
        compute_floor = MagicMock()
        correct_margin = MagicMock()

        with (
            patch.object(coordinator._dynamic_floor, "compute_floor", compute_floor),
            patch.object(coordinator._dynamic_floor, "correct_margin", correct_margin),
        ):
            await _run_collect_then_plan(coordinator, build)

        compute_floor.assert_not_called()
        correct_margin.assert_not_called()
        assert [i.dynamic_discharge_floor_pct for i in solved] == [None]
        assert coordinator._effective_discharge_floor_pct is None
        assert coordinator._effective_discharge_floor_diag is None


class TestBuildBridgeSlots:
    """Unit coverage for the reference-plan overlay."""

    @staticmethod
    def _rec(start: datetime, house: float, pv: float) -> HourlyRecommendation:
        rec = MagicMock(spec=HourlyRecommendation)
        rec.start = start
        rec.end = start + _SLOT
        rec.avg_house_consumption_kwh = house
        rec.solcast_pv_estimate_kwh = pv
        rec.batteries_charged_kwh = 0.0
        rec.recommendation = None
        return rec

    def test_load_pv_and_charge_come_from_the_plan_slot(self) -> None:
        """Net load is the plan slot's per-slot house load minus its PV.

        The regenerated recommendation still holds the unscaled hourly
        Solcast value (0.6 kWh/h here), which is not comparable with a
        15-minute load (issue #1187).
        """
        recs = [self._rec(_CHARGE_START, 0.25, 0.6)]
        plan = PlannerOutput(
            slots=[
                PlannedSlot(
                    start=_CHARGE_START,
                    end=_CHARGE_START + _SLOT,
                    recommendation=_CHARGE,
                    price=SlotPrice(0.45, 0.2),
                    avg_house_consumption_kwh=0.25,
                    solcast_pv_estimate_kwh=0.15,
                    batteries_charged_kwh=1.5,
                    estimated_net_consumption_kwh=9.9,  # includes EV — not used
                )
            ]
        )

        (slot,) = build_dynamic_floor_bridge_slots(recs, plan)

        assert slot.estimated_net_consumption_kwh == pytest.approx(0.10)
        assert slot.batteries_charged_kwh == pytest.approx(1.5)
        assert slot.recommendation == _CHARGE
        # The price is the plan's too (issue #1156), not the regenerated 0.0.
        assert slot.import_price == pytest.approx(0.45)

    def test_matches_slots_across_timezones(self) -> None:
        """The same instant in another tzinfo still matches (UTC keyed)."""
        recs = [self._rec(_CHARGE_START, 0.2, 0.0)]
        utc_start = _CHARGE_START.astimezone(UTC)
        plan = PlannerOutput(
            slots=[
                PlannedSlot(
                    start=utc_start,
                    end=utc_start + _SLOT,
                    recommendation=_CHARGE,
                    batteries_charged_kwh=1.0,
                )
            ]
        )

        (slot,) = build_dynamic_floor_bridge_slots(recs, plan)

        assert slot.recommendation == _CHARGE
        assert slot.start == _CHARGE_START

    def test_slot_outside_the_plan_keeps_regenerated_values(self) -> None:
        """A slot the reference plan does not cover falls back per slot."""
        recs = [self._rec(_CHARGE_END + timedelta(hours=40), 0.2, 0.0)]

        (slot,) = build_dynamic_floor_bridge_slots(recs, _plan(grid_charge=True))

        assert slot.batteries_charged_kwh == pytest.approx(0.0)
        assert slot.recommendation is None
        assert math.isnan(slot.import_price)

    @pytest.mark.parametrize("interval_minutes", [15, 30, 60])
    def test_unplanned_slot_scales_the_hourly_pv_to_the_slot(
        self, interval_minutes: int
    ) -> None:
        """Without a plan slot the raw hourly PV is split over the slot (#1187).

        One hour with 1.0 kWh of load and 0.6 kWh of PV has a deficit in every
        slot.  The populator stores the hourly PV on each sub-hourly slot, so
        an unscaled subtraction reported a surplus at 15 and 30 minutes.
        """
        hour_start = _CHARGE_START
        with patch.object(coordinator_builder, "hsem_now", return_value=_NOW):
            recs = [
                rec
                for rec in coordinator_builder.generate_recommendation_intervals(
                    interval_minutes, 48
                )
                if hour_start <= rec.start < hour_start + timedelta(hours=1)
            ]
        assert len(recs) == 60 // interval_minutes
        for rec in recs:
            rec.avg_house_consumption_kwh = 1.0 * interval_minutes / 60
        matched = _populate_from_attributes(
            {
                "detailedHourly": [
                    {"period_start": hour_start.isoformat(), "pv_estimate": 0.6}
                ]
            },
            recs,
            "solcast_pv_estimate_kwh",
            "pv_estimate",
            60,
        )
        assert matched == len(recs)

        bridge = build_dynamic_floor_bridge_slots(recs, None)

        for slot in bridge:
            assert slot.estimated_net_consumption_kwh == pytest.approx(
                0.4 * interval_minutes / 60
            )

    def test_no_plan_keeps_every_regenerated_value(self) -> None:
        """Without a plan the scan sees no charge and logs why."""
        recs = [self._rec(_CHARGE_START, 0.2, 0.0)]
        log = MagicMock()

        with patch("custom_components.hsem.coordinator_dynamic_floor.async_log", log):
            (slot,) = build_dynamic_floor_bridge_slots(recs, None)

        assert slot.batteries_charged_kwh == pytest.approx(0.0)
        assert any("No reference plan" in call.args[1] for call in log.call_args_list)


# ---------------------------------------------------------------------------
# Real planner: both solves through run_planner (hourly slots, 48 h horizon)
# ---------------------------------------------------------------------------

_HOURLY_LOAD = [0.5] * 6 + [0.7] * 3 + [0.5] * 8 + [0.8] * 5 + [0.7] * 2
_PV_TOMORROW = [0.0] * 7 + [0.2, 0.8, 1.6, 2.4, 2.8, 3.0, 2.8, 2.3, 1.6, 0.8, 0.2]
_PV_TOMORROW += [0.0] * (24 - len(_PV_TOMORROW))
# Half the PV: tomorrow's surplus no longer refills the battery on its own.
_CLOUDY = 0.5


def _price_points(night: float) -> list[PricePoint]:
    """Evening 0.19, a 02:00-06:00 night window, 0.25 morning/evening peaks."""
    today = [0.12] * 6 + [0.22] * 3 + [0.12] * 8 + [0.21] * 4 + [0.19] * 3
    tomorrow = (
        [night + 0.04] * 2 + [night] * 4 + [0.25] * 3 + [0.12] * 7 + [0.25] * 5
    ) + [0.15] * 3
    return [
        PricePoint(
            hour=hour,
            import_price=price,
            export_price=max(price - 0.10, 0.0),
            day_offset=day,
        )
        for day, series in enumerate((today, tomorrow))
        for hour, price in enumerate(series)
    ]


def _planner_input(
    night: float, pv_scale: float = 1.0, interval_minutes: int = 60
) -> PlannerInput:
    """Return the #1125 shape: 68 % at 21:30, cheap-or-not night, PV at 09:00."""
    return PlannerInput(
        now_iso=_NOW.isoformat(),
        interval_minutes=interval_minutes,
        interval_length_hours=48,
        battery_soc_pct=_LIVE_SOC_PCT,
        battery_rated_capacity_kwh=_RATED_WH / 1000.0,
        battery_end_of_discharge_soc_pct=_HARDWARE_FLOOR_PCT,
        battery_max_charge_power_w=5000.0,
        battery_max_discharge_power_w=5000.0,
        battery_purchase_price=3000.0,
        battery_expected_cycles=6000,
        weight_1d=25,
        weight_3d=30,
        weight_7d=30,
        weight_14d=15,
        consumption_averages=[
            HourlyConsumptionAverage(hour=h, avg_1d=v, avg_3d=v, avg_7d=v, avg_14d=v)
            for h, v in enumerate(_HOURLY_LOAD)
        ],
        price_points=_price_points(night),
        solcast_slots=[
            SolcastSlot(hour=h, pv_estimate=v * pv_scale, day_offset=1)
            for h, v in enumerate(_PV_TOMORROW)
        ]
        + [SolcastSlot(hour=h, pv_estimate=0.0) for h in range(24)],
        months_winter=[1, 2, 3, 4, 10, 11, 12],
        time_discount_rate=1.0,
    )


def _hourly_recommendations(
    pv_scale: float = 1.0, interval_minutes: int = 60
) -> list[HourlyRecommendation]:
    """Return this cycle's regenerated slots with the same forecast.

    As the populators leave them: the house load is per slot, the PV is the
    unscaled hourly Solcast value on every slot of that hour.
    """
    with patch.object(coordinator_builder, "hsem_now", return_value=_NOW):
        recs = coordinator_builder.generate_recommendation_intervals(
            interval_minutes, 48
        )
    for rec in recs:
        rec.avg_house_consumption_kwh = (
            _HOURLY_LOAD[rec.start.hour] * interval_minutes / 60
        )
        tomorrow = rec.start.date() > _NOW.date()
        rec.solcast_pv_estimate_kwh = (
            _PV_TOMORROW[rec.start.hour] * pv_scale if tomorrow else 0.0
        )
    return recs


def _evening_discharge_kwh(output: PlannerOutput) -> float:
    """Return the battery discharge planned from 21:00 to 02:00."""
    evening_end = _MIDNIGHT + timedelta(days=1, hours=2)
    return sum(
        s.batteries_discharged_kwh
        for s in output.slots
        if _NOW - timedelta(minutes=30) <= s.start < evening_end
    )


def _replan(
    night: float, pv_scale: float = 1.0, interval_minutes: int = 60
) -> tuple[float, dict, PlannerOutput, PlannerOutput]:
    """Run one replan the way the coordinator does: reference, floor, final."""
    planner_input = _planner_input(night, pv_scale, interval_minutes)
    reference = run_planner(planner_input)
    floor_pct, diag, profile = compute_dynamic_floor_from_plan(
        DynamicDischargeFloor(),
        _hourly_recommendations(pv_scale, interval_minutes),
        reference,
        planner_input,
        _live(),
        _NOW,
    )
    final = run_planner(
        replace(
            planner_input,
            dynamic_discharge_floor_pct=floor_pct,
            dynamic_floor_profile=profile,
        )
    )
    return floor_pct, diag, reference, final


class TestRealPlanner:
    """End-to-end on the real planner, for both night-price regimes."""

    def test_cheap_night_releases_the_floor_on_the_first_replan(self) -> None:
        """#1125: a 0.03 night lets the battery serve the evening at once.

        Tomorrow is cloudy, so the reference plan needs the cheap night to
        refill the battery.  With full PV it does not: see
        ``test_cheap_night_with_a_pv_refill_releases_the_floor``.
        """
        floor_pct, diag, reference, final = _replan(night=0.03, pv_scale=_CLOUDY)

        assert diag["refill_type"] == "grid_charge"
        assert floor_pct == pytest.approx(_HARDWARE_FLOOR_PCT)
        # Same plan as with the floor off: the evening is served from the
        # battery and refilled in the cheap window.
        assert _evening_discharge_kwh(final) > 3.0
        assert _evening_discharge_kwh(final) == pytest.approx(
            _evening_discharge_kwh(reference)
        )

    def test_cheap_night_with_a_pv_refill_releases_the_floor(self) -> None:
        """#1156: the cheap night releases the floor even without a night buy.

        The reference plan serves the evening from the battery and lets PV
        refill it: buying at 0.03 would only displace PV that is then
        exported, at the same end SoC (#1138).  The 0.03 night is still the
        look-ahead's cheapest price, so it is an affordable refill: the
        bridge ends there, and the final plan equals the floor-free one.
        """
        floor_pct, diag, reference, final = _replan(night=0.03)

        night_grid_kwh = sum(
            s.batteries_charged_kwh
            for s in reference.slots
            if s.recommendation == _CHARGE and s.start < _PV_FIRST_SURPLUS
        )
        assert night_grid_kwh == pytest.approx(0.0, abs=1e-3)
        assert diag["refill_type"] == "grid_available"
        assert diag["next_refill_slot"] == _CHARGE_START.isoformat()
        assert floor_pct == pytest.approx(_HARDWARE_FLOOR_PCT)
        assert _evening_discharge_kwh(final) > 3.0
        assert _evening_discharge_kwh(final) == pytest.approx(
            _evening_discharge_kwh(reference)
        )
        assert final.plan_cost is not None
        assert reference.plan_cost is not None
        assert final.plan_cost.total_cost == pytest.approx(
            reference.plan_cost.total_cost, abs=0.01
        )
        # Deterministic per replan: the same inputs give the same floor.
        assert _replan(night=0.03)[0] == pytest.approx(floor_pct)

    def test_quarter_hour_bridge_ends_at_the_plans_first_surplus(self) -> None:
        """#1187: at 15-minute slots the bridge ends where the plan has surplus.

        Tomorrow's 07:00 hour has 0.2 kWh of PV against 0.7 kWh of load: a
        deficit.  Compared per slot against the unscaled hourly PV it looked
        like a surplus (0.2 > 0.175), so the bridge ended an hour early.
        """
        floor_pct, diag, reference, _final = _replan(night=0.15, interval_minutes=15)

        first_surplus = next(
            s.start
            for s in reference.slots
            if s.end > _NOW
            and s.avg_house_consumption_kwh - s.solcast_pv_estimate_kwh < -1e-9
        )
        assert first_surplus == _MIDNIGHT + timedelta(days=1, hours=8)
        assert diag["refill_type"] == "solar_surplus"
        assert diag["next_refill_slot"] == first_surplus.isoformat()
        assert floor_pct > _LIVE_SOC_PCT

    def test_quarter_hour_cheap_night_still_releases_the_floor(self) -> None:
        """#1187 does not undo #1156: a cheap night releases at 15 minutes too."""
        floor_pct, diag, reference, final = _replan(night=0.03, interval_minutes=15)

        assert diag["refill_type"] == "grid_available"
        assert floor_pct == pytest.approx(_HARDWARE_FLOOR_PCT)
        assert final.plan_cost is not None
        assert reference.plan_cost is not None
        assert final.plan_cost.total_cost == pytest.approx(
            reference.plan_cost.total_cost, abs=0.01
        )

    def test_moderate_night_keeps_the_solar_bridge_and_is_stable(self) -> None:
        """A 0.15 night is not refilled from the grid: pre-#1140 floor, no flip."""
        first = _replan(night=0.15)
        second = _replan(night=0.15)

        floor_pct, diag, _reference, final = first
        # Tomorrow's 0.12 day is cheaper, so the 0.15 night is no cheap refill.
        assert diag["cheap_refill_price"] < 0.15
        assert diag["refill_type"] == "solar_surplus"
        assert floor_pct > _LIVE_SOC_PCT
        # The reserve is above the battery.  The 0.19 evening is dearer than
        # the 0.15 night, so the battery serves the live slot and takes the
        # shortfall in the night (issue #1222) instead of holding now.
        live_slot = next(s for s in final.slots if s.start <= _NOW < s.end)
        assert live_slot.batteries_discharged_kwh > 0.3
        # It follows its reserve (issue #1188) instead of holding until
        # morning, and never ends a slot below it.
        assert _evening_discharge_kwh(final) > 2.0
        for slot in final.slots:
            if slot.end > _NOW:
                assert (
                    slot.estimated_battery_capacity_kwh
                    >= slot.discharge_reserve_kwh - 1e-3
                )
        # Deterministic per replan: the committed plan never feeds back.
        assert second[0] == pytest.approx(floor_pct)
        assert _evening_discharge_kwh(second[3]) == pytest.approx(
            _evening_discharge_kwh(final)
        )


# ---------------------------------------------------------------------------
# The reference solve keeps the house-battery target (issue #1186)
# ---------------------------------------------------------------------------


def _floor_with_battery_target(
    *, enabled: bool, target_time: str
) -> tuple[float, dict, PlannerOutput]:
    """Return the floor a reference solve gives with the target on or off.

    The #1125 fixture with a 0.45 export price at 21:00-23:00 and the house
    battery target at 100 % by *target_time*.  Floor-free, the spike empties
    the battery and the plan buys back at night.
    """
    spike = 0.45
    planner_input = replace(
        _planner_input(night=0.15),
        excess_export_enabled=True,
        battery_target_soc_enabled=enabled,
        battery_target_soc_pct=100.0,
        battery_target_soc_time=target_time,
    )
    planner_input.price_points = [
        PricePoint(
            hour=point.hour,
            import_price=max(point.import_price, spike + 0.02),
            export_price=spike,
            day_offset=point.day_offset,
        )
        if point.day_offset == 0 and point.hour in (21, 22)
        else point
        for point in planner_input.price_points
    ]
    reference = run_planner(planner_input)
    floor_pct, diag, _profile = compute_dynamic_floor_from_plan(
        DynamicDischargeFloor(),
        _hourly_recommendations(),
        reference,
        planner_input,
        _live(),
        _NOW,
    )
    return floor_pct, diag, reference


def _night_grid_charge_kwh(reference: PlannerOutput) -> float:
    """Return the grid charge the plan makes before tomorrow's PV surplus."""
    return sum(
        slot.batteries_charged_kwh
        for slot in reference.slots
        if slot.end > _NOW
        and slot.start < _PV_FIRST_SURPLUS
        and slot.recommendation == _CHARGE
    )


class TestReferenceSolveKeepsTheBatteryTarget:
    """The house-battery target does not change the floor of this fixture.

    Issue #1186 proposed solving the reference plan with the target off.  At
    the time the target's stage 2 could keep battery energy that stage 1 sold
    before the deadline; the plan then bought less afterwards, a night charge
    the bridge scan credited disappeared, and the floor moved by 33 points.
    Since #1203 stage 2 may only hold back PV, so that case is gone: with no
    PV before the deadline the target changes neither the plan nor the floor.
    That is what lets the reference solve drop the target when no EV is in
    the plan (issue #1207, ``tests/test_dynamic_floor_reference_target.py``).
    """

    def test_target_without_pv_before_the_deadline_changes_nothing(self) -> None:
        """Issue #1203: target by 23:00, no PV until 08:00, 0.45 export at 21-23.

        Stage 1 sells the battery into the spike and buys back at night.  The
        target must not cancel that sale: it is funded only by PV the normal
        plan would otherwise export, and there is none.
        """
        with_target, diag, reference = _floor_with_battery_target(
            enabled=True, target_time="23:00:00"
        )
        without_target, diag_off, stage1_only = _floor_with_battery_target(
            enabled=False, target_time="23:00:00"
        )

        report = reference.battery_target
        assert report is not None
        assert report["stage2_ran"] is True
        assert report["stage2_status"] == "no_gain"
        assert report["stage1_projected_kwh"] == pytest.approx(0.0, abs=1e-3)
        assert report["projected_kwh"] == pytest.approx(0.0, abs=1e-3)
        assert report["max_import_delta_kwh"] == pytest.approx(0.0)
        # The spike is sold exactly as without the target …
        spike = [
            slot
            for slot in stage1_only.slots
            if slot.end > _NOW and slot.start.day == _NOW.day
        ]
        assert sum(slot.primary_battery_export_kwh for slot in spike) > 4.0
        assert reference.slots == stage1_only.slots
        # … the plan costs the same …
        assert reference.plan_cost is not None and stage1_only.plan_cost is not None
        assert reference.plan_cost.total_cost == pytest.approx(
            stage1_only.plan_cost.total_cost
        )
        # … and the night charge the scan credits is still there.
        assert _night_grid_charge_kwh(reference) > 2.0
        assert diag == diag_off
        assert with_target == pytest.approx(without_target)

    def test_floor_is_the_same_when_the_charge_lies_before_the_target(self) -> None:
        """Target by 06:00: the night charge is inside the pinned window."""
        with_target, _diag, reference = _floor_with_battery_target(
            enabled=True, target_time="06:00:00"
        )
        without_target, _diag, stage1_only = _floor_with_battery_target(
            enabled=False, target_time="06:00:00"
        )

        assert reference.battery_target is not None
        assert reference.battery_target["stage2_ran"] is True
        assert _night_grid_charge_kwh(reference) == pytest.approx(
            _night_grid_charge_kwh(stage1_only), abs=1e-3
        )
        assert with_target == pytest.approx(without_target)
