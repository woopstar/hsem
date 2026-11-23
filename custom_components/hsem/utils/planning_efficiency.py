"""Chooses the battery efficiency the planner plans with (issue #1431).

The planner takes one charge and one discharge efficiency per run.  They are
the configured values unless the user turns on
``hsem_batteries_learned_efficiency_enabled``.  Then a measured round trip
replaces the configured round trip.

Two measurements exist, and the first that is there is used:

- **AC side**: the energy tracker's monthly round trip from the user's
  AC-side battery power sensor (issue #1450).  It is the loss the grid meter
  sees, which is what the planner's efficiency means.
- **Battery (DC) side**: the round trip of the
  :class:`~custom_components.hsem.utils.efficiency_learner.EfficiencyLearner`.
  The inverter's AC/DC conversion loss is not in it, so it is an upper bound
  of the AC-side one.

Only a round trip is used.  The learner's charge and discharge figures are
measured against the rated capacity, so each carries the battery's state of
health; the factor cancels in their product alone.  A round trip cannot tell
its two halves apart, so both configured values are scaled by the same
factor until their product is the measured round trip.  The per-step curve is
not used: the planner needs one constant per direction, and the curve is not
an independent efficiency per power level.

A measured round trip can only lower the planner's efficiency.  When it is
not below the configured round trip, the configured values stay.

The value must not jump.  A measurement that was trusted once is held when
trust lapses (no AC figure any more, samples too old, an undated file), and a
round trip under :data:`MIN_ROUND_TRIP_PCT` is raised to it.  Both keep the
planner away from the configured value, which is the more optimistic one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from custom_components.hsem.models.energy_tracker import EnergyTracker, previous_month
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.conversion import convert_to_float
from custom_components.hsem.utils.efficiency_learner import (
    CHARGE,
    DISCHARGE,
    SIDE_AC,
    SIDE_DC,
    STATUS_IMPLAUSIBLE,
    STATUS_LEARNING,
    EfficiencyLearner,
)
from custom_components.hsem.utils.misc import clamp_efficiency, round_or_none

SOURCE_CONFIGURED = "configured"
SOURCE_LEARNED = "learned"

#: The option is off: the planner did not look at the measurement.
REASON_DISABLED = "disabled"
#: The battery-side (DC) round trip of the learner replaced the configured one.
REASON_LEARNED = "learned_round_trip"
#: The AC-side monthly round trip of the energy tracker replaced it.
REASON_LEARNED_AC = "learned_ac"
#: No measurement is trusted right now: the last trusted one is still used.
REASON_LEARNED_HELD = "learned_held"
#: Too few measurements in one of the two directions.
REASON_LEARNING = "learning"
#: A direction, or the round trip, is outside the range a battery can have.
REASON_IMPLAUSIBLE = "implausible"
#: The measured round trip is not below the configured one.
REASON_NOT_BELOW_CONFIGURED = "not_below_configured"

#: Measurements each direction needs before the planner uses the round trip.
#: Twice what the sensor needs to show it: with 16 measurements per direction
#: a simulated round trip of 94.1 % read between 93.2 % and 95.4 %.
MIN_SAMPLES = 40
#: Lowest round trip the planner plans with (%).  A lower measurement is
#: raised to it: falling back to the configured value would be a jump to the
#: optimistic side.
MIN_ROUND_TRIP_PCT = 70.0
#: Oldest latest measurement, per direction, that still counts as fresh
#: (days).  Older sums are used as a held value.
MAX_SAMPLE_AGE_DAYS = 30.0

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


@dataclass(frozen=True)
class _Measurement:
    """The round trip picked for one run."""

    pct: float | None
    """The measured round trip (%), or ``None`` when there is none."""

    side: str | None
    """:data:`SIDE_AC` or :data:`SIDE_DC`."""

    reason: str
    """Why this one, or why none."""


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


def _ac_round_trip(
    tracker: EnergyTracker | None, now: datetime
) -> tuple[float | None, str | None]:
    """Return the AC-side monthly round trip (%) and its month.

    The current month counts once it passes the energy tracker's own gates
    (throughput in both directions, a small SoC change, 50-100 %).  Until
    then the month before it is used.

    Args:
        tracker: The energy tracker, or ``None`` when there is none.
        now: Current local time.
    """
    if tracker is None:
        return None, None
    month = now.strftime("%Y-%m")
    for key in (month, previous_month(month)):
        pct = tracker.month_summary(key).get("ac_round_trip_efficiency")
        if pct is not None:
            return float(pct), key
    return None, None


def _dc_round_trip(
    learner: EfficiencyLearner, age_days: float | None
) -> tuple[float | None, str]:
    """Return the learner's round trip (%) and how far it can be trusted.

    Args:
        learner: The efficiency learner.
        age_days: Age of the older direction's latest measurement.

    Returns:
        The round trip with :data:`REASON_LEARNED` when its measurements
        are fresh, or with :data:`REASON_LEARNED_HELD` when they are old or
        undated.  ``None`` with the reason when it cannot be used.
    """
    if STATUS_IMPLAUSIBLE in (learner.status(CHARGE), learner.status(DISCHARGE)):
        return None, REASON_IMPLAUSIBLE
    samples = min(learner.sample_count(CHARGE), learner.sample_count(DISCHARGE))
    if learner.round_trip_status == STATUS_LEARNING or samples < MIN_SAMPLES:
        return None, REASON_LEARNING
    round_trip_pct = learner.round_trip_efficiency_pct
    if round_trip_pct is None:
        return None, REASON_IMPLAUSIBLE
    fresh = age_days is not None and 0.0 <= age_days <= MAX_SAMPLE_AGE_DAYS
    return round_trip_pct, REASON_LEARNED if fresh else REASON_LEARNED_HELD


def _pick(
    learner: EfficiencyLearner,
    ac_pct: float | None,
    dc_pct: float | None,
    dc_reason: str,
    now: datetime,
) -> _Measurement:
    """Return the round trip to plan with, and remember a trusted one.

    Order: a fresh AC-side figure, a fresh battery-side figure, the last
    trusted figure, battery-side sums that are old or undated.  A held
    AC-side figure stays ahead of a fresh battery-side one that reads
    higher, because the battery side is an upper bound of the AC side.

    Args:
        learner: The efficiency learner, which also keeps the held figure.
        ac_pct: The AC-side monthly round trip (%), or ``None``.
        dc_pct: The learner's round trip (%), or ``None``.
        dc_reason: How far *dc_pct* is trusted, or why it is ``None``.
        now: Current time (timezone-aware).
    """
    held = learner.held_round_trip
    if ac_pct is not None:
        learner.hold_round_trip(ac_pct, SIDE_AC, now)
        return _Measurement(ac_pct, SIDE_AC, REASON_LEARNED_AC)
    held_ac_is_lower = (
        held is not None and held.side == SIDE_AC and held.pct < (dc_pct or 0.0)
    )
    if dc_pct is not None and dc_reason == REASON_LEARNED and not held_ac_is_lower:
        learner.hold_round_trip(dc_pct, SIDE_DC, now)
        return _Measurement(dc_pct, SIDE_DC, REASON_LEARNED)
    if held is not None:
        return _Measurement(held.pct, held.side, REASON_LEARNED_HELD)
    if dc_pct is not None:
        return _Measurement(dc_pct, SIDE_DC, REASON_LEARNED_HELD)
    return _Measurement(None, None, dc_reason)


def resolve_planning_efficiency(
    cfg: SensorConfig,
    learner: EfficiencyLearner | None,
    now: datetime,
    energy_tracker: EnergyTracker | None = None,
) -> PlanningEfficiency:
    """Return the charge and discharge efficiency for one planner run.

    With the option off this returns the configured values before it looks
    at the learner, so the planner input is what it was without the option.

    With it on, a trusted round trip is remembered on the learner
    (:meth:`EfficiencyLearner.hold_round_trip`) and saved with it.

    Args:
        cfg: Current sensor configuration.
        learner: The efficiency learner, or ``None`` when there is none.
        now: Current local time (timezone-aware).
        energy_tracker: Source of the AC-side monthly round trip, or
            ``None``.

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
    ac_pct, ac_month = _ac_round_trip(energy_tracker, now)
    dc_pct, dc_reason = _dc_round_trip(learner, age_days)
    picked = _pick(learner, ac_pct, dc_pct, dc_reason, now)
    reason = picked.reason
    used_charge, used_discharge = charge, discharge
    clamped = False
    if picked.pct is not None:
        clamped = picked.pct < MIN_ROUND_TRIP_PCT
        round_trip = max(picked.pct, MIN_ROUND_TRIP_PCT) / 100.0
        if round_trip >= configured_round_trip - _EPSILON:
            reason = REASON_NOT_BELOW_CONFIGURED
        else:
            scale = math.sqrt(round_trip / configured_round_trip)
            used_charge = round(configured_charge * scale * 100.0, 2)
            used_discharge = round(configured_discharge * scale * 100.0, 2)
    learned = reason in (REASON_LEARNED, REASON_LEARNED_AC, REASON_LEARNED_HELD)
    held = learner.held_round_trip
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
            "learned_round_trip_efficiency_pct": round_or_none(picked.pct, 2),
            "raised_to_minimum": clamped,
            "measured_side": picked.side,
            "ac_round_trip_efficiency_pct": ac_pct,
            "ac_round_trip_month": ac_month,
            "dc_round_trip_efficiency_pct": round_or_none(
                learner.round_trip_efficiency_pct, 2
            ),
            "charge_samples": learner.sample_count(CHARGE),
            "discharge_samples": learner.sample_count(DISCHARGE),
            "sample_age_days": round_or_none(age_days, 1),
            "held_since": None if held is None else held.at.isoformat(),
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
        Source, reason, the charge and discharge efficiency in percent and
        the side of the inverter the measurement was taken on.
    """
    if basis is not None:
        return {
            "planner_efficiency_source": basis.get("source"),
            "planner_efficiency_reason": basis.get("reason"),
            "planner_charge_efficiency_pct": basis.get("charge_efficiency_pct"),
            "planner_discharge_efficiency_pct": basis.get("discharge_efficiency_pct"),
            "planner_efficiency_measured_side": basis.get("measured_side"),
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
        "planner_efficiency_measured_side": None,
    }
