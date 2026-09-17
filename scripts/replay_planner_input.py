"""Rebuild a :class:`PlannerInput` from an HSEM diagnostics dump.

Measurement support for issue #1036.  ``hsem.export_diagnostics`` (and the HA
diagnostics download) serialise the planner input via
``utils/diagnostics.py::_planner_input_to_dict``; this is the inverse, so a real
production cycle can be replayed offline against the current code.

Not imported by the integration — this is an analysis tool.

Usage::

    from scripts.replay_planner_input import load_planner_input
    inp, report = load_planner_input("logs/extati-diagnostics.json")
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from custom_components.hsem.models.hourly_consumption_average import (
    HourlyConsumptionAverage,
)
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.price_point import PricePoint
from custom_components.hsem.models.solcast_slot import SolcastSlot

_NESTED: dict[str, Any] = {
    "consumption_averages": HourlyConsumptionAverage,
    "price_points": PricePoint,
    "solcast_slots": SolcastSlot,
}


@dataclass
class ReplayReport:
    """What the shim had to drop or default when rebuilding the input.

    A dump is only a faithful replay if this is empty.  Fields the dump carries
    but the current ``PlannerInput`` no longer defines (or vice versa) are
    recorded rather than silently ignored, because a replay that quietly drops
    inputs would produce numbers that look real and are not.

    Attributes:
        dropped: Keys present in the dump but absent from ``PlannerInput``.
        missing: ``PlannerInput`` fields the dump did not carry (left at their
            dataclass defaults).
        dropped_nested: Per nested list, keys dropped from each element.
        source_version: ``hsem_version`` recorded in the dump.
        dump_timestamp: When the dump was taken.
    """

    dropped: list[str]
    missing: list[str]
    dropped_nested: dict[str, list[str]]
    source_version: str
    dump_timestamp: str

    @property
    def is_faithful(self) -> bool:
        """True when every dump field mapped onto a current input field."""
        return not self.dropped and not self.missing and not self.dropped_nested


def _build_nested(cls: Any, rows: list[dict[str, Any]]) -> tuple[list[Any], list[str]]:
    """Rebuild one nested dataclass list, reporting keys the class lacks."""
    known = {f.name for f in fields(cls)}
    dropped = sorted({k for row in rows for k in row if k not in known})
    built = [cls(**{k: v for k, v in row.items() if k in known}) for row in rows]
    return built, dropped


def load_planner_input(path: str | Path) -> tuple[PlannerInput, ReplayReport]:
    """Load a diagnostics dump and rebuild the planner input it recorded.

    Args:
        path: Path to a diagnostics JSON file — either the HA diagnostics
            download (wrapped in a top-level ``data`` key) or the
            ``hsem.export_diagnostics`` service response.

    Returns:
        The rebuilt :class:`PlannerInput` and a :class:`ReplayReport` describing
        anything the shim could not map.

    Raises:
        KeyError: If the file carries no ``planner_input`` section.
    """
    raw = json.loads(Path(path).read_text())
    payload = raw.get("data", raw)
    dump = payload["planner_input"]

    known = {f.name for f in fields(PlannerInput)}
    dropped = sorted(k for k in dump if k not in known)
    missing = sorted(k for k in known if k not in dump)

    kwargs: dict[str, Any] = {}
    dropped_nested: dict[str, list[str]] = {}
    for key, value in dump.items():
        if key not in known:
            continue
        if key in _NESTED and isinstance(value, list):
            built, nested_dropped = _build_nested(_NESTED[key], value)
            kwargs[key] = built
            if nested_dropped:
                dropped_nested[key] = nested_dropped
        else:
            kwargs[key] = value

    # The solar corrector is a runtime object the dump deliberately nulls out.
    kwargs["solar_corrector"] = None

    report = ReplayReport(
        dropped=dropped,
        missing=missing,
        dropped_nested=dropped_nested,
        source_version=str(payload.get("hsem_version", "unknown")),
        dump_timestamp=str(payload.get("dump_timestamp", "unknown")),
    )
    return PlannerInput(**kwargs), report
