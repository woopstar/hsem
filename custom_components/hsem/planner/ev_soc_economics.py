"""EV SoC economics — what charging to each target SoC would cost.

Single responsibility: for a given EV, answer "what would it cost to charge
to X% by deadline Y" for a small grid of target SoC values and deadlines
(the configured times of day plus the EV's own target deadline), by
re-running the existing pure :func:`run_planner` on a cloned
:class:`~custom_components.hsem.models.planner_input.PlannerInput` with only
that EV's target-SoC/deadline fields overridden.

Each row's cost is the EV's **charging cost**: the plan's money cost
(``PlannerOutput.plan_cost.total_cost``, which covers the whole house over
the whole horizon) minus that of one baseline plan per EV in which its target
is its current SoC, i.e. no target-driven charging (issue #1348).

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
from custom_components.hsem.utils.datetime_utils import as_tz, utc_key
from custom_components.hsem.utils.ev_economics_deadlines import (
    DEFAULT_ECONOMICS_DEADLINES,
    format_economics_deadline,
)
from custom_components.hsem.utils.misc import clamp_efficiency

#: Target SoC percentages evaluated by default, ascending.
DEFAULT_SOC_TARGETS: tuple[float, ...] = (50.0, 60.0, 70.0, 80.0, 100.0)


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


def effective_target_deadline(
    deadline: datetime | None, now: datetime
) -> datetime | None:
    """Return the EV's target deadline when it is still ahead of ``now``.

    A deadline that has passed (or was never set) gets no column in the
    table, so both the engine and the coordinator's refresh check read it
    through this one rule.
    """
    if deadline is None or utc_key(deadline) <= utc_key(now):
        return None
    return deadline


def deadline_columns(
    now: datetime,
    deadlines: Sequence[tuple[int, int]],
    target_deadline: datetime | None,
) -> list[tuple[str, datetime, bool]]:
    """Return the table's ``(label, deadline, is_target_deadline)`` columns.

    Each configured ``(hour, minute)`` resolves to its next occurrence via
    :func:`next_time_of_day`.  The target deadline, when given, is added at
    its exact datetime with its local ``HH:MM`` as the label; a configured
    deadline with the same label is the same time of day, so the two merge
    into one target column instead of appearing twice.  Columns are ordered
    by deadline.

    Args:
        now: Timezone-aware current datetime.
        deadlines: Configured ``(hour, minute)`` deadlines.
        target_deadline: The EV's target deadline, already filtered by
            :func:`effective_target_deadline`, or ``None``.

    Returns:
        One entry per column, earliest deadline first.
    """
    columns: dict[str, tuple[datetime, bool]] = {}
    for deadline in deadlines:
        columns.setdefault(
            format_economics_deadline(deadline),
            (next_time_of_day(now, *deadline), False),
        )
    if target_deadline is not None:
        label = as_tz(target_deadline, now.tzinfo).strftime("%H:%M")
        columns[label] = (target_deadline, True)
    return sorted(
        ((label, dl, is_target) for label, (dl, is_target) in columns.items()),
        key=lambda column: utc_key(column[1]),
    )


@dataclass
class EVSoCEconomicsPoint:
    """Cost of charging to one target SoC by one deadline.

    Attributes:
        target_soc_pct: Target SoC percentage evaluated (0-100).
        deadline_label: Human-readable deadline label (e.g. ``"08:00"``).
        deadline: Timezone-aware deadline datetime actually used.
        total_cost: EV charging cost of reaching this target by this
            deadline: ``plan_total_cost`` minus the EV's baseline plan cost
            (issue #1348). ``0.0`` when the target equals the current SoC.
            Not clamped: it can be slightly negative, because the planner
            minimises money plus the value of energy left in the house
            battery, not money alone.
        feasible: Whether the charger's rated power can physically reach
            this target by this deadline, independent of price.
        delta_from_previous: Cost delta vs. the previous (lower) target in
            the same deadline column. ``None`` for the first target.
        delta_per_10pct: ``delta_from_previous`` normalised to cost per
            10 percentage points of SoC, for comparing unevenly spaced
            targets. ``None`` when there is no previous target.
        is_target_deadline: Whether this column is the EV's own target
            deadline (issue #1344).
        plan_total_cost: Money cost of the whole plan (whole house, whole
            horizon) with this target and deadline, read from
            ``PlannerOutput.plan_cost.total_cost``. The baseline plan cost
            when the target equals the current SoC.
    """

    target_soc_pct: float
    deadline_label: str
    deadline: datetime
    total_cost: float = 0.0
    feasible: bool = True
    delta_from_previous: float | None = None
    delta_per_10pct: float | None = None
    is_target_deadline: bool = False
    plan_total_cost: float = 0.0


@dataclass
class EVSoCEconomicsResult:
    """Full cost/feasibility table for one EV across all target/deadline pairs.

    Attributes:
        state: One of ``"ready"``, ``"not_connected"``,
            ``"smart_charging_disabled"``, or ``STATE_UNAVAILABLE``.
        current_soc_pct: EV's current SoC at computation time, or ``None``
            when unavailable (issue #988) — the result is then an empty
            ``STATE_UNAVAILABLE`` table, never computed from a fabricated 0 %.
        points: Flat list of :class:`EVSoCEconomicsPoint`, one per retained
            (target_soc_pct, deadline_label) combination. Targets below the
            current SoC are omitted. Deliberately flat (not pre-grouped by
            deadline) — Jinja's ``groupby`` filter and any future consumer
            can group it either way.
        target_deadline: The EV's target deadline when it is ahead of the
            computation time, else ``None``. Set in every state, so the
            sensor always shows it (issue #1344).
        configured_deadlines: The configured ``(hour, minute)`` deadlines
            this table was computed for.
        baseline_plan_cost: Money cost of the whole plan with this EV's
            target at its current SoC (no target-driven charging), which
            every row's ``total_cost`` is measured against. ``None`` when no
            row was computed.
    """

    state: str = STATE_UNAVAILABLE
    current_soc_pct: float | None = None
    points: list[EVSoCEconomicsPoint] = field(default_factory=list)
    target_deadline: datetime | None = None
    configured_deadlines: tuple[tuple[int, int], ...] = ()
    baseline_plan_cost: float | None = None

    def as_attributes(self) -> dict[str, Any]:
        """Serialise to a flat HA sensor attributes dict."""
        return {
            "state": self.state,
            "current_soc_pct": (
                round(self.current_soc_pct, 1)
                if self.current_soc_pct is not None
                else None
            ),
            "target_deadline": (
                self.target_deadline.isoformat()
                if self.target_deadline is not None
                else None
            ),
            "configured_deadlines": [
                format_economics_deadline(d) for d in self.configured_deadlines
            ],
            "baseline_plan_cost": (
                round(self.baseline_plan_cost, 4)
                if self.baseline_plan_cost is not None
                else None
            ),
            "points": [
                {
                    "target_soc_pct": p.target_soc_pct,
                    "deadline_label": p.deadline_label,
                    "deadline": p.deadline.isoformat(),
                    "total_cost": round(p.total_cost, 4),
                    "plan_total_cost": round(p.plan_total_cost, 4),
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
                    "is_target_deadline": p.is_target_deadline,
                }
                for p in self.points
            ],
        }


def _plan_cost(
    base_input: PlannerInput,
    *,
    is_second: bool,
    target_soc_pct: float,
    deadline: datetime | None = None,
) -> float:
    """Return the plan's money cost with only this EV's target overridden.

    ``deadline`` replaces the EV's deadline when given; the baseline solve
    keeps the base input's deadline, which has no effect once the target is
    at or below the SoC (``engine_ev_milp`` maps a deadline to a slot only
    for an EV below its target).
    """
    clone = deepcopy(base_input)
    if is_second:
        clone.ev_second_planned_load_target_soc_pct = target_soc_pct
        if deadline is not None:
            clone.ev_second_planned_load_deadline = deadline
    else:
        clone.ev_planned_load_target_soc_pct = target_soc_pct
        if deadline is not None:
            clone.ev_planned_load_deadline = deadline
    output = run_planner(clone)
    return output.plan_cost.total_cost if output.plan_cost is not None else 0.0


def compute_ev_soc_economics(
    base_input: PlannerInput,
    *,
    is_second: bool,
    current_soc_pct: float | None,
    capacity_kwh: float,
    max_charge_kw: float,
    now: datetime,
    soc_targets: Sequence[float] | None = None,
    deadlines: Sequence[tuple[int, int]] | None = None,
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

    The deadline columns are the configured ``deadlines`` plus the EV's own
    target deadline (``ev_planned_load_deadline`` /
    ``ev_second_planned_load_deadline``) when it is still ahead of ``now``;
    see :func:`deadline_columns`.

    Guard clauses mirror :func:`~custom_components.hsem.planner.ev_planner.build_ev_charging_plan`'s
    early-outs so a disconnected/disabled EV never triggers an extra solve:

    - Not enabled → ``"smart_charging_disabled"``.
    - Not connected → ``"not_connected"``.
    - Smart charging disabled → ``"smart_charging_disabled"``.
    - Zero battery capacity or zero charger power → ``STATE_UNAVAILABLE``.
    - SoC unavailable (``None``) → ``STATE_UNAVAILABLE`` with empty points
      (issue #988) — the table is never computed from a fabricated 0 %.

    Each row's ``total_cost`` is its plan cost minus a baseline plan cost:
    one extra solve per EV with only its target SoC set to its current SoC
    (issue #1348). The baseline runs only when at least one row is kept.

    Targets below ``current_soc_pct`` are omitted. A target equal to the
    current SoC remains visible with cost ``0.0``; its plan cost is the
    baseline, so it triggers no solve of its own.

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
        deadlines: Configured ``(hour, minute)`` deadlines. Defaults to
            :data:`DEFAULT_ECONOMICS_DEADLINES` (08:00 and 17:00).

    Returns:
        An :class:`EVSoCEconomicsResult` with one point per retained
        (target, deadline) combination at or above the current SoC, or an
        empty-``points`` result when a guard clause short-circuits.
    """
    targets = tuple(soc_targets) if soc_targets is not None else DEFAULT_SOC_TARGETS
    configured = (
        tuple(deadlines) if deadlines is not None else DEFAULT_ECONOMICS_DEADLINES
    )

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

    target_deadline = effective_target_deadline(
        (
            base_input.ev_second_planned_load_deadline
            if is_second
            else base_input.ev_planned_load_deadline
        ),
        now,
    )

    result = EVSoCEconomicsResult(
        current_soc_pct=current_soc_pct,
        target_deadline=target_deadline,
        configured_deadlines=configured,
    )

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
    if current_soc_pct is None:
        result.state = STATE_UNAVAILABLE
        return result

    result.state = "ready"
    eff = clamp_efficiency(charger_efficiency_pct)
    baseline: float | None = None

    for deadline_label, deadline, is_target_deadline in deadline_columns(
        now, configured, target_deadline
    ):
        time_available_hours = max((deadline - now).total_seconds() / 3600.0, 0.0)
        max_deliverable_kwh = max_charge_kw * time_available_hours * eff

        previous_cost: float | None = None
        previous_target: float | None = None

        for target in targets:
            if target < current_soc_pct - 1e-9:
                continue

            energy_needed_kwh = max(
                (target - current_soc_pct) / 100.0 * capacity_kwh, 0.0
            )
            feasible = energy_needed_kwh <= max_deliverable_kwh + 1e-9

            if baseline is None:
                baseline = _plan_cost(
                    base_input, is_second=is_second, target_soc_pct=current_soc_pct
                )
                result.baseline_plan_cost = round(baseline, 4)
            if target <= current_soc_pct + 1e-9:
                plan_cost = baseline
            else:
                plan_cost = _plan_cost(
                    base_input,
                    is_second=is_second,
                    target_soc_pct=target,
                    deadline=deadline,
                )
            total_cost = plan_cost - baseline

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
                    is_target_deadline=is_target_deadline,
                    plan_total_cost=round(plan_cost, 4),
                )
            )
            previous_cost = total_cost
            previous_target = target

    return result
