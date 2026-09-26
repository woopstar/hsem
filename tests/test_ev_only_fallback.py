"""Tests for issue #1106 — EV-only smart charging while the load forecast is not ready.

While the house-load forecast is not ready the coordinator skips the planner
and holds the home battery. A managed EV on smart charging now follows a
grid-only fallback plan: cheapest import slots before its deadline, no PV
surplus credited, battery fields untouched.

Covered:

- ``planner/ev_fallback.py`` in isolation: cheapest-first selection, the
  guard states (SoC unknown, at target, smart off, not connected), the
  current-slot command conversion, and the unpriced-tail estimation.
- Full update cycles with mocked OCPP servers: selected versus non-selected
  current slot, both EVs, force-charge precedence, the fuse clamp, the plan
  sensor data, and the battery staying held.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.coordinator import HSEMDataUpdateCoordinator
from custom_components.hsem.coordinator_data import CoordinatorData
from custom_components.hsem.coordinator_load_hold import EV_ONLY_FALLBACK_CONSTRAINT
from custom_components.hsem.planner.ev_fallback import (
    EV_ONLY_FALLBACK_MODE,
    EvFallbackSlot,
    build_ev_only_fallback_plan,
    estimate_unpriced_tail,
)
from custom_components.hsem.planner.ev_planner_models import EVPlannerInput
from custom_components.hsem.utils.datetime_utils import slot_contains
from custom_components.hsem.utils.recommendations import Recommendations
from tests.coordinator_fixtures import make_real_coordinator
from tests.test_ha_mock_integration import (
    _BASE_ENTITY_STATES,
    _patch_all_ha_helpers,
    make_fake_config_entry,
    make_fake_hass,
)

_SLOT = timedelta(minutes=15)
_START = datetime(2026, 9, 26, 20, 0, tzinfo=UTC)
_NOW = _START + timedelta(minutes=5)
_CYCLE_MODULE = "custom_components.hsem.coordinator_cycle"
_WAIT = Recommendations.BatteriesWaitMode.value
_EV = Recommendations.EVSmartCharging.value


# ---------------------------------------------------------------------------
# Pure fallback module
# ---------------------------------------------------------------------------


def _slots(prices: list[float]) -> list[EvFallbackSlot]:
    return [
        EvFallbackSlot(
            start=_START + i * _SLOT, end=_START + (i + 1) * _SLOT, import_price=p
        )
        for i, p in enumerate(prices)
    ]


def _inp(**overrides: Any) -> EVPlannerInput:
    values: dict[str, Any] = {
        "enabled": True,
        "ev_connected": True,
        "smart_charging_enabled": True,
        "current_soc_pct": 50.0,
        "target_soc_pct": 55.0,
        "battery_capacity_kwh": 40.0,
        "charger_power_kw": 11.0,
        "charger_efficiency_pct": 100.0,
        "charger_min_power_w": 1380.0,
        "deadline": _START + 8 * _SLOT,
        "now": _NOW,
    }
    values.update(overrides)
    return EVPlannerInput(**values)


def _selected_starts(result: Any) -> list[datetime]:
    return [s.start for s in result.plan.charging_slots]


class TestFallbackPlan:
    """Cheapest-first, grid-only EV plan with the EV planner's guard states."""

    def test_charges_cheapest_slots_before_the_deadline(self) -> None:
        # 2 kWh needed; 11 kW x 15 min = 2.75 kWh, so one full cheap slot.
        result = build_ev_only_fallback_plan(
            _inp(),
            _slots([3.0, 2.5, 0.5, 2.0, 1.8, 2.2, 2.4, 2.6, 0.1]),
            interval_minutes=15,
            load_forecast_reason="source_unavailable",
        )

        assert _selected_starts(result) == [_START + 2 * _SLOT]
        # The 0.1 slot starts at the deadline, so it is never selected.
        assert result.slot_commands_w[2] == pytest.approx(8000.0)
        assert result.slot_commands_w[0] == pytest.approx(0.0)
        assert all(
            s.solar_surplus_kwh == pytest.approx(0.0)
            for s in result.plan.charging_slots
        )
        assert result.plan.data_quality["mode"] == EV_ONLY_FALLBACK_MODE
        assert result.plan.data_quality["load_forecast"] == "source_unavailable"

    def test_current_slot_command_uses_remaining_time(self) -> None:
        """The current slot is scaled to the 10 minutes left, then capped."""
        result = build_ev_only_fallback_plan(
            _inp(target_soc_pct=60.0),
            _slots([0.1, 3.0, 3.0, 3.0]),
            interval_minutes=15,
            load_forecast_reason="source_unavailable",
        )

        assert result.plan.charging_slots[0].start == _START
        # 1.833 kWh (rounded to 3 dp by the EV planner) over 10 minutes.
        assert result.slot_commands_w[0] == pytest.approx(11000.0, abs=5.0)
        assert result.plan.state == "charging"

    @pytest.mark.parametrize(
        ("overrides", "state"),
        [
            ({"current_soc_pct": None}, "unavailable"),
            ({"current_soc_pct": 80.0, "target_soc_pct": 80.0}, "fully_charged"),
            ({"smart_charging_enabled": False}, "smart_charging_disabled"),
            ({"ev_connected": False}, "not_connected"),
        ],
    )
    def test_guard_states_command_nothing(
        self, overrides: dict[str, Any], state: str
    ) -> None:
        result = build_ev_only_fallback_plan(
            _inp(**overrides),
            _slots([0.1, 0.2, 0.3]),
            interval_minutes=15,
            load_forecast_reason="source_unavailable",
        )

        assert result.plan.state == state
        assert result.plan.charging_slots == []
        assert all(c == pytest.approx(0.0) for c in result.slot_commands_w)


class TestUnpricedTail:
    """Unpublished trailing prices must never look free (issue #1002 rule)."""

    def test_tail_takes_the_same_clock_price_from_an_earlier_day(self) -> None:
        day = [
            EvFallbackSlot(
                start=_START + timedelta(hours=h),
                end=_START + timedelta(hours=h + 1),
                import_price=p,
            )
            for h, p in ((0, 1.5), (1, 2.5), (24, 0.0), (25, 0.0))
        ]

        assert estimate_unpriced_tail(day) == pytest.approx([1.5, 2.5, 1.5, 2.5])

    def test_tail_without_an_earlier_day_takes_the_highest_known_price(self) -> None:
        assert estimate_unpriced_tail(_slots([1.0, 3.0, 0.0])) == pytest.approx(
            [1.0, 3.0, 3.0]
        )

    def test_genuine_zero_inside_the_published_range_is_kept(self) -> None:
        assert estimate_unpriced_tail(_slots([1.0, 0.0, 2.0])) == pytest.approx(
            [1.0, 0.0, 2.0]
        )

    def test_no_prices_at_all_are_left_unchanged(self) -> None:
        assert estimate_unpriced_tail(_slots([0.0, 0.0])) == pytest.approx([0.0, 0.0])

    def test_fallback_never_picks_the_unpriced_tail(self) -> None:
        result = build_ev_only_fallback_plan(
            _inp(deadline=None),
            _slots([2.0, 1.0, 3.0, 0.0, 0.0]),
            interval_minutes=15,
            load_forecast_reason="source_unavailable",
        )

        assert _selected_starts(result) == [_START + _SLOT]


# ---------------------------------------------------------------------------
# Full hold-path cycles
# ---------------------------------------------------------------------------

_PRIMARY_OPTIONS: dict[str, Any] = {
    "hsem_read_only": True,
    "hsem_ocpp_enabled": True,
    "hsem_ev_planned_load_enabled": True,
    "hsem_ev_smart_charging": True,
    "hsem_ev_soc": "sensor.ev_soc",
    "hsem_ev_target_soc": 90,
    "hsem_ev_planned_load_battery_capacity_kwh": 60.0,
    "hsem_ev_planned_load_charger_power_kw": 11.0,
    "hsem_ev_planned_load_charger_phase_topology": "three_phase_balanced",
    "hsem_main_fuse_amps": 25,
    "hsem_main_fuse_phases": 3,
}

_SECOND_OPTIONS: dict[str, Any] = {
    "hsem_ocpp_second_enabled": True,
    "hsem_ev_second_planned_load_enabled": True,
    "hsem_ev_second_smart_charging": True,
    "hsem_ev_second_soc": "sensor.ev2_soc",
    "hsem_ev_second_target_soc": 90,
    "hsem_ev_second_planned_load_battery_capacity_kwh": 60.0,
    "hsem_ev_second_planned_load_charger_power_kw": 11.0,
    "hsem_ev_second_planned_load_charger_phase_topology": "three_phase_balanced",
}

_EV_STATES: dict[str, str | dict] = {"sensor.ev_soc": "40", "sensor.ev2_soc": "40"}


def _server() -> MagicMock:
    server = MagicMock()
    server.charger_sessions = {}
    server.is_listening = True
    server.last_requested_current_a = None
    server.anti_flap_state = "idle"
    server.is_stalled = False
    server.update_charge_target = AsyncMock()
    return server


def _coordinator(
    options: dict[str, Any], states: dict[str, str | dict] | None = None
) -> tuple[HSEMDataUpdateCoordinator, list[CoordinatorData], MagicMock, MagicMock]:
    entry = make_fake_config_entry(options)
    coordinator = make_real_coordinator(
        hass=make_fake_hass({**_BASE_ENTITY_STATES, **_EV_STATES, **(states or {})}),
        config_entry=entry,
    )
    published: list[CoordinatorData] = []
    coordinator.async_set_updated_data = published.append  # type: ignore[method-assign, assignment]  # test monkey-patch
    primary, second = _server(), _server()
    coordinator._ocpp_server = primary
    coordinator._ocpp_second_server = second
    return coordinator, published, primary, second


def _price_current_slot_cheapest(cheap: bool) -> Callable[[list, Any, Any], None]:
    """Return a price populator making the live slot the cheapest (or dearest)."""

    def populate(recs: list, _snapshot: Any, _cfg: Any) -> None:
        now = datetime.now(UTC)
        for rec in recs:
            if slot_contains(rec.start, rec.end, now):
                rec.import_price = 0.10 if cheap else 9.0
            else:
                rec.import_price = 2.0 if cheap else 1.0

    return populate


async def _run_unready_cycle(
    coordinator: HSEMDataUpdateCoordinator, *, cheap_now: bool
) -> None:
    with (
        _patch_all_ha_helpers(),
        patch(
            f"{_CYCLE_MODULE}.populate_avg_house_consumption_from_snapshot",
            return_value=False,
        ),
        patch(
            f"{_CYCLE_MODULE}.populate_price_and_solcast_from_snapshot",
            side_effect=_price_current_slot_cheapest(cheap_now),
        ),
    ):
        await coordinator._async_run_update_cycle()


def _target_kw(server: MagicMock) -> float:
    call = server.update_charge_target.await_args
    assert call is not None
    return float(call.args[1])


class TestHoldPathFallback:
    """A managed EV on smart charging charges grid-only during the hold."""

    @pytest.mark.asyncio
    async def test_cheapest_current_slot_charges_while_battery_stays_held(
        self,
    ) -> None:
        coordinator, published, primary, _ = _coordinator(_PRIMARY_OPTIONS)

        await _run_unready_cycle(coordinator, cheap_now=True)

        data = published[-1]
        current = data.hourly_recommendation
        assert current is not None
        assert data.state == _EV
        assert current.ev_charger_calculated_power > 1380.0
        assert current.ev_charger_calculated_power <= 11040.0 + 1e-6
        assert current.batteries_charged_kwh == pytest.approx(0.0)
        assert current.batteries_discharged_kwh == pytest.approx(0.0)
        assert _target_kw(primary) > 0.0
        assert primary.update_charge_target.await_args.kwargs["managed"] is True
        explanation = data.plan_explanation
        assert explanation.winner_name == "safety_hold"
        assert EV_ONLY_FALLBACK_CONSTRAINT in explanation.constraints
        plan = data.ev_charging_plan
        assert plan is not None
        assert plan.state == "charging"
        assert plan.data_quality["mode"] == EV_ONLY_FALLBACK_MODE
        assert plan.charging_slots
        assert all(
            s.solar_surplus_kwh == pytest.approx(0.0) for s in plan.charging_slots
        )

    @pytest.mark.asyncio
    async def test_dear_current_slot_keeps_the_enforced_zero(self) -> None:
        coordinator, published, primary, _ = _coordinator(_PRIMARY_OPTIONS)

        await _run_unready_cycle(coordinator, cheap_now=False)

        data = published[-1]
        current = data.hourly_recommendation
        assert current is not None
        assert data.state == _WAIT
        assert current.ev_charger_calculated_power == pytest.approx(0.0)
        assert _target_kw(primary) == pytest.approx(0.0)
        assert primary.update_charge_target.await_args.kwargs["managed"] is True
        plan = data.ev_charging_plan
        assert plan is not None
        assert plan.state == "waiting"
        # Future cheap slots are still planned and shown on the sensor.
        assert plan.charging_slots

    @pytest.mark.asyncio
    async def test_unknown_soc_never_charges(self) -> None:
        coordinator, published, primary, _ = _coordinator(
            _PRIMARY_OPTIONS, states={"sensor.ev_soc": "unavailable"}
        )

        await _run_unready_cycle(coordinator, cheap_now=True)

        data = published[-1]
        assert data.hourly_recommendation is not None
        assert data.hourly_recommendation.ev_charger_calculated_power == pytest.approx(
            0.0
        )
        assert _target_kw(primary) == pytest.approx(0.0)
        assert data.ev_charging_plan is not None
        assert data.ev_charging_plan.state == "unavailable"

    @pytest.mark.asyncio
    async def test_at_target_never_charges(self) -> None:
        coordinator, published, primary, _ = _coordinator(
            _PRIMARY_OPTIONS, states={"sensor.ev_soc": "95"}
        )

        await _run_unready_cycle(coordinator, cheap_now=True)

        assert _target_kw(primary) == pytest.approx(0.0)
        assert published[-1].ev_charging_plan is not None
        assert published[-1].ev_charging_plan.state == "fully_charged"

    @pytest.mark.asyncio
    async def test_smart_charging_off_plans_nothing(self) -> None:
        coordinator, published, primary, _ = _coordinator(
            {**_PRIMARY_OPTIONS, "hsem_ev_smart_charging": False}
        )

        await _run_unready_cycle(coordinator, cheap_now=True)

        assert _target_kw(primary) == pytest.approx(0.0)
        assert primary.update_charge_target.await_args.kwargs["managed"] is False
        assert published[-1].ev_charging_plan is not None
        assert published[-1].ev_charging_plan.state == "smart_charging_disabled"

    @pytest.mark.asyncio
    async def test_feature_off_keeps_the_previous_behaviour(self) -> None:
        coordinator, published, primary, _ = _coordinator(
            {**_PRIMARY_OPTIONS, "hsem_ev_planned_load_enabled": False}
        )

        await _run_unready_cycle(coordinator, cheap_now=True)

        data = published[-1]
        assert data.state == _WAIT
        assert _target_kw(primary) == pytest.approx(0.0)
        assert data.ev_charging_plan is None
        assert EV_ONLY_FALLBACK_CONSTRAINT not in data.plan_explanation.constraints

    @pytest.mark.asyncio
    async def test_force_charge_overrides_a_dear_slot(self) -> None:
        coordinator, published, primary, _ = _coordinator(
            {**_PRIMARY_OPTIONS, "hsem_ev_force_charge_now": True}
        )

        await _run_unready_cycle(coordinator, cheap_now=False)

        current = published[-1].hourly_recommendation
        assert current is not None
        assert current.ev_charger_calculated_power == pytest.approx(10350.0)
        assert _target_kw(primary) == pytest.approx(10.35)
        assert current.batteries_charged_kwh == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_both_evs_share_the_fuse_budget(self) -> None:
        # 25 A x 3 x 230 V = 17.25 kW, minus 1.2 kW live house load.
        coordinator, published, primary, second = _coordinator(
            {**_PRIMARY_OPTIONS, **_SECOND_OPTIONS}
        )

        await _run_unready_cycle(coordinator, cheap_now=True)

        current = published[-1].hourly_recommendation
        assert current is not None
        primary_w = current.ev_charger_calculated_power
        second_w = current.ev_second_charger_calculated_power
        assert primary_w > 0.0
        assert second_w > 0.0
        assert primary_w + second_w <= 17250.0 - 1200.0 + 1e-6
        assert _target_kw(primary) > 0.0
        assert _target_kw(second) > 0.0
        assert published[-1].ev_second_charging_plan is not None
        assert (
            published[-1].ev_second_charging_plan.data_quality["mode"]
            == EV_ONLY_FALLBACK_MODE
        )
        assert current.batteries_charged_kwh == pytest.approx(0.0)
        assert current.batteries_discharged_kwh == pytest.approx(0.0)
