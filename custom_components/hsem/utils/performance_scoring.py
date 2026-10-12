"""Score recorded slots for the HSEM performance sensors (issue #1182).

Turns the per-slot actuals of ``models/performance_record.py`` into the
realized window the shared hindsight scoring works on
(``planner/hindsight_scoring.py``), and refuses to score anything it cannot
stand behind: a run with a gap, limits that do not describe the battery, or
meters that contradict each other get a reason and no number.

Pure functions with no Home Assistant imports: the coordinator runs
:func:`evaluate_performance` in the executor.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from custom_components.hsem.models.performance_record import (
    REASON_ENERGY_BALANCE_RESIDUAL,
    REASON_INSUFFICIENT_COVERAGE,
    REASON_NO_SITE_LIMITS,
    REASON_ORACLE_UNSOLVED,
    REASON_SCIPY_UNAVAILABLE,
    REASON_STORED_ENERGY_OUTSIDE_LIMITS,
    PerformanceScore,
    PerformanceSlot,
)
from custom_components.hsem.planner._scipy_probe import is_scipy_available
from custom_components.hsem.planner.hindsight_ev import EvScore, ev_sessions, score_ev
from custom_components.hsem.planner.hindsight_scoring import (
    LIMIT_NOTE_SHARE,
    RealizedSlot,
    RealizedWindow,
    SiteLimits,
    battery_flows_from_soc,
    realized_window,
    score_window,
)

#: Least observed share of a slot before the run it is in can be scored.
MIN_SLOT_COVERAGE = 0.9

#: Energy the meters may leave unexplained before a run is not scored: the
#: largest of a fixed amount, a share of the usable capacity and a share of
#: the PV produced.  One step of a 1 % SoC reading is 0.1 kWh on a 10 kWh
#: battery, so a consistent installation stays well below all three.
RESIDUAL_MIN_KWH = 1.0
RESIDUAL_CAPACITY_SHARE = 0.15
RESIDUAL_PV_SHARE = 0.10


@dataclass(frozen=True)
class PerformanceRequest:
    """Everything one evaluation needs, detached from the live tracker.

    Attributes:
        slots: The recorded slots by UTC start.
        slot_minutes: Slot width the runs are expected in.
        window: UTC slot starts of the trailing window, in order.
        days: Local days to score, each with its UTC slot starts in order.
        site: The installation's hard limits, or ``None`` before the first plan.
        time_limit_s: Wall-clock limit for each oracle solve.
    """

    slots: Mapping[datetime, PerformanceSlot]
    slot_minutes: int
    window: tuple[datetime, ...]
    days: tuple[tuple[str, tuple[datetime, ...]], ...]
    site: SiteLimits | None
    time_limit_s: float | None = None


@dataclass(frozen=True)
class PerformanceEvaluation:
    """The outcome of one evaluation.

    Attributes:
        window: Score of the trailing window.
        days: Scores of the local days that were asked for.
    """

    window: PerformanceScore
    days: tuple[PerformanceScore, ...] = ()


def realized_slots(
    slots: Sequence[PerformanceSlot], site: SiteLimits
) -> list[RealizedSlot]:
    """Return consecutive recorded slots in the form the scoring takes.

    Each slot's battery flow runs from its own starting SoC to the next
    slot's, so the flows telescope to the last slot's ending SoC whatever a
    short gap inside a slot left out.

    Args:
        slots: Consecutive recorded slots whose SoC readings are present.
        site: The installation's hard limits.

    Returns:
        One realized slot per recorded slot.
    """
    realized: list[RealizedSlot] = []
    for index, slot in enumerate(slots):
        following = slots[index + 1].soc_start_pct if index + 1 < len(slots) else None
        soc_to = following if following is not None else slot.soc_end_pct
        charged, discharged = battery_flows_from_soc(
            slot.soc_start_pct or 0.0, soc_to or 0.0, site
        )
        realized.append(
            RealizedSlot(
                hours=slot.minutes / 60.0,
                grid_import_kwh=slot.grid_import_kwh,
                grid_export_kwh=slot.grid_export_kwh,
                charged_kwh=charged,
                discharged_kwh=discharged,
                import_price=slot.import_price,
                export_price=slot.export_price,
                pv_kwh=slot.pv_kwh,
            )
        )
    return realized


def unexplained_energy_kwh(slots: Sequence[RealizedSlot]) -> float | None:
    """Return the energy the meters cannot explain, or ``None`` if unknown.

    A slot's load is its net load plus its PV, and a load cannot be negative.
    The running total of the load must therefore never fall; the largest fall
    is energy the house would have had to produce.  A single noisy slot
    cancels against its neighbours, so this stays near zero on consistent
    meters and grows when a meter, a unit or the battery capacity is wrong.

    Args:
        slots: The run's realized slots.

    Returns:
        The largest fall of the cumulative load in kWh, or ``None`` when PV
        was not observed in every slot.
    """
    total = peak = fall = 0.0
    for slot in slots:
        if slot.pv_kwh is None:
            return None
        total += slot.net_load_kwh + slot.pv_kwh
        peak = max(peak, total)
        fall = max(fall, peak - total)
    return fall


def _residual_limit_kwh(window: RealizedWindow, slots: Sequence[RealizedSlot]) -> float:
    """Return the unexplained energy above which a run is not scored."""
    usable = window.configured.max_stored_kwh - window.configured.min_stored_kwh
    produced = sum(slot.pv_kwh or 0.0 for slot in slots)
    return max(
        RESIDUAL_MIN_KWH,
        RESIDUAL_CAPACITY_SHARE * usable,
        RESIDUAL_PV_SHARE * produced,
    )


def _score_ev(
    slots: Sequence[PerformanceSlot],
    window: RealizedWindow,
    time_limit_s: float | None,
) -> EvScore | None:
    """Return the EV timing costs of a run, or ``None`` without a charger record.

    Sessions are found per charger and scored together: the chargers share
    the grid and the battery.
    """
    chargers = max((len(slot.chargers) for slot in slots), default=0)
    if chargers == 0:
        return None
    starts = [slot.start for slot in slots]
    hours = [slot.minutes / 60.0 for slot in slots]
    sessions = [
        session
        for index in range(chargers)
        for session in ev_sessions(
            [
                slot.chargers[index].as_slot() if index < len(slot.chargers) else None
                for slot in slots
            ],
            starts,
            hours,
        )
    ]
    return score_ev(window, sessions, time_limit_s=time_limit_s)


def score_slots(
    keys: Sequence[datetime],
    slots: Mapping[datetime, PerformanceSlot],
    slot_minutes: int,
    site: SiteLimits | None,
    *,
    time_limit_s: float | None = None,
) -> PerformanceScore:
    """Score the run of slots that starts at each of *keys*.

    Args:
        keys: UTC starts of the run's slots, consecutive and in order.
        slots: The recorded slots by UTC start.
        slot_minutes: Slot width the run is expected in.
        site: The installation's hard limits, or ``None`` when unknown.
        time_limit_s: Wall-clock limit for each oracle solve.

    Returns:
        The run's score, or a score whose ``reason`` says why there is none.

    Raises:
        ValueError: If *keys* is empty.
    """
    if not keys:
        raise ValueError("a run needs at least one slot")
    step = timedelta(minutes=slot_minutes)
    recorded = [slots.get(key) for key in keys]
    usable = [
        slot
        for slot in recorded
        if slot is not None
        and slot.minutes == slot_minutes
        and slot.coverage >= MIN_SLOT_COVERAGE
        and slot.soc_start_pct is not None
        and slot.soc_end_pct is not None
    ]
    observed = sum(
        min(slot.coverage_seconds, step.total_seconds())
        for slot in recorded
        if slot is not None and slot.minutes == slot_minutes
    )
    controlled = sum(
        slot.control_seconds
        for slot in recorded
        if slot is not None and slot.minutes == slot_minutes
    )
    described = PerformanceScore(
        start=keys[0],
        end=keys[-1] + step,
        slot_count=len(keys),
        coverage=observed / (len(keys) * step.total_seconds()),
        control_coverage=min(controlled / observed, 1.0) if observed > 0.0 else None,
    )

    if site is None:
        return replace(described, reason=REASON_NO_SITE_LIMITS)
    if len(usable) != len(keys):
        return replace(described, reason=REASON_INSUFFICIENT_COVERAGE)
    if not is_scipy_available():
        return replace(described, reason=REASON_SCIPY_UNAVAILABLE)

    realized = realized_slots(usable, site)
    start_kwh = site.rated_kwh * (usable[0].soc_start_pct or 0.0) / 100.0
    window = realized_window(realized, start_kwh, site)
    residual = unexplained_energy_kwh(realized)
    measured = replace(
        described,
        start_kwh=start_kwh,
        realized_end_kwh=window.stored_kwh[-1],
        residual_kwh=residual,
        notes=window.notes,
    )
    capacity = window.configured.max_stored_kwh - window.configured.min_stored_kwh
    if window.capacity_drift_kwh > LIMIT_NOTE_SHARE * capacity:
        return replace(measured, reason=REASON_STORED_ENERGY_OUTSIDE_LIMITS)
    if residual is not None and residual > _residual_limit_kwh(window, realized):
        return replace(measured, reason=REASON_ENERGY_BALANCE_RESIDUAL)

    score = score_window(window, time_limit_s=time_limit_s, breakdown=True)
    if score is None:
        return replace(measured, reason=REASON_ORACLE_UNSOLVED)
    ev = _score_ev(usable, window, time_limit_s)
    return replace(
        measured,
        ev_shiftable_kwh=None if ev is None else ev.shiftable_kwh,
        ev_joint_oracle_cost=None if ev is None else ev.joint_oracle_cost,
        ev_plug_and_charge_cost=None if ev is None else ev.plug_and_charge_cost,
        realized_cost=score.realized_cost,
        oracle_cost=score.oracle_cost,
        baseline_cost=score.baseline_cost,
        potential=score.potential,
        pv_as_realized_cost=score.pv_as_realized_cost,
        realized_end_kwh=score.realized_end_kwh,
        baseline_end_kwh=score.baseline_end_kwh,
    )


def evaluate_performance(request: PerformanceRequest) -> PerformanceEvaluation:
    """Score the trailing window and any local days that are due.

    Blocking: each scored run solves three mixed-integer programs (two for
    the score, one for its battery / solar split), and two more when an EV
    session could have been timed differently.  Call it from the executor.

    Args:
        request: The slots, the runs to score and the site's limits.

    Returns:
        The window's score and one score per requested day.
    """
    return PerformanceEvaluation(
        window=score_slots(
            request.window,
            request.slots,
            request.slot_minutes,
            request.site,
            time_limit_s=request.time_limit_s,
        ),
        days=tuple(
            score_slots(
                keys,
                request.slots,
                request.slot_minutes,
                request.site,
                time_limit_s=request.time_limit_s,
            ).for_day(day)
            for day, keys in request.days
        ),
    )
