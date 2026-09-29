"""Regression tests for issue #1140 — the dynamic floor never saw a planned charge.

Every coordinator cycle regenerates ``_hourly_recommendations`` from scratch
(``batteries_charged_kwh = 0.0``, ``recommendation = None``) before the
planner phase builds the dynamic floor's bridge slots.  The plan is only
written back afterwards, so ``compute_floor()``'s grid-charge refill branch
never fired: the bridge always ran to the next PV surplus.  After sunset that
is the whole night's house load, the floor exceeded the live SoC, and the
#1094 live-SoC cap then pinned the model at 0 kWh above the origin — the
battery sat in ``batteries_wait_mode`` all evening (issue #1125).

These tests go through the real regeneration in
``_async_collect_and_populate`` and the real ``compute_floor()``.  Only the
HA entity reads, the consumption/PV population (replaced by a deterministic
profile), and the MILP executor job are faked.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.coordinator import HSEMDataUpdateCoordinator
from custom_components.hsem.coordinator_dynamic_floor import (
    build_dynamic_floor_bridge_slots,
)
from custom_components.hsem.custom_sensors.hourly_data_populator.consumption import (
    ConsumptionPopulation,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.planner_output import PlannerOutput
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


def _previous_plan() -> PlannerOutput:
    """Return the last committed plan: grid charge 02:00-02:45, wait otherwise."""
    slots: list[PlannedSlot] = []
    start = _MIDNIGHT
    while start < _MIDNIGHT + timedelta(hours=48):
        charging = _CHARGE_START <= start < _CHARGE_END
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
    previous_plan: PlannerOutput | None,
    *,
    floor_enabled: bool = True,
) -> tuple[HSEMDataUpdateCoordinator, MagicMock]:
    """Return a real coordinator, with the dynamic floor enabled by default."""
    config_entry = make_fake_config_entry(
        {
            "hsem_dynamic_discharge_floor": floor_enabled,
            "hsem_recommendation_interval_minutes": 15,
            "hsem_recommendation_interval_length": 48,
        }
    )
    hass = MagicMock()
    hass.config.config_dir = str(tmp_path)
    hass.async_add_executor_job = AsyncMock(return_value=_previous_plan())
    coordinator = make_real_coordinator(hass=hass, config_entry=config_entry)
    coordinator._last_planner_output = previous_plan
    coordinator._set_update_interval = AsyncMock()  # type: ignore[method-assign]
    build = MagicMock(return_value=PlannerInput())
    return coordinator, build


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


class TestFloorReadsCommittedPlan:
    """The bridge scan sees the grid charge in the last committed plan."""

    @pytest.mark.asyncio
    async def test_planned_overnight_charge_is_the_refill(self, tmp_path: Path) -> None:
        """At 21:30 the 02:00 grid charge is the refill, so the floor is released."""
        coordinator, build = _coordinator(tmp_path, _previous_plan())

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
        assert floor_pct is not None
        assert floor_pct == pytest.approx(_HARDWARE_FLOOR_PCT)
        assert build.call_args.kwargs["dynamic_discharge_floor_pct"] == (
            pytest.approx(_HARDWARE_FLOOR_PCT)
        )
        # Not pinned at the live SoC: the model has the full 68 % → 5 % to
        # spend on evening load, so batteries_wait_mode is no longer forced.
        origin = _model_origin_pct(floor_pct)
        assert origin == pytest.approx(_HARDWARE_FLOOR_PCT)
        assert origin < _LIVE_SOC_PCT - 1e-9
        dischargeable_kwh = (_LIVE_SOC_PCT - origin) / 100.0 * _RATED_WH / 1000.0
        assert dischargeable_kwh == pytest.approx(6.3)

    @pytest.mark.asyncio
    async def test_no_committed_plan_falls_back_to_the_solar_bridge(
        self, tmp_path: Path
    ) -> None:
        """First cycle after start-up: pre-#1140 behaviour, pinned at live SoC."""
        coordinator, build = _coordinator(tmp_path, None)
        log = MagicMock()

        with patch("custom_components.hsem.coordinator_dynamic_floor.async_log", log):
            await _run_collect_then_plan(coordinator, build)

        diag = coordinator._effective_discharge_floor_diag
        assert diag is not None
        assert diag["refill_type"] == "solar_surplus"
        assert diag["next_refill_slot"] == _PV_FIRST_SURPLUS.isoformat()
        # 21:30 → 09:30 is 12 h of 0.6 kW = 7.2 kWh × 1.15 margin / 9.5 kWh.
        assert diag["reserve_kwh"] == pytest.approx(7.2)
        floor_pct = coordinator._effective_discharge_floor_pct
        assert floor_pct is not None
        assert floor_pct == pytest.approx(7.2 / _USABLE_KWH * 100.0 * 1.15)
        assert floor_pct > _LIVE_SOC_PCT
        assert _model_origin_pct(floor_pct) == pytest.approx(_LIVE_SOC_PCT)
        assert any(
            "No committed plan yet" in call.args[1] for call in log.call_args_list
        )

    @pytest.mark.asyncio
    async def test_disabled_floor_stays_a_no_op(self, tmp_path: Path) -> None:
        """The opt-in switch off: no floor, whatever the committed plan holds."""
        coordinator, build = _coordinator(
            tmp_path, _previous_plan(), floor_enabled=False
        )
        compute_floor = MagicMock()

        with patch.object(coordinator._dynamic_floor, "compute_floor", compute_floor):
            await _run_collect_then_plan(coordinator, build)

        compute_floor.assert_not_called()
        assert coordinator._effective_discharge_floor_pct is None
        assert coordinator._effective_discharge_floor_diag is None
        assert build.call_args.kwargs["dynamic_discharge_floor_pct"] is None


class TestBuildBridgeSlots:
    """Unit coverage for the committed-plan overlay."""

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
                    estimated_net_consumption_kwh=9.9,  # stale — must not be used
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
        """A slot the committed plan does not cover falls back per slot."""
        recs = [self._rec(_CHARGE_END + timedelta(hours=40), 0.2, 0.0)]

        (slot,) = build_dynamic_floor_bridge_slots(recs, _previous_plan())

        assert slot.batteries_charged_kwh == pytest.approx(0.0)
        assert slot.recommendation is None
