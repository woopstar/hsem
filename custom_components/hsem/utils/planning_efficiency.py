"""Chooses the battery efficiency the planner plans with (issue #1431).

The planner takes one charge and one discharge efficiency per run.  They are
the configured values unless the user turns on
``hsem_batteries_learned_efficiency_enabled``.  Then the round trip the
:class:`~custom_components.hsem.utils.efficiency_learner.EfficiencyLearner`
measured replaces the configured round trip, when it can be trusted.

Only the round trip is used.  The learner's charge and discharge figures are
measured against the rated capacity, so each carries the battery's state of
health; the factor cancels in their product alone.  The round trip cannot
tell its two halves apart, so both configured values are scaled by the same
factor until their product is the measured round trip.  The per-step curve is
not used: the planner needs one constant per direction, and the curve is not
an independent efficiency per power level.

The battery power sensor reads at the battery (DC side).  The inverter's
AC/DC conversion loss is therefore not in the measurement, and the measured
round trip is an upper bound of the round trip the grid meter sees.  For that
reason a measured round trip can only lower the planner's efficiency.  When it
is not below the configured round trip, the configured values stay.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.conversion import convert_to_float
from custom_components.hsem.utils.efficiency_learner import (
    CHARGE,
    DISCHARGE,
    STATUS_LEARNING,
    EfficiencyLearner,
)
from custom_components.hsem.utils.misc import clamp_efficiency, round_or_none

SOURCE_CONFIGURED = "configured"
SOURCE_LEARNED = "learned"

#: The option is off: the planner did not look at the measurement.
REASON_DISABLED = "disabled"
#: The measured round trip replaced the configured one.
REASON_LEARNED = "learned_round_trip"
#: Too few measurements in one of the two directions.
REASON_LEARNING = "learning"
#: The measured round trip is outside the range a battery can have.
REASON_IMPLAUSIBLE = "implausible"
#: The latest measurement of one direction is too old, or its age is unknown.
REASON_STALE = "stale"
#: The measured (DC side) round trip is not below the configured one.
REASON_NOT_BELOW_CONFIGURED = "not_below_configured"

#: Measurements each direction needs before the planner uses the round trip.
#: Twice what the sensor needs to show it: with 16 measurements per direction
#: a simulated round trip of 94.1 % read between 93.2 % and 95.4 %.
MIN_SAMPLES = 40
#: Lowest measured round trip the planner accepts (%).  Below it the reading
#: points at a sensor problem, not at the battery.
MIN_ROUND_TRIP_PCT = 70.0
#: Oldest latest measurement, per direction, the planner accepts (days).
MAX_SAMPLE_AGE_DAYS = 30.0
#: Side of the inverter the battery power is measured on.
MEASURED_SIDE = "battery_dc"

_FALLBACK_PCT = 95.0
_EPSILON = 1e-9


@dataclass(frozen=True)
class PlanningEfficiency:
    """The efficiencies of one planner run and where they came from."""

    charge_pct: float
    """Charge efficiency the planner uses (%)."""

    discharge_pct: float
    """Discharge efficiency the planner uses (%)."""

    basis: dict[str, Any] | None = None
    """Source, reason and numbers for the plan explanation.  ``None`` while
    the option is off."""


def _sample_age_days(learner: EfficiencyLearner, now: datetime) -> float | None:
    """Return the age of the older direction's latest measurement in days.

    Args:
        learner: The efficiency learner.
        now: Current time (timezone-aware).

    Returns:
        The age in days, or ``None`` when a direction has no dated
        measurement.
    """
    charge_at = learner.last_sample_at.get(CHARGE)
    discharge_at = learner.last_sample_at.get(DISCHARGE)
    if charge_at is None or discharge_at is None:
        return None
    return (now - min(charge_at, discharge_at)).total_seconds() / 86400.0


def _trust(learner: EfficiencyLearner, age_days: float | None) -> str:
    """Return :data:`REASON_LEARNED` when the round trip can be trusted.

    Args:
        learner: The efficiency learner.
        age_days: Age of the older direction's latest measurement.

    Returns:
        :data:`REASON_LEARNED`, or the reason the round trip is not used.
    """
    samples = min(learner.sample_count(CHARGE), learner.sample_count(DISCHARGE))
    if learner.round_trip_status == STATUS_LEARNING or samples < MIN_SAMPLES:
        return REASON_LEARNING
    round_trip_pct = learner.round_trip_efficiency_pct
    if round_trip_pct is None or round_trip_pct < MIN_ROUND_TRIP_PCT:
        return REASON_IMPLAUSIBLE
    if age_days is None or not 0.0 <= age_days <= MAX_SAMPLE_AGE_DAYS:
        return REASON_STALE
    return REASON_LEARNED


def resolve_planning_efficiency(
    cfg: SensorConfig, learner: EfficiencyLearner | None, now: datetime
) -> PlanningEfficiency:
    """Return the charge and discharge efficiency for one planner run.

    With the option off this returns the configured values before it looks
    at the learner, so the planner input is what it was without the option.

    Args:
        cfg: Current sensor configuration.
        learner: The efficiency learner, or ``None`` when there is none.
        now: Current time (timezone-aware).

    Returns:
        The efficiencies in percent, with their basis when the option is on.
    """
    charge = convert_to_float(cfg.batteries_charge_efficiency) or _FALLBACK_PCT
    discharge = convert_to_float(cfg.batteries_discharge_efficiency) or _FALLBACK_PCT
    if cfg.batteries_learned_efficiency_enabled is not True or learner is None:
        return PlanningEfficiency(charge, discharge)

    configured_charge = clamp_efficiency(charge)
    configured_discharge = clamp_efficiency(discharge)
    configured_round_trip = configured_charge * configured_discharge
    age_days = _sample_age_days(learner, now)
    reason = _trust(learner, age_days)
    learned_pct = learner.round_trip_efficiency_pct
    used_charge, used_discharge = charge, discharge
    if reason == REASON_LEARNED and learned_pct is not None:
        learned_round_trip = learned_pct / 100.0
        if learned_round_trip >= configured_round_trip - _EPSILON:
            reason = REASON_NOT_BELOW_CONFIGURED
        else:
            scale = math.sqrt(learned_round_trip / configured_round_trip)
            used_charge = round(configured_charge * scale * 100.0, 2)
            used_discharge = round(configured_discharge * scale * 100.0, 2)
    learned = reason == REASON_LEARNED
    return PlanningEfficiency(
        used_charge,
        used_discharge,
        {
            "source": SOURCE_LEARNED if learned else SOURCE_CONFIGURED,
            "reason": reason,
            "charge_efficiency_pct": used_charge,
            "discharge_efficiency_pct": used_discharge,
            "round_trip_efficiency_pct": round(
                clamp_efficiency(used_charge) * clamp_efficiency(used_discharge) * 100,
                2,
            ),
            "configured_charge_efficiency_pct": charge,
            "configured_discharge_efficiency_pct": discharge,
            "configured_round_trip_efficiency_pct": round(
                configured_round_trip * 100.0, 2
            ),
            "learned_round_trip_efficiency_pct": round_or_none(learned_pct, 2),
            "charge_samples": learner.sample_count(CHARGE),
            "discharge_samples": learner.sample_count(DISCHARGE),
            "sample_age_days": round_or_none(age_days, 1),
            "measured_side": MEASURED_SIDE,
        },
    )


def planner_efficiency_attributes(
    basis: dict[str, Any] | None, cfg: SensorConfig | None
) -> dict[str, Any]:
    """Return the sensor attributes that say what the planner planned with.

    Args:
        basis: ``PlanExplanation.battery_efficiency`` of the plan in force,
            or ``None`` when that plan was made with the option off.
        cfg: Current sensor configuration, or ``None`` before the first cycle.

    Returns:
        Source, reason and the charge and discharge efficiency in percent.
    """
    if basis is not None:
        return {
            "planner_efficiency_source": basis.get("source"),
            "planner_efficiency_reason": basis.get("reason"),
            "planner_charge_efficiency_pct": basis.get("charge_efficiency_pct"),
            "planner_discharge_efficiency_pct": basis.get("discharge_efficiency_pct"),
        }
    return {
        "planner_efficiency_source": SOURCE_CONFIGURED,
        "planner_efficiency_reason": REASON_DISABLED,
        "planner_charge_efficiency_pct": (
            convert_to_float(cfg.batteries_charge_efficiency) if cfg else None
        ),
        "planner_discharge_efficiency_pct": (
            convert_to_float(cfg.batteries_discharge_efficiency) if cfg else None
        ),
    }
