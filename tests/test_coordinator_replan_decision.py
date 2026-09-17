"""Tests for ``_should_replan`` and the EV delivered-energy replan trigger.

The coordinator reuses the last accepted plan unless something material
changed since it was accepted. Each test persists a baseline with
``_persist_plan_state`` (exactly as a successful cycle does), changes one
input, and checks whether a replan is requested.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.hsem.coordinator import HSEMDataUpdateCoordinator
from custom_components.hsem.coordinator_cycle import (
    EV_DELIVERED_ENERGY_REPLAN_DELTA_KWH,
    EV_DELIVERED_ENERGY_REPLAN_MIN_SECONDS,
)
from custom_components.hsem.coordinator_helpers import LoadForecastSignature
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.planner_output import PlannerOutput
from tests.coordinator_fixtures import make_real_coordinator

_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
_WITHIN_SLOT = _NOW + timedelta(minutes=5)
_EV_CAPACITY_KWH = 100.0


def _baseline_live() -> LiveState:
    """Return a live snapshot with every replan-relevant field populated."""
    live = LiveState()
    live.import_electricity_price = 1.5
    live.ev_planned_load_deadline = _NOW + timedelta(hours=8)
    live.ev_second_planned_load_deadline = _NOW + timedelta(hours=9)
    return live


def _accepted(
    live: LiveState,
    *,
    load_forecast_signature: LoadForecastSignature | None = None,
) -> HSEMDataUpdateCoordinator:
    """Return a coordinator whose last accepted plan was made from *live*."""
    coordinator = make_real_coordinator()
    coordinator._cfg.recommendation_interval_minutes = 15
    coordinator._cfg.ev_planned_load_battery_capacity_kwh = _EV_CAPACITY_KWH
    coordinator._cfg.ev_second_planned_load_battery_capacity_kwh = _EV_CAPACITY_KWH
    coordinator._last_planner_output = PlannerOutput()
    coordinator._last_plan_slot_start = _NOW
    coordinator._persist_plan_state(
        live, load_forecast_signature=load_forecast_signature
    )
    return coordinator


def _set_ev_connected(live: LiveState) -> None:
    live.ev.is_connected = True


def _set_ev_charging(live: LiveState) -> None:
    live.ev.is_charging = True


def _set_ev_soc_below_target(live: LiveState) -> None:
    live.ev.soc_pct = 40.0
    live.ev.soc_target_pct = 80.0


def _set_ev2_connected(live: LiveState) -> None:
    live.ev_second.is_connected = True


def _set_ev2_charging(live: LiveState) -> None:
    live.ev_second.is_charging = True


def _set_ev2_soc_below_target(live: LiveState) -> None:
    live.ev_second.soc_pct = 40.0
    live.ev_second.soc_target_pct = 80.0


def _set_ev_target_soc(live: LiveState) -> None:
    live.ev_planned_load_target_soc_pct = 90.0


def _set_ev_smart_charging_off(live: LiveState) -> None:
    live.ev_planned_load_smart_charging_enabled = False


def _set_ev_deadline(live: LiveState) -> None:
    live.ev_planned_load_deadline = _NOW + timedelta(hours=10)


def _set_ev2_target_soc(live: LiveState) -> None:
    live.ev_second_planned_load_target_soc_pct = 90.0


def _set_ev2_smart_charging_off(live: LiveState) -> None:
    live.ev_second_planned_load_smart_charging_enabled = False


def _set_ev2_deadline(live: LiveState) -> None:
    live.ev_second_planned_load_deadline = _NOW + timedelta(hours=10)


def _set_force_mode(live: LiveState) -> None:
    live.force_working_mode_state = "batteries_charge_grid"


def _set_new_import_price(live: LiveState) -> None:
    live.import_electricity_price = 1.6


_MATERIAL_CHANGES: list[Callable[[LiveState], None]] = [
    _set_ev_connected,
    _set_ev_charging,
    _set_ev_soc_below_target,
    _set_ev2_connected,
    _set_ev2_charging,
    _set_ev2_soc_below_target,
    _set_ev_target_soc,
    _set_ev_smart_charging_off,
    _set_ev_deadline,
    _set_ev2_target_soc,
    _set_ev2_smart_charging_off,
    _set_ev2_deadline,
    _set_force_mode,
    _set_new_import_price,
]


class TestShouldReplanTriggers:
    """Material changes since the accepted plan force a replan."""

    def test_first_cycle_always_plans(self) -> None:
        """Without an accepted plan there is nothing to reuse."""
        coordinator = make_real_coordinator()

        assert coordinator._should_replan(LiveState(), _NOW) is True

    def test_unchanged_state_reuses_the_plan(self) -> None:
        """Same inputs within the same slot → no replan."""
        coordinator = _accepted(_baseline_live())

        assert coordinator._should_replan(_baseline_live(), _WITHIN_SLOT) is False

    @pytest.mark.parametrize(
        "change", _MATERIAL_CHANGES, ids=lambda fn: fn.__name__.removeprefix("_set_")
    )
    def test_material_change_triggers_replan(
        self, change: Callable[[LiveState], None]
    ) -> None:
        """Each monitored input change on its own requests a replan."""
        coordinator = _accepted(_baseline_live())
        live = _baseline_live()
        change(live)

        assert coordinator._should_replan(live, _WITHIN_SLOT) is True

    def test_tiny_target_soc_and_price_jitter_is_ignored(self) -> None:
        """Sub-threshold target SoC and price wobble do not replan."""
        coordinator = _accepted(_baseline_live())
        live = _baseline_live()
        live.ev_planned_load_target_soc_pct = 80.4
        live.ev_second_planned_load_target_soc_pct = 79.6
        live.import_electricity_price = 1.5005

        assert coordinator._should_replan(live, _WITHIN_SLOT) is False

    @pytest.mark.parametrize(
        "now",
        [
            pytest.param(_NOW + timedelta(minutes=15), id="next_slot"),
            pytest.param(_NOW + timedelta(days=1), id="same_slot_next_day"),
        ],
    )
    def test_slot_boundary_or_date_change_triggers_replan(self, now: datetime) -> None:
        """Entering another slot — or the same slot on another day — replans."""
        coordinator = _accepted(_baseline_live())

        assert coordinator._should_replan(_baseline_live(), now) is True

    def test_pending_load_forecast_recovery_triggers_replan(self) -> None:
        """A recovered load forecast after a safety hold forces a fresh plan."""
        coordinator = _accepted(_baseline_live())
        coordinator._load_forecast_recovery_replan_pending = True

        assert coordinator._should_replan(_baseline_live(), _WITHIN_SLOT) is True

    def test_live_power_request_triggers_replan(self) -> None:
        """A matured live-power correction request forces a fresh plan."""
        coordinator = _accepted(_baseline_live())

        assert (
            coordinator._should_replan(
                _baseline_live(),
                _WITHIN_SLOT,
                live_power_replan_request_slot=_NOW,
            )
            is True
        )

    def test_changed_load_forecast_signature_triggers_replan(self) -> None:
        """A different future load profile invalidates the accepted plan."""
        accepted_signature: LoadForecastSignature = (
            (_NOW.isoformat(), 1.0, 1.0, 1.0, 1.0, 1.0),
        )
        changed_signature: LoadForecastSignature = (
            (_NOW.isoformat(), 2.0, 1.0, 1.0, 1.0, 1.0),
        )
        coordinator = _accepted(
            _baseline_live(), load_forecast_signature=accepted_signature
        )

        assert (
            coordinator._should_replan(
                _baseline_live(),
                _WITHIN_SLOT,
                load_forecast_signature=accepted_signature,
            )
            is False
        )
        assert (
            coordinator._should_replan(
                _baseline_live(),
                _WITHIN_SLOT,
                load_forecast_signature=changed_signature,
            )
            is True
        )

    def test_unreachable_ev_deadline_triggers_replan(self) -> None:
        """An EV that can no longer make its deadline at full power replans."""
        live = _baseline_live()
        live.ev_planned_load_connected = True
        live.ev_planned_load_target_soc_pct = 80.0
        live.ev.effective_soc_pct = 20.0
        live.ev_planned_load_deadline = _NOW + timedelta(hours=1)
        coordinator = _accepted(live)
        coordinator._cfg.ev_planned_load_charger_power_kw = 7.0
        coordinator._cfg.ev_planned_load_charger_efficiency_pct = 100.0
        later = _NOW + timedelta(minutes=10)

        assert coordinator._should_replan(live, later) is True


def _charging_live(effective_soc_pct: float) -> LiveState:
    """Return a live snapshot of a charging primary EV at *effective_soc_pct*."""
    live = _baseline_live()
    live.ev.is_charging = True
    live.ev.effective_soc_pct = effective_soc_pct
    return live


class TestEvDeliveredEnergyReplan:
    """Credited EV energy replans on target crossing or material drift."""

    def test_crossing_the_accepted_target_replans_immediately(self) -> None:
        """Reaching the target bypasses both the cadence gate and threshold."""
        coordinator = _accepted(_charging_live(79.9))
        coordinator._last_plan_ev_target_soc = 80.0
        just_after_acceptance = _NOW + timedelta(seconds=1)

        assert (
            coordinator._ev_delivered_energy_requires_replan(
                _charging_live(80.0), just_after_acceptance
            )
            is True
        )

    def test_drift_below_the_threshold_does_not_replan(self) -> None:
        """Small delivered-energy drift after the cadence gate is ignored."""
        coordinator = _accepted(_charging_live(50.0))
        coordinator._last_plan_ev_target_soc = None
        after_cadence = _NOW + timedelta(
            seconds=EV_DELIVERED_ENERGY_REPLAN_MIN_SECONDS + 1
        )
        small_step_pct = EV_DELIVERED_ENERGY_REPLAN_DELTA_KWH / 2

        assert (
            coordinator._ev_delivered_energy_requires_replan(
                _charging_live(50.0 + small_step_pct), after_cadence
            )
            is False
        )

    @pytest.mark.parametrize(
        ("baseline_kwh", "target_soc"),
        [
            pytest.param("not a number", 80.0, id="non_numeric_baseline"),
            pytest.param(float("nan"), 80.0, id="non_finite_baseline"),
        ],
    )
    def test_unusable_baseline_is_skipped(
        self, baseline_kwh: object, target_soc: object
    ) -> None:
        """A corrupted accepted baseline never triggers a replan."""
        coordinator = _accepted(_charging_live(50.0))
        coordinator._last_plan_ev_effective_energy_kwh = baseline_kwh  # type: ignore[assignment]  # corrupted state
        coordinator._last_plan_ev_target_soc = target_soc  # type: ignore[assignment]  # corrupted state
        after_cadence = _NOW + timedelta(
            seconds=EV_DELIVERED_ENERGY_REPLAN_MIN_SECONDS + 1
        )

        assert (
            coordinator._ev_delivered_energy_requires_replan(
                _charging_live(99.0), after_cadence
            )
            is False
        )

    @pytest.mark.parametrize(
        "target_soc",
        [
            pytest.param("not a number", id="non_numeric_target"),
            pytest.param(150.0, id="out_of_range_target"),
        ],
    )
    def test_unusable_target_falls_back_to_the_drift_threshold(
        self, target_soc: object
    ) -> None:
        """Without a valid target only material drift can trigger a replan."""
        coordinator = _accepted(_charging_live(50.0))
        coordinator._last_plan_ev_target_soc = target_soc  # type: ignore[assignment]  # corrupted state
        just_after_acceptance = _NOW + timedelta(seconds=1)
        after_cadence = _NOW + timedelta(
            seconds=EV_DELIVERED_ENERGY_REPLAN_MIN_SECONDS + 1
        )

        assert (
            coordinator._ev_delivered_energy_requires_replan(
                _charging_live(99.0), just_after_acceptance
            )
            is False
        )
        assert (
            coordinator._ev_delivered_energy_requires_replan(
                _charging_live(99.0), after_cadence
            )
            is True
        )
