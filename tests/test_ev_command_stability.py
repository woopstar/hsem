"""Tests for the EV charger command-stability layer.

Covers the ceiling deadband (asymmetric: reductions damped, increases always
published) and the slot-tail stop suppression, plus the safety clamps that
must always win over stability.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.hsem.coordinator_cycle import CoordinatorCycleMixin
from custom_components.hsem.coordinator_ev_command_stability import (
    CoordinatorEvCommandStabilityMixin,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import EVLiveState, LiveState
from custom_components.hsem.models.sensor_config import SensorConfig

SLOT_START = datetime(2026, 8, 30, 16, 30, tzinfo=UTC)
SLOT_END = datetime(2026, 8, 30, 16, 45, tzinfo=UTC)
DEADLINE = datetime(2026, 8, 30, 17, 0, tzinfo=UTC)

# 3-phase @ 230 V: one amp is 690 W.
AMP_W = 690.0


class _Harness(CoordinatorEvCommandStabilityMixin):
    """Minimal stand-in exposing only what the stability layer touches."""

    # The production coordinator resolves this from CoordinatorCycleMixin via
    # MRO; mirror that binding rather than reimplementing the conversion.
    _ev_effective_energy_kwh = staticmethod(
        CoordinatorCycleMixin._ev_effective_energy_kwh
    )

    def __init__(self, recommendations: list[HourlyRecommendation]) -> None:
        self._hourly_recommendations = recommendations
        self._ev_last_command_w: dict[str, float] = {}
        # get_config_value falls back to DEFAULT_CONFIG_VALUES for None, which
        # is exactly the shipped default set we want to exercise.
        self._config_entry = None  # type: ignore[assignment]


def _rec(
    start: datetime = SLOT_START,
    end: datetime = SLOT_END,
    *,
    import_price: float = 0.3530,
    ev_power_w: float = 0.0,
    **kwargs: float,
) -> HourlyRecommendation:
    """Build a recommendation slot carrying an EV command."""
    defaults: dict = {
        "avg_house_consumption_kwh": 0.25,
        "avg_house_consumption_1d_kwh": 0.25,
        "avg_house_consumption_3d_kwh": 0.25,
        "avg_house_consumption_7d_kwh": 0.25,
        "avg_house_consumption_14d_kwh": 0.25,
        "batteries_charged_kwh": 0.0,
        "batteries_discharged_kwh": 0.0,
        "estimated_battery_capacity_kwh": 5.0,
        "estimated_battery_soc_pct": 50,
        "estimated_cost_currency": 0.0,
        "estimated_net_consumption_kwh": 0.0,
        "export_price": 0.05,
        "grid_export_kwh": 0.0,
        "grid_import_kwh": 0.0,
        "import_price": import_price,
        "recommendation": None,
        "solcast_pv_estimate_kwh": 0.0,
    }
    defaults.update(kwargs)
    rec = HourlyRecommendation(start=start, end=end, **defaults)  # type: ignore[arg-type]
    rec.ev_charger_calculated_power = ev_power_w
    return rec


def _cfg(**overrides: float) -> SensorConfig:
    """Build a config with the primary EV planned load enabled."""
    cfg = SensorConfig()
    cfg.ev_planned_load_enabled = True
    cfg.ev_planned_load_charger_phase_topology = "three_phase_balanced"
    cfg.ev_planned_load_charger_power_kw = 11.0
    cfg.ev_planned_load_charger_min_power_w = 1380.0
    cfg.ev_planned_load_battery_capacity_kwh = 86.5
    cfg.ev_planned_load_command_deadband_a = 3.0
    cfg.ev_planned_load_stub_floor_minutes = 2.0
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def _live(
    *,
    soc_pct: float = 75.0,
    is_charging: bool = True,
    connected: bool = True,
    smart_charging: bool = True,
    house_w: float = 1000.0,
) -> LiveState:
    """Build a live state with a mid-session primary EV."""
    live = LiveState()
    live.ev = EVLiveState(
        is_charging=is_charging,
        power_w=9660.0 if is_charging else 0.0,
        soc_pct=soc_pct,
        effective_soc_pct=soc_pct,
        is_connected=connected,
    )
    live.ev_planned_load_connected = connected
    live.ev_planned_load_smart_charging_enabled = smart_charging
    live.ev_planned_load_target_soc_pct = 80.0
    live.ev_planned_load_deadline = DEADLINE
    live.house_consumption_power_w = house_w
    return live


def _run(
    harness: _Harness,
    now: datetime,
    cfg: SensorConfig,
    live: LiveState,
) -> float:
    """Run the stability layer and return the published primary command."""
    harness._apply_ev_command_stability(now, live, cfg)
    return harness._hourly_recommendations[0].ev_charger_calculated_power


# ---------------------------------------------------------------------------
# Ceiling deadband
# ---------------------------------------------------------------------------


def test_small_reduction_is_held() -> None:
    """A 2 A reduction below the 3 A deadband keeps the previous ceiling."""
    rec = _rec(ev_power_w=14 * AMP_W)
    harness = _Harness([rec])
    harness._ev_last_command_w["ev"] = 16 * AMP_W
    published = _run(harness, SLOT_START + timedelta(minutes=5), _cfg(), _live())
    assert published == pytest.approx(16 * AMP_W)


def test_large_reduction_passes_through() -> None:
    """A reduction at or beyond the deadband is published immediately."""
    rec = _rec(ev_power_w=13 * AMP_W)
    harness = _Harness([rec])
    harness._ev_last_command_w["ev"] = 16 * AMP_W
    published = _run(harness, SLOT_START + timedelta(minutes=5), _cfg(), _live())
    assert published == pytest.approx(13 * AMP_W)


def test_increase_is_never_held() -> None:
    """Raising the ceiling only grants headroom, so it is never damped."""
    rec = _rec(ev_power_w=16 * AMP_W)
    harness = _Harness([rec])
    harness._ev_last_command_w["ev"] = 14 * AMP_W
    published = _run(harness, SLOT_START + timedelta(minutes=5), _cfg(), _live())
    assert published == pytest.approx(16 * AMP_W)


def test_zero_deadband_disables_holding() -> None:
    """A 0 A deadband reproduces the pre-feature pass-through behaviour."""
    rec = _rec(ev_power_w=15 * AMP_W)
    harness = _Harness([rec])
    harness._ev_last_command_w["ev"] = 16 * AMP_W
    published = _run(
        harness,
        SLOT_START + timedelta(minutes=5),
        _cfg(ev_planned_load_command_deadband_a=0.0),
        _live(),
    )
    assert published == pytest.approx(15 * AMP_W)


def test_materially_cheaper_reduction_bypasses_deadband() -> None:
    """Holding is abandoned when it would cost more than the bypass fraction.

    The live slot is expensive and the next EV slot is far cheaper, so holding
    the higher ceiling here rather than shifting energy forward is materially
    worse than the plan and must be published despite the deadband.
    """
    now = SLOT_START + timedelta(minutes=5)
    live_slot = _rec(import_price=1.0, ev_power_w=14 * AMP_W)
    next_slot = _rec(
        start=SLOT_END,
        end=SLOT_END + timedelta(minutes=15),
        import_price=0.05,
        ev_power_w=10 * AMP_W,
    )
    harness = _Harness([live_slot, next_slot])
    harness._ev_last_command_w["ev"] = 16 * AMP_W
    assert _run(harness, now, _cfg(), _live()) == pytest.approx(14 * AMP_W)


def test_equally_priced_alternative_keeps_the_hold() -> None:
    """With no price difference to exploit, the deadband still holds."""
    now = SLOT_START + timedelta(minutes=5)
    live_slot = _rec(import_price=0.35, ev_power_w=14 * AMP_W)
    next_slot = _rec(
        start=SLOT_END,
        end=SLOT_END + timedelta(minutes=15),
        import_price=0.35,
        ev_power_w=10 * AMP_W,
    )
    harness = _Harness([live_slot, next_slot])
    harness._ev_last_command_w["ev"] = 16 * AMP_W
    assert _run(harness, now, _cfg(), _live()) == pytest.approx(16 * AMP_W)


# ---------------------------------------------------------------------------
# Slot-tail stop suppression
# ---------------------------------------------------------------------------


def test_slot_tail_zero_is_suppressed_while_need_remains() -> None:
    """A zero command in the slot tail holds the previous ceiling instead."""
    rec = _rec(ev_power_w=0.0)
    harness = _Harness([rec])
    harness._ev_last_command_w["ev"] = 16 * AMP_W
    # 23 seconds left — the exact stub that stopped the session at 14:44.
    now = SLOT_END - timedelta(seconds=23)
    assert _run(harness, now, _cfg(), _live()) == pytest.approx(16 * AMP_W)


def test_zero_outside_the_tail_window_stops_normally() -> None:
    """Earlier in the slot a zero command is a real stop and is published."""
    rec = _rec(ev_power_w=0.0)
    harness = _Harness([rec])
    harness._ev_last_command_w["ev"] = 16 * AMP_W
    now = SLOT_START + timedelta(minutes=5)
    assert _run(harness, now, _cfg(), _live()) == pytest.approx(0.0)


def test_slot_tail_zero_stops_once_target_reached() -> None:
    """A finished target stops immediately even inside the tail window."""
    rec = _rec(ev_power_w=0.0)
    harness = _Harness([rec])
    harness._ev_last_command_w["ev"] = 16 * AMP_W
    now = SLOT_END - timedelta(seconds=23)
    assert _run(harness, now, _cfg(), _live(soc_pct=80.0)) == pytest.approx(0.0)


def test_slot_tail_zero_stops_when_not_charging() -> None:
    """Suppression requires a live session — an idle charger is left alone."""
    rec = _rec(ev_power_w=0.0)
    harness = _Harness([rec])
    harness._ev_last_command_w["ev"] = 16 * AMP_W
    now = SLOT_END - timedelta(seconds=23)
    assert _run(harness, now, _cfg(), _live(is_charging=False)) == pytest.approx(0.0)


def test_slot_tail_zero_stops_after_deadline() -> None:
    """Past the deadline there is no need left to protect."""
    rec = _rec(
        start=DEADLINE,
        end=DEADLINE + timedelta(minutes=15),
        ev_power_w=0.0,
    )
    harness = _Harness([rec])
    harness._ev_last_command_w["ev"] = 16 * AMP_W
    now = DEADLINE + timedelta(minutes=15) - timedelta(seconds=23)
    assert _run(harness, now, _cfg(), _live()) == pytest.approx(0.0)


def test_zero_stub_floor_minutes_disables_suppression() -> None:
    """0 minutes reproduces the pre-feature stop behaviour."""
    rec = _rec(ev_power_w=0.0)
    harness = _Harness([rec])
    harness._ev_last_command_w["ev"] = 16 * AMP_W
    now = SLOT_END - timedelta(seconds=23)
    published = _run(
        harness, now, _cfg(ev_planned_load_stub_floor_minutes=0.0), _live()
    )
    assert published == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Safety clamps always win over stability
# ---------------------------------------------------------------------------


def test_disconnected_ev_follows_the_plan_immediately() -> None:
    """An unplugged car is never held at a stale ceiling."""
    rec = _rec(ev_power_w=0.0)
    harness = _Harness([rec])
    harness._ev_last_command_w["ev"] = 16 * AMP_W
    now = SLOT_END - timedelta(seconds=23)
    assert _run(harness, now, _cfg(), _live(connected=False)) == pytest.approx(0.0)


def test_smart_charging_off_follows_the_plan_immediately() -> None:
    """Switching smart charging off must take effect on the same cycle."""
    rec = _rec(ev_power_w=0.0)
    harness = _Harness([rec])
    harness._ev_last_command_w["ev"] = 16 * AMP_W
    now = SLOT_END - timedelta(seconds=23)
    published = _run(harness, now, _cfg(), _live(smart_charging=False))
    assert published == pytest.approx(0.0)


def test_held_command_is_clamped_to_live_fuse_budget() -> None:
    """A hold can never exceed the headroom the main fuse actually leaves.

    The default 25 A x 3 phases = 17 250 W total.  The house sensor includes
    EV draw by default, so the fixed site load is 22 770 - 9 660 = 13 110 W,
    leaving 4 140 W — exactly 6 A on a three-phase charger.
    """
    rec = _rec(ev_power_w=14 * AMP_W)
    harness = _Harness([rec])
    harness._ev_last_command_w["ev"] = 16 * AMP_W
    published = _run(
        harness,
        SLOT_START + timedelta(minutes=5),
        _cfg(),
        _live(house_w=22770.0),
    )
    assert published == pytest.approx(6 * AMP_W)


def test_hold_below_charger_minimum_collapses_to_zero() -> None:
    """A ceiling the charger cannot run at is published as a stop, not a trickle."""
    rec = _rec(ev_power_w=14 * AMP_W)
    harness = _Harness([rec])
    harness._ev_last_command_w["ev"] = 16 * AMP_W
    # 25 910 - 9 660 = 16 250 W fixed load leaves 1 000 W of headroom,
    # below the 1 380 W (2 A) charger minimum.
    published = _run(
        harness,
        SLOT_START + timedelta(minutes=5),
        _cfg(),
        _live(house_w=25910.0),
    )
    assert published == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Slot accounting stays coherent
# ---------------------------------------------------------------------------


def test_hold_updates_slot_energy_and_cost_coherently() -> None:
    """Holding a ceiling moves the slot's energy, grid flow and cost with it."""
    rec = _rec(ev_power_w=14 * AMP_W, grid_import_kwh=1.0)
    harness = _Harness([rec])
    harness._ev_last_command_w["ev"] = 16 * AMP_W
    now = SLOT_START + timedelta(minutes=5)
    published = _run(harness, now, _cfg(), _live())

    remaining_hours = (SLOT_END - now).total_seconds() / 3600.0
    expected_kwh = round(published * remaining_hours / 1000.0, 3)
    assert rec.ev_total_planned_load_kwh == pytest.approx(expected_kwh)
    # The default config counts EV draw inside the house forecast.
    assert rec.ev_accounted_load_kwh == pytest.approx(expected_kwh)
    assert rec.ev_planned_load_kwh == pytest.approx(0.0)
    assert rec.estimated_cost_currency == pytest.approx(
        round(
            rec.grid_import_kwh * rec.import_price
            - rec.grid_export_kwh * rec.export_price,
            4,
        )
    )


def test_published_command_is_remembered_for_the_next_cycle() -> None:
    """The layer holds against what it published, not what the plan asked."""
    rec = _rec(ev_power_w=14 * AMP_W)
    harness = _Harness([rec])
    harness._ev_last_command_w["ev"] = 16 * AMP_W
    _run(harness, SLOT_START + timedelta(minutes=5), _cfg(), _live())
    assert harness._ev_last_command_w["ev"] == pytest.approx(16 * AMP_W)
