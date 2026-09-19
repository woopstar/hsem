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
from custom_components.hsem.coordinator_helpers import ocpp_charge_target
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
# Charge-past-target EVs follow the plan (issue #1015)
# ---------------------------------------------------------------------------


def _past_target_cfg() -> SensorConfig:
    """Config with charge-past-target enabled for the primary EV."""
    cfg = _cfg()
    cfg.ev.allow_charge_past_target_soc = True
    return cfg


def test_past_target_small_reduction_is_not_held() -> None:
    """A past-target ceiling drops with the surplus instead of being held.

    Holding 16 A against a 14 A surplus plan would draw the difference from
    the grid; the same 2 A reduction is held for a below-target EV
    (``test_small_reduction_is_held``).
    """
    rec = _rec(ev_power_w=14 * AMP_W)
    harness = _Harness([rec])
    harness._ev_last_command_w["ev"] = 16 * AMP_W
    published = _run(
        harness,
        SLOT_START + timedelta(minutes=5),
        _past_target_cfg(),
        _live(soc_pct=94.0),
    )
    assert published == pytest.approx(14 * AMP_W)


def test_past_target_is_not_held_when_prices_are_flat() -> None:
    """The cost bypass cannot release a past-target hold, so none is taken.

    Mirrors ``test_equally_priced_alternative_keeps_the_hold``: with equal
    prices the bypass never fires, which is exactly why a past-target EV
    must not be held at all.
    """
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
    assert _run(harness, now, _past_target_cfg(), _live(soc_pct=94.0)) == pytest.approx(
        14 * AMP_W
    )


def test_past_target_zero_stops_in_the_slot_tail() -> None:
    """A past-target zero is never suppressed, even with a live session."""
    rec = _rec(ev_power_w=0.0)
    harness = _Harness([rec])
    harness._ev_last_command_w["ev"] = 16 * AMP_W
    now = SLOT_END - timedelta(seconds=23)
    assert _run(harness, now, _past_target_cfg(), _live(soc_pct=94.0)) == pytest.approx(
        0.0
    )


def test_setting_alone_does_not_disable_the_deadband() -> None:
    """Below target the EV is deadline-driven, so the deadband still holds."""
    rec = _rec(ev_power_w=14 * AMP_W)
    harness = _Harness([rec])
    harness._ev_last_command_w["ev"] = 16 * AMP_W
    published = _run(
        harness,
        SLOT_START + timedelta(minutes=5),
        _past_target_cfg(),
        _live(soc_pct=75.0),
    )
    assert published == pytest.approx(16 * AMP_W)


def test_above_target_without_the_setting_keeps_the_deadband() -> None:
    """Being above target is not past-target mode unless the setting is on."""
    rec = _rec(ev_power_w=14 * AMP_W)
    harness = _Harness([rec])
    harness._ev_last_command_w["ev"] = 16 * AMP_W
    published = _run(
        harness, SLOT_START + timedelta(minutes=5), _cfg(), _live(soc_pct=94.0)
    )
    assert published == pytest.approx(16 * AMP_W)


@pytest.mark.parametrize(
    ("soc_pct", "expected"),
    [(79.9, False), (80.0, True), (94.0, True), (100.0, True), (None, False)],
)
def test_past_target_predicate(soc_pct: float | None, expected: bool) -> None:
    """Past-target needs the setting and a known SoC at or above target."""
    harness = _Harness([_rec()])
    live = _live(soc_pct=80.0)
    live.ev.effective_soc_pct = soc_pct
    (spec, _second) = harness._resolve_ev_command_specs(_past_target_cfg(), live)
    assert harness._ev_is_past_target(spec) is expected


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
    # Without a configured EV power entity, an EV-inclusive raw meter leaves
    # this contribution embedded in the normalized planner baseline.
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


# ---------------------------------------------------------------------------
# Auto-phase-switching chargers (issue #1001): mode-aware quantisation
# ---------------------------------------------------------------------------


def _cfg_switchable(**overrides: float) -> SensorConfig:
    """Build a config for an 11 kW auto-phase-switching charger."""
    cfg = _cfg()
    cfg.ev_planned_load_charger_phase_topology = "three_phase_switchable"
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def test_switchable_one_phase_command_quantises_on_one_phase() -> None:
    """2300 W is exactly 10 A one-phase — published unchanged."""
    rec = _rec(ev_power_w=2300.0)
    harness = _Harness([rec])
    published = _run(
        harness, SLOT_START + timedelta(minutes=1), _cfg_switchable(), _live()
    )
    assert published == pytest.approx(2300.0)


def test_switchable_gap_command_floors_to_one_phase_ceiling() -> None:
    """4000 W sits in the 3681–4139 W gap — floors to 16 A one-phase (3680 W)."""
    rec = _rec(ev_power_w=4000.0)
    harness = _Harness([rec])
    published = _run(
        harness, SLOT_START + timedelta(minutes=1), _cfg_switchable(), _live()
    )
    assert published == pytest.approx(3680.0)


def test_switchable_three_phase_command_quantises_on_three_phases() -> None:
    """6900 W is exactly 10 A three-phase — published unchanged."""
    rec = _rec(ev_power_w=6900.0)
    harness = _Harness([rec])
    published = _run(
        harness, SLOT_START + timedelta(minutes=1), _cfg_switchable(), _live()
    )
    assert published == pytest.approx(6900.0)


def test_switchable_command_is_clamped_to_three_phase_nameplate() -> None:
    """15 kW exceeds the 11 kW (16 A x 3) nameplate — clamps to 11040 W."""
    rec = _rec(ev_power_w=15_000.0)
    harness = _Harness([rec])
    published = _run(
        harness, SLOT_START + timedelta(minutes=1), _cfg_switchable(), _live()
    )
    assert published == pytest.approx(11_040.0)


def test_switchable_minimum_is_the_single_phase_floor() -> None:
    """A 1000 W command cannot start the charger — collapses to zero."""
    rec = _rec(ev_power_w=1000.0)
    harness = _Harness([rec])
    published = _run(
        harness, SLOT_START + timedelta(minutes=1), _cfg_switchable(), _live()
    )
    assert published == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Auto-phase-switching chargers (issue #1083): disruptive-mode hysteresis
# ---------------------------------------------------------------------------


def test_active_three_to_one_phase_transition_keeps_three_phase_mode() -> None:
    """Near-equal prices keep an active go-e session in three-phase mode."""
    now = SLOT_START + timedelta(minutes=5)
    live_slot = _rec(import_price=0.279, ev_power_w=1840.0)
    next_slot = _rec(
        start=SLOT_END,
        end=SLOT_END + timedelta(hours=1),
        import_price=0.273,
        ev_power_w=6900.0,
    )
    harness = _Harness([live_slot, next_slot])
    harness._ev_last_command_w["ev"] = 11_040.0
    live = _live(soc_pct=73.7)
    live.ev_planned_load_deadline = SLOT_START + timedelta(hours=2, minutes=30)

    published = _run(harness, now, _cfg_switchable(), live)

    assert published == pytest.approx(4140.0)
    target_kw, amps, phases = ocpp_charge_target(
        published, "three_phase_switchable", rated_current_a=16
    )
    assert target_kw == pytest.approx(4.14)
    assert amps == 6
    assert phases == 3
    expected_kwh = round(published * (SLOT_END - now).total_seconds() / 3_600_000, 3)
    assert live_slot.ev_charger_calculated_power == pytest.approx(published)
    assert live_slot.ev_total_planned_load_kwh == pytest.approx(expected_kwh)
    assert live_slot.ev_accounted_load_kwh == pytest.approx(expected_kwh)
    assert live_slot.ev_planned_load_kwh == pytest.approx(0.0)


def test_active_one_to_three_phase_transition_keeps_one_phase_mode() -> None:
    """A feasible inverse crossing does not flap an active session to three phases."""
    rec = _rec(ev_power_w=4140.0)
    next_slot = _rec(
        start=SLOT_END,
        end=SLOT_END + timedelta(minutes=15),
        ev_power_w=6900.0,
    )
    harness = _Harness([rec, next_slot])
    harness._ev_last_command_w["ev"] = 1840.0
    live = _live(soc_pct=79.0)
    live.ev.power_w = 1840.0
    live.grid_phase_power_w = (3000.0, 3200.0, 3400.0)

    published = _run(
        harness,
        SLOT_START + timedelta(minutes=5),
        _cfg_switchable(main_fuse_amps=25.0),
        live,
    )

    assert published == pytest.approx(1840.0)
    _, amps, phases = ocpp_charge_target(
        published, "three_phase_switchable", rated_current_a=16
    )
    assert amps == 8
    assert phases == 1


def test_inverse_phase_hold_yields_when_phase_telemetry_is_missing() -> None:
    """A configured fuse without complete phase readings fails closed."""
    rec = _rec(ev_power_w=4140.0)
    next_slot = _rec(
        start=SLOT_END,
        end=SLOT_END + timedelta(minutes=15),
        ev_power_w=6900.0,
    )
    harness = _Harness([rec, next_slot])
    harness._ev_last_command_w["ev"] = 1840.0
    live = _live(soc_pct=79.0)
    live.ev.power_w = 1840.0

    assert _run(
        harness,
        SLOT_START + timedelta(minutes=5),
        _cfg_switchable(main_fuse_amps=25.0),
        live,
    ) == pytest.approx(4140.0)


def test_inverse_phase_hold_yields_without_per_phase_safety_proof() -> None:
    """An inverse hold cannot replace a phase-safe plan without live proof."""
    rec = _rec(ev_power_w=4140.0)
    next_slot = _rec(
        start=SLOT_END,
        end=SLOT_END + timedelta(minutes=15),
        ev_power_w=6900.0,
    )
    harness = _Harness([rec, next_slot])
    harness._ev_last_command_w["ev"] = 1840.0
    live = _live(soc_pct=79.0)
    live.ev.power_w = 1840.0
    live.grid_phase_power_w = (5900.0, 3200.0, 3400.0)

    assert _run(
        harness,
        SLOT_START + timedelta(minutes=5),
        _cfg_switchable(main_fuse_amps=25.0),
        live,
    ) == pytest.approx(4140.0)


def test_idle_switchable_session_follows_phase_transition() -> None:
    """Phase hysteresis applies only while the charger is actively charging."""
    rec = _rec(ev_power_w=1840.0)
    harness = _Harness([rec])
    harness._ev_last_command_w["ev"] = 11_040.0

    published = _run(
        harness,
        SLOT_START + timedelta(minutes=5),
        _cfg_switchable(),
        _live(is_charging=False),
    )

    assert published == pytest.approx(1840.0)


def test_phase_hold_yields_to_live_fuse_headroom() -> None:
    """Fuse clamping can force the observed three-to-one transition immediately."""
    rec = _rec(ev_power_w=1840.0)
    next_slot = _rec(
        start=SLOT_END,
        end=SLOT_END + timedelta(hours=1),
        ev_power_w=6900.0,
    )
    harness = _Harness([rec, next_slot])
    harness._ev_last_command_w["ev"] = 11_040.0
    live = _live(soc_pct=73.7, house_w=25_070.0)
    live.ev_planned_load_deadline = SLOT_START + timedelta(hours=2, minutes=30)

    published = _run(
        harness, SLOT_START + timedelta(minutes=5), _cfg_switchable(), live
    )

    assert published == pytest.approx(1840.0)
    _, amps, phases = ocpp_charge_target(
        published, "three_phase_switchable", rated_current_a=16
    )
    assert amps == 8
    assert phases == 1


def test_material_phase_transition_savings_bypass_hold() -> None:
    """A materially cheaper alternative slot permits the planned phase transition."""
    now = SLOT_START + timedelta(minutes=5)
    live_slot = _rec(import_price=1.0, ev_power_w=1840.0)
    next_slot = _rec(
        start=SLOT_END,
        end=SLOT_END + timedelta(hours=1),
        import_price=0.05,
        ev_power_w=6900.0,
    )
    harness = _Harness([live_slot, next_slot])
    harness._ev_last_command_w["ev"] = 11_040.0
    live = _live(soc_pct=73.7)
    live.ev_planned_load_deadline = SLOT_START + timedelta(hours=2, minutes=30)

    assert _run(harness, now, _cfg_switchable(), live) == pytest.approx(1840.0)


def test_free_current_slot_bypasses_inverse_hold_for_material_savings() -> None:
    """Free current energy is not shifted later merely to retain one phase."""
    now = SLOT_START + timedelta(minutes=5)
    rec = _rec(import_price=0.0, ev_power_w=4140.0)
    next_slot = _rec(
        start=SLOT_END,
        end=SLOT_END + timedelta(minutes=15),
        import_price=1.0,
        ev_power_w=6900.0,
    )
    harness = _Harness([rec, next_slot])
    harness._ev_last_command_w["ev"] = 1840.0
    live = _live(soc_pct=79.0)
    live.ev.power_w = 1840.0
    live.grid_phase_power_w = (3000.0, 3200.0, 3400.0)

    assert _run(
        harness, now, _cfg_switchable(main_fuse_amps=25.0), live
    ) == pytest.approx(4140.0)


def test_phase_hold_yields_to_target_overshoot_protection() -> None:
    """The minimum three-phase hold cannot exceed the remaining target energy."""
    rec = _rec(ev_power_w=1840.0)
    harness = _Harness([rec])
    harness._ev_last_command_w["ev"] = 11_040.0

    published = _run(
        harness,
        SLOT_START + timedelta(minutes=5),
        _cfg_switchable(),
        _live(soc_pct=79.5),
    )

    assert published == pytest.approx(1840.0)


def test_inverse_phase_hold_yields_when_deadline_would_become_infeasible() -> None:
    """One-phase retention cannot sacrifice a physically reachable deadline."""
    rec = _rec(ev_power_w=4140.0)
    harness = _Harness([rec])
    harness._ev_last_command_w["ev"] = 1840.0

    published = _run(
        harness,
        SLOT_START + timedelta(minutes=5),
        _cfg_switchable(),
        _live(soc_pct=75.0),
    )

    assert published == pytest.approx(4140.0)
