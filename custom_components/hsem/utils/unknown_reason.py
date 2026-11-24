"""Why an HSEM sensor is unknown, sorted into four kinds (issue #1482).

A sensor that shows ``unknown`` gives its reason as a code in an attribute.
The code alone does not say whether HSEM is still collecting data, whether
the user has to do something, or whether something is broken.  This module
maps every code to one :class:`UnknownKind` and to a sentence for the log.

The codes are defined where they are set: ``models/energy_round_trip.py``,
``models/energy_tracker.py``, ``models/performance_record.py``,
``models/performance_tracker.py``, ``coordinator_performance.py`` and
``utils/efficiency_learner.py``.  They are repeated here as strings so this
module imports none of those, and ``tests/test_unknown_reason.py`` fails
when a ``REASON_*`` / ``STATUS_*`` constant there is missing from
:data:`UNKNOWN_REASONS`, or an entry here has no constant.

A code that is not in the registry counts as a fault.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class UnknownKind(StrEnum):
    """What an unknown sensor asks of the user."""

    #: HSEM is collecting data.  Nothing to do.
    WAITING = "waiting"
    #: An optional entity or option is not set.
    NOT_CONFIGURED = "not_configured"
    #: The user must fix the configuration or a source entity.
    ACTION_NEEDED = "action_needed"
    #: The data is implausible, or a computation failed.
    FAULT = "fault"


#: Log level of the line that reports a reason of each kind.
LOG_LEVELS: dict[UnknownKind, str] = {
    UnknownKind.WAITING: "info",
    UnknownKind.NOT_CONFIGURED: "info",
    UnknownKind.ACTION_NEEDED: "warning",
    UnknownKind.FAULT: "error",
}


@dataclass(frozen=True)
class UnknownReason:
    """One reason code's kind and its sentence for the log.

    Attributes:
        kind: What the reason asks of the user.
        sentence: What the code means, in one or two English sentences.
    """

    kind: UnknownKind
    sentence: str


#: Why the forecast accuracy sensor has no value: no slot is finalised yet.
REASON_NO_FINALISED_SLOTS = "no_finalised_slots"
#: Why the prediction accuracy sensor has no value: no record is scored yet.
REASON_NO_PREDICTION_RECORDS = "no_prediction_records"
#: Why the solar confidence sensor has no value: no hour has a factor yet.
REASON_NO_SOLAR_FACTORS = "no_solar_factors"

_WAITING = UnknownKind.WAITING
_NOT_CONFIGURED = UnknownKind.NOT_CONFIGURED
_ACTION_NEEDED = UnknownKind.ACTION_NEEDED
_FAULT = UnknownKind.FAULT

#: Every reason code a sensor can give for an unknown value.
UNKNOWN_REASONS: dict[str, UnknownReason] = {
    # --- Monthly energy sensors: models/energy_round_trip.py ---------------
    "no_data": UnknownReason(_WAITING, "The month has no recorded day yet."),
    # The battery power entity is optional, and an idle battery reports 0 W:
    # neither is a fault.
    "battery_power_not_measured": UnknownReason(
        _NOT_CONFIGURED,
        "No battery charge or discharge was measured this month. The battery "
        "power entity is not set, or the battery has not moved yet.",
    ),
    "ac_battery_power_not_configured": UnknownReason(
        _NOT_CONFIGURED, "No AC-side battery power sensor is configured."
    ),
    "ac_battery_power_unit_not_power": UnknownReason(
        _ACTION_NEEDED,
        "The AC-side battery power sensor has no power unit (W, kW). "
        "Pick a sensor that reports power.",
    ),
    "ac_battery_power_no_readings": UnknownReason(
        _ACTION_NEEDED,
        "The AC-side battery power sensor gave no usable reading this month. "
        "Check that it exists and reports a number.",
    ),
    "ac_soc_or_capacity_missing": UnknownReason(
        _ACTION_NEEDED,
        "The AC-side battery power sensor gave readings, but never together "
        "with a battery SoC and a rated capacity. Check those two entities.",
    ),
    "throughput_below_minimum": UnknownReason(
        _WAITING, "Too little energy has gone through the battery this month."
    ),
    # The SoC change is at most one capacity, the minimum charge is five: the
    # reason ends by itself as the month's charged energy grows.
    "soc_change_too_large": UnknownReason(
        _WAITING,
        "The battery's SoC has changed too much against the energy charged "
        "this month for a precise figure. It ends as more energy is charged.",
    ),
    "implausible_ratio": UnknownReason(
        _FAULT,
        "The measured round-trip efficiency is outside the plausible range. "
        "Check the sign and the unit of the battery power sensor.",
    ),
    # --- Monthly energy sensors: models/energy_tracker.py ------------------
    "consumption_not_measured": UnknownReason(
        _ACTION_NEEDED,
        "The house consumption was not measured this month. Check the house "
        "consumption power entity.",
    ),
    "consumption_below_minimum": UnknownReason(
        _WAITING, "Too little house consumption has been measured this month."
    ),
    "import_exceeds_consumption": UnknownReason(
        _FAULT,
        "The grid import is larger than the house consumption and the "
        "battery charge can explain. The consumption figure misses a load.",
    ),
    "battery_not_measured": UnknownReason(
        _ACTION_NEEDED,
        "No battery flow was known this month: neither a battery power nor "
        "a SoC with a rated capacity. Check the battery entities.",
    ),
    # No purchase price and expected cycles, and no configured cycle cost.
    "cycle_cost_unknown": UnknownReason(
        _NOT_CONFIGURED,
        "The battery throughput has no cycle cost. Set the purchase price "
        "and expected cycles, or a cycle cost.",
    ),
    # The grid import energy entity is optional.
    "grid_import_not_measured": UnknownReason(
        _NOT_CONFIGURED,
        "The grid import was not measured together with the house "
        "consumption. The grid import energy entity is not set or gave no "
        "readings.",
    ),
    "capacity_unknown": UnknownReason(
        _ACTION_NEEDED,
        "The usable battery capacity is unknown. Check the rated capacity "
        "and the SoC limit entities.",
    ),
    # --- Performance sensors: models/performance_record.py -----------------
    # The limits come from the planner input of the first plan.
    "no_site_limits": UnknownReason(
        _WAITING, "The installation's limits are known after the first plan."
    ),
    "insufficient_coverage": UnknownReason(
        _WAITING, "The last 24 hours are not recorded in full yet."
    ),
    "scipy_unavailable": UnknownReason(
        _FAULT, "SciPy could not be loaded, so the hindsight solve cannot run."
    ),
    "stored_energy_outside_limits": UnknownReason(
        _FAULT,
        "The recorded stored energy leaves the configured battery capacity. "
        "The meters contradict each other.",
    ),
    "energy_balance_residual": UnknownReason(
        _FAULT, "The meters leave too much energy unexplained to score the day."
    ),
    "oracle_unsolved": UnknownReason(_FAULT, "The hindsight solve gave no solution."),
    # By design: with almost nothing to gain, a share says nothing.
    "potential_below_threshold": UnknownReason(
        _WAITING, "There was almost nothing to gain in the last 24 hours."
    ),
    "breakdown_unsolved": UnknownReason(
        _FAULT, "The solve behind the battery and solar split failed."
    ),
    "no_ev_session": UnknownReason(
        _WAITING, "No managed EV session delivered energy in the last 24 hours."
    ),
    "ev_unsolved": UnknownReason(_FAULT, "The EV hindsight solves failed."),
    # --- Performance sensors: models/performance_tracker.py ----------------
    "not_evaluated": UnknownReason(
        _WAITING, "The first evaluation has not finished yet."
    ),
    # --- Performance sensors: coordinator_performance.py -------------------
    "evaluation_failed": UnknownReason(
        _FAULT, "The performance evaluation raised an error. See the log."
    ),
    # --- Battery efficiency sensor: utils/efficiency_learner.py ------------
    "learning": UnknownReason(
        _WAITING, "Too few charge and discharge measurements have been taken."
    ),
    "implausible": UnknownReason(
        _FAULT,
        "The measured round trip is outside the plausible range. Check the "
        "battery power, SoC and rated capacity entities.",
    ),
    # --- Accuracy sensors: this module -------------------------------------
    REASON_NO_FINALISED_SLOTS: UnknownReason(
        _WAITING, "No forecast slot has been compared with its actuals yet."
    ),
    REASON_NO_PREDICTION_RECORDS: UnknownReason(
        _WAITING, "No SoC prediction has been compared with its actual yet."
    ),
    REASON_NO_SOLAR_FACTORS: UnknownReason(
        _WAITING, "No hour has a solar accuracy factor yet."
    ),
}

#: What a code outside the registry is taken for.
UNMAPPED_REASON = UnknownReason(
    UnknownKind.FAULT, "HSEM has no description for this reason. Please report it."
)


def classify(code: str) -> UnknownReason:
    """Return the kind and the sentence of a reason code.

    Args:
        code: The reason code, as a sensor publishes it.

    Returns:
        The registry entry, or :data:`UNMAPPED_REASON` (a fault) for a code
        that is not in :data:`UNKNOWN_REASONS`.
    """
    return UNKNOWN_REASONS.get(code, UNMAPPED_REASON)


def reason_kind(code: str | None) -> str | None:
    """Return the kind of a reason code as a string, ``None`` without a code.

    Args:
        code: Why the sensor's value is unknown, or ``None`` when it has one.
    """
    return None if code is None else classify(code).kind.value


def unknown_reason_attributes(code: str | None) -> dict[str, Any]:
    """Return the ``reason`` and ``reason_kind`` attributes for a sensor.

    Args:
        code: Why the sensor's value is unknown, or ``None`` when it has one.
    """
    return {"reason": code, "reason_kind": reason_kind(code)}
