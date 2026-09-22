"""Load realized outcomes and align them to planner slots (issue #1037, Stage 2).

Stage 1 replays *forecasts*: a ``planner_input`` carries ``solcast_slots`` and
``consumption_averages``, never what actually happened.  Scoring a plan — savings
against a no-action baseline, regret against a perfect-foresight oracle — needs
realized PV, house load, grid flows, battery SoC and prices on the same slot
grid the planner used.  This module is the loading and alignment half of that.

It deliberately stops short of scoring.  What it produces is a per-slot
``SlotActuals`` list plus an :class:`AlignmentReport` saying how much of the
horizon is actually covered, because "how many slots can I score?" is the first
question any scoring pass has to answer.

Missing is not zero
-------------------
Every field is ``float | None`` and a slot with no observation stays ``None``.
This is the same rule the planner itself follows for telemetry (issues #988 and
#1056), and it matters more here than anywhere else: silently reading an absent
actual as ``0.0`` would make a plan's realized cost look better than it was, and
regret is a *difference* of two costs, so the error does not cancel.

``HistoryReader`` drops zero deltas, so a series like PV is genuinely absent
overnight rather than present-and-zero.  Where absence really does mean zero,
say so explicitly with :meth:`Actuals.fill_absent_with_zero` — never by
defaulting.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from custom_components.hsem.ml.history_reader import HistoryReader
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.utils.datetime_utils import slot_key

__all__ = [
    "ACTUALS_SCHEMA",
    "ENERGY_SERIES",
    "VALUE_SERIES",
    "Actuals",
    "AlignmentReport",
    "SlotActuals",
    "align_to_slots",
    "build_actuals_payload",
    "load_actuals",
    "readings_from_ha_history",
    "slot_deltas_from_readings",
]

#: Version tag every actuals file must carry, so a format change is loud.
ACTUALS_SCHEMA = "hsem-actuals-1"

#: Series recorded as per-slot integrated energy deltas, in kWh.
ENERGY_SERIES: tuple[str, ...] = (
    "pv_produced",
    "house_load",
    "grid_import",
    "grid_export",
)

#: Series recorded as one scalar per slot — a level sampled at the slot start,
#: or a price that applies across the slot.
VALUE_SERIES: tuple[str, ...] = (
    "battery_soc_pct",
    "import_price",
    "export_price",
)


@dataclass(frozen=True)
class SlotActuals:
    """What actually happened during one planner slot.

    Every measurement is optional.  ``None`` means "not observed", which is a
    different statement from ``0.0`` and must never be collapsed into it.

    Attributes:
        start: Slot start, copied from the planner slot it aligns to.
        end: Slot end, copied from the planner slot it aligns to.
        pv_produced_kwh: Realized PV production.
        house_load_kwh: Realized house consumption.
        grid_import_kwh: Realized grid import.
        grid_export_kwh: Realized grid export.
        battery_soc_pct: Battery state of charge sampled at the slot start.
        import_price: Realized import price for the slot.
        export_price: Realized export price for the slot.
    """

    start: datetime
    end: datetime
    pv_produced_kwh: float | None = None
    house_load_kwh: float | None = None
    grid_import_kwh: float | None = None
    grid_export_kwh: float | None = None
    battery_soc_pct: float | None = None
    import_price: float | None = None
    export_price: float | None = None

    @property
    def has_energy_balance(self) -> bool:
        """Return ``True`` when all four energy series were observed.

        A slot missing any of them cannot be scored: the realized cost of a
        slot is a function of its grid flows, and its regret additionally needs
        the PV and load an oracle would have optimised against.
        """
        return None not in (
            self.pv_produced_kwh,
            self.house_load_kwh,
            self.grid_import_kwh,
            self.grid_export_kwh,
        )

    @property
    def has_prices(self) -> bool:
        """Return ``True`` when both realized prices were observed."""
        return self.import_price is not None and self.export_price is not None

    @property
    def is_scorable(self) -> bool:
        """Return ``True`` when this slot carries everything scoring needs."""
        return self.has_energy_balance and self.has_prices


@dataclass(frozen=True)
class AlignmentReport:
    """How much of a plan's horizon the actuals actually cover.

    Attributes:
        slot_count: Number of planner slots the actuals were aligned to.
        covered: Per series, how many slots carried an observation.
        scorable_slots: Slots carrying every energy series and both prices.
        unknown_series: Series names in the file that this module does not
            recognise; loaded but never aligned.
        zero_filled: Series the caller explicitly declared absent-means-zero.
        first_covered: Start of the earliest slot with any observation.
        last_covered: Start of the latest slot with any observation.
    """

    slot_count: int
    covered: dict[str, int]
    scorable_slots: int
    unknown_series: tuple[str, ...] = ()
    zero_filled: tuple[str, ...] = ()
    first_covered: datetime | None = None
    last_covered: datetime | None = None

    @property
    def is_complete(self) -> bool:
        """Return ``True`` when every slot in the horizon is scorable."""
        return self.slot_count > 0 and self.scorable_slots == self.slot_count

    def describe(self) -> str:
        """Render a readable coverage summary.

        Returns:
            A multi-line string naming per-series coverage and the scorable
            slot count, suitable for printing from an analysis script.
        """
        lines = [
            f"aligned {self.slot_count} slot(s); "
            f"{self.scorable_slots} scorable "
            f"({'complete' if self.is_complete else 'partial'})"
        ]
        for name in (*ENERGY_SERIES, *VALUE_SERIES):
            got = self.covered.get(name, 0)
            lines.append(f"  {name}: {got}/{self.slot_count}")
        if self.zero_filled:
            lines.append(f"  zero-filled by request: {list(self.zero_filled)}")
        if self.unknown_series:
            lines.append(f"  unknown series ignored: {list(self.unknown_series)}")
        if self.first_covered and self.last_covered:
            lines.append(
                f"  covered {self.first_covered.isoformat()} → "
                f"{self.last_covered.isoformat()}"
            )
        return "\n".join(lines)


@dataclass
class Actuals:
    """Realized series keyed by canonical slot key.

    Both buckets map a :func:`slot_key` to one float.  Keeping the canonical
    key rather than a raw timestamp is what makes alignment survive timezone
    objects that differ, microsecond jitter, and both folds of an autumn
    repeated hour.

    Attributes:
        slot_minutes: Slot width the series were computed at.  Aligning to a
            plan with a different width is an error, not a resample.
        energy_kwh: Per-series per-slot integrated energy, in kWh.
        values: Per-series per-slot scalar values.
        unknown_series: Series names the file carried that are not recognised.
        zero_filled: Series the caller declared absent-means-zero.
        source: Where the actuals were loaded from, for reporting.
    """

    slot_minutes: int
    energy_kwh: dict[str, dict[datetime, float]] = field(default_factory=dict)
    values: dict[str, dict[datetime, float]] = field(default_factory=dict)
    unknown_series: tuple[str, ...] = ()
    zero_filled: tuple[str, ...] = ()
    source: str = "<memory>"

    def fill_absent_with_zero(self, *series: str) -> None:
        """Declare that an absent observation means zero for *series*.

        ``HistoryReader`` drops zero deltas, so a PV series is genuinely absent
        overnight rather than present-and-zero.  Treating that as missing would
        make a night unscorable; treating it as zero *by default* would hide a
        real export failure.  So it is neither — it is this call, which the
        alignment report then names.

        Args:
            *series: Series names, from :data:`ENERGY_SERIES` or
                :data:`VALUE_SERIES`.

        Raises:
            KeyError: If a name is not a recognised series.
        """
        known = {*ENERGY_SERIES, *VALUE_SERIES}
        for name in series:
            if name not in known:
                raise KeyError(f"unknown actuals series: {name!r}")
        self.zero_filled = tuple(sorted({*self.zero_filled, *series}))

    def _lookup(self, series: str, key: datetime) -> float | None:
        """Return one observation, honouring an explicit zero-fill request."""
        bucket = self.energy_kwh if series in ENERGY_SERIES else self.values
        observed = bucket.get(series, {}).get(key)
        if observed is not None:
            return observed
        if series in self.zero_filled:
            return 0.0
        return None


def slot_deltas_from_readings(
    readings: Sequence[tuple[datetime, float]],
    now: datetime,
    slot_minutes: int,
) -> dict[datetime, float]:
    """Turn raw accumulator readings into per-slot energy keyed by slot key.

    Delegates the hard part to ``HistoryReader._compute_slot_deltas`` rather
    than reimplementing it.  That routine already handles meter resets, gaps
    that must not become one oversized slot, implausible deltas, and DST folds
    — duplicating any of it here would give the harness a second, quietly
    diverging opinion about what a slot's energy was.

    Args:
        readings: ``(timestamp, accumulator_value)`` pairs, as the HA history
            API returns them for a ``TOTAL_INCREASING`` sensor.
        now: The moment the export was taken.  The slot containing it is
            incomplete and is dropped.
        slot_minutes: Slot width in minutes.

    Returns:
        A mapping of canonical slot key to energy in kWh.  Slots with a zero
        or unusable delta are absent, not zero.
    """
    rows = HistoryReader._compute_slot_deltas(list(readings), now, slot_minutes)
    return {slot_key(start, slot_minutes): kwh for start, _index, kwh in rows}


def _series_from_pairs(
    raw: Any, slot_minutes: int
) -> tuple[dict[str, dict[datetime, float]], list[str]]:
    """Rebuild one bucket of ``[[iso, value], ...]`` pairs into slot-keyed dicts.

    Args:
        raw: The bucket as loaded from JSON.
        slot_minutes: Slot width used to derive canonical keys.

    Returns:
        The rebuilt bucket and the names it carried, so the caller can decide
        which are recognised.
    """
    bucket: dict[str, dict[datetime, float]] = {}
    names: list[str] = []
    if not isinstance(raw, dict):
        return bucket, names
    for name, pairs in raw.items():
        names.append(name)
        series: dict[datetime, float] = {}
        for entry in pairs or ():
            timestamp, value = entry[0], entry[1]
            series[slot_key(datetime.fromisoformat(timestamp), slot_minutes)] = float(
                value
            )
        bucket[name] = series
    return bucket, names


def load_actuals(path: str | Path) -> Actuals:
    """Load an actuals file produced by the collection recipe.

    See ``docs/backtest-harness.md`` for the format and for how to export one
    from Home Assistant.

    Args:
        path: Path to the JSON file.

    Returns:
        The loaded :class:`Actuals`.

    Raises:
        ValueError: If the file carries no or an unsupported ``schema``, or no
            usable ``slot_minutes``.
    """
    source = Path(path)
    raw = json.loads(source.read_text(encoding="utf-8"))

    schema = raw.get("schema")
    if schema != ACTUALS_SCHEMA:
        raise ValueError(
            f"{source}: expected schema {ACTUALS_SCHEMA!r}, got {schema!r}"
        )
    slot_minutes = raw.get("slot_minutes")
    if not isinstance(slot_minutes, int) or slot_minutes <= 0:
        raise ValueError(f"{source}: slot_minutes must be a positive int")

    energy, energy_names = _series_from_pairs(raw.get("slot_energy_kwh"), slot_minutes)
    values, value_names = _series_from_pairs(raw.get("slot_values"), slot_minutes)
    unknown = tuple(
        sorted(
            name
            for name in (*energy_names, *value_names)
            if name not in ENERGY_SERIES and name not in VALUE_SERIES
        )
    )
    return Actuals(
        slot_minutes=slot_minutes,
        energy_kwh={k: v for k, v in energy.items() if k in ENERGY_SERIES},
        values={k: v for k, v in values.items() if k in VALUE_SERIES},
        unknown_series=unknown,
        source=str(source),
    )


def align_to_slots(
    actuals: Actuals, slots: Sequence[PlannedSlot]
) -> tuple[list[SlotActuals], AlignmentReport]:
    """Align realized series onto a plan's slot grid.

    Matching is by :func:`slot_key` on both sides — never by wall-clock
    equality, which would collapse the two folds of an autumn repeated hour and
    fail outright between a ``ZoneInfo`` slot and a fixed-offset export.

    Args:
        actuals: The loaded realized series.
        slots: The planner slots to align to, in chronological order.

    Returns:
        One :class:`SlotActuals` per planner slot — ``None`` wherever nothing
        was observed — and an :class:`AlignmentReport` describing coverage.

    Raises:
        ValueError: If the actuals were computed at a different slot width than
            the plan uses.  Resampling is a decision with real consequences for
            a scoring pass, so it is refused rather than done silently.
    """
    if slots:
        plan_minutes = round((slots[0].end - slots[0].start).total_seconds() / 60.0)
        if plan_minutes != actuals.slot_minutes:
            raise ValueError(
                f"actuals are {actuals.slot_minutes}-minute slots but the plan "
                f"uses {plan_minutes}-minute slots; re-export the actuals at "
                f"the plan's width rather than resampling"
            )

    aligned: list[SlotActuals] = []
    covered = dict.fromkeys((*ENERGY_SERIES, *VALUE_SERIES), 0)
    first_covered: datetime | None = None
    last_covered: datetime | None = None

    for slot in slots:
        key = slot_key(slot.start, actuals.slot_minutes)
        observed = {
            name: actuals._lookup(name, key) for name in (*ENERGY_SERIES, *VALUE_SERIES)
        }
        for name, value in observed.items():
            if value is not None:
                covered[name] += 1
        if any(value is not None for value in observed.values()):
            first_covered = first_covered or slot.start
            last_covered = slot.start
        aligned.append(
            SlotActuals(
                start=slot.start,
                end=slot.end,
                pv_produced_kwh=observed["pv_produced"],
                house_load_kwh=observed["house_load"],
                grid_import_kwh=observed["grid_import"],
                grid_export_kwh=observed["grid_export"],
                battery_soc_pct=observed["battery_soc_pct"],
                import_price=observed["import_price"],
                export_price=observed["export_price"],
            )
        )

    report = AlignmentReport(
        slot_count=len(slots),
        covered=covered,
        scorable_slots=sum(1 for row in aligned if row.is_scorable),
        unknown_series=actuals.unknown_series,
        zero_filled=actuals.zero_filled,
        first_covered=first_covered,
        last_covered=last_covered,
    )
    return aligned, report


# ---------------------------------------------------------------------------
# Building an actuals file from a Home Assistant history export
# ---------------------------------------------------------------------------

#: State strings the recorder uses for "no reading", which must never be
#: parsed as a number.  A sensor that was unavailable produced no observation.
_NON_NUMERIC_STATES: frozenset[str] = frozenset({"unknown", "unavailable", "none", ""})


def readings_from_ha_history(
    blocks: Sequence[Sequence[dict[str, Any]]],
) -> dict[str, list[tuple[datetime, float]]]:
    """Parse a Home Assistant ``/api/history/period`` response into readings.

    The response is a list of per-entity blocks.  With ``minimal_response`` the
    entries after the first carry only ``state`` and a timestamp, so the
    entity id is taken from the block's first entry.

    Non-numeric states (``unknown``, ``unavailable``) are skipped rather than
    coerced: an unavailable sensor produced no observation, and turning that
    into a number is the exact mistake this module exists to prevent.

    Args:
        blocks: The decoded history response.

    Returns:
        Per entity id, its ``(timestamp, value)`` readings sorted oldest-first.
    """
    readings: dict[str, list[tuple[datetime, float]]] = {}
    for block in blocks:
        entity_id = ""
        for entry in block:
            entity_id = entry.get("entity_id") or entity_id
            if not entity_id:
                continue
            state = str(entry.get("state", "")).strip()
            if state.lower() in _NON_NUMERIC_STATES:
                continue
            stamp = entry.get("last_changed") or entry.get("last_updated")
            if not stamp:
                continue
            try:
                value = float(state)
            except ValueError:
                continue
            readings.setdefault(entity_id, []).append(
                (datetime.fromisoformat(str(stamp)), value)
            )
    for rows in readings.values():
        rows.sort(key=lambda row: row[0])
    return readings


def _sampled_by_slot(
    readings: Sequence[tuple[datetime, float]], slot_minutes: int
) -> dict[datetime, float]:
    """Reduce level readings to one value per slot — the earliest in the slot.

    A level (battery SoC, a price) is sampled *at* the slot start, so the
    reading that best represents the slot is the first one inside it.

    Args:
        readings: ``(timestamp, value)`` pairs, in any order.
        slot_minutes: Slot width in minutes.

    Returns:
        A mapping of canonical slot key to the earliest value in that slot.
    """
    best: dict[datetime, tuple[datetime, float]] = {}
    for timestamp, value in readings:
        key = slot_key(timestamp, slot_minutes)
        current = best.get(key)
        if current is None or timestamp < current[0]:
            best[key] = (timestamp, value)
    return {key: value for key, (_stamp, value) in best.items()}


def build_actuals_payload(
    readings: dict[str, list[tuple[datetime, float]]],
    mapping: dict[str, str],
    now: datetime,
    slot_minutes: int,
) -> dict[str, Any]:
    """Assemble an ``hsem-actuals-1`` payload from raw entity readings.

    Energy series are integrated into per-slot deltas via
    :func:`slot_deltas_from_readings`; value series are sampled per slot.
    Entities named in *mapping* with no usable readings are simply absent from
    the result — which the alignment report will show as zero coverage rather
    than as zeroes.

    Args:
        readings: Per entity id, its ``(timestamp, value)`` readings.
        mapping: Entity id to series name, from :data:`ENERGY_SERIES` or
            :data:`VALUE_SERIES`.
        now: The moment the export was taken; the slot containing it is
            incomplete and is dropped from energy series.
        slot_minutes: Slot width in minutes.

    Returns:
        A JSON-serialisable payload ready for :func:`load_actuals`.

    Raises:
        KeyError: If *mapping* names a series this module does not recognise.
    """
    energy: dict[str, dict[str, float]] = {}
    values: dict[str, dict[str, float]] = {}
    for entity_id, series in mapping.items():
        if series in ENERGY_SERIES:
            keyed = slot_deltas_from_readings(
                readings.get(entity_id, []), now, slot_minutes
            )
            target = energy
        elif series in VALUE_SERIES:
            keyed = _sampled_by_slot(readings.get(entity_id, []), slot_minutes)
            target = values
        else:
            raise KeyError(f"unknown actuals series: {series!r}")
        if keyed:
            target[series] = {k.isoformat(): v for k, v in keyed.items()}

    return {
        "schema": ACTUALS_SCHEMA,
        "slot_minutes": slot_minutes,
        "slot_energy_kwh": {
            name: sorted(rows.items()) for name, rows in sorted(energy.items())
        },
        "slot_values": {
            name: sorted(rows.items()) for name, rows in sorted(values.items())
        },
    }
