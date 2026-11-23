"""Measure the estimated price tail of the terminal end value (issue #1453).

The planner can value the energy left at the horizon end with price-profile
prices for the 24 hours after it (``planner/terminal_tail.py``).  This script
produces the numbers in ``docs/planner-spec.md`` → *Estimated price tail*.

Prices are the committed DK1 day-ahead prices
(``scripts/data/dk1_day_ahead_prices.csv``) with the grid tariff of the corpus
installation ``site-a`` added.  That reproduces the prices in its recorded
cycles to 0.001 DKK/kWh::

    import = 1.25 × spot + tariff(clock hour)      export = spot − 0.015

The price profile of a run is built from the 56 days before it with the
production code, which is what the price store would hold.

Usage::

    python3 scripts/backtest_price_tail.py value
    python3 scripts/backtest_price_tail.py cycles [corpus directory ...]
    python3 scripts/backtest_price_tail.py day
    python3 scripts/backtest_price_tail.py sim --horizon 24 --weight 0.5 \\
        --first 2026-09-20 --last 2026-10-19 [--no-pv]

``value``
    Error of the end value ``V`` against the ``V`` of the real next day, for
    several weights of the tail.  No planner run.
``cycles``
    Every corpus cycle planned once without and once with the tail: what
    changes in the plan, and the time of ``run_planner``.
``day``
    The committed recorded days replayed slot by slot
    (``tests/backtest/attribution.rolling_run``) without and with the tail.
``sim``
    Closed loop on the DK1 prices: the planner runs every hour, its plan is
    executed for that hour, tomorrow's prices are published at 13:00, and the
    load and PV forecasts of one recorded cycle are taken as what happens.
    Prints the realised cost per day.  Run it once per weight and compare.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import sys
import time
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from statistics import fmean, median, stdev

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from custom_components.hsem.models.price_point import PricePoint  # noqa: E402
from custom_components.hsem.models.price_profile import (  # noqa: E402
    PriceProfile,
    PriceSample,
)
from custom_components.hsem.models.solcast_slot import SolcastSlot  # noqa: E402
from custom_components.hsem.planner.cost_helpers import (  # noqa: E402
    TERMINAL_NIGHT_END_HOUR,
    terminal_end_value,
)
from custom_components.hsem.planner.engine_core import run_planner  # noqa: E402
from custom_components.hsem.planner.hindsight_scoring import (  # noqa: E402
    SiteLimits,
)
from custom_components.hsem.planner.terminal_tail import (  # noqa: E402
    TERMINAL_TAIL_WEIGHT,
)
from scripts.score_price_estimates import (  # noqa: E402
    DEFAULT_PRICES,
    FULL_HISTORY_DAYS,
    STEP,
    ZONE,
    load_days,
)
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
    load_planner_input,
    planner_input_from_dict,
)
from tests.backtest.scoring import realized_day  # noqa: E402

CORPUS = _REPO_ROOT / "tests" / "backtest" / "corpus"
#: The recorded cycle whose installation, load and PV the simulation uses.
SIM_CYCLE = CORPUS / "cycle-2026-09-24-1016.json"
#: Grid tariff of ``site-a`` in DKK/kWh as ``(night, day, peak)``, before and
#: from the date the winter tariff starts.
SUMMER_TARIFF = (0.249, 0.289, 0.480)
WINTER_TARIFF = (0.266, 0.458, 1.035)
WINTER_START = date(2026, 10, 1)
PEAK_HOURS = range(17, 21)
EXPORT_FEE = 0.015
#: Hour the day-ahead prices of tomorrow are published.
PUBLISH_HOUR = 13
WEIGHTS = (0.0, 0.25, 0.5, 0.75, 1.0)


class Prices:
    """The site's import and export prices on the DK1 price file."""

    def __init__(self, path: Path = DEFAULT_PRICES) -> None:
        """Load the price file.

        Args:
            path: A file written by ``scripts/score_price_estimates.py``.
        """
        self.days: dict[date, list[datetime]] = {
            day: [start for start, _, _ in slots]
            for day, slots in load_days(path).items()
        }
        self._spot: dict[datetime, float] = {
            start: export
            for slots in load_days(path).values()
            for start, _, export in slots
        }

    def pair(self, start: datetime) -> tuple[float, float]:
        """Return the ``(import, export)`` price of the slot at *start*."""
        local = start.astimezone(ZONE)
        spot = self._spot[start.astimezone(UTC)]
        night, day, peak = (
            WINTER_TARIFF if local.date() >= WINTER_START else SUMMER_TARIFF
        )
        if local.hour < TERMINAL_NIGHT_END_HOUR:
            tariff = night
        else:
            tariff = peak if local.hour in PEAK_HOURS else day
        return 1.25 * spot + tariff, spot - EXPORT_FEE

    def profile(self, now: datetime) -> PriceProfile:
        """Return the profile of the 56 days up to *now*, as the store has it."""
        today = now.astimezone(ZONE).date()
        samples: list[PriceSample] = [
            (start, STEP, *self.pair(start))
            for offset in range(FULL_HISTORY_DAYS + 1)
            for start in self.days.get(today - timedelta(days=offset), [])
            if start <= now
        ]
        return PriceProfile.from_samples(samples, ZONE)


# ---------------------------------------------------------------------------
# value: the error of V
# ---------------------------------------------------------------------------


def _end_value(prices: Sequence[tuple[datetime, float]], site: SiteLimits) -> float:
    """Return ``V`` of one day of import prices for the corpus battery."""
    top = sorted((price for _, price in prices), reverse=True)[:12]
    night = [
        price
        for start, price in prices
        if start.astimezone(ZONE).hour < TERMINAL_NIGHT_END_HOUR
    ]
    return terminal_end_value(
        peak_import=fmean(top),
        night_import=fmean(night),
        charge_eff=site.charge_efficiency,
        discharge_eff=site.discharge_efficiency,
        cycle_cost_per_kwh=0.31,
    )


def report_value(prices: Prices) -> list[str]:
    """Score ``V`` from the last day, from the tail and from blends of both.

    Two cases, as in the planner.  *Gap 1*: the last day of the horizon is
    published and the tail is the next day, shifted to its level.  *Gap 2*:
    the last day of the horizon is itself filled from the shifted profile and
    the tail is the day after, 24 hours or more after the last price, so it
    takes the profile as recorded.

    Returns:
        The report lines.
    """
    site = SiteLimits.from_planner_input(load_planner_input(SIM_CYCLE)[0])
    days = sorted(prices.days)
    rows: dict[int, list[tuple[float, float, float]]] = {1: [], 2: []}
    for target in days:
        for gap in rows:
            anchor = target - timedelta(days=gap)
            if anchor - timedelta(days=FULL_HISTORY_DAYS) < days[0]:
                continue
            anchor_slots = prices.days[anchor]
            profile = prices.profile(anchor_slots[-1])
            published = {start: prices.pair(start)[0] for start in anchor_slots}
            shift = profile.level_shift(published, 0, STEP, ZONE)
            if shift is None:
                continue

            def shape(start: datetime, profile: PriceProfile = profile) -> float:
                local = start.astimezone(ZONE)
                pair = profile.lookup(local.weekday(), local.hour, local.minute, STEP)
                return math.nan if pair is None else pair[0]

            real = [(start, prices.pair(start)[0]) for start in prices.days[target]]
            if gap == 1:
                last = list(published.items())
                tail = [(start, shape(start) + shift) for start, _ in real]
            else:
                between = prices.days[target - timedelta(days=1)]
                last = [(start, shape(start) + shift) for start in between]
                tail = [(start, shape(start)) for start, _ in real]
            rows[gap].append(
                (_end_value(real, site), _end_value(last, site), _end_value(tail, site))
            )
    lines: list[str] = []
    for gap, data in rows.items():
        lines.append(
            f"tail {gap} day(s) after the last price: {len(data)} days, "
            f"mean true V {fmean(row[0] for row in data):.3f}"
        )
        for weight in WEIGHTS:
            errors = [
                (1.0 - weight) * last + weight * tail - true
                for true, last, tail in data
            ]
            lines.append(
                f"  weight {weight:.2f}  MAE {fmean(abs(e) for e in errors):.3f}  "
                f"bias {fmean(errors):+.3f}  worst {max(abs(e) for e in errors):.3f}"
            )
        gain = [
            abs(
                (1.0 - TERMINAL_TAIL_WEIGHT) * last + TERMINAL_TAIL_WEIGHT * tail - true
            )
            - abs(last - true)
            for true, last, tail in data
        ]
        lines.append(
            f"  weight {TERMINAL_TAIL_WEIGHT} against 0: {fmean(gain):+.3f} "
            f"± {stdev(gain) / math.sqrt(len(gain)):.3f}"
        )
    return lines


# ---------------------------------------------------------------------------
# cycles: single plans
# ---------------------------------------------------------------------------


def report_cycles(prices: Prices, directories: Sequence[Path]) -> list[str]:
    """Plan every cycle without and with the tail and compare the plans.

    Args:
        prices: The price model the profiles are built from.
        directories: Corpus directories; a cycle name is read once.

    Returns:
        One line per cycle and a summary.
    """
    files = sorted(
        {
            path.name: path
            for directory in directories
            for path in directory.glob("cycle-*.json")
        }.values()
    )
    lines: list[str] = []
    seconds: dict[float, list[float]] = {0.0: [], TERMINAL_TAIL_WEIGHT: []}
    changed = live_changed = 0
    for path in files:
        recorded, _report = load_planner_input(path)
        profile = prices.profile(datetime.fromisoformat(recorded.now_iso))
        outputs = {}
        for weight, timings in seconds.items():
            planner_input = replace(
                recorded, price_profile=profile, terminal_price_tail_weight=weight
            )
            runs = []
            for _ in range(3):
                started = time.perf_counter()
                outputs[weight] = run_planner(planner_input)
                runs.append(time.perf_counter() - started)
            timings.append(min(runs))
        before, after = outputs[0.0], outputs[TERMINAL_TAIL_WEIGHT]
        tail = after.terminal_price_tail
        if tail is None:
            lines.append(f"{path.stem}: no tail")
            continue
        different = sum(
            1
            for a, b in zip(before.slots, after.slots)
            if a.recommendation != b.recommendation
            or abs(a.batteries_charged_kwh - b.batteries_charged_kwh) > 1e-3
            or abs(a.batteries_discharged_kwh - b.batteries_discharged_kwh) > 1e-3
        )
        changed += int(different > 0)
        live_changed += int(
            before.current_recommendation != after.current_recommendation
        )
        lines.append(
            f"{path.stem}: V {tail.last_day_value_per_kwh:.3f} -> "
            f"{tail.value_per_kwh:.3f} (tail {tail.tail_value_per_kwh:.3f})  "
            f"end SoC {before.battery_soc_at_end:.0f} -> "
            f"{after.battery_soc_at_end:.0f} %  slots changed {different}"
        )
    lines.append(
        f"{len(files)} cycles: {changed} plans changed, "
        f"{live_changed} live-slot recommendations changed"
    )
    for weight, timings in seconds.items():
        ordered = sorted(timings)
        lines.append(
            f"run_planner, weight {weight}: median {median(ordered):.2f} s, "
            f"95th percentile {ordered[int(0.95 * len(ordered))]:.2f} s, "
            f"slowest {ordered[-1]:.2f} s"
        )
    return lines


# ---------------------------------------------------------------------------
# day: the recorded days
# ---------------------------------------------------------------------------


def report_days(prices: Prices) -> list[str]:
    """Replay the committed recorded days without and with the tail.

    Returns:
        One line per day and weight: realised cost and stored energy at the
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
        for weight in (0.0, TERMINAL_TAIL_WEIGHT, 1.0):
            patched = copy.deepcopy(payloads)
            for payload in patched:
                planner_input = payload["planner_input"]
                now = datetime.fromisoformat(planner_input["now_iso"])
                planner_input["price_profile"] = prices.profile(now).as_dict()
                planner_input["terminal_price_tail_weight"] = weight
            recorded = site_cycles_by_slot(
                patched, day, ZONE, actuals.slot_minutes, actuals.site_tag
            )
            if isinstance(recorded, str):
                lines.append(f"{day}: {recorded}")
                continue
            run = rolling_run(
                prepared, recorded, actuals, ZONE, site, realized_forecasts=False
            )
            if run is None:
                lines.append(f"{day}: no cycle recorded")
                continue
            lines.append(
                f"{day} weight {weight}: cost {run.run.cost:.2f}, "
                f"stored at the end {run.run.stored_kwh[-1]:.2f} kWh"
            )
    return lines


# ---------------------------------------------------------------------------
# sim: closed loop on the price file
# ---------------------------------------------------------------------------


def simulate(
    prices: Prices,
    first: date,
    last: date,
    *,
    horizon_hours: int,
    weight: float,
    with_pv: bool,
) -> list[str]:
    """Run the planner every hour from *first* to *last* and execute its plan.

    Args:
        prices: The price model.
        first: First local day.
        last: Last local day.
        horizon_hours: Planning horizon in hours.
        weight: Weight of the tail's end value; ``0`` is the planner without
            a tail.
        with_pv: Keep the recorded cycle's PV forecast, or plan without PV.

    Returns:
        One line per day with its realised cost and the stored energy at its
        end, and a summary as JSON.
    """
    recorded, _report = load_planner_input(SIM_CYCLE)
    base = replace(
        recorded,
        ev_planned_load_enabled=False,
        live_solar_production_available=False,
        live_house_consumption_available=False,
        previous_winner_name=None,
        previous_winner_score=0.0,
        terminal_price_tail_weight=weight,
        interval_length_hours=horizon_hours,
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
    stored = 0.5 * site.rated_kwh
    lines: list[str] = []
    solve_seconds: list[float] = []
    total = 0.0
    tails = 0
    day = first
    while day <= last:
        cost = 0.0
        hours = sorted(
            {start.astimezone(ZONE).replace(minute=0) for start in prices.days[day]},
            key=lambda start: start.astimezone(UTC),
        )
        for now in hours:
            published = [day]
            if now.hour >= PUBLISH_HOUR:
                published.append(day + timedelta(days=1))
            points: list[PricePoint] = []
            for offset, published_day in enumerate(published):
                for index, start in enumerate(prices.days[published_day]):
                    import_price, export_price = prices.pair(start)
                    points.append(
                        PricePoint(
                            hour=start.astimezone(ZONE).hour,
                            import_price=import_price,
                            export_price=export_price,
                            day_offset=offset,
                            slot_in_day=index,
                        )
                    )
            started = time.perf_counter()
            plan = run_planner(
                replace(
                    base,
                    now_iso=now.isoformat(),
                    battery_soc_pct=stored / site.rated_kwh * 100.0,
                    price_points=points,
                    price_profile=prices.profile(now),
                    price_published_until=prices.days[published[-1]][-1]
                    + timedelta(minutes=STEP),
                )
            )
            solve_seconds.append(time.perf_counter() - started)
            tails += int(plan.terminal_price_tail is not None)
            by_start = {slot.start.astimezone(UTC): slot for slot in plan.slots}
            for quarter in range(60 // STEP):
                start = (now + timedelta(minutes=STEP * quarter)).astimezone(UTC)
                slot = by_start[start]
                net = slot.avg_house_consumption_kwh - slot.solcast_pv_estimate_kwh
                charge, discharge = execute_decision(
                    Decision(
                        recommendation=slot.recommendation,
                        charged_kwh=slot.batteries_charged_kwh / site.charge_efficiency,
                        discharged_kwh=slot.batteries_discharged_kwh
                        * site.discharge_efficiency,
                    ),
                    net_load_kwh=net,
                    house_net_kwh=net,
                    stored_kwh=stored,
                    battery=battery,
                    hours=STEP / 60.0,
                )
                stored += charge * battery.charge_efficiency
                stored -= discharge / battery.discharge_efficiency
                grid = net + charge - discharge
                import_price, export_price = prices.pair(start)
                cost += max(grid, 0.0) * import_price - max(-grid, 0.0) * export_price
        total += cost
        lines.append(f"{day} cost {cost:.3f} stored {stored:.2f}")
        day += timedelta(days=1)
    ordered = sorted(solve_seconds)
    lines.append(
        json.dumps(
            {
                "horizon_hours": horizon_hours,
                "weight": weight,
                "pv": with_pv,
                "days": (last - first).days + 1,
                "total_cost": round(total, 2),
                "end_stored_kwh": round(stored, 2),
                "runs": len(ordered),
                "runs_with_tail": tails,
                "run_planner_median_s": round(median(ordered), 3),
                "run_planner_max_s": round(ordered[-1], 3),
            }
        )
    )
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    """Run one mode and print its report.

    Args:
        argv: Command-line arguments, defaulting to ``sys.argv[1:]``.

    Returns:
        ``0``.
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("mode", choices=("value", "cycles", "day", "sim"))
    parser.add_argument("corpus", nargs="*", type=Path, help="corpus directories")
    parser.add_argument("--horizon", type=int, default=48, help="horizon in hours")
    parser.add_argument("--weight", type=float, default=TERMINAL_TAIL_WEIGHT)
    parser.add_argument("--first", type=date.fromisoformat, default=date(2026, 9, 20))
    parser.add_argument("--last", type=date.fromisoformat, default=date(2026, 10, 19))
    parser.add_argument("--no-pv", action="store_true", help="simulate without PV")
    args = parser.parse_args(argv)

    prices = Prices()
    if args.mode == "value":
        lines = report_value(prices)
    elif args.mode == "cycles":
        lines = report_cycles(prices, args.corpus or [CORPUS])
    elif args.mode == "day":
        lines = report_days(prices)
    else:
        lines = simulate(
            prices,
            args.first,
            args.last,
            horizon_hours=args.horizon,
            weight=args.weight,
            with_pv=not args.no_pv,
        )
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
