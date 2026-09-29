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

The coordinator tests go through the real regeneration in
``_async_collect_and_populate`` and the real ``compute_floor()``; only the HA
entity reads, the consumption/PV population (a deterministic profile), and
the planner executor job are faked.  ``TestRealPlanner`` runs the real
``run_planner`` for both solves.
"""

from __future__ import annotations

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
# 21:30 → 02:00 is 18 slots × 0.15 kWh = 2.7 kWh; the cumulative charge
# (1.0, 2.0, 3.0 kWh) first covers it in the 02:30 slot.
_EXPECTED_REFILL = datetime(2026, 9, 29, 2, 30, tzinfo=_TZ)
# 21:30 → 09:30 is 12 h of 0.6 kW = 7.2 kWh; × 1.15 margin / 9.5 kWh.
_SOLAR_BRIDGE_FLOOR_PCT = 7.2 / _USABLE_KWH * 100.0 * 1.15

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


def _plan(*, grid_charge: bool) -> PlannerOutput:
    """Return a 48 h plan: grid charge 02:00-02:45 (or none), wait otherwise."""
    slots: list[PlannedSlot] = []
    start = _MIDNIGHT
    while start < _MIDNIGHT + timedelta(hours=48):
        charging = grid_charge and _CHARGE_START <= start < _CHARGE_END
        slots.append(
            PlannedSlot(
                start=start,
                end=start + _SLOT,
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
        """At 21:30 the 02:00 grid charge is the refill, so the floor is released."""
        coordinator, build, solved = _coordinator(
            tmp_path, reference=_plan(grid_charge=True)
        )

        await _run_collect_then_plan(coordinator, build)

        diag = coordinator._effective_discharge_floor_diag
        assert diag is not None
        assert diag["refill_type"] == "grid_charge"
        assert diag["next_refill_slot"] == _EXPECTED_REFILL.isoformat()
        # 4.5 h of 0.6 kW load (2.7 kWh) is covered by the planned charge, so
        # the bridge reserve is zero (documented in planner-spec.md, #1140).
        assert diag["reserve_kwh"] == pytest.approx(0.0)
        assert diag["bridge_duration_hours"] == pytest.approx(5.0)

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

    def test_forecast_comes_from_recommendations_and_charge_from_the_plan(
        self,
    ) -> None:
        """Net load is this cycle's forecast; the charge is the plan's."""
        recs = [self._rec(_CHARGE_START, 0.4, 0.1)]
        plan = PlannerOutput(
            slots=[
                PlannedSlot(
                    start=_CHARGE_START,
                    end=_CHARGE_START + _SLOT,
                    recommendation=_CHARGE,
                    batteries_charged_kwh=1.5,
                    estimated_net_consumption_kwh=9.9,  # includes EV — not used
                )
            ]
        )

        (slot,) = build_dynamic_floor_bridge_slots(recs, plan)

        assert slot.estimated_net_consumption_kwh == pytest.approx(0.3)
        assert slot.batteries_charged_kwh == pytest.approx(1.5)
        assert slot.recommendation == _CHARGE

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


def _planner_input(night: float) -> PlannerInput:
    """Return the #1125 shape: 68 % at 21:30, cheap-or-not night, PV at 09:00."""
    return PlannerInput(
        now_iso=_NOW.isoformat(),
        interval_minutes=60,
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
            SolcastSlot(hour=h, pv_estimate=v, day_offset=1)
            for h, v in enumerate(_PV_TOMORROW)
        ]
        + [SolcastSlot(hour=h, pv_estimate=0.0) for h in range(24)],
        months_winter=[1, 2, 3, 4, 10, 11, 12],
        time_discount_rate=1.0,
    )


def _hourly_recommendations() -> list[HourlyRecommendation]:
    """Return this cycle's regenerated hourly slots with the same forecast."""
    with patch.object(coordinator_builder, "hsem_now", return_value=_NOW):
        recs = coordinator_builder.generate_recommendation_intervals(60, 48)
    for rec in recs:
        rec.avg_house_consumption_kwh = _HOURLY_LOAD[rec.start.hour]
        tomorrow = rec.start.date() > _NOW.date()
        rec.solcast_pv_estimate_kwh = _PV_TOMORROW[rec.start.hour] if tomorrow else 0.0
    return recs


def _evening_discharge_kwh(output: PlannerOutput) -> float:
    """Return the battery discharge planned from 21:00 to 02:00."""
    evening_end = _MIDNIGHT + timedelta(days=1, hours=2)
    return sum(
        s.batteries_discharged_kwh
        for s in output.slots
        if _NOW - timedelta(minutes=30) <= s.start < evening_end
    )


def _replan(night: float) -> tuple[float, dict, PlannerOutput, PlannerOutput]:
    """Run one replan the way the coordinator does: reference, floor, final."""
    planner_input = _planner_input(night)
    reference = run_planner(planner_input)
    floor_pct, diag = compute_dynamic_floor_from_plan(
        DynamicDischargeFloor(), _hourly_recommendations(), reference, _live(), _NOW
    )
    final = run_planner(replace(planner_input, dynamic_discharge_floor_pct=floor_pct))
    return floor_pct, diag, reference, final


class TestRealPlanner:
    """End-to-end on the real planner, for both night-price regimes."""

    def test_cheap_night_releases_the_floor_on_the_first_replan(self) -> None:
        """#1125: a 0.03 night lets the battery serve the evening at once."""
        floor_pct, diag, reference, final = _replan(night=0.03)

        assert diag["refill_type"] == "grid_charge"
        assert floor_pct == pytest.approx(_HARDWARE_FLOOR_PCT)
        # Same plan as with the floor off: the evening is served from the
        # battery and refilled in the cheap window.
        assert _evening_discharge_kwh(final) > 3.0
        assert _evening_discharge_kwh(final) == pytest.approx(
            _evening_discharge_kwh(reference)
        )

    def test_moderate_night_keeps_the_solar_bridge_and_is_stable(self) -> None:
        """A 0.15 night is not refilled from the grid: pre-#1140 floor, no flip."""
        first = _replan(night=0.15)
        second = _replan(night=0.15)

        floor_pct, diag, _reference, final = first
        assert diag["refill_type"] == "solar_surplus"
        assert floor_pct > _LIVE_SOC_PCT
        assert _evening_discharge_kwh(final) == pytest.approx(0.0)
        # Deterministic per replan: the committed plan never feeds back.
        assert second[0] == pytest.approx(floor_pct)
