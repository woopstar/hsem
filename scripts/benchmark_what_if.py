"""Measure how long an ``hsem.what_if`` replay takes (issue #1454).

Replays recorded days through :func:`run_what_if`, the function the service
runs in its worker thread, and prints the time per solve and for the whole
run.  One run per scenario count: by default 90 days of 15-minute slots with
1 scenario and with 10, the most one call compares.

The days are synthetic: evening peak, midday PV and a cheap night, the same
days the tests use (``tests/utils/what_if_days.py``).  Real days can be easier
or harder for the solver.

The run has no 10-minute budget here, so the full time is measured, also when
the service would have stopped.  The 10-second limit per solve is the
service's own.

To approximate a slow device, limit the CPU from the outside.  With Docker::

    docker run --rm --cpus 0.2 -v "$PWD":/work -w /work <devcontainer image> \
        python3 scripts/benchmark_what_if.py

Usage::

    python3 scripts/benchmark_what_if.py
    python3 scripts/benchmark_what_if.py --days 30 --scenarios 1 5 --slot-minutes 60
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from custom_components.hsem.coordinator_what_if import (  # noqa: E402
    ORACLE_TIME_LIMIT_S,
    PAUSE_BETWEEN_DAYS_S,
    RUN_TIME_LIMIT_S,
)
from custom_components.hsem.utils import what_if  # noqa: E402
from custom_components.hsem.utils.what_if import (  # noqa: E402
    MAX_SCENARIOS,
    WhatIfRequest,
    WhatIfScenario,
    run_what_if,
    usable_capacity_kwh,
)
from tests.utils.what_if_days import SITE, make_days  # noqa: E402

#: Usable capacity between two scenarios of a benchmark run.
_CAPACITY_STEP_KWH = 2.5


@dataclass(frozen=True)
class Measurement:
    """What one replay took.

    Attributes:
        scenarios: Scenarios compared.
        days_compared: Days every side was solved for.
        days_timed_out: Days lost to the per-solve time limit.
        solve_seconds: Wall-clock time of each solve, in order.
        total_seconds: Wall-clock time of the whole run, pauses included.
    """

    scenarios: int
    days_compared: int
    days_timed_out: int
    solve_seconds: tuple[float, ...]
    total_seconds: float


def measure(
    days: int, scenarios: int, *, slot_minutes: int, time_limit_s: float
) -> Measurement:
    """Replay *days* synthetic days with *scenarios* batteries and time it.

    Args:
        days: Recorded days to replay.
        scenarios: Batteries to compare with the configured one.
        slot_minutes: Slot width of the recorded days.
        time_limit_s: Wall-clock limit for one solve.

    Raises:
        RuntimeError: If the run was cancelled, which nothing here asks for.
    """
    recorded = make_days(days, minutes=slot_minutes)
    base_kwh = usable_capacity_kwh(SITE)
    request = WhatIfRequest(
        days=tuple(recorded),
        recorded=recorded,
        reasons={},
        site=SITE,
        cycle_cost_per_kwh=0.05,
        scenarios=tuple(
            WhatIfScenario(usable_kwh=base_kwh + (index + 1) * _CAPACITY_STEP_KWH)
            for index in range(scenarios)
        ),
        time_limit_s=time_limit_s,
        budget_s=None,
        pause_s=PAUSE_BETWEEN_DAYS_S,
    )
    solve = what_if.solve_hindsight_oracle
    solve_seconds: list[float] = []

    def timed(*args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            return solve(*args, **kwargs)
        finally:
            solve_seconds.append(time.perf_counter() - started)

    what_if.solve_hindsight_oracle = timed  # type: ignore[assignment]
    started = time.perf_counter()
    try:
        response = run_what_if(request)
    finally:
        what_if.solve_hindsight_oracle = solve
    total = time.perf_counter() - started
    if response is None:
        raise RuntimeError("the replay was cancelled")
    return Measurement(
        scenarios=scenarios,
        days_compared=response["days_compared"],
        days_timed_out=response["days_timed_out"],
        solve_seconds=tuple(solve_seconds),
        total_seconds=total,
    )


def report(measurement: Measurement, days: int) -> str:
    """Return one measurement as text.

    Args:
        measurement: The measured run.
        days: Days that were asked for.
    """
    solves = measurement.solve_seconds
    lines = [
        f"{days} days, {measurement.scenarios} scenario(s)",
        f"  days compared   {measurement.days_compared}"
        f" ({measurement.days_timed_out} timed out)",
        f"  solves          {len(solves)}",
    ]
    if solves:
        lines += [
            f"  per solve       mean {statistics.fmean(solves):.3f} s,"
            f" median {statistics.median(solves):.3f} s, max {max(solves):.3f} s",
        ]
    over = measurement.total_seconds > RUN_TIME_LIMIT_S
    lines += [
        f"  total           {measurement.total_seconds:.1f} s"
        f" ({'over' if over else 'within'} the service's"
        f" {RUN_TIME_LIMIT_S:.0f} s budget)",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Run the benchmark and print the measurements.

    Args:
        argv: Command-line arguments, defaulting to ``sys.argv[1:]``.

    Returns:
        ``0`` when every run finished, ``2`` when the solver is not installed.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=90, help="days to replay")
    parser.add_argument(
        "--scenarios",
        type=int,
        nargs="+",
        default=[1, MAX_SCENARIOS],
        help="scenario counts, one run each",
    )
    parser.add_argument(
        "--slot-minutes", type=int, default=15, help="slot width of the days"
    )
    parser.add_argument(
        "--time-limit",
        type=float,
        default=ORACLE_TIME_LIMIT_S,
        help="wall-clock limit for one solve, in seconds",
    )
    args = parser.parse_args(argv)
    if not what_if.is_scipy_available():
        print("scipy is not installed: nothing to measure")
        return 2
    for count in args.scenarios:
        measurement = measure(
            args.days,
            count,
            slot_minutes=args.slot_minutes,
            time_limit_s=args.time_limit,
        )
        print(report(measurement, args.days), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
