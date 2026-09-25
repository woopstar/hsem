"""Build a backtest actuals file from a Home Assistant history export.

Thin command-line front end for ``tests/backtest/actuals.py``; see
``docs/backtest-harness.md`` for the workflow and the file format.

Capture the history first (``minimal_response`` and ``no_attributes`` keep the
response small; the parser handles both)::

    curl -sG -H "Authorization: Bearer $HA_TOKEN" \
      --data-urlencode "end_time=2026-09-22T00:00:00+02:00" \
      --data-urlencode "filter_entity_id=sensor.pv_energy,sensor.house_energy" \
      --data minimal_response --data no_attributes \
      "$HA_URL/api/history/period/2026-09-21T00:00:00+02:00" > history.json

Then convert every history file in one call, so day boundaries are stitched::

    python3 scripts/build_actuals.py history-*.json \
        --map sensor.pv_energy=pv_produced \
        --map sensor.house_energy=house_load \
        --slot-minutes 15 \
        --out actuals.json

Export at least one chatty entity (house load is ideal) with every run: the
outage check needs *something* reporting to tell a flat meter from a stopped
recorder.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from custom_components.hsem.utils.datetime_utils import (  # noqa: E402
    normalize_datetime,
)
from tests.backtest.actuals import (  # noqa: E402
    ENERGY_SERIES,
    VALUE_SERIES,
    build_actuals_payload,
    readings_from_ha_history,
)


def _parse_mapping(pairs: list[str]) -> dict[str, str]:
    """Parse ``entity_id=series`` arguments into a mapping.

    Args:
        pairs: Raw ``--map`` values.

    Returns:
        Entity id to series name.

    Raises:
        SystemExit: If a pair is malformed or names an unknown series.
    """
    known = {*ENERGY_SERIES, *VALUE_SERIES}
    mapping: dict[str, str] = {}
    for pair in pairs:
        entity_id, _, series = pair.partition("=")
        if not entity_id or not series:
            raise SystemExit(f"--map expects entity_id=series, got {pair!r}")
        if series not in known:
            raise SystemExit(f"unknown series {series!r}; choose from {sorted(known)}")
        mapping[entity_id] = series
    return mapping


def main(argv: list[str] | None = None) -> int:
    """Convert an HA history export into an ``hsem-actuals-1`` file.

    Args:
        argv: Command-line arguments, defaulting to ``sys.argv[1:]``.

    Returns:
        ``0`` when at least one series carried data, ``1`` otherwise.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("history", nargs="+", help="HA history period JSON file(s)")
    parser.add_argument(
        "--map",
        action="append",
        default=[],
        metavar="ENTITY=SERIES",
        help=f"repeatable; SERIES is one of {sorted({*ENERGY_SERIES, *VALUE_SERIES})}",
    )
    parser.add_argument("--slot-minutes", type=int, default=15)
    parser.add_argument(
        "--now",
        help=(
            "ISO timestamp of the export; the slot containing it is incomplete "
            "and is dropped. Defaults to the latest reading seen."
        ),
    )
    parser.add_argument(
        "--max-silence-minutes",
        type=int,
        default=10,
        help=(
            "Longest silence across all exported entities before the slots it "
            "overlaps are treated as a recorder outage (default: 10)."
        ),
    )
    parser.add_argument("--out", required=True, metavar="PATH")
    args = parser.parse_args(argv)

    mapping = _parse_mapping(args.map)
    if not mapping:
        raise SystemExit("at least one --map is required")

    readings: dict[str, list[tuple]] = {}
    for path in args.history:
        blocks = json.loads(Path(path).read_text(encoding="utf-8"))
        for entity_id, rows in readings_from_ha_history(blocks).items():
            readings.setdefault(entity_id, []).extend(rows)
    for rows in readings.values():
        rows.sort(key=lambda row: row[0])

    latest = max((rows[-1][0] for rows in readings.values() if rows), default=None)
    if args.now:
        now = normalize_datetime(datetime.fromisoformat(args.now))
    elif latest is not None:
        now = latest
    else:
        raise SystemExit("no usable readings found in the history export")

    payload = build_actuals_payload(
        readings,
        mapping,
        now,
        args.slot_minutes,
        max_silence=timedelta(minutes=args.max_silence_minutes),
    )
    Path(args.out).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    energy = payload["slot_energy_kwh"]
    values = payload["slot_values"]
    for name, rows in sorted({**energy, **values}.items()):
        print(f"{name}: {len(rows)} slot(s)")
    for entity_id, series in sorted(mapping.items()):
        if series not in energy and series not in values:
            print(f"[warn] {entity_id} -> {series}: no usable readings")
    print(f"wrote {args.out}")
    return 0 if (energy or values) else 1


if __name__ == "__main__":
    raise SystemExit(main())
