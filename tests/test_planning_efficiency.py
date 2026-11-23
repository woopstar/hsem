"""The battery efficiency the planner plans with (issue #1431, stage 2).

``utils/planning_efficiency.py`` picks the configured values or the measured
round trip.  These tests pin the rules:

- off: the configured values, and the learner is never looked at;
- on: the measured round trip only when it has enough measurements, is
  plausible, is recent and is below the configured round trip;
- only the round trip is used, so the battery's state of health, which the
  per-direction figures carry, cannot reach the planner;
- the learner dates its measurements and keeps the dates across a restart.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.efficiency_learner import (
    CHARGE,
    DISCHARGE,
    EfficiencyLearner,
    EfficiencyStep,
)
from custom_components.hsem.utils.planning_efficiency import (
    MAX_SAMPLE_AGE_DAYS,
    MIN_ROUND_TRIP_PCT,
    MIN_SAMPLES,
    REASON_DISABLED,
    REASON_IMPLAUSIBLE,
    REASON_LEARNED,
    REASON_LEARNING,
    REASON_NOT_BELOW_CONFIGURED,
    REASON_STALE,
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
        assert result.basis["measured_side"] == "battery_dc"

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

    def test_below_the_lowest_accepted_round_trip(self) -> None:
        basis = self._configured(
            _learner(MIN_ROUND_TRIP_PCT / 100.0 - 0.01), REASON_IMPLAUSIBLE
        )

        assert basis["learned_round_trip_efficiency_pct"] == pytest.approx(69.0)

    def test_a_round_trip_above_one_hundred_percent(self) -> None:
        basis = self._configured(_learner(1.05), REASON_IMPLAUSIBLE)

        assert basis["learned_round_trip_efficiency_pct"] is None

    def test_too_old(self) -> None:
        basis = self._configured(
            _learner(age_days=MAX_SAMPLE_AGE_DAYS + 1.0), REASON_STALE
        )

        assert basis["sample_age_days"] == pytest.approx(MAX_SAMPLE_AGE_DAYS + 1.0)

    def test_one_direction_too_old(self) -> None:
        self._configured(
            _learner(age_days=1.0, discharge_age_days=MAX_SAMPLE_AGE_DAYS + 1.0),
            REASON_STALE,
        )

    def test_no_dated_measurement(self) -> None:
        """A file from before the dates existed has no known age."""
        self._configured(_learner(age_days=None), REASON_STALE)

    def test_a_measurement_from_the_future(self) -> None:
        self._configured(_learner(age_days=-1.0), REASON_STALE)

    @pytest.mark.parametrize("round_trip", [0.97 * 0.96, 0.97, 0.999])
    def test_a_dc_round_trip_never_raises_the_efficiency(
        self, round_trip: float
    ) -> None:
        """The DC figure is a ceiling of what the meter sees."""
        basis = self._configured(_learner(round_trip), REASON_NOT_BELOW_CONFIGURED)

        assert basis["learned_round_trip_efficiency_pct"] == pytest.approx(
            round_trip * 100.0, abs=0.01
        )

    def test_at_the_lowest_accepted_round_trip(self) -> None:
        result = resolve_planning_efficiency(
            _cfg(), _learner(MIN_ROUND_TRIP_PCT / 100.0 + 1e-6), _NOW
        )

        assert result.basis is not None
        assert result.basis["source"] == SOURCE_LEARNED


class TestSensorAttributes:
    """What the sensor says the planner planned with."""

    def test_a_plan_made_with_the_option_off(self) -> None:
        attributes = planner_efficiency_attributes(None, _cfg(97.0, 96.0))

        assert attributes == {
            "planner_efficiency_source": SOURCE_CONFIGURED,
            "planner_efficiency_reason": REASON_DISABLED,
            "planner_charge_efficiency_pct": pytest.approx(97.0),
            "planner_discharge_efficiency_pct": pytest.approx(96.0),
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
