"""The battery efficiency the planner plans with (issue #1431, stage 2).

``utils/planning_efficiency.py`` picks the configured values or the measured
round trip.  These tests pin the rules:

- off: the configured values, and the learner is never looked at;
- on: a measured round trip only when it has enough measurements, is
  plausible and is below the configured round trip;
- the AC-side monthly round trip goes before the battery-side one;
- the value does not jump: a trusted round trip is held when trust lapses,
  and one under the minimum is raised to it;
- only the round trip is used, so the battery's state of health, which the
  per-direction figures carry, cannot reach the planner;
- the learner dates its measurements and keeps the dates across a restart.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from custom_components.hsem.models.energy_tracker import (
    EnergyDayEntry,
    EnergyTracker,
)
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.planner.hindsight_scoring import SiteLimits
from custom_components.hsem.utils.efficiency_learner import (
    CHARGE,
    DISCHARGE,
    SIDE_AC,
    SIDE_DC,
    EfficiencyLearner,
    EfficiencyStep,
    HeldRoundTrip,
)
from custom_components.hsem.utils.planning_efficiency import (
    MAX_SAMPLE_AGE_DAYS,
    MIN_ROUND_TRIP_PCT,
    MIN_SAMPLES,
    REASON_DISABLED,
    REASON_IMPLAUSIBLE,
    REASON_LEARNED,
    REASON_LEARNED_AC,
    REASON_LEARNED_HELD,
    REASON_LEARNING,
    REASON_NOT_BELOW_CONFIGURED,
    SOURCE_CONFIGURED,
    SOURCE_LEARNED,
    planner_efficiency_attributes,
    resolve_planning_efficiency,
)

_NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
_ENERGY_KWH = 100.0


def _cfg(
    charge: float | None = 98.0, discharge: float | None = 98.0, *, enabled: bool = True
) -> SensorConfig:
    """Return a configuration with the option on unless told otherwise."""
    cfg = SensorConfig()
    cfg.batteries_charge_efficiency = cast(float, charge)
    cfg.batteries_discharge_efficiency = cast(float, discharge)
    cfg.batteries_learned_efficiency_enabled = enabled
    return cfg


def _learner(
    round_trip: float = 0.90,
    *,
    state_of_health: float = 1.0,
    samples: int = MIN_SAMPLES,
    discharge_samples: int | None = None,
    age_days: float | None = 1.0,
    discharge_age_days: float | None = None,
) -> EfficiencyLearner:
    """Return a learner that measured *round_trip* on a worn battery.

    Each direction is ``sqrt(round_trip)`` efficient.  Measured against the
    rated capacity, charging reads ``efficiency / state_of_health`` and
    discharging ``efficiency x state_of_health``.
    """
    one_way = math.sqrt(round_trip)
    learner = EfficiencyLearner(
        charge_total=EfficiencyStep(
            samples, _ENERGY_KWH * state_of_health / one_way, _ENERGY_KWH
        ),
        discharge_total=EfficiencyStep(
            samples if discharge_samples is None else discharge_samples,
            _ENERGY_KWH * one_way * state_of_health,
            _ENERGY_KWH,
        ),
    )
    if age_days is not None:
        learner.last_sample_at[CHARGE] = _NOW - timedelta(days=age_days)
        learner.last_sample_at[DISCHARGE] = _NOW - timedelta(
            days=age_days if discharge_age_days is None else discharge_age_days
        )
    return learner


class _Untouchable:
    """Stands in for a learner that must not be read."""

    def __getattribute__(self, name: str) -> Any:
        raise AssertionError(f"the learner was read: {name}")


class TestOff:
    """With the option off the planner input is what it was before."""

    def test_default_is_off(self) -> None:
        assert SensorConfig().batteries_learned_efficiency_enabled is False

    def test_configured_values_without_reading_the_learner(self) -> None:
        cfg = _cfg(97.0, 96.0, enabled=False)

        result = resolve_planning_efficiency(
            cfg, cast(EfficiencyLearner, _Untouchable()), _NOW
        )

        assert result.charge_pct == pytest.approx(97.0)
        assert result.discharge_pct == pytest.approx(96.0)
        assert result.basis is None

    def test_a_trusted_measurement_is_ignored(self) -> None:
        result = resolve_planning_efficiency(_cfg(enabled=False), _learner(), _NOW)

        assert result.charge_pct == pytest.approx(98.0)
        assert result.discharge_pct == pytest.approx(98.0)
        assert result.basis is None

    @pytest.mark.parametrize("enabled", [False, True])
    def test_unreadable_configured_value_falls_back_as_before(
        self, enabled: bool
    ) -> None:
        """The builder used 95 % for an unreadable value before this option."""
        result = resolve_planning_efficiency(
            _cfg(None, 0.0, enabled=enabled), None, _NOW
        )

        assert result.charge_pct == pytest.approx(95.0)
        assert result.discharge_pct == pytest.approx(95.0)
        assert result.basis is None

    def test_on_without_a_learner_is_the_configured_value(self) -> None:
        result = resolve_planning_efficiency(_cfg(), None, _NOW)

        assert result.charge_pct == pytest.approx(98.0)
        assert result.basis is None


class TestLearned:
    """A trusted round trip replaces the configured round trip."""

    def test_equal_configured_values_get_equal_halves(self) -> None:
        result = resolve_planning_efficiency(_cfg(), _learner(0.90), _NOW)

        assert result.charge_pct == pytest.approx(94.87, abs=0.005)
        assert result.discharge_pct == pytest.approx(94.87, abs=0.005)
        assert result.basis is not None
        assert result.basis["source"] == SOURCE_LEARNED
        assert result.basis["reason"] == REASON_LEARNED
        assert result.basis["charge_efficiency_pct"] == pytest.approx(result.charge_pct)
        assert result.basis["discharge_efficiency_pct"] == pytest.approx(
            result.discharge_pct
        )
        assert result.basis["round_trip_efficiency_pct"] == pytest.approx(
            90.0, abs=0.01
        )
        assert result.basis["configured_charge_efficiency_pct"] == pytest.approx(98.0)
        assert result.basis["configured_discharge_efficiency_pct"] == pytest.approx(
            98.0
        )
        assert result.basis["configured_round_trip_efficiency_pct"] == pytest.approx(
            96.04
        )
        assert result.basis["learned_round_trip_efficiency_pct"] == pytest.approx(90.0)
        assert result.basis["charge_samples"] == MIN_SAMPLES
        assert result.basis["discharge_samples"] == MIN_SAMPLES
        assert result.basis["sample_age_days"] == pytest.approx(1.0)
        assert result.basis["measured_side"] == SIDE_DC
        assert result.basis["raised_to_minimum"] is False
        assert result.basis["dc_round_trip_efficiency_pct"] == pytest.approx(90.0)
        assert result.basis["ac_round_trip_efficiency_pct"] is None
        assert result.basis["held_since"] == _NOW.isoformat()

    def test_the_configured_ratio_is_kept(self) -> None:
        """Both values are scaled by one factor: 100/90 stays 10/9."""
        result = resolve_planning_efficiency(_cfg(100.0, 90.0), _learner(0.81), _NOW)

        assert result.charge_pct / result.discharge_pct == pytest.approx(
            100.0 / 90.0, abs=1e-3
        )
        assert result.charge_pct * result.discharge_pct / 100.0 == pytest.approx(
            81.0, abs=0.02
        )
        assert result.charge_pct < 100.0

    @pytest.mark.parametrize("state_of_health", [0.7, 0.85, 1.0])
    def test_state_of_health_does_not_reach_the_planner(
        self, state_of_health: float
    ) -> None:
        """A worn battery reads high charging and low discharging.

        The planner's values are the same as for a new one.
        """
        worn = _learner(0.90, state_of_health=state_of_health)
        assert worn.charge_efficiency_pct == pytest.approx(
            math.sqrt(0.90) / state_of_health * 100.0
        )

        result = resolve_planning_efficiency(_cfg(), worn, _NOW)

        assert result.charge_pct == pytest.approx(94.87, abs=0.005)
        assert result.discharge_pct == pytest.approx(94.87, abs=0.005)

    def test_never_above_one_hundred_percent(self) -> None:
        result = resolve_planning_efficiency(_cfg(100.0, 100.0), _learner(0.99), _NOW)

        assert result.charge_pct <= 100.0
        assert result.discharge_pct <= 100.0
        assert result.basis is not None
        assert result.basis["source"] == SOURCE_LEARNED


class TestFallback:
    """Without trust the configured values stay, and the basis says why."""

    def _configured(self, learner: EfficiencyLearner, reason: str) -> dict[str, Any]:
        result = resolve_planning_efficiency(_cfg(97.0, 96.0), learner, _NOW)

        assert result.charge_pct == pytest.approx(97.0)
        assert result.discharge_pct == pytest.approx(96.0)
        assert result.basis is not None
        assert result.basis["source"] == SOURCE_CONFIGURED
        assert result.basis["reason"] == reason
        assert result.basis["charge_efficiency_pct"] == pytest.approx(97.0)
        assert result.basis["discharge_efficiency_pct"] == pytest.approx(96.0)
        return result.basis

    def test_empty_learner(self) -> None:
        basis = self._configured(EfficiencyLearner(), REASON_LEARNING)

        assert basis["learned_round_trip_efficiency_pct"] is None
        assert basis["sample_age_days"] is None

    def test_one_measurement_short(self) -> None:
        self._configured(_learner(samples=MIN_SAMPLES - 1), REASON_LEARNING)

    def test_rare_discharge_measurements_hold_it_back(self) -> None:
        """Plenty of charge measurements do not make up for few discharges."""
        self._configured(
            _learner(samples=200, discharge_samples=MIN_SAMPLES - 1), REASON_LEARNING
        )

    def test_a_round_trip_above_one_hundred_percent(self) -> None:
        basis = self._configured(_learner(1.05), REASON_IMPLAUSIBLE)

        assert basis["learned_round_trip_efficiency_pct"] is None

    @pytest.mark.parametrize("state_of_health", [0.45, 2.2])
    def test_an_implausible_direction_is_named_as_such(
        self, state_of_health: float
    ) -> None:
        """A direction outside 50-200 % is a fault, not a lack of samples."""
        self._configured(
            _learner(0.90, state_of_health=state_of_health), REASON_IMPLAUSIBLE
        )

    @pytest.mark.parametrize("round_trip", [0.97 * 0.96, 0.97, 0.999])
    def test_a_dc_round_trip_never_raises_the_efficiency(
        self, round_trip: float
    ) -> None:
        """The DC figure is a ceiling of what the meter sees."""
        basis = self._configured(_learner(round_trip), REASON_NOT_BELOW_CONFIGURED)

        assert basis["learned_round_trip_efficiency_pct"] == pytest.approx(
            round_trip * 100.0, abs=0.01
        )


def _used_round_trip(result: Any) -> float:
    """Return the round trip a result plans with (%)."""
    return float(result.charge_pct * result.discharge_pct / 100.0)


class TestHold:
    """A trusted round trip stays when trust lapses: no jump to configured."""

    @pytest.mark.parametrize(
        ("age_days", "discharge_age_days"),
        [(MAX_SAMPLE_AGE_DAYS + 1.0, None), (1.0, MAX_SAMPLE_AGE_DAYS + 1.0)],
    )
    def test_samples_passing_thirty_days_change_nothing(
        self, age_days: float, discharge_age_days: float | None
    ) -> None:
        """The day before and the day after plan with the same efficiency."""
        fresh = resolve_planning_efficiency(_cfg(), _learner(0.84), _NOW)
        old = resolve_planning_efficiency(
            _cfg(),
            _learner(0.84, age_days=age_days, discharge_age_days=discharge_age_days),
            _NOW,
        )

        assert fresh.basis is not None and old.basis is not None
        assert fresh.basis["reason"] == REASON_LEARNED
        assert old.basis["source"] == SOURCE_LEARNED
        assert old.basis["reason"] == REASON_LEARNED_HELD
        assert old.charge_pct == pytest.approx(fresh.charge_pct)
        assert old.discharge_pct == pytest.approx(fresh.discharge_pct)

    @pytest.mark.parametrize("age_days", [None, -1.0])
    def test_an_undated_file_with_enough_samples_is_used(
        self, age_days: float | None
    ) -> None:
        """A file from before the dates, or a backup: its sums are the value."""
        learner = _learner(0.84, age_days=age_days)

        result = resolve_planning_efficiency(_cfg(), learner, _NOW)

        assert result.basis is not None
        assert result.basis["reason"] == REASON_LEARNED_HELD
        assert _used_round_trip(result) == pytest.approx(84.0, abs=0.02)
        assert learner.held_round_trip is None
        assert result.basis["held_since"] is None

    def test_an_undated_file_with_too_few_samples_is_not(self) -> None:
        result = resolve_planning_efficiency(
            _cfg(), _learner(0.84, samples=MIN_SAMPLES - 1, age_days=None), _NOW
        )

        assert result.basis is not None
        assert result.basis["reason"] == REASON_LEARNING
        assert result.charge_pct == pytest.approx(98.0)

    def test_the_held_value_survives_an_implausible_measurement(self) -> None:
        """Trusted at 84 %, then the sums read above 100 %: 84 % stays."""
        learner = _learner(0.84)
        resolve_planning_efficiency(_cfg(), learner, _NOW)
        broken = _learner(1.05)
        broken.held_round_trip = learner.held_round_trip

        result = resolve_planning_efficiency(_cfg(), broken, _NOW + timedelta(days=3))

        assert result.basis is not None
        assert result.basis["reason"] == REASON_LEARNED_HELD
        assert result.basis["measured_side"] == SIDE_DC
        assert result.basis["held_since"] == _NOW.isoformat()
        assert _used_round_trip(result) == pytest.approx(84.0, abs=0.02)

    def test_the_held_value_survives_a_restart(self) -> None:
        learner = _learner(0.84)
        resolve_planning_efficiency(_cfg(), learner, _NOW)
        assert learner.dirty is True

        restored = EfficiencyLearner()
        restored.restore(learner.as_dict())

        held = restored.held_round_trip
        assert held is not None
        assert held.pct == pytest.approx(84.0)
        assert (held.side, held.at) == (SIDE_DC, _NOW)

    @pytest.mark.parametrize(
        "stored",
        [
            None,
            "84",
            {"pct": 84.0, "side": SIDE_DC},
            {"pct": 84.0, "side": "meter", "at": _NOW.isoformat()},
            {"pct": 184.0, "side": SIDE_DC, "at": _NOW.isoformat()},
            {"pct": 84.0, "side": SIDE_DC, "at": "2026-11-23T12:00:00"},
        ],
    )
    def test_an_unreadable_held_value_is_dropped(self, stored: Any) -> None:
        data = _learner(0.84).as_dict()
        data["planner_round_trip"] = stored

        restored = EfficiencyLearner()
        restored.restore(data)

        assert restored.held_round_trip is None
        assert restored.sample_count(CHARGE) == MIN_SAMPLES

    def test_it_is_saved_on_a_change_and_once_a_day(self) -> None:
        learner = EfficiencyLearner()

        learner.hold_round_trip(84.0, SIDE_DC, _NOW)
        assert learner.dirty is True
        learner._dirty = False

        learner.hold_round_trip(84.01, SIDE_DC, _NOW + timedelta(hours=1))
        assert learner.dirty is False
        assert learner.held_round_trip == HeldRoundTrip(84.0, SIDE_DC, _NOW)

        learner.hold_round_trip(84.2, SIDE_DC, _NOW + timedelta(hours=2))
        assert learner.dirty is True
        learner._dirty = False

        learner.hold_round_trip(84.2, SIDE_AC, _NOW + timedelta(hours=3))
        assert learner.dirty is True
        learner._dirty = False

        learner.hold_round_trip(84.2, SIDE_AC, _NOW + timedelta(days=1, hours=3))
        assert learner.dirty is True
        assert learner.held_round_trip is not None
        assert learner.held_round_trip.at == _NOW + timedelta(days=1, hours=3)


class TestMinimum:
    """A round trip under 70 % is raised to 70 %, not swapped for 96 %."""

    def test_raised_to_the_minimum(self) -> None:
        result = resolve_planning_efficiency(_cfg(), _learner(0.62), _NOW)

        assert result.basis is not None
        assert result.basis["source"] == SOURCE_LEARNED
        assert result.basis["reason"] == REASON_LEARNED
        assert result.basis["raised_to_minimum"] is True
        assert result.basis["learned_round_trip_efficiency_pct"] == pytest.approx(62.0)
        assert _used_round_trip(result) == pytest.approx(MIN_ROUND_TRIP_PCT, abs=0.02)

    def test_no_flip_sample_by_sample_around_the_minimum(self) -> None:
        """70.2, 69.8, 70.1, 69.9 %: the planner sees 70.2, 70, 70.1, 70."""
        learner = _learner(0.702)
        used = []
        for step, round_trip in enumerate([0.702, 0.698, 0.701, 0.699]):
            measured = _learner(round_trip)
            measured.held_round_trip = learner.held_round_trip
            learner = measured
            result = resolve_planning_efficiency(
                _cfg(), learner, _NOW + timedelta(hours=step)
            )
            assert result.basis is not None
            assert result.basis["source"] == SOURCE_LEARNED
            used.append(_used_round_trip(result))

        assert used == pytest.approx([70.2, 70.0, 70.1, 70.0], abs=0.02)
        assert max(used) - min(used) < 0.25

    def test_below_fifty_percent_with_nothing_held_is_a_fault(self) -> None:
        """The learner itself gives no round trip under 50 %."""
        result = resolve_planning_efficiency(_cfg(), _learner(0.45), _NOW)

        assert result.basis is not None
        assert result.basis["reason"] == REASON_IMPLAUSIBLE
        assert result.charge_pct == pytest.approx(98.0)


def _tracker(**months: float) -> EnergyTracker:
    """Return an energy tracker with an AC-side round trip per month.

    Keys are ``m2026_11``-style months, values the round trip as a fraction.
    Each month charged 100 kWh on the AC side of a 10 kWh battery.
    """
    tracker = EnergyTracker()
    for key, round_trip in months.items():
        day = f"{key[1:5]}-{key[6:8]}-10"
        tracker.daily_log[day] = EnergyDayEntry(
            date=day,
            ac_charged_kwh=100.0,
            ac_discharged_kwh=100.0 * round_trip,
            ac_power_hours=24.0,
            ac_hours=24.0,
            rated_capacity_kwh=10.0,
        )
    return tracker


class TestAcSide:
    """The AC-side monthly round trip goes before the battery-side one."""

    def test_the_current_month_is_used(self) -> None:
        learner = _learner(0.93)

        result = resolve_planning_efficiency(
            _cfg(), learner, _NOW, _tracker(m2026_11=0.88, m2026_10=0.80)
        )

        assert result.basis is not None
        assert result.basis["source"] == SOURCE_LEARNED
        assert result.basis["reason"] == REASON_LEARNED_AC
        assert result.basis["measured_side"] == SIDE_AC
        assert result.basis["ac_round_trip_efficiency_pct"] == pytest.approx(88.0)
        assert result.basis["ac_round_trip_month"] == "2026-11"
        assert result.basis["dc_round_trip_efficiency_pct"] == pytest.approx(93.0)
        assert _used_round_trip(result) == pytest.approx(88.0, abs=0.02)
        held = learner.held_round_trip
        assert held is not None
        assert held.pct == pytest.approx(88.0)
        assert (held.side, held.at) == (SIDE_AC, _NOW)

    def test_the_month_before_while_this_one_has_too_little(self) -> None:
        result = resolve_planning_efficiency(
            _cfg(), _learner(0.93), _NOW, _tracker(m2026_10=0.86)
        )

        assert result.basis is not None
        assert result.basis["reason"] == REASON_LEARNED_AC
        assert result.basis["ac_round_trip_month"] == "2026-10"
        assert _used_round_trip(result) == pytest.approx(86.0, abs=0.02)

    def test_it_needs_no_battery_side_measurement(self) -> None:
        result = resolve_planning_efficiency(
            _cfg(), EfficiencyLearner(), _NOW, _tracker(m2026_11=0.88)
        )

        assert result.basis is not None
        assert result.basis["reason"] == REASON_LEARNED_AC
        assert _used_round_trip(result) == pytest.approx(88.0, abs=0.02)

    @pytest.mark.parametrize(
        "tracker", [None, EnergyTracker(), _tracker(m2026_08=0.88)]
    )
    def test_without_it_the_battery_side_is_used(
        self, tracker: EnergyTracker | None
    ) -> None:
        """No AC sensor, or no month that passes: behaviour as before."""
        result = resolve_planning_efficiency(_cfg(), _learner(0.90), _NOW, tracker)

        assert result.basis is not None
        assert result.basis["reason"] == REASON_LEARNED
        assert result.basis["measured_side"] == SIDE_DC
        assert result.basis["ac_round_trip_month"] is None
        assert _used_round_trip(result) == pytest.approx(90.0, abs=0.02)

    def test_it_never_raises_the_efficiency_either(self) -> None:
        result = resolve_planning_efficiency(
            _cfg(), _learner(0.90), _NOW, _tracker(m2026_11=0.98)
        )

        assert result.basis is not None
        assert result.basis["source"] == SOURCE_CONFIGURED
        assert result.basis["reason"] == REASON_NOT_BELOW_CONFIGURED
        assert result.charge_pct == pytest.approx(98.0)

    def test_it_is_raised_to_the_minimum_too(self) -> None:
        result = resolve_planning_efficiency(
            _cfg(), _learner(0.90), _NOW, _tracker(m2026_11=0.60)
        )

        assert result.basis is not None
        assert result.basis["reason"] == REASON_LEARNED_AC
        assert result.basis["raised_to_minimum"] is True
        assert _used_round_trip(result) == pytest.approx(MIN_ROUND_TRIP_PCT, abs=0.02)

    def test_a_lapsed_ac_figure_is_held_below_a_fresh_battery_side_one(self) -> None:
        """Two months without an AC figure: no step up to the DC upper bound."""
        learner = _learner(0.93)
        resolve_planning_efficiency(_cfg(), learner, _NOW, _tracker(m2026_11=0.88))
        later = _NOW + timedelta(days=70)
        fresh = _learner(0.93)
        fresh.last_sample_at = {CHARGE: later, DISCHARGE: later}
        fresh.held_round_trip = learner.held_round_trip

        result = resolve_planning_efficiency(
            _cfg(), fresh, later, _tracker(m2026_11=0.88)
        )

        assert result.basis is not None
        assert result.basis["reason"] == REASON_LEARNED_HELD
        assert result.basis["measured_side"] == SIDE_AC
        assert result.basis["ac_round_trip_efficiency_pct"] is None
        assert _used_round_trip(result) == pytest.approx(88.0, abs=0.02)

    def test_a_fresh_battery_side_figure_below_the_held_ac_one_is_used(self) -> None:
        """The battery side is an upper bound: reading lower, it is the news."""
        learner = _learner(0.85)
        learner.held_round_trip = HeldRoundTrip(88.0, SIDE_AC, _NOW)

        result = resolve_planning_efficiency(_cfg(), learner, _NOW)

        assert result.basis is not None
        assert result.basis["reason"] == REASON_LEARNED
        assert _used_round_trip(result) == pytest.approx(85.0, abs=0.02)


class TestConsumersOfThePlannerInput:
    """What-if and performance scoring read the planner's own efficiency."""

    def test_site_limits_carry_the_learned_values(self) -> None:
        """``SiteLimits.from_planner_input`` feeds the what-if day record."""
        result = resolve_planning_efficiency(_cfg(), _learner(0.84), _NOW)

        site = SiteLimits.from_planner_input(
            PlannerInput(
                now_iso=_NOW.isoformat(),
                battery_charge_efficiency_pct=result.charge_pct,
                battery_discharge_efficiency_pct=result.discharge_pct,
            )
        )

        assert site.charge_efficiency * site.discharge_efficiency == pytest.approx(
            0.84, abs=2e-4
        )


class TestSensorAttributes:
    """What the sensor says the planner planned with."""

    def test_a_plan_made_with_the_option_off(self) -> None:
        attributes = planner_efficiency_attributes(None, _cfg(97.0, 96.0))

        assert attributes == {
            "planner_efficiency_source": SOURCE_CONFIGURED,
            "planner_efficiency_reason": REASON_DISABLED,
            "planner_charge_efficiency_pct": pytest.approx(97.0),
            "planner_discharge_efficiency_pct": pytest.approx(96.0),
            "planner_efficiency_measured_side": None,
        }

    def test_before_the_first_cycle(self) -> None:
        attributes = planner_efficiency_attributes(None, None)

        assert attributes["planner_efficiency_source"] == SOURCE_CONFIGURED
        assert attributes["planner_charge_efficiency_pct"] is None
        assert attributes["planner_discharge_efficiency_pct"] is None

    def test_a_plan_made_with_the_measured_round_trip(self) -> None:
        basis = resolve_planning_efficiency(_cfg(), _learner(0.90), _NOW).basis

        attributes = planner_efficiency_attributes(basis, _cfg())

        assert attributes == {
            "planner_efficiency_source": SOURCE_LEARNED,
            "planner_efficiency_reason": REASON_LEARNED,
            "planner_charge_efficiency_pct": pytest.approx(94.87, abs=0.005),
            "planner_discharge_efficiency_pct": pytest.approx(94.87, abs=0.005),
            "planner_efficiency_measured_side": SIDE_DC,
        }


def _measure(learner: EfficiencyLearner, power_w: float, start: datetime) -> datetime:
    """Feed one clean measurement at *power_w* and return its closing time."""
    soc = 50.0
    step = 1.0 if power_w > 0 else -1.0
    at = start
    for index in range(7):
        at = start + timedelta(minutes=5 * index)
        learner.update(at, power_w, soc + step * index, 10.0)
    return at


class TestMeasurementDates:
    """The learner dates the latest measurement of each direction."""

    def test_a_measurement_is_dated_per_direction(self) -> None:
        learner = EfficiencyLearner()

        charged_at = _measure(learner, 1200.0, _NOW)

        assert learner.sample_count(CHARGE) == 1
        assert learner.last_sample_at == {CHARGE: charged_at}

        discharged_at = _measure(learner, -1200.0, _NOW + timedelta(hours=2))

        assert learner.last_sample_at == {
            CHARGE: charged_at,
            DISCHARGE: discharged_at,
        }

    def test_dates_survive_a_restart(self) -> None:
        learner = EfficiencyLearner()
        charged_at = _measure(learner, 1200.0, _NOW)

        restored = EfficiencyLearner()
        restored.last_sample_at[DISCHARGE] = _NOW
        restored.restore(learner.as_dict())

        assert restored.last_sample_at == {CHARGE: charged_at}
        assert restored.sample_count(CHARGE) == 1

    @pytest.mark.parametrize(
        "stored", [None, "yesterday", 5, "2026-11-23T12:00:00", "missing"]
    )
    def test_an_unreadable_date_keeps_the_sums(self, stored: Any) -> None:
        """A file from before the field, or a damaged one, has no known age."""
        learner = EfficiencyLearner()
        _measure(learner, 1200.0, _NOW)
        data = learner.as_dict()
        if stored == "missing":
            del data[CHARGE]["last_sample_at"]
        else:
            data[CHARGE]["last_sample_at"] = stored

        restored = EfficiencyLearner()
        restored.restore(data)

        assert restored.sample_count(CHARGE) == 1
        assert restored.last_sample_at == {}
