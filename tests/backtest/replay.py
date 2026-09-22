"""Rebuild a :class:`PlannerInput` from an HSEM diagnostics dump (issue #1037).

``utils/diagnostics.py::_planner_input_to_dict`` serialises the planner input
with ``asdict()``, nulling only ``solar_corrector`` (a runtime object its own
docstring records as "not needed to reproduce planner logic offline").  This
module is the inverse, so a real production cycle can be replayed against the
current code.

Three things do not survive ``asdict()`` + JSON on their own and are rebuilt
here:

* the nested ``HourlyConsumptionAverage`` / ``PricePoint`` / ``SolcastSlot``
  lists, which arrive as plain dicts;
* the ``datetime`` fields (EV deadlines and charger-power holds), which arrive
  as ISO-8601 strings;
* ``solar_corrector``, which is pinned back to ``None``.

Everything the shim could not map is reported on :class:`ReplayReport` rather
than dropped quietly, because a replay that silently loses an input produces
numbers that look real and are not.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Any

from custom_components.hsem.models.hourly_consumption_average import (
    HourlyConsumptionAverage,
)
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.price_point import PricePoint
from custom_components.hsem.models.solcast_slot import SolcastSlot

__all__ = [
    "DATETIME_FIELDS",
    "ReplayReport",
    "load_dump",
    "load_planner_input",
    "planner_input_from_dict",
]

#: Nested dataclass lists that ``asdict()`` flattened into lists of dicts.
_NESTED: dict[str, type[Any]] = {
    "consumption_averages": HourlyConsumptionAverage,
    "price_points": PricePoint,
    "solcast_slots": SolcastSlot,
}


def _datetime_field_names() -> frozenset[str]:
    """Return every ``PlannerInput`` field annotated as a ``datetime``.

    Derived from the dataclass rather than hard-coded so that a datetime field
    added to ``PlannerInput`` later cannot silently start replaying as a raw
    ISO string.  ``PlannerInput`` uses ``from __future__ import annotations``,
    so ``Field.type`` is the annotation source text.

    Returns:
        The names of all datetime-annotated fields.
    """
    return frozenset(f.name for f in fields(PlannerInput) if "datetime" in str(f.type))


#: Fields serialised as ISO-8601 strings that must be parsed back.
DATETIME_FIELDS: frozenset[str] = _datetime_field_names()


@dataclass
class ReplayReport:
    """What the shim had to drop, default, or fail to parse.

    A dump is only a faithful replay when :attr:`is_faithful` is ``True``.
    Fields the dump carries but the current ``PlannerInput`` no longer defines
    (or vice versa) are recorded rather than ignored.

    Attributes:
        dropped: Keys present in the dump but absent from ``PlannerInput``.
        missing: ``PlannerInput`` fields the dump did not carry (left at their
            dataclass defaults).
        dropped_nested: Per nested list, keys dropped from each element.
        malformed_datetimes: Datetime fields whose value was neither ``None``
            nor a parseable ISO-8601 string, mapped to the raw value.  Left at
            ``None`` on the rebuilt input.
        source_version: ``hsem_version`` recorded in the dump.
        dump_timestamp: When the dump was taken.
    """

    dropped: list[str]
    missing: list[str]
    dropped_nested: dict[str, list[str]]
    malformed_datetimes: dict[str, str]
    source_version: str
    dump_timestamp: str

    @property
    def is_faithful(self) -> bool:
        """Return ``True`` when every dump field mapped onto a current field."""
        return not (
            self.dropped
            or self.missing
            or self.dropped_nested
            or self.malformed_datetimes
        )

    def describe(self) -> str:
        """Render a one-paragraph human summary of the replay fidelity.

        Returns:
            A multi-line string naming the source version and anything the
            shim could not map.
        """
        lines = [
            f"hsem_version={self.source_version} dumped={self.dump_timestamp}",
            f"faithful={self.is_faithful}",
        ]
        if self.dropped:
            lines.append(f"  dropped (dump has, PlannerInput lacks): {self.dropped}")
        if self.missing:
            lines.append(f"  missing (defaulted): {self.missing}")
        if self.dropped_nested:
            lines.append(f"  dropped nested keys: {self.dropped_nested}")
        if self.malformed_datetimes:
            lines.append(f"  unparseable datetimes: {self.malformed_datetimes}")
        return "\n".join(lines)


def _build_nested(cls: type[Any], rows: list[Any]) -> tuple[list[Any], list[str]]:
    """Rebuild one nested dataclass list, reporting keys the class lacks.

    Args:
        cls: The nested dataclass to instantiate.
        rows: The serialised rows, each a dict of field name to value.

    Returns:
        The rebuilt objects and the sorted set of keys no longer defined on
        ``cls``.
    """
    known = {f.name for f in fields(cls)}
    dicts = [row for row in rows if isinstance(row, dict)]
    dropped = sorted({k for row in dicts for k in row if k not in known})
    built = [cls(**{k: v for k, v in row.items() if k in known}) for row in dicts]
    return built, dropped


def _parse_datetime(value: Any) -> tuple[datetime | None, str | None]:
    """Parse one serialised datetime field.

    Args:
        value: The raw value from the dump — ``None``, an ISO-8601 string, or
            (when a caller handed the shim an already-rebuilt payload) a
            ``datetime``.

    Returns:
        A ``(parsed, malformed)`` pair.  Exactly one element is ``None``:
        ``malformed`` carries the raw value's ``repr`` when it could not be
        parsed.
    """
    if value is None or isinstance(value, datetime):
        return value, None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value), None
        except ValueError:
            return None, value
    return None, repr(value)


def planner_input_from_dict(
    dump: dict[str, Any],
) -> tuple[PlannerInput, ReplayReport]:
    """Rebuild the planner input recorded in one diagnostics payload.

    Args:
        dump: A diagnostics payload — the ``hsem.export_diagnostics`` service
            response, or the ``data`` section of an HA diagnostics download.

    Returns:
        The rebuilt :class:`PlannerInput` and a :class:`ReplayReport`
        describing anything the shim could not map.

    Raises:
        KeyError: If the payload carries no ``planner_input`` section.
    """
    raw = dump["planner_input"]

    known = {f.name for f in fields(PlannerInput)}
    kwargs: dict[str, Any] = {}
    dropped_nested: dict[str, list[str]] = {}
    malformed: dict[str, str] = {}

    for key, value in raw.items():
        if key not in known:
            continue
        if key in _NESTED and isinstance(value, list):
            built, nested_dropped = _build_nested(_NESTED[key], value)
            kwargs[key] = built
            if nested_dropped:
                dropped_nested[key] = nested_dropped
        elif key in DATETIME_FIELDS:
            parsed, bad = _parse_datetime(value)
            kwargs[key] = parsed
            if bad is not None:
                malformed[key] = bad
        else:
            kwargs[key] = value

    # The solar corrector is a runtime object the dump deliberately nulls out.
    kwargs["solar_corrector"] = None

    report = ReplayReport(
        dropped=sorted(k for k in raw if k not in known),
        missing=sorted(k for k in known if k not in raw),
        dropped_nested=dropped_nested,
        malformed_datetimes=malformed,
        source_version=str(dump.get("hsem_version", "unknown")),
        dump_timestamp=str(dump.get("dump_timestamp", "unknown")),
    )
    return PlannerInput(**kwargs), report


def load_dump(path: str | Path) -> dict[str, Any]:
    """Read a diagnostics JSON file and unwrap it to the HSEM payload.

    Accepts both shapes HSEM produces: the ``hsem.export_diagnostics`` service
    response, and the HA diagnostics download that nests the same payload
    under a top-level ``data`` key.

    Args:
        path: Path to the JSON file.

    Returns:
        The HSEM payload dict, carrying ``planner_input`` at its top level.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    payload = raw.get("data", raw)
    return dict(payload)


def load_planner_input(path: str | Path) -> tuple[PlannerInput, ReplayReport]:
    """Load a diagnostics dump and rebuild the planner input it recorded.

    Args:
        path: Path to a diagnostics JSON file — either the HA diagnostics
            download (wrapped in a top-level ``data`` key) or the
            ``hsem.export_diagnostics`` service response.

    Returns:
        The rebuilt :class:`PlannerInput` and a :class:`ReplayReport`
        describing anything the shim could not map.

    Raises:
        KeyError: If the file carries no ``planner_input`` section.
    """
    return planner_input_from_dict(load_dump(path))
