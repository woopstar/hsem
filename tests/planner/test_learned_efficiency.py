"""The planner planning with the measured round trip (issue #1431, stage 2).

The coordinator resolves one charge and one discharge efficiency per run and
hands them to the planner in the two fields it always had.  These tests pin
what the spec promises:

- with the option off, plan and cost are those of ``origin/main``;
- with it on and a trusted measurement, the MILP, the SoC walk and the final
  scoring all use the measured values, and every invariant holds;
- the plan, the explanation and the efficiency sensor say which values were
  used.
"""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from custom_components.hsem.const import DEFAULT_CONFIG_VALUES
from custom_components.hsem.coordinator_builder import build_planner_input
from custom_components.hsem.coordinator_data import CoordinatorData
from custom_components.hsem.coordinator_efficiency import async_track_battery_efficiency
from custom_components.hsem.coordinator_state import CoordinatorSharedState
from custom_components.hsem.custom_sensors.battery_efficiency_sensor import (
    HSEMBatteryEfficiencySensor,
)
from custom_components.hsem.custom_sensors.config_reader import build_sensor_config
from custom_components.hsem.flows.battery_economics import (
    get_battery_economics_step_schema,
    validate_battery_economics_input,
)
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.planner.engine_core import run_planner
from custom_components.hsem.planner.milp_optimizer import is_scipy_available
from custom_components.hsem.utils.efficiency_learner import EfficiencyLearner
from custom_components.hsem.utils.planning_efficiency import (
    MIN_SAMPLES,
    REASON_DISABLED,
    REASON_LEARNED,
    REASON_LEARNING,
    SOURCE_CONFIGURED,
    SOURCE_LEARNED,
)
from tests.backtest.conftest import corpus_paths
from tests.backtest.invariants import check_invariants, format_violations
from tests.backtest.replay import load_planner_input

_OPTION = "hsem_batteries_learned_efficiency_enabled"
_NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
_CYCLE = "cycle-2026-09-24-1016"
#: The round trip the cycles below measure: 95 % each way.
_ROUND_TRIP = 0.95 * 0.95
_ONE_WAY_PCT = 95.0

_needs_scipy = pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available"
)

_GOLDEN = json.loads(
    (
        Path(__file__).parent.parent / "fixtures" / "learned_efficiency_off_golden.json"
    ).read_text()
)


def _recorded(name: str = _CYCLE) -> PlannerInput:
    """Return a recorded production cycle."""
    path = next(p for p in corpus_paths() if p.stem == name)
    recorded, _report = load_planner_input(path)
    return recorded


def _coordinator(tmp_path: Path) -> CoordinatorSharedState:
    """Return the slice of the coordinator the efficiency tracking uses."""
    return cast(
        CoordinatorSharedState,
        SimpleNamespace(
            hass=SimpleNamespace(config=SimpleNamespace(config_dir=str(tmp_path))),
            _efficiency_learner=EfficiencyLearner(),
            last_update_success=True,
            data=CoordinatorData(cfg=SensorConfig()),
        ),
    )


async def _learn(
    coordinator: CoordinatorSharedState, *, samples: int = MIN_SAMPLES
) -> None:
    """Measure 95 % each way through the real cycle hook.

    A 10 kWh battery moves 1 % SoC per cycle.  Charging at 1500 W for 252.6 s
    measures 0.1053 kWh for 0.1 kWh stored; discharging at 2000 W for 171 s
    measures 0.095 kWh for 0.1 kWh removed.  The charge ends 12 hours before
    ``_NOW`` and the discharge at ``_NOW``.  The SoC is a plain ramp, as in
    ``tests/test_coordinator_efficiency.py``: the learner reads its changes.
    """
    runs = (
        (1500.0, 0.1 / 0.95 / 1.5 * 3600.0, _NOW - timedelta(hours=12)),
        (-2000.0, 0.1 * 0.95 / 2.0 * 3600.0, _NOW),
    )
    cycles = 5 * samples + 2
    for power_w, step_s, end in runs:
        sign = 1.0 if power_w > 0 else -1.0
        for index in range(cycles):
            live = LiveState()
            live.huawei_batteries_charge_discharge_power_w = power_w
            live.huawei_batteries_soc_pct = 500.0 + sign * index
            live.huawei_batteries_rated_capacity_wh = 10_000.0
            await async_track_battery_efficiency(
                coordinator,
                end - timedelta(seconds=step_s * (cycles - 1 - index)),
                live,
                SensorConfig(),
            )


def _build(cfg: SensorConfig, learner: EfficiencyLearner | None) -> PlannerInput:
    """Build a planner input at ``_NOW`` the way the coordinator does."""
    with patch("homeassistant.util.dt.now", return_value=_NOW):
        return build_planner_input(
            cfg=cfg,
            live=LiveState(),
            hourly_recommendations=[],
            previous_winner_name=None,
            previous_winner_score=0.0,
            efficiency_learner=learner,
        )


def _enabled() -> SensorConfig:
    """Return a configuration with the option on."""
    cfg = SensorConfig()
    cfg.batteries_learned_efficiency_enabled = True
    return cfg


def _with_efficiency(recorded: PlannerInput, built: PlannerInput) -> PlannerInput:
    """Return *recorded* planned with the efficiency the builder resolved."""
    return replace(
        recorded,
        battery_charge_efficiency_pct=built.battery_charge_efficiency_pct,
        battery_discharge_efficiency_pct=built.battery_discharge_efficiency_pct,
        battery_efficiency_basis=built.battery_efficiency_basis,
    )


def _assert_golden(output: PlannerOutput, golden: dict[str, Any]) -> None:
    """Assert *output* is the plan ``origin/main`` made."""
    assert output.plan_cost is not None
    assert output.winner_name == golden["winner_name"]
    assert output.plan_cost.total_cost == pytest.approx(golden["total_cost"], abs=1e-6)
    assert output.plan_cost.score == pytest.approx(golden["score"], abs=1e-6)
    assert output.plan_cost.terminal_soc_value == pytest.approx(
        golden["terminal_soc_value"], abs=1e-6
    )
    assert output.battery_soc_at_end == pytest.approx(
        golden["battery_soc_at_end"], abs=1e-6
    )
    assert len(output.slots) == len(golden["slots"])
    for slot, (label, charged, discharged, grid_import, grid_export) in zip(
        output.slots, golden["slots"]
    ):
        assert slot.recommendation == label, slot.start
        assert slot.batteries_charged_kwh == pytest.approx(charged, abs=1e-6)
        assert slot.batteries_discharged_kwh == pytest.approx(discharged, abs=1e-6)
        assert slot.grid_import_kwh == pytest.approx(grid_import, abs=1e-6)
        assert slot.grid_export_kwh == pytest.approx(grid_export, abs=1e-6)


# ---------------------------------------------------------------------------
# The off path is the planner of origin/main
# ---------------------------------------------------------------------------


@_needs_scipy
@pytest.mark.parametrize("name", sorted(_GOLDEN["cycles"]))
@pytest.mark.parametrize("mode", ["off", "on_untrusted"])
def test_off_path_equals_the_plan_recorded_from_main(name: str, mode: str) -> None:
    """The cost and every slot match what ``origin/main`` planned.

    The fixture was recorded from ``origin/main`` before this option existed,
    from the same corpus cycles.  The efficiency goes through the builder
    here, with the configured values of the recorded cycle: with the option
    off, and with it on while the learner has nothing to offer, the plan must
    be that plan.
    """
    golden = _GOLDEN["cycles"][name]
    recorded = _recorded(name)
    assert recorded.battery_efficiency_basis is None
    cfg = SensorConfig()
    cfg.batteries_charge_efficiency = golden["charge_efficiency_pct"]
    cfg.batteries_discharge_efficiency = golden["discharge_efficiency_pct"]
    cfg.batteries_learned_efficiency_enabled = mode != "off"
    built = _build(cfg, EfficiencyLearner())

    output = run_planner(_with_efficiency(recorded, built))

    if mode == "off":
        assert built.battery_efficiency_basis is None
        assert output.battery_efficiency is None
        assert output.explanation.battery_efficiency is None
    else:
        assert output.battery_efficiency is not None
        assert output.battery_efficiency["source"] == SOURCE_CONFIGURED
        assert output.battery_efficiency["reason"] == REASON_LEARNING
    _assert_golden(output, golden)


# ---------------------------------------------------------------------------
# The option
# ---------------------------------------------------------------------------


class TestOption:
    """``hsem_batteries_learned_efficiency_enabled`` is opt-in."""

    def test_default_is_off(self) -> None:
        assert DEFAULT_CONFIG_VALUES[_OPTION] is False
        assert PlannerInput(now_iso="").battery_efficiency_basis is None

    @pytest.mark.parametrize(
        ("options", "expected"), [({}, False), ({_OPTION: False}, False)]
    )
    def test_off_keeps_the_configured_values(
        self, options: dict[str, Any], expected: bool
    ) -> None:
        entry = MagicMock()
        entry.data = {}
        entry.options = {
            "hsem_batteries_charge_efficiency": 97,
            "hsem_batteries_discharge_efficiency": 93,
            **options,
        }
        cfg = build_sensor_config(entry)

        planner_input = _build(cfg, EfficiencyLearner())

        assert cfg.batteries_learned_efficiency_enabled is expected
        assert planner_input.battery_charge_efficiency_pct == pytest.approx(97.0)
        assert planner_input.battery_discharge_efficiency_pct == pytest.approx(93.0)
        assert planner_input.battery_efficiency_basis is None

    @pytest.mark.asyncio
    async def test_on_feeds_the_measured_round_trip_to_the_planner(
        self, tmp_path: Path
    ) -> None:
        """Option set in the entry, measured by the cycle hook, read by the builder."""
        coordinator = _coordinator(tmp_path)
        await _learn(coordinator)
        entry = MagicMock()
        entry.data = {}
        entry.options = {_OPTION: True}
        cfg = build_sensor_config(entry)

        planner_input = _build(cfg, coordinator._efficiency_learner)

        assert cfg.batteries_learned_efficiency_enabled is True
        assert planner_input.battery_charge_efficiency_pct == pytest.approx(
            _ONE_WAY_PCT, abs=0.02
        )
        assert planner_input.battery_discharge_efficiency_pct == pytest.approx(
            _ONE_WAY_PCT, abs=0.02
        )
        basis = planner_input.battery_efficiency_basis
        assert basis is not None
        assert basis["source"] == SOURCE_LEARNED
        assert basis["learned_round_trip_efficiency_pct"] == pytest.approx(
            _ROUND_TRIP * 100.0, abs=0.02
        )
        assert basis["sample_age_days"] == pytest.approx(0.5, abs=0.1)

    @pytest.mark.asyncio
    async def test_too_few_measurements_keep_the_configured_values(
        self, tmp_path: Path
    ) -> None:
        coordinator = _coordinator(tmp_path)
        await _learn(coordinator, samples=MIN_SAMPLES - 1)

        planner_input = _build(_enabled(), coordinator._efficiency_learner)

        assert planner_input.battery_charge_efficiency_pct == pytest.approx(98.0)
        assert planner_input.battery_discharge_efficiency_pct == pytest.approx(98.0)
        assert planner_input.battery_efficiency_basis is not None
        assert planner_input.battery_efficiency_basis["reason"] == REASON_LEARNING

    @pytest.mark.asyncio
    async def test_flow_offers_and_requires_it(self) -> None:
        schema = await get_battery_economics_step_schema(None)
        keys = {str(key.schema) for key in schema.schema}

        assert _OPTION in keys
        errors = await validate_battery_economics_input({})
        assert errors[_OPTION] == "required"


# ---------------------------------------------------------------------------
# Planning with the measured round trip
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def runs() -> tuple[PlannerInput, PlannerOutput, PlannerOutput]:
    """Return the learned input, its plan and the configured plan."""
    recorded = _recorded()
    one_way = math.sqrt(0.90) * 100.0
    learned_input = replace(
        recorded,
        battery_charge_efficiency_pct=one_way,
        battery_discharge_efficiency_pct=one_way,
        battery_efficiency_basis={"source": SOURCE_LEARNED},
    )
    return learned_input, run_planner(learned_input), run_planner(recorded)


@_needs_scipy
class TestPlanningWithTheMeasuredRoundTrip:
    """One efficiency per direction, the same in every stage of the planner."""

    def test_every_invariant_holds(
        self, runs: tuple[PlannerInput, PlannerOutput, PlannerOutput]
    ) -> None:
        """Energy balance, SoC bounds, cost identity and terminal SoC."""
        learned_input, learned, _configured = runs

        violations = check_invariants(learned_input, learned)

        assert not violations, format_violations(violations)

    def test_the_grid_flow_of_every_slot_uses_the_measured_values(
        self, runs: tuple[PlannerInput, PlannerOutput, PlannerOutput]
    ) -> None:
        """``import - export = net load + charge / eta_c - discharge x eta_d``.

        The MILP wrote these slots and the scorer priced them.  The balance
        only closes when both used the efficiency of the planner input.
        """
        learned_input, learned, _configured = runs
        eta_c = learned_input.battery_charge_efficiency_pct / 100.0
        eta_d = learned_input.battery_discharge_efficiency_pct / 100.0
        active = [
            slot
            for slot in learned.slots
            if slot.recommendation != "time_passed"
            and slot.batteries_charged_kwh + slot.batteries_discharged_kwh > 0.0
        ]

        assert len(active) > 20
        for slot in active:
            assert slot.grid_import_kwh - slot.grid_export_kwh == pytest.approx(
                slot.estimated_net_consumption_kwh
                + slot.batteries_charged_kwh / eta_c
                - slot.batteries_discharged_kwh * eta_d,
                abs=2e-3,
            ), slot.start

    def test_the_soc_walk_follows_the_stored_energy(
        self, runs: tuple[PlannerInput, PlannerOutput, PlannerOutput]
    ) -> None:
        """Charge and discharge are battery-side: the SoC moves by their sum."""
        learned_input, learned, _configured = runs
        capacity = learned_input.battery_rated_capacity_kwh
        future = [s for s in learned.slots if s.recommendation != "time_passed"]

        for before, slot in zip(future, future[1:]):
            assert slot.estimated_battery_soc_pct == pytest.approx(
                before.estimated_battery_soc_pct
                + (slot.batteries_charged_kwh - slot.batteries_discharged_kwh)
                / capacity
                * 100.0,
                abs=0.05,
            ), slot.start
        assert learned.battery_soc_at_end == pytest.approx(
            future[-1].estimated_battery_soc_pct
        )

    def test_a_lower_round_trip_costs_more_and_cycles_less(
        self, runs: tuple[PlannerInput, PlannerOutput, PlannerOutput]
    ) -> None:
        """Loss is paid as more import: the same horizon cannot get cheaper."""
        _learned_input, learned, configured = runs
        assert learned.plan_cost is not None
        assert configured.plan_cost is not None

        assert learned.plan_cost.score > configured.plan_cost.score
        assert sum(s.batteries_charged_kwh for s in learned.slots) < sum(
            s.batteries_charged_kwh for s in configured.slots
        )

    def test_the_plan_says_what_it_used(
        self, runs: tuple[PlannerInput, PlannerOutput, PlannerOutput]
    ) -> None:
        learned_input, learned, configured = runs

        assert learned.battery_efficiency == learned_input.battery_efficiency_basis
        assert learned.explanation.battery_efficiency == {"source": SOURCE_LEARNED}
        assert learned.explanation.as_dict()["battery_efficiency"] == {
            "source": SOURCE_LEARNED
        }
        assert configured.battery_efficiency is None
        assert configured.explanation.as_dict()["battery_efficiency"] is None


# ---------------------------------------------------------------------------
# The sensor shows what the plan used
# ---------------------------------------------------------------------------


def _sensor(coordinator: Any) -> HSEMBatteryEfficiencySensor:
    """Construct the efficiency sensor bound to *coordinator*."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.options = {}
    entry.data = {}
    return HSEMBatteryEfficiencySensor(entry, coordinator)


class TestSensorShowsWhatThePlanUsed:
    """Measured by the cycle hook, planned by the planner, read by the sensor."""

    @_needs_scipy
    @pytest.mark.asyncio
    async def test_learned(self, tmp_path: Path) -> None:
        coordinator = _coordinator(tmp_path)
        await _learn(coordinator)
        built = _build(_enabled(), coordinator._efficiency_learner)
        output = run_planner(_with_efficiency(_recorded(), built))
        coordinator.data.plan_explanation = output.explanation

        attributes = _sensor(coordinator).extra_state_attributes

        assert attributes is not None
        assert attributes["planner_efficiency_source"] == SOURCE_LEARNED
        assert attributes["planner_efficiency_reason"] == REASON_LEARNED
        assert attributes["planner_charge_efficiency_pct"] == pytest.approx(
            _ONE_WAY_PCT, abs=0.02
        )
        assert attributes["planner_discharge_efficiency_pct"] == pytest.approx(
            _ONE_WAY_PCT, abs=0.02
        )
        assert attributes["measured_round_trip_efficiency_pct"] == pytest.approx(
            _ROUND_TRIP * 100.0, abs=0.1
        )

    @pytest.mark.asyncio
    async def test_option_off(self, tmp_path: Path) -> None:
        """A trusted measurement and the option off: the sensor says configured."""
        coordinator = _coordinator(tmp_path)
        await _learn(coordinator)

        attributes = _sensor(coordinator).extra_state_attributes

        assert attributes is not None
        assert attributes["planner_efficiency_source"] == SOURCE_CONFIGURED
        assert attributes["planner_efficiency_reason"] == REASON_DISABLED
        assert attributes["planner_charge_efficiency_pct"] == pytest.approx(98.0)
        assert attributes["planner_discharge_efficiency_pct"] == pytest.approx(98.0)

    def test_before_the_first_cycle(self, tmp_path: Path) -> None:
        coordinator = _coordinator(tmp_path)
        cast(Any, coordinator).data = None

        attributes = _sensor(coordinator).extra_state_attributes

        assert attributes is not None
        assert attributes["planner_efficiency_source"] == SOURCE_CONFIGURED
        assert attributes["planner_charge_efficiency_pct"] is None
