"""Warn when the MILP solver keeps stopping at its time limit (issue #1309).

One time-limited solve is harmless: HiGHS returns the best plan it found and
the next cycle usually finishes.  A run of them means the hardware cannot
solve the model within the configured limit, and the published plans may be
far from optimal (an all-Wait horizon on a Raspberry Pi in #978).  This module
counts consecutive time-limited cycles and logs one WARNING naming the
setting once the run is long enough, then stays at DEBUG until a solve
finishes again.
"""

from __future__ import annotations

from custom_components.hsem.models.plan_explanation import PlanExplanation
from custom_components.hsem.planner.milp._incumbent import (
    SOLVER_STATUS_OPTIMAL,
    SOLVER_STATUS_TIME_LIMIT,
)
from custom_components.hsem.utils.logger import log_latched_warning

#: Consecutive time-limited cycles before the WARNING.
SOLVER_TIME_LIMIT_WARNING_CYCLES = 3

_STREAK_ATTR = "_hsem_solver_time_limit_streak"
_LATCH_KEY = "solver_time_limit"


def note_solver_outcome(owner: object, explanation: PlanExplanation) -> None:
    """Count one fresh planner run's solve outcome and warn on a long run.

    A time-limited solve extends the run, a finished solve ends it and
    re-arms the warning, and a failed or absent solve leaves it unchanged.

    Args:
        owner: Long-lived object holding the counter and the latch (the
            coordinator).
        explanation: The explanation of the plan this cycle produced.
    """
    status = explanation.solver_status
    if status == SOLVER_STATUS_OPTIMAL:
        setattr(owner, _STREAK_ATTR, 0)
        log_latched_warning(owner, _LATCH_KEY, False, "")
        return
    if status != SOLVER_STATUS_TIME_LIMIT:
        return
    streak = int(getattr(owner, _STREAK_ATTR, 0)) + 1
    setattr(owner, _STREAK_ATTR, streak)
    log_latched_warning(
        owner,
        _LATCH_KEY,
        streak >= SOLVER_TIME_LIMIT_WARNING_CYCLES,
        "[planner] The optimiser stopped at its %.1f s time limit in %d "
        "consecutive cycles, so the published plans may be far from optimal. "
        "Raise 'Planner solver time limit' (hsem_planner_solver_time_limit_s) "
        "in the battery economics settings.",
        explanation.solver_time_limit_s or 0.0,
        streak,
    )
