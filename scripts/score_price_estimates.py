"""Score the planner's missing-price estimates on real prices (issue #1452).

The planner estimates a slot without a published price (see *Missing-price
estimation* in ``docs/planner-spec.md``).  This script measures how good each
estimate is on real day-ahead prices.  For every target day it hides the day,
builds the price profile from the days before it with the production code
(:class:`~custom_components.hsem.models.price_profile.PriceProfile`) and
scores, per 15-minute slot:

* **nearest earlier day**: the price of the same wall-clock time on the last
  day with prices,
* **shifted profile**: the weekday and time-of-day profile moved to the level
  of the last day with prices,
* **unshifted profile**: the profile as recorded.

The last day with prices is the day before the target (tomorrow's prices are
not out yet), or two days before it (a price outage on its second day).

Three reports are printed as Markdown tables:

1. Fill order with a full history.
2. Bucket minimum: the estimate by the number of days in a bucket, for
   same-weekday buckets and for every-weekday buckets, and the fill order as
   implemented for a minimum of 3, 4 and 6 days while the history grows.
3. Outage fallback after the held prices expired: the profile as recorded,
   the profile with the last known level shift, and the last recorded day,
   one and two days after the last day with prices.

Prices: the import price is ``IMPORT_FACTOR`` times the spot price (25 % VAT)
and the export price is the spot price, in DKK per kWh.  A tariff that is
fixed or depends on the time of day only is the same on every day, so it
drops out of every estimate above and is left out.

Usage::

    python3 scripts/score_price_estimates.py
    python3 scripts/score_price_estimates.py --prices my_prices.csv
    python3 scripts/score_price_estimates.py --fetch 2026-07-25 2026-11-21 DK1

``--fetch`` downloads day-ahead prices from Energi Data Service
(https://www.energidataservice.dk, dataset ``DayAheadPrices``) and rewrites
the price file.  The request carries the dates and the price area only.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from statistics import fmean, stdev
from urllib.parse import urlencode
from urllib.request import urlopen
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from custom_components.hsem.models.price_profile import (  # noqa: E402
    PROFILE_MIN_DAYS,
    PROFILE_STEP_MINUTES,
    PricePair,
    PriceProfile,
    PriceSample,
)

DEFAULT_PRICES = _REPO_ROOT / "scripts" / "data" / "dk1_day_ahead_prices.csv"
ZONE = ZoneInfo("Europe/Copenhagen")
STEP = PROFILE_STEP_MINUTES
#: Import price per spot price: 25 % VAT.
IMPORT_FACTOR = 1.25
#: Days of history the price store keeps (``PRICE_HISTORY_RETENTION_DAYS``).
FULL_HISTORY_DAYS = 56
BUCKET_MINIMUMS = (3, 4, 6)
_API = "https://api.energidataservice.dk/dataset/DayAheadPrices"
_SIDES = ("import", "export")

#: The real prices of one local day: ``(start, import, export)`` per slot.
Day = list[tuple[datetime, float, float]]
#: Absolute error per slot of one target day: ``(import, export)``.
Errors = list[tuple[float, float]]
#: Returns a profile price for a slot start, or ``None``.
Lookup = Callable[[datetime], PricePair | None]


def fetch_prices(start: str, end: str, area: str) -> list[tuple[datetime, float]]:
    """Download day-ahead spot prices from Energi Data Service.

    Args:
        start: First local date, ``YYYY-MM-DD``.
        end: Local date after the last one, ``YYYY-MM-DD``.
        area: Price area, for example ``DK1``.

    Returns:
        ``(UTC start, DKK per MWh)`` per 15-minute slot, in time order.
    """
    query = urlencode(
        {
            "start": f"{start}T00:00",
            "end": f"{end}T00:00",
            "filter": json.dumps({"PriceArea": [area]}),
            "columns": "TimeUTC,DayAheadPriceDKK",
            "sort": "TimeUTC asc",
            "limit": 0,
        }
    )
    with urlopen(f"{_API}?{query}", timeout=60) as response:  # noqa: S310
        records = json.load(response)["records"]
    return [
        (
            datetime.fromisoformat(record["TimeUTC"]).replace(tzinfo=UTC),
            float(record["DayAheadPriceDKK"]),
        )
        for record in records
    ]


def write_prices(path: Path, rows: Sequence[tuple[datetime, float]], area: str) -> None:
    """Write spot prices as one CSV row of up to 96 slots per UTC start.

    Args:
        path: File to write.
        rows: ``(UTC start, DKK per MWh)`` per 15-minute slot, without gaps.
        area: Price area, written to the header.
    """
    lines = [
        f"# Day-ahead spot prices, price area {area}, DKK per MWh, 15-minute slots.",
        "# Source: Energinet (www.energidataservice.dk), dataset DayAheadPrices.",
        "# Licence: CC BY 4.0.  Changed: prices are rounded to two decimals.",
        "# Row: UTC start of the first slot, then one price per 15 minutes.",
    ]
    for first in range(0, len(rows), 96):
        chunk = rows[first : first + 96]
        prices = ",".join(f"{price:.2f}" for _, price in chunk)
        lines.append(f"{chunk[0][0]:%Y-%m-%dT%H:%MZ},{prices}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_days(path: Path) -> dict[date, Day]:
    """Read a price file into complete local days.

    Args:
        path: File written by :func:`write_prices`.

    Returns:
        Local date to its slots.  The first and the last date of the file
        are dropped when they are not complete.
    """
    days: dict[date, Day] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        first, *prices = line.split(",")
        start = datetime.strptime(first, "%Y-%m-%dT%H:%MZ").replace(tzinfo=UTC)
        for index, raw in enumerate(prices):
            slot = start + timedelta(minutes=STEP * index)
            spot = float(raw) / 1000.0
            days.setdefault(slot.astimezone(ZONE).date(), []).append(
                (slot, spot * IMPORT_FACTOR, spot)
            )
    for edge in (min(days), max(days)):
        midnight = datetime.combine(edge, datetime.min.time(), ZONE)
        slots = (midnight + timedelta(days=1)).astimezone(UTC) - midnight.astimezone(
            UTC
        )
        if len(days[edge]) * STEP < slots.total_seconds() / 60:
            del days[edge]
    return days


def _clock_prices(day: Day) -> dict[tuple[int, int], PricePair]:
    """Return a day's prices by local ``(hour, minute)``.

    Both passes of a DST fall-back hour share a key and are averaged, as in
    the planner's nearest-earlier-day estimate.
    """
    grouped: dict[tuple[int, int], list[PricePair]] = {}
    for start, import_price, export_price in day:
        local = start.astimezone(ZONE)
        grouped.setdefault((local.hour, local.minute), []).append(
            (import_price, export_price)
        )
    return {
        key: (fmean(pair[0] for pair in pairs), fmean(pair[1] for pair in pairs))
        for key, pairs in grouped.items()
    }


def _samples(days: dict[date, Day], last: date, count: int) -> list[PriceSample]:
    """Return the price samples of the *count* days that end on *last*."""
    return [
        (start, STEP, import_price, export_price)
        for offset in range(count)
        for start, import_price, export_price in days.get(
            last - timedelta(days=offset), []
        )
    ]


def profile_lookup(profile: PriceProfile) -> Lookup:
    """Return the profile's price for a slot start, as the planner reads it."""

    def lookup(start: datetime) -> PricePair | None:
        local = start.astimezone(ZONE)
        return profile.lookup(local.weekday(), local.hour, local.minute, STEP)

    return lookup


def level_shift(anchor: Day, profile: PriceProfile) -> PricePair | None:
    """Return the shift that moves the profile to the level of *anchor*.

    The planner's own shift (``PriceProfile.level_shift``): the median
    difference between the real price and the profile, per side.

    Args:
        anchor: The last day with prices.
        profile: The price profile.

    Returns:
        ``(import shift, export shift)``, or ``None`` when fewer than
        ``PROFILE_MIN_ANCHOR_MINUTES`` of the day overlap the profile.
    """
    shifts = [
        profile.level_shift(
            {slot[0]: slot[side + 1] for slot in anchor}, side, STEP, ZONE
        )
        for side in range(len(_SIDES))
    ]
    if shifts[0] is None or shifts[1] is None:
        return None
    return shifts[0], shifts[1]


def estimate_day(
    target: Day, anchor: Day, profile: PriceProfile
) -> dict[str, list[PricePair | None]]:
    """Estimate every slot of a hidden day in three ways.

    Args:
        target: The hidden day; only its slot starts are used.
        anchor: The last day with prices.
        profile: Profile built from the days before *anchor*.

    Returns:
        ``nearest``, ``shifted`` and ``unshifted``: one price pair per slot
        of *target*, ``None`` where the estimate has no value.
    """
    clock = _clock_prices(anchor)
    lookup = profile_lookup(profile)
    shift = level_shift(anchor, profile)
    nearest: list[PricePair | None] = []
    shifted: list[PricePair | None] = []
    unshifted: list[PricePair | None] = []
    for start, _, _ in target:
        local = start.astimezone(ZONE)
        nearest.append(clock.get((local.hour, local.minute)))
        shape = lookup(start)
        unshifted.append(shape)
        shifted.append(
            None
            if shape is None or shift is None
            else (shape[0] + shift[0], shape[1] + shift[1])
        )
    return {"nearest": nearest, "shifted": shifted, "unshifted": unshifted}


def slot_errors(
    target: Day,
    estimate: Sequence[PricePair | None],
    fallback: Sequence[PricePair | None] | None = None,
) -> Errors:
    """Return the absolute error of an estimate per slot.

    Args:
        target: The real prices of the day.
        estimate: One price pair per slot, ``None`` where it has no value.
        fallback: Used for a slot the estimate has no value for.  A slot
            neither covers is left out.

    Returns:
        ``(import error, export error)`` per scored slot.
    """
    errors: Errors = []
    for index, (_, import_price, export_price) in enumerate(target):
        pair = estimate[index]
        if pair is None and fallback is not None:
            pair = fallback[index]
        if pair is not None:
            errors.append((abs(pair[0] - import_price), abs(pair[1] - export_price)))
    return errors


@dataclass
class Score:
    """Errors of one estimate over the target days.

    Attributes:
        label: Row label in the report.
        days: Absolute errors per target day.
    """

    label: str
    days: list[Errors]

    def mae(self, side: int) -> float:
        """Return the mean absolute error over every scored slot."""
        values = [errors[side] for day in self.days for errors in day]
        return fmean(values) if values else float("nan")

    def worst(self, side: int) -> float:
        """Return the largest absolute error of a single slot."""
        return max(
            (errors[side] for day in self.days for errors in day), default=float("nan")
        )

    def daily_mae(self, side: int) -> list[float]:
        """Return the mean absolute error of each target day."""
        return [fmean(errors[side] for errors in day) for day in self.days if day]

    def slot_count(self) -> int:
        """Return the number of scored slots."""
        return sum(len(day) for day in self.days)


def compare(score: Score, reference: Score, side: int) -> tuple[float, float, float]:
    """Compare two estimates day by day.

    Args:
        score: The estimate under test.
        reference: The estimate it is compared with, over the same days.
        side: 0 for import, 1 for export.

    Returns:
        ``(mean of the daily MAE difference, its standard error, share of
        days the estimate under test has the lower MAE)``.  A negative
        difference means *score* is better.
    """
    differences = [
        own - other
        for own, other in zip(score.daily_mae(side), reference.daily_mae(side))
    ]
    if len(differences) < 2:
        return float("nan"), float("nan"), float("nan")
    wins = sum(1 for value in differences if value < 0) / len(differences)
    return fmean(differences), stdev(differences) / len(differences) ** 0.5, wins


def weekday_only(profile: PriceProfile) -> PriceProfile:
    """Return the profile with its same-weekday buckets only."""
    return replace(profile, any_day_quarters={}, any_day_hours={})


def any_day_only(profile: PriceProfile) -> PriceProfile:
    """Return the profile with its every-weekday buckets only."""
    return replace(profile, quarters={}, hours={})


def score_targets(
    days: dict[date, Day],
    targets: Iterable[date],
    *,
    gap: int = 1,
    history_days: int = FULL_HISTORY_DAYS,
    min_days: int = PROFILE_MIN_DAYS,
    shape: Callable[[PriceProfile], PriceProfile] = lambda profile: profile,
) -> dict[str, Score]:
    """Score the three estimates over the target days.

    Args:
        days: Real prices by local date.
        targets: The days to hide and estimate.
        gap: Days between the last day with prices and the target.
        history_days: Days before the last day with prices in the profile.
        min_days: Days a profile bucket needs.
        shape: Reduces the profile to some of its bucket tables.

    Returns:
        ``nearest``, ``shifted``, ``unshifted`` and ``implemented`` (the
        shifted profile, and the nearest earlier day where it has no value).
    """
    scores = {
        name: Score(name, [])
        for name in ("nearest", "shifted", "unshifted", "implemented")
    }
    for target_date in targets:
        target = days[target_date]
        anchor_date = target_date - timedelta(days=gap)
        profile = shape(
            PriceProfile.from_samples(
                _samples(days, anchor_date - timedelta(days=1), history_days),
                ZONE,
                min_days,
            )
        )
        estimates = estimate_day(target, days[anchor_date], profile)
        for name in ("nearest", "shifted", "unshifted"):
            scores[name].days.append(slot_errors(target, estimates[name]))
        scores["implemented"].days.append(
            slot_errors(target, estimates["shifted"], estimates["nearest"])
        )
    return scores


def target_days(days: dict[date, Day], gap: int = 2) -> list[date]:
    """Return the days that have a full history before them.

    Args:
        days: Real prices by local date.
        gap: Largest gap between the last day with prices and the target, so
            every report scores the same days.
    """
    return [
        day
        for day in sorted(days)
        if all(
            day - timedelta(days=offset) in days
            for offset in range(1, FULL_HISTORY_DAYS + gap + 1)
        )
    ]


def _table(header: Sequence[str], rows: Iterable[Sequence[str]]) -> list[str]:
    """Return a Markdown table."""
    return [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
        *("| " + " | ".join(row) + " |" for row in rows),
    ]


def _score_row(score: Score, reference: Score | None = None) -> list[str]:
    """Return MAE and worst slot per side, and the comparison on export."""
    row = [score.label, str(score.slot_count())]
    for side in range(len(_SIDES)):
        row += [f"{score.mae(side):.3f}", f"{score.worst(side):.3f}"]
    if reference is not None:
        difference, error, wins = compare(score, reference, 1)
        row += [f"{difference:+.3f} ± {error:.3f}", f"{wins:.0%}"]
    return row


_SCORE_HEADER = (
    "Estimate",
    "Slots",
    "Import MAE",
    "Import worst slot",
    "Export MAE",
    "Export worst slot",
)
_COMPARE_HEADER = (*_SCORE_HEADER, "Export MAE vs nearest day", "Days better")


def report_fill_order(days: dict[date, Day], targets: Sequence[date]) -> list[str]:
    """Report the fill order with a full history."""
    full = score_targets(days, targets)
    any_day = score_targets(days, targets, shape=any_day_only)
    rows = [
        replace(full["nearest"], label="Nearest earlier day"),
        replace(full["shifted"], label="Shifted profile, same weekday"),
        replace(any_day["shifted"], label="Shifted profile, every weekday"),
        replace(full["unshifted"], label="Unshifted profile, same weekday"),
    ]
    return _table(
        _COMPARE_HEADER,
        [
            [*_score_row(rows[0]), "", ""],
            *(_score_row(row, rows[0]) for row in rows[1:]),
        ],
    )


def report_bucket_days(days: dict[date, Day], targets: Sequence[date]) -> list[str]:
    """Report the shifted profile by the number of days in a bucket."""
    nearest = score_targets(days, targets)["nearest"]
    rows: list[list[str]] = []
    for count in range(1, 9):
        weekday = score_targets(
            days, targets, history_days=7 * count, min_days=1, shape=weekday_only
        )["shifted"]
        any_day = score_targets(
            days, targets, history_days=count, min_days=1, shape=any_day_only
        )["shifted"]
        cells = [str(count)]
        for score in (weekday, any_day):
            difference, error, wins = compare(score, nearest, 1)
            cells += [
                f"{score.mae(1):.3f}",
                f"{score.worst(1):.3f}",
                f"{difference:+.3f} ± {error:.3f}",
                f"{wins:.0%}",
            ]
        rows.append(cells)
    header = ["Days in bucket"]
    for kind in ("Same weekday", "Every weekday"):
        header += [
            f"{kind}: export MAE",
            f"{kind}: worst slot",
            f"{kind}: vs nearest day",
            f"{kind}: days better",
        ]
    return [
        f"Nearest earlier day: export MAE {nearest.mae(1):.3f}, "
        f"worst slot {nearest.worst(1):.3f}.",
        "",
        *_table(header, rows),
    ]


def report_minimums(days: dict[date, Day], targets: Sequence[date]) -> list[str]:
    """Report the implemented fill order while the history grows."""
    rows: list[list[str]] = []
    for history_days in (3, 4, 5, 6, 7, 14, 21, 28, 35, 42, 56):
        cells = [str(history_days)]
        for minimum in BUCKET_MINIMUMS:
            score = score_targets(
                days, targets, history_days=history_days, min_days=minimum
            )["implemented"]
            cells += [f"{score.mae(1):.3f}", f"{score.worst(1):.3f}"]
        rows.append(cells)
    header = ["History days"]
    for minimum in BUCKET_MINIMUMS:
        header += [f"Min {minimum}: export MAE", f"Min {minimum}: worst slot"]
    return _table(header, rows)


def report_outage(days: dict[date, Day], targets: Sequence[date]) -> list[str]:
    """Report the estimates for an outage after the held prices expired."""
    rows: list[list[str]] = []
    for gap in (1, 2):
        scores = score_targets(days, targets, gap=gap)
        labelled = [
            replace(scores["unshifted"], label="Unshifted profile"),
            replace(scores["shifted"], label="Profile with the last known shift"),
            replace(scores["nearest"], label="Last recorded day"),
        ]
        for score in labelled:
            row = _score_row(score)
            difference, error, wins = compare(score, labelled[0], 1)
            versus = (
                ["", ""]
                if score is labelled[0]
                else [f"{difference:+.3f} ± {error:.3f}", f"{wins:.0%}"]
            )
            rows.append([str(gap), *row, *versus])
    return _table(
        (
            "Days since last price",
            *_SCORE_HEADER,
            "Export MAE vs unshifted",
            "Days better",
        ),
        rows,
    )


def build_report(days: dict[date, Day]) -> list[str]:
    """Return the full report as Markdown lines.

    Args:
        days: Real prices by local date.

    Raises:
        ValueError: When no day has a full history before it.
    """
    targets = target_days(days)
    if not targets:
        raise ValueError(
            f"need more than {FULL_HISTORY_DAYS + 2} consecutive days of prices"
        )
    mean_export = fmean(
        export_price for day in targets for _, _, export_price in days[day]
    )
    return [
        f"Prices: {min(days)} to {max(days)} ({len(days)} days).  "
        f"Target days: {targets[0]} to {targets[-1]} ({len(targets)} days), "
        f"each with {FULL_HISTORY_DAYS} days of history.  "
        f"Mean export price on the target days: {mean_export:.3f} DKK/kWh.  "
        f"Errors in DKK/kWh.",
        "",
        "## 1. Fill order with a full history",
        "",
        *report_fill_order(days, targets),
        "",
        "## 2a. Shifted profile by days in a bucket",
        "",
        *report_bucket_days(days, targets),
        "",
        "## 2b. Implemented fill order while the history grows",
        "",
        *report_minimums(days, targets),
        "",
        "## 3. Outage after the held prices expired",
        "",
        *report_outage(days, targets),
    ]


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line interface."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--prices", type=Path, default=DEFAULT_PRICES)
    parser.add_argument(
        "--fetch",
        nargs=3,
        metavar=("START", "END", "AREA"),
        help="download prices from Energi Data Service and rewrite the price file",
    )
    args = parser.parse_args(argv)
    if args.fetch:
        start, end, area = args.fetch
        write_prices(args.prices, fetch_prices(start, end, area), area)
    print("\n".join(build_report(load_days(args.prices))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
