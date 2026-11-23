"""Measure the pre-peak reserve for load-forecast uncertainty (issue #1361).

The planner can hold a reserve at the end of an expensive window, sized from
how far the load forecast has been off over that window on earlier days
(``planner/load_reserve.py``).  This script produces the numbers in
``docs/planner-spec.md`` → *Load-forecast uncertainty reserve*.

No recorded cycle carries a load error history, so every mode builds one from
a model of the forecast error: an AR(1) series per 15-minute slot.  Its
defaults reproduce the two figures measured on ``site-a`` (issue #1361): a
per-slot RMSE of 0.17 kWh and a spread of 1.94 kWh over a 22-slot evening
window.

Usage::

    python3 scripts/backtest_load_reserve.py cycles [corpus directory ...]
    python3 scripts/backtest_load_reserve.py day
    python3 scripts/backtest_load_reserve.py sim --first 2026-10-01 \\
        --last 2026-10-30 [--no-pv] [--seed 1]

``cycles``
    Every corpus cycle planned once without and once with the reserve: the
    decision, what changes in the plan, and the time of ``run_planner``.
``day``
    The committed recorded days replayed slot by slot against the real load
    (``tests/backtest/attribution.rolling_run``) without and with the
    reserve.
``sim``
    Closed loop on the DK1 prices: the planner runs every hour, its plan is
    executed for that hour against a load that differs from the forecast by
    the error model, and tomorrow's prices are published at 13:00.  Both
    planners see the same load.  Prints the realised cost per day.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import random
import sys
import time
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from statistics import fmean, median

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from custom_components.hsem.models.load_error_profile import (  # noqa: E402
    LoadErrorProfile,
)
from custom_components.hsem.models.planner_input import PlannerInput  # noqa: E402
from custom_components.hsem.models.planner_output import (  # noqa: E402
    PlannerOutput,
)
from custom_components.hsem.models.price_point import PricePoint  # noqa: E402
from custom_components.hsem.models.solcast_slot import SolcastSlot  # noqa: E402
from custom_components.hsem.planner.engine_core import run_planner  # noqa: E402
from custom_components.hsem.planner.hindsight_scoring import (  # noqa: E402
    SiteLimits,
)
from scripts.backtest_price_tail import (  # noqa: E402
    CORPUS,
    PUBLISH_HOUR,
    SIM_CYCLE,
    Prices,
)
from scripts.score_price_estimates import STEP, ZONE  # noqa: E402
from tests.backtest.attribution import (  # noqa: E402
    Decision,
    execute_decision,
    rolling_run,
    site_cycles_by_slot,
)
from tests.backtest.recorded_day import (  # noqa: E402
    load_recorded_day,
    recorded_days,
)
from tests.backtest.replay import (  # noqa: E402
    iter_dumps,
    planner_input_from_dict,
)
from tests.backtest.scoring import realized_day  # noqa: E402

#: Per-slot RMSE of the load forecast measured on ``site-a`` (kWh per 15 min).
SLOT_RMSE_KWH = 0.17
#: Slot-to-slot correlation that gives the measured 1.94 kWh over 22 slots.
SLOT_CORRELATION = 0.75
#: Days of error history the planner is given before the first planned day.
HISTORY_DAYS = 30


def error_series(
    first: datetime,
    slots: int,
    *,
    seed: int,
    slot_rmse: float = SLOT_RMSE_KWH,
    correlation: float = SLOT_CORRELATION,
) -> dict[int, float]:
    """Return an AR(1) load forecast error per slot, keyed by UTC epoch minute.

    Args:
        first: Start of the first slot.
        slots: Number of ``STEP``-minute slots.
        seed: Seed of the random series.
        slot_rmse: Standard deviation of one slot's error, in kWh.
        correlation: Correlation between neighbouring slots.

    Returns:
        ``actual - forecast`` per slot.
    """
    rng = random.Random(seed)
    innovation = slot_rmse * math.sqrt(max(1.0 - correlation * correlation, 0.0))
    start = int(first.astimezone(UTC).timestamp() // 60)
    errors: dict[int, float] = {}
    value = rng.gauss(0.0, slot_rmse)
    for index in range(slots):
        errors[start + index * STEP] = value
        value = correlation * value + rng.gauss(0.0, innovation)
    return errors


def history_before(now: datetime, seed: int) -> LoadErrorProfile:
    """Return ``HISTORY_DAYS`` of modelled errors ending at the hour of *now*."""
    end = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    slots = HISTORY_DAYS * 24 * 60 // STEP
    first = end - timedelta(minutes=STEP * slots)
    return LoadErrorProfile(errors=error_series(first, slots, seed=seed))


def _timed(inp: PlannerInput) -> tuple[PlannerOutput, float]:
    """Run the planner and return its output with the wall time in seconds."""
    started = time.perf_counter()
    output = run_planner(inp)
    return output, time.perf_counter() - started


# ---------------------------------------------------------------------------
# cycles: one plan without and one with the reserve
# ---------------------------------------------------------------------------


def report_cycles(directories: Sequence[Path], seed: int) -> list[str]:
    """Plan every corpus cycle without and with the reserve.

    Args:
        directories: Corpus directories to read.
        seed: Seed of the modelled error history.

    Returns:
        One line per cycle and a JSON summary.
    """
    lines: list[str] = []
    off_s: list[float] = []
    on_s: list[float] = []
    reasons: dict[str, int] = {}
    changed = 0
    for directory in directories:
        for path in sorted(directory.glob("*.json")):
            for dump in iter_dumps(path):
                recorded, _report = planner_input_from_dict(dump)
                now = datetime.fromisoformat(recorded.now_iso)
                off, off_time = _timed(recorded)
                on, on_time = _timed(
                    replace(
                        recorded,
                        load_reserve_enabled=True,
                        load_error_profile=history_before(now, seed),
                    )
                )
                if off.plan_cost is None or on.plan_cost is None:
                    lines.append(f"{path.stem}: no plan")
                    continue
                off_s.append(off_time)
                on_s.append(on_time)
                reserve = on.load_reserve or {}
                reason = str(reserve.get("reason"))
                reasons[reason] = reasons.get(reason, 0) + 1
                delta = on.plan_cost.total_cost - off.plan_cost.total_cost
                changed += int(abs(delta) > 1e-6)
                lines.append(
                    f"{path.stem}: {reason}, windows "
                    f"{len(reserve.get('windows', []))}, sigma "
                    f"{reserve.get('sigma_kwh', 0.0):.2f} kWh, held "
                    f"{reserve.get('total_reserve_kwh', 0.0):.2f} kWh, planned "
                    f"cost {delta:+.3f}, run_planner {off_time:.2f} -> "
                    f"{on_time:.2f} s"
                )
    if off_s:
        lines.append(
            json.dumps(
                {
                    "cycles": len(off_s),
                    "reasons": reasons,
                    "plans_with_another_cost": changed,
                    "run_planner_median_off_s": round(median(off_s), 3),
                    "run_planner_median_on_s": round(median(on_s), 3),
                    "run_planner_max_off_s": round(max(off_s), 3),
                    "run_planner_max_on_s": round(max(on_s), 3),
                }
            )
        )
    return lines


# ---------------------------------------------------------------------------
# day: recorded days against the real load
# ---------------------------------------------------------------------------


def report_days(seed: int) -> list[str]:
    """Replay the committed recorded days without and with the reserve.

    Returns:
        One line per day and setting: realised cost and stored energy at the
        end of the day.
    """
    lines: list[str] = []
    for day, directory in sorted(recorded_days().items()):
        payloads, actuals = load_recorded_day(directory)
        site = SiteLimits.from_planner_input(planner_input_from_dict(payloads[0])[0])
        prepared = realized_day(actuals, day, ZONE, site)
        if isinstance(prepared, str):
            lines.append(f"{day}: {prepared}")
            continue
        for enabled in (False, True):
            patched = copy.deepcopy(payloads)
            for payload in patched:
                planner_input = payload["planner_input"]
                now = datetime.fromisoformat(planner_input["now_iso"])
                planner_input["load_reserve_enabled"] = enabled
                planner_input["load_error_profile"] = (
                    history_before(now, seed).as_dict() if enabled else None
                )
            recorded = site_cycles_by_slot(
                patched, day, ZONE, actuals.slot_minutes, actuals.site_tag
            )
            if isinstance(recorded, str):
                lines.append(f"{day}: {recorded}")
                continue
            started = time.perf_counter()
            run = rolling_run(
                prepared, recorded, actuals, ZONE, site, realized_forecasts=False
            )
            elapsed = time.perf_counter() - started
            if run is None:
                lines.append(f"{day}: no cycle recorded")
                continue
            changes = sum(
                1
                for a, b in zip(run.recommendations, run.recommendations[1:])
                if a != b
            )
            lines.append(
                f"{day} reserve {'on' if enabled else 'off'}: cost "
                f"{run.run.cost:.3f}, stored at the end "
                f"{run.run.stored_kwh[-1]:.2f} kWh, label changes {changes}, "
                f"replay {elapsed:.1f} s"
            )
    return lines


# ---------------------------------------------------------------------------
# sim: closed loop on the price file with a load that misses the forecast
# ---------------------------------------------------------------------------


class _Run:
    """One planner setting followed through the simulation."""

    def __init__(self, enabled: bool, stored: float) -> None:
        self.enabled = enabled
        self.stored = stored
        self.cost = 0.0
        self.day_cost = 0.0
        self.seconds: list[float] = []
        self.held: list[float] = []
        self.shortfall_kwh = 0.0
        self.labels: list[str | None] = []
        self.inconsistent_plans = 0


def simulate(
    prices: Prices,
    first: date,
    last: date,
    *,
    with_pv: bool,
    seed: int,
    slot_rmse: float,
) -> list[str]:
    """Run the planner every hour without and with the reserve.

    Both runs meet the same load: the recorded cycle's forecast plus the
    modelled error.  The error history the planner gets holds the errors of
    the slots that have passed.

    Args:
        prices: The price model.
        first: First local day.
        last: Last local day.
        with_pv: Keep the recorded cycle's PV forecast, or plan without PV.
        seed: Seed of the load error series.
        slot_rmse: Standard deviation of one slot's load error, in kWh.

    Returns:
        One line per day with the realised cost of both runs, and a JSON
        summary.
    """
    recorded, _report = planner_input_from_dict(next(iter_dumps(SIM_CYCLE)))
    base = replace(
        recorded,
        ev_planned_load_enabled=False,
        live_solar_production_available=False,
        live_house_consumption_available=False,
        previous_winner_name=None,
        previous_winner_score=0.0,
    )
    if not with_pv:
        base = replace(
            base,
            solcast_slots=[
                SolcastSlot(hour=hour, pv_estimate=0.0, day_offset=offset)
                for offset in (0, 1)
                for hour in range(24)
            ],
        )
    site = SiteLimits.from_planner_input(base)
    battery = site.battery()
    series_start = min(prices.days[first]) - timedelta(days=HISTORY_DAYS)
    series_slots = ((last - first).days + 2 + HISTORY_DAYS) * 24 * 60 // STEP
    errors = error_series(series_start, series_slots, seed=seed, slot_rmse=slot_rmse)
    runs = [_Run(False, 0.5 * site.rated_kwh), _Run(True, 0.5 * site.rated_kwh)]
    lines: list[str] = []
    day = first
    while day <= last:
        hours = sorted(
            {start.astimezone(ZONE).replace(minute=0) for start in prices.days[day]},
            key=lambda start: start.astimezone(UTC),
        )
        for now in hours:
            published = [day]
            if now.hour >= PUBLISH_HOUR:
                published.append(day + timedelta(days=1))
            points = [
                PricePoint(
                    hour=start.astimezone(ZONE).hour,
                    import_price=prices.pair(start)[0],
                    export_price=prices.pair(start)[1],
                    day_offset=offset,
                    slot_in_day=index,
                )
                for offset, published_day in enumerate(published)
                for index, start in enumerate(prices.days[published_day])
            ]
            now_minute = int(now.astimezone(UTC).timestamp() // 60)
            known = LoadErrorProfile(
                errors={key: e for key, e in errors.items() if key < now_minute}
            )
            for run in runs:
                plan, seconds = _timed(
                    replace(
                        base,
                        now_iso=now.isoformat(),
                        battery_soc_pct=run.stored / site.rated_kwh * 100.0,
                        price_points=points,
                        load_reserve_enabled=run.enabled,
                        load_error_profile=known if run.enabled else None,
                    )
                )
                run.seconds.append(seconds)
                run.inconsistent_plans += int(bool(plan.plan_consistency_violations))
                if plan.load_reserve is not None:
                    run.held.append(plan.load_reserve["total_reserve_kwh"])
                by_start = {slot.start.astimezone(UTC): slot for slot in plan.slots}
                for quarter in range(60 // STEP):
                    start = (now + timedelta(minutes=STEP * quarter)).astimezone(UTC)
                    slot = by_start[start]
                    load = max(
                        slot.avg_house_consumption_kwh
                        + errors[int(start.timestamp() // 60)],
                        0.0,
                    )
                    net = load - slot.solcast_pv_estimate_kwh
                    charge, discharge = execute_decision(
                        Decision(
                            recommendation=slot.recommendation,
                            charged_kwh=slot.batteries_charged_kwh
                            / site.charge_efficiency,
                            discharged_kwh=slot.batteries_discharged_kwh
                            * site.discharge_efficiency,
                        ),
                        net_load_kwh=net,
                        house_net_kwh=net,
                        stored_kwh=run.stored,
                        battery=battery,
                        hours=STEP / 60.0,
                    )
                    run.stored += charge * battery.charge_efficiency
                    run.stored -= discharge / battery.discharge_efficiency
                    grid = net + charge - discharge
                    import_price, export_price = prices.pair(start)
                    run.day_cost += (
                        max(grid, 0.0) * import_price - max(-grid, 0.0) * export_price
                    )
                    run.labels.append(slot.recommendation)
        lines.append(
            f"{day} cost off {runs[0].day_cost:.3f} on {runs[1].day_cost:.3f} "
            f"({runs[1].day_cost - runs[0].day_cost:+.3f}), stored off "
            f"{runs[0].stored:.2f} on {runs[1].stored:.2f}"
        )
        for run in runs:
            run.cost += run.day_cost
            run.day_cost = 0.0
        day += timedelta(days=1)
    off, on = runs
    days = (last - first).days + 1
    lines.append(
        json.dumps(
            {
                "days": days,
                "pv": with_pv,
                "seed": seed,
                "slot_rmse_kwh": slot_rmse,
                "total_cost_off": round(off.cost, 2),
                "total_cost_on": round(on.cost, 2),
                "cost_on_minus_off": round(on.cost - off.cost, 2),
                "cost_on_minus_off_per_day": round((on.cost - off.cost) / days, 3),
                "end_stored_off_kwh": round(off.stored, 2),
                "end_stored_on_kwh": round(on.stored, 2),
                "mean_held_reserve_kwh": round(fmean(on.held), 3) if on.held else 0.0,
                "runs_with_reserve": sum(1 for held in on.held if held > 0.0),
                "runs": len(on.seconds),
                "inconsistent_plans_off": off.inconsistent_plans,
                "inconsistent_plans_on": on.inconsistent_plans,
                "label_changes_off": _changes(off.labels),
                "label_changes_on": _changes(on.labels),
                "run_planner_median_off_s": round(median(off.seconds), 3),
                "run_planner_median_on_s": round(median(on.seconds), 3),
                "run_planner_max_off_s": round(max(off.seconds), 3),
                "run_planner_max_on_s": round(max(on.seconds), 3),
            }
        )
    )
    return lines


def _changes(labels: Sequence[str | None]) -> int:
    """Return how often the executed recommendation changes between slots."""
    return sum(1 for a, b in zip(labels, labels[1:]) if a != b)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one mode and print its report."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    modes = parser.add_subparsers(dest="mode", required=True)
    cycles = modes.add_parser("cycles")
    cycles.add_argument("directories", nargs="*", type=Path, default=[CORPUS])
    cycles.add_argument("--seed", type=int, default=1)
    days = modes.add_parser("day")
    days.add_argument("--seed", type=int, default=1)
    sim = modes.add_parser("sim")
    sim.add_argument("--first", type=date.fromisoformat, required=True)
    sim.add_argument("--last", type=date.fromisoformat, required=True)
    sim.add_argument("--no-pv", action="store_true")
    sim.add_argument("--seed", type=int, default=1)
    sim.add_argument("--slot-rmse", type=float, default=SLOT_RMSE_KWH)
    args = parser.parse_args(argv)
    if args.mode == "cycles":
        lines = report_cycles(args.directories, args.seed)
    elif args.mode == "day":
        lines = report_days(args.seed)
    else:
        lines = simulate(
            Prices(),
            args.first,
            args.last,
            with_pv=not args.no_pv,
            seed=args.seed,
            slot_rmse=args.slot_rmse,
        )
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
