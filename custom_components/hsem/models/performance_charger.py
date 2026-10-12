"""Per-charger records behind the EV timing score (issue #1271).

What one coordinator cycle read of one EV charger
(:class:`ChargerSample`), and what the charger did in one slot, built up from
such samples (:class:`ChargerRecord`).  Kept apart from
:mod:`custom_components.hsem.models.performance_record` for the 30 KB file
limit, together with the small JSON helpers both modules use.

No Home Assistant imports.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from custom_components.hsem.planner.hindsight_ev import ChargerSlot
from custom_components.hsem.utils.persistence import (
    aware_datetime_from_iso,
    finite_float,
)


def add_optional(total: float | None, part: float | None) -> float | None:
    """Add *part* to *total*; once either is unobserved the sum stays unknown."""
    if total is None or part is None:
        return None
    return total + part


def optional_bool(value: Any) -> bool | None:
    """Return a JSON boolean, or ``None`` for anything else."""
    return value if isinstance(value, bool) else None


def as_list(value: Any) -> list[Any]:
    """Return a JSON list, or an empty one for anything else."""
    return value if isinstance(value, list) else []


def iso(value: datetime | None) -> str | None:
    """Return a datetime as a UTC ISO string for JSON."""
    return None if value is None else value.astimezone(UTC).isoformat()


def utc(value: datetime | None) -> datetime | None:
    """Return a datetime in UTC."""
    return None if value is None else value.astimezone(UTC)


@dataclass(frozen=True)
class ChargerSample:
    """What one coordinator cycle read of one EV charger.

    Attributes:
        power_w: Charging power, or ``None`` when unavailable.
        connected: Whether a car was plugged in, or ``None`` without a
            connected sensor.
        managed: Whether HSEM was managing the charging: EV planning enabled,
            smart charging on and "force charge now" off.
        deadline: The charge deadline in force, if any.
        max_kw: The configured charger power, or ``None`` when not configured.
    """

    power_w: float | None = None
    connected: bool | None = None
    managed: bool = False
    deadline: datetime | None = None
    max_kw: float | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the sample as a plain dict for JSON serialisation."""
        return {
            "power_w": self.power_w,
            "connected": self.connected,
            "managed": self.managed,
            "deadline": iso(self.deadline),
            "max_kw": self.max_kw,
        }

    @classmethod
    def from_dict(cls, data: Any) -> ChargerSample:
        """Rebuild a charger sample from JSON; anything unusable is unknown."""
        if not isinstance(data, dict):
            return cls()
        return cls(
            power_w=finite_float(data.get("power_w")),
            connected=optional_bool(data.get("connected")),
            managed=data.get("managed") is True,
            deadline=utc(aware_datetime_from_iso(data.get("deadline"))),
            max_kw=finite_float(data.get("max_kw"), minimum=0.0),
        )


@dataclass
class ChargerRecord:
    """What one EV charger did in one slot, built up from samples.

    Attributes:
        kwh: Energy delivered, or ``None`` when it was not observed for every
            covered second.
        connected: Whether a car was plugged in throughout what was observed;
            ``None`` when that is not known.
        managed: Whether HSEM was managing the charging throughout.
        deadline: The charge deadline last seen in the slot.
        max_kw: The configured charger power last seen in the slot.
    """

    kwh: float | None = 0.0
    connected: bool | None = True
    managed: bool = True
    deadline: datetime | None = None
    max_kw: float | None = None

    def add(
        self,
        *,
        kwh: float | None,
        connected: bool | None,
        managed: bool,
        deadline: datetime | None,
        max_kw: float | None,
    ) -> None:
        """Add one observed stretch of the slot."""
        self.kwh = add_optional(self.kwh, kwh)
        if connected is None or self.connected is None:
            self.connected = None
        else:
            self.connected = self.connected and connected
        self.managed = self.managed and managed
        self.deadline = deadline
        self.max_kw = max_kw

    def as_slot(self) -> ChargerSlot:
        """Return the record in the form the EV scoring takes."""
        return ChargerSlot(
            kwh=self.kwh,
            connected=self.connected,
            managed=self.managed,
            deadline=self.deadline,
            max_kw=self.max_kw,
        )

    def as_dict(self) -> dict[str, Any]:
        """Return the record as a plain dict for JSON serialisation."""
        return {
            "kwh": self.kwh,
            "connected": self.connected,
            "managed": self.managed,
            "deadline": iso(self.deadline),
            "max_kw": self.max_kw,
        }

    @classmethod
    def from_dict(cls, data: Any) -> ChargerRecord:
        """Rebuild a charger record from JSON; anything unusable is unknown."""
        if not isinstance(data, dict):
            return cls(kwh=None, connected=None, managed=False)
        return cls(
            kwh=finite_float(data.get("kwh")),
            connected=optional_bool(data.get("connected")),
            managed=data.get("managed") is True,
            deadline=utc(aware_datetime_from_iso(data.get("deadline"))),
            max_kw=finite_float(data.get("max_kw"), minimum=0.0),
        )


@dataclass(frozen=True)
class ChargerPiece:
    """What one charger did between two consecutive samples.

    Attributes:
        kwh: Energy delivered, the mean of the two power readings held for
            the interval; ``None`` when either reading is missing.
        connected: Whether a car was plugged in at both samples; ``None``
            when either does not say.
        managed: Whether HSEM was managing the charging at both samples.
        deadline: The charge deadline at the later sample.
        max_kw: The configured charger power at the later sample.
    """

    kwh: float | None
    connected: bool | None
    managed: bool
    deadline: datetime | None
    max_kw: float | None

    def share(self, share: float) -> ChargerPiece:
        """Return the part of this piece that falls into a *share* of its time."""
        return ChargerPiece(
            kwh=None if self.kwh is None else self.kwh * share,
            connected=self.connected,
            managed=self.managed,
            deadline=self.deadline,
            max_kw=self.max_kw,
        )


def charger_pieces(
    before: Sequence[ChargerSample], after: Sequence[ChargerSample], hours: float
) -> tuple[ChargerPiece, ...]:
    """Return what each charger did between two samples *hours* apart.

    Chargers are matched by position.  One that only the later sample knows
    (it was configured in between) is reported with nothing observed.
    """
    pieces: list[ChargerPiece] = []
    for index, now in enumerate(after):
        then = before[index] if index < len(before) else ChargerSample()
        powers = (then.power_w, now.power_w) if index < len(before) else (None, None)
        known = None not in (then.connected, now.connected)
        pieces.append(
            ChargerPiece(
                kwh=(
                    None
                    if powers[0] is None or powers[1] is None
                    else (powers[0] + powers[1]) / 2.0 * hours / 1000.0
                ),
                connected=bool(then.connected and now.connected) if known else None,
                managed=then.managed and now.managed,
                deadline=now.deadline,
                max_kw=now.max_kw,
            )
        )
    return tuple(pieces)


def merge_charger_pieces(
    records: list[ChargerRecord], pieces: Sequence[ChargerPiece], *, first: bool
) -> None:
    """Add one observed stretch of a slot to its per-charger records.

    Args:
        records: The slot's records, extended in place.
        pieces: What each charger did in the stretch.
        first: Whether this is the slot's first stretch.  A charger that
            turns up later was not observed for the whole slot, so its record
            starts out unknown.
    """
    for index, piece in enumerate(pieces):
        if index >= len(records):
            records.append(
                ChargerRecord()
                if first
                else ChargerRecord(kwh=None, connected=None, managed=False)
            )
        records[index].add(
            kwh=piece.kwh,
            connected=piece.connected,
            managed=piece.managed,
            deadline=piece.deadline,
            max_kw=piece.max_kw,
        )
    for record in records[len(pieces) :]:
        record.add(kwh=None, connected=None, managed=False, deadline=None, max_kw=None)
