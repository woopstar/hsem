"""EV SoC economics — real-money cost of charging to each target SoC.

Single responsibility: for a given EV, answer "what would it cost to charge
to X% by deadline Y" for a small grid of target SoC values and deadlines, by
re-running the existing pure :func:`run_planner` on a cloned
:class:`~custom_components.hsem.models.planner_input.PlannerInput` with only
that EV's target-SoC/deadline fields overridden.

No planner/MILP semantics change — this module only calls ``run_planner``
with different inputs and reads ``PlannerOutput.plan_cost.total_cost`` back
out per combination.  All functions are pure (no I/O, no HA imports beyond
the ``STATE_UNAVAILABLE`` string constant, mirroring ``ev_planner_models.py``).
"""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE

from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.planner.engine_core import run_planner
from custom_components.hsem.utils.misc import clamp_efficiency

#: Target SoC percentages evaluated by default, ascending.
DEFAULT_SOC_TARGETS: tuple[float, ...] = (50.0, 60.0, 70.0, 80.0, 100.0)

#: (label, hour) pairs for the deadlines evaluated by default.
_DEADLINES: tuple[tuple[str, int], ...] = (("08:00", 8), ("17:00", 17))


def next_time_of_day(now: datetime, hour: int, minute: int = 0) -> datetime:
    """Return the next occurrence of ``hour:minute`` at or after ``now``.

    Uses today's date when the time-of-day has not yet passed, otherwise
    tomorrow's date.  ``now`` must be timezone-aware; the returned datetime
    carries the same timezone.

    Args:
        now: Timezone-aware current datetime.
        hour: Hour of day (0-23).
        minute: Minute of hour (0-59). Defaults to 0.

    Returns:
        Timezone-aware datetime strictly after ``now``... or equal to ``now``
        when ``now`` lands exactly on the requested time-of-day (still valid,
        not "already passed").
    """
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate < now:
        candidate += timedelta(days=1)
    return candidate


@dataclass
class EVSoCEconomicsPoint:
    """Cost of charging to one target SoC by one deadline.

    Attributes:
        target_soc_pct: Target SoC percentage evaluated (0-100).
        deadline_label: Human-readable deadline label (e.g. ``"08:00"``).
        deadline: Timezone-aware deadline datetime actually used.
        total_cost: Real-money cost of reaching this target by this
            deadline, read from ``PlannerOutput.plan_cost.total_cost``.
            ``0.0`` when the target is already met (no solve is triggered).
        feasible: Whether the charger's rated power can physically reach
            this target by this deadline, independent of price.
        delta_from_previous: Cost delta vs. the previous (lower) target in
            the same deadline column. ``None`` for the first target.
        delta_per_10pct: ``delta_from_previous`` normalised to cost per
            10 percentage points of SoC, for comparing unevenly spaced
            targets. ``None`` when there is no previous target.
    """

    target_soc_pct: float
    deadline_label: str
    deadline: datetime
    total_cost: float = 0.0
    feasible: bool = True
    delta_from_previous: float | None = None
    delta_per_10pct: float | None = None


@dataclass
class EVSoCEconomicsResult:
    """Full cost/feasibility table for one EV across all target/deadline pairs.

    Attributes:
        state: One of ``"ready"``, ``"not_connected"``,
            ``"smart_charging_disabled"``, or ``STATE_UNAVAILABLE``.
        current_soc_pct: EV's current SoC at computation time.
        points: Flat list of :class:`EVSoCEconomicsPoint`, one per
            (target_soc_pct, deadline_label) combination.  Deliberately flat
            (not pre-grouped by deadline) — Jinja's ``groupby`` filter and
            any future consumer can group it either way.
    """

    state: str = STATE_UNAVAILABLE
    current_soc_pct: float = 0.0
    points: list[EVSoCEconomicsPoint] = field(default_factory=list)

    def as_attributes(self) -> dict[str, Any]:
        """Serialise to a flat HA sensor attributes dict."""
        return {
            "state": self.state,
            "current_soc_pct": round(self.current_soc_pct, 1),
            "points": [
                {
                    "target_soc_pct": p.target_soc_pct,
                    "deadline_label": p.deadline_label,
                    "deadline": p.deadline.isoformat(),
                    "total_cost": round(p.total_cost, 4),
                    "feasible": p.feasible,
                    "delta_from_previous": (
                        round(p.delta_from_previous, 4)
                        if p.delta_from_previous is not None
                        else None
                    ),
                    "delta_per_10pct": (
                        round(p.delta_per_10pct, 4)
                        if p.delta_per_10pct is not None
                        else None
                    ),
                }
                for p in self.points
            ],
        }


def compute_ev_soc_economics(
    base_input: PlannerInput,
    *,
    is_second: bool,
    current_soc_pct: float,
    capacity_kwh: float,
    max_charge_kw: float,
    now: datetime,
    soc_targets: Sequence[float] | None = None,
) -> EVSoCEconomicsResult:
    """Compute the cost of charging an EV to each target SoC by each deadline.

    For every (target_soc_pct, deadline) combination, ``base_input`` is
    cloned and only that EV's target-SoC/deadline fields are overridden
    (``ev_planned_load_target_soc_pct`` / ``ev_planned_load_deadline``, or
    the ``ev_second_*`` equivalents when ``is_second=True``) before
    re-running the pure :func:`run_planner`.  No other planner input changes
    — the rest of ``base_input`` (prices, PV forecast, battery state, other
    EV, etc.) is reused verbatim, so the result reflects "what would it cost
    to change *only* this EV's target/deadline right now".

    Guard clauses mirror :func:`~custom_components.hsem.planner.ev_planner.build_ev_charging_plan`'s
    early-outs so a disconnected/disabled EV never triggers an extra solve:

    - Not enabled → ``"smart_charging_disabled"``.
    - Not connected → ``"not_connected"``.
    - Smart charging disabled → ``"smart_charging_disabled"``.
    - Zero battery capacity or zero charger power → ``STATE_UNAVAILABLE``.

    Targets at or below ``current_soc_pct`` cost ``0.0`` and do not trigger
    a ``run_planner()`` solve.

    Args:
        base_input: The coordinator's last-used planner input, used as the
            template for every clone. Not mutated.
        is_second: When ``True``, read/override the ``ev_second_*`` fields
            instead of the primary EV fields.
        current_soc_pct: The EV's current SoC (0-100) at computation time.
        capacity_kwh: EV battery nameplate capacity in kWh.
        max_charge_kw: Charger AC output power in kW, used only for the
            price-independent feasibility check.
        now: Timezone-aware current datetime, used to compute deadlines and
            the time available to charge.
        soc_targets: Target SoC percentages to evaluate. Defaults to
            :data:`DEFAULT_SOC_TARGETS` (50/60/70/80/100 %).

    Returns:
        An :class:`EVSoCEconomicsResult` with one point per
        (target, deadline) combination, or an empty-``points`` result when a
        guard clause short-circuits.
    """
    targets = tuple(soc_targets) if soc_targets is not None else DEFAULT_SOC_TARGETS

    enabled = (
        base_input.ev_second_planned_load_enabled
        if is_second
        else base_input.ev_planned_load_enabled
    )
    connected = (
        base_input.ev_second_planned_load_connected
        if is_second
        else base_input.ev_planned_load_connected
    )
    smart_charging_enabled = (
        base_input.ev_second_planned_load_smart_charging_enabled
        if is_second
        else base_input.ev_planned_load_smart_charging_enabled
    )
    charger_efficiency_pct = (
        base_input.ev_second_planned_load_charger_efficiency_pct
        if is_second
        else base_input.ev_planned_load_charger_efficiency_pct
    )

    result = EVSoCEconomicsResult(current_soc_pct=current_soc_pct)

    if not enabled:
        result.state = "smart_charging_disabled"
        return result
    if not connected:
        result.state = "not_connected"
        return result
    if not smart_charging_enabled:
        result.state = "smart_charging_disabled"
        return result
    if capacity_kwh <= 0 or max_charge_kw <= 0:
        result.state = STATE_UNAVAILABLE
        return result

    result.state = "ready"
    eff = clamp_efficiency(charger_efficiency_pct)

    for deadline_label, hour in _DEADLINES:
        deadline = next_time_of_day(now, hour)
        time_available_hours = max((deadline - now).total_seconds() / 3600.0, 0.0)
        max_deliverable_kwh = max_charge_kw * time_available_hours * eff

        previous_cost: float | None = None
        previous_target: float | None = None

        for target in targets:
            energy_needed_kwh = max(
                (target - current_soc_pct) / 100.0 * capacity_kwh, 0.0
            )
            feasible = energy_needed_kwh <= max_deliverable_kwh + 1e-9

            if target <= current_soc_pct + 1e-9:
                total_cost = 0.0
            else:
                clone = deepcopy(base_input)
                if is_second:
                    clone.ev_second_planned_load_target_soc_pct = target
                    clone.ev_second_planned_load_deadline = deadline
                else:
                    clone.ev_planned_load_target_soc_pct = target
                    clone.ev_planned_load_deadline = deadline
                output = run_planner(clone)
                total_cost = (
                    output.plan_cost.total_cost if output.plan_cost is not None else 0.0
                )

            delta_from_previous: float | None = None
            delta_per_10pct: float | None = None
            if previous_cost is not None and previous_target is not None:
                delta_from_previous = total_cost - previous_cost
                target_delta = target - previous_target
                if target_delta > 1e-9:
                    delta_per_10pct = delta_from_previous / target_delta * 10.0

            result.points.append(
                EVSoCEconomicsPoint(
                    target_soc_pct=target,
                    deadline_label=deadline_label,
                    deadline=deadline,
                    total_cost=round(total_cost, 4),
                    feasible=feasible,
                    delta_from_previous=delta_from_previous,
                    delta_per_10pct=delta_per_10pct,
                )
            )
            previous_cost = total_cost
            previous_target = target

    return result
