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

Zero is an observation
----------------------
Home Assistant records a state only when it changes, so an accumulator that
did not move for six hours has no history rows in those six hours.  Energy is
therefore taken from the value *in force* at each slot boundary, and a slot in
which nothing flowed is ``0.0`` — an observation, not a gap.  What makes that
safe is the outage check in :func:`build_actuals_payload`: a flat stretch and a
recorder outage look identical on one sensor, but not across all of them.
"""

from __future__ import annotations

import json
import math
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from custom_components.hsem.ml.history_reader import MAX_SLOT_KWH
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.utils.datetime_utils import slot_key, utc_key

__all__ = [
    "ACTUALS_SCHEMA",
    "PriceComparison",
    "ENERGY_SERIES",
    "VALUE_SERIES",
    "Actuals",
    "AlignmentReport",
    "SlotActuals",
    "align_to_slots",
    "compare_prices",
    "DEFAULT_MAX_SILENCE",
    "build_actuals_payload",
    "load_actuals",
    "readings_from_ha_history",
    "slot_energy_from_readings",
    "slot_values_from_readings",
]

#: Version tag every actuals file must carry, so a format change is loud.
ACTUALS_SCHEMA = "hsem-actuals-1"

#: Series recorded as per-slot integrated energy deltas, in kWh.
ENERGY_SERIES: tuple[str, ...] = (
    "pv_produced",
    "house_load",
    "grid_import",
    "grid_export",
    "battery_charged",
    "battery_discharged",
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
        battery_charged_kwh: Realized energy charged into the battery.
        battery_discharged_kwh: Realized energy discharged from the battery.
        battery_soc_pct: Battery state of charge sampled at the slot start.
        import_price: Realized import price for the slot.  Usually absent —
            see :attr:`has_prices`.
        export_price: Realized export price for the slot.
    """

    start: datetime
    end: datetime
    pv_produced_kwh: float | None = None
    house_load_kwh: float | None = None
    grid_import_kwh: float | None = None
    grid_export_kwh: float | None = None
    battery_charged_kwh: float | None = None
    battery_discharged_kwh: float | None = None
    battery_soc_pct: float | None = None
    import_price: float | None = None
    export_price: float | None = None

    @property
    def has_energy_balance(self) -> bool:
        """Return ``True`` when all four energy series were observed.

        A slot missing any of them cannot be scored: the realized cost of a
        slot is a function of its grid flows, and its regret additionally needs
        the PV and load an oracle would have optimised against.  The realized
        battery flows are *not* required here — they sharpen the comparison but
        a plan can be scored without them.
        """
        return None not in (
            self.pv_produced_kwh,
            self.house_load_kwh,
            self.grid_import_kwh,
            self.grid_export_kwh,
        )

    @property
    def has_prices(self) -> bool:
        """Return ``True`` when both realized prices were observed.

        Worth having: the planner reads its prices straight from the configured
        price sensors, so those sensors' recorded state *is* the price a slot
        was settled at.  Exporting them makes realized cost computable for the
        whole recorder window, with no matching dump needed.

        Two things to confirm for a given installation rather than assume.  The
        sensor must already include tariffs (Energi Data Service does; a bare
        spot feed does not), and ``hsem_export_fee_per_kwh``, if set, is
        subtracted from the export price by the planner and must be subtracted
        here too.  ``collect_actuals.sh --verify`` cross-checks both against a
        dump's own ``price_points`` on overlapping slots.

        Not required by :attr:`is_scorable`: an energy-only export still
        supports every comparison that does not need money.
        """
        return self.import_price is not None and self.export_price is not None

    @property
    def has_battery_flows(self) -> bool:
        """Return ``True`` when realized battery charge and discharge were seen.

        Optional, but it is the difference between measuring what the battery
        did and inferring it from SoC deltas under assumed efficiencies.
        """
        return (
            self.battery_charged_kwh is not None
            and self.battery_discharged_kwh is not None
        )

    @property
    def is_scorable(self) -> bool:
        """Return ``True`` when this slot carries everything scoring needs.

        Prices are deliberately not required — see :attr:`has_prices`.
        """
        return self.has_energy_balance


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

        Rarely the right call for a file built by :func:`build_actuals_payload`:
        there, a slot in which nothing flowed is already ``0.0``, and absence
        means the value could not be established — an outage, a meter reset, a
        slot before the sensor's first reading.  Zero-filling those hides
        exactly what the report exists to show.  It is for hand-built files, or
        sources that genuinely omit zeros; the alignment report names every
        series it was applied to.

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
                battery_charged_kwh=observed["battery_charged"],
                battery_discharged_kwh=observed["battery_discharged"],
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

#: State strings the recorder uses for "no reading".  They are kept, as a
#: ``None`` value, rather than dropped: an unavailable sensor is a *known* gap,
#: and the slots it spans must come out missing instead of being bridged by
#: carrying the last good value across them.
_NON_NUMERIC_STATES: frozenset[str] = frozenset({"unknown", "unavailable", "none", ""})

#: Longest silence across *every* exported entity before the slots it overlaps
#: are treated as unobserved.  A running system reports constantly — house load
#: changes by the second — so ten quiet minutes means the recorder was not
#: running, not that nothing happened.
DEFAULT_MAX_SILENCE = timedelta(minutes=10)

#: Tolerance for "the accumulator went down", i.e. a meter reset.
_RESET_EPS = 1e-9

#: One recorded state: when, and its value — ``None`` while unavailable.
Reading = tuple[datetime, float | None]


def readings_from_ha_history(
    blocks: Sequence[Sequence[dict[str, Any]]],
) -> dict[str, list[Reading]]:
    """Parse a Home Assistant ``/api/history/period`` response into readings.

    The response is a list of per-entity blocks.  With ``minimal_response`` the
    entries after the first carry only ``state`` and a timestamp, so the
    entity id is taken from the block's first entry.

    Non-numeric states (``unknown``, ``unavailable``) are kept as ``None``,
    never coerced to a number and never silently dropped: an unavailable
    stretch is a known gap, and dropping the row would let the previous value
    carry straight across it.

    Args:
        blocks: The decoded history response.

    Returns:
        Per entity id, its readings sorted oldest-first by physical instant.
    """
    readings: dict[str, list[Reading]] = {}
    for block in blocks:
        entity_id = ""
        for entry in block:
            entity_id = entry.get("entity_id") or entity_id
            if not entity_id:
                continue
            stamp = entry.get("last_changed") or entry.get("last_updated")
            if not stamp:
                continue
            state = str(entry.get("state", "")).strip()
            value: float | None = None
            if state.lower() not in _NON_NUMERIC_STATES:
                try:
                    parsed = float(state)
                except ValueError:
                    parsed = math.nan
                value = parsed if math.isfinite(parsed) else None
            readings.setdefault(entity_id, []).append(
                (datetime.fromisoformat(str(stamp)), value)
            )
    for rows in readings.values():
        rows.sort(key=lambda row: utc_key(row[0]))
    return readings


def _step_series(
    readings: Sequence[Reading],
) -> tuple[list[datetime], list[float | None]]:
    """Split readings into parallel UTC-time and value lists for bisection."""
    ordered = sorted(readings, key=lambda row: utc_key(row[0]))
    return [utc_key(t) for t, _v in ordered], [v for _t, v in ordered]


def _value_at(
    times: list[datetime], values: list[float | None], at: datetime
) -> float | None:
    """Return the value in force at *at*: the last reading at or before it.

    Home Assistant writes a state only when it changes, so a sensor that has
    not moved for hours has no rows in those hours — its value persisted.

    Returns:
        ``None`` before the first reading, and while the reading in force was
        unavailable.
    """
    index = bisect_right(times, utc_key(at)) - 1
    return values[index] if index >= 0 else None


def _slot_keys(first: datetime, now: datetime, slot_minutes: int) -> list[datetime]:
    """Return every *complete* slot from the one containing *first* up to *now*."""
    step = timedelta(minutes=slot_minutes)
    key = slot_key(first, slot_minutes)
    end = utc_key(now)
    keys: list[datetime] = []
    while key + step <= end:
        keys.append(key)
        key += step
    return keys


def _observed_slots(
    event_times: Sequence[datetime],
    keys: Sequence[datetime],
    slot_minutes: int,
    now: datetime,
    max_silence: timedelta,
) -> set[datetime]:
    """Return the slots during which Home Assistant was demonstrably recording.

    A flat accumulator and a recorder outage are indistinguishable on one
    sensor: both leave no rows.  Across every exported entity they are not — a
    running system keeps reporting *something*, while an outage silences
    everything at once.  A slot overlapping any silence longer than
    *max_silence* (including the tail up to *now*), or starting before the
    first event, is unobserved.

    Args:
        event_times: Every reading's timestamp, from every entity, UTC-sorted.
        keys: Candidate slot keys.
        slot_minutes: Slot width in minutes.
        now: The moment the export was taken.
        max_silence: Longest tolerated gap between consecutive events.

    Returns:
        The subset of *keys* that can be trusted.
    """
    if not event_times:
        return set()
    step = timedelta(minutes=slot_minutes)
    gaps = [
        (a, b)
        for a, b in zip(event_times, event_times[1:], strict=False)
        if b - a > max_silence
    ]
    end = utc_key(now)
    if end - event_times[-1] > max_silence:
        gaps.append((event_times[-1], end))
    first = event_times[0]
    return {
        key
        for key in keys
        if key >= first and not any(a < key + step and b > key for a, b in gaps)
    }


def slot_energy_from_readings(
    readings: Sequence[Reading],
    keys: Sequence[datetime],
    slot_minutes: int,
) -> dict[datetime, float]:
    """Per-slot energy from an accumulator, via the value in force at each boundary.

    A slot's energy is ``value(slot end) − value(slot start)``.  A slot in
    which the accumulator did not move is ``0.0``, and the first slot after a
    quiet stretch keeps its full energy — neither needs a reading *inside* the
    slot, which is what makes this different from the ML layer's
    ``HistoryReader._compute_slot_deltas``.  That routine answers a different
    question (what did the house consume?) and correctly discards zero slots
    and any slot whose predecessor had no reading; for actuals, both are real
    observations.

    A slot is missing, never zero, when either boundary falls before the first
    reading or inside an unavailable stretch, when the accumulator went down (a
    meter reset), or when the delta exceeds ``MAX_SLOT_KWH`` — the same
    plausibility cap the ML reader applies.

    Args:
        readings: One accumulator's readings; ``None`` values mark
            unavailability.
        keys: The slot keys to evaluate — normally already restricted to
            observed slots.
        slot_minutes: Slot width in minutes.

    Returns:
        Canonical slot key to energy in kWh, for every slot that could be
        established.
    """
    times, values = _step_series(readings)
    step = timedelta(minutes=slot_minutes)
    energy: dict[datetime, float] = {}
    for key in keys:
        start = _value_at(times, values, key)
        end = _value_at(times, values, key + step)
        if start is None or end is None:
            continue
        delta = end - start
        if delta < -_RESET_EPS or delta > MAX_SLOT_KWH:
            continue
        # Integration sensors resolve to 6 decimals; strip float noise to match.
        energy[key] = round(max(delta, 0.0), 6)
    return energy


def slot_values_from_readings(
    readings: Sequence[Reading], keys: Sequence[datetime]
) -> dict[datetime, float]:
    """Per-slot level — the value in force at each slot start.

    Battery SoC moves in whole-percent steps and can sit unchanged for an hour;
    requiring a reading inside every slot would leave most of them empty.

    Args:
        readings: One level sensor's readings; ``None`` marks unavailability.
        keys: The slot keys to sample.

    Returns:
        Canonical slot key to value, for every slot with a known value.
    """
    times, values = _step_series(readings)
    sampled: dict[datetime, float] = {}
    for key in keys:
        value = _value_at(times, values, key)
        if value is not None:
            sampled[key] = value
    return sampled


def build_actuals_payload(
    readings: dict[str, list[Reading]],
    mapping: dict[str, str],
    now: datetime,
    slot_minutes: int,
    max_silence: timedelta = DEFAULT_MAX_SILENCE,
) -> dict[str, Any]:
    """Assemble an ``hsem-actuals-1`` payload from raw entity readings.

    Every entity in *readings* — mapped or not — contributes to the outage
    check, so exporting a chatty sensor alongside the sparse ones (house load
    is ideal) is what lets a flat import meter be read as a genuine zero.
    Slots that fail the check are absent from every series.

    Args:
        readings: Per entity id, its readings.
        mapping: Entity id to series name, from :data:`ENERGY_SERIES` or
            :data:`VALUE_SERIES`.
        now: The moment the export was taken; only slots that ended by then
            are emitted.
        slot_minutes: Slot width in minutes.
        max_silence: Longest tolerated silence across all entities.

    Returns:
        A JSON-serialisable payload ready for :func:`load_actuals`.

    Raises:
        KeyError: If *mapping* names a series this module does not recognise.
    """
    event_times = sorted(utc_key(t) for rows in readings.values() for t, _v in rows)
    keys = _slot_keys(event_times[0], now, slot_minutes) if event_times else []
    observed = _observed_slots(event_times, keys, slot_minutes, now, max_silence)
    live = [key for key in keys if key in observed]

    energy: dict[str, dict[str, float]] = {}
    values: dict[str, dict[str, float]] = {}
    for entity_id, series in mapping.items():
        rows = readings.get(entity_id, [])
        if series in ENERGY_SERIES:
            keyed = slot_energy_from_readings(rows, live, slot_minutes)
            target = energy
        elif series in VALUE_SERIES:
            keyed = slot_values_from_readings(rows, live)
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


# ---------------------------------------------------------------------------
# Cross-checking realized prices against the plan
# ---------------------------------------------------------------------------

#: A settled price this far from the plan's is a fee or tariff difference, not
#: rounding.  Currency per kWh.
PRICE_OFFSET_TOLERANCE = 0.005

#: Fraction of normally distributed samples expected beyond three standard
#: deviations.  Used to tell "some slots are wrong" from "this is what 96
#: samples of noise look like".
_THREE_SIGMA_RATE = 0.0027


@dataclass(frozen=True)
class PriceComparison:
    """How a dump's prices relate to the prices actually recorded.

    The planner reads its prices from the same sensors an actuals export
    records, so on overlapping slots they should describe the same money.  They
    can legitimately differ in *granularity* — a plan built when the market
    published hourly prices carries one value per hour, while a 15-minute
    export varies within it — and that is not an error.  A constant gap is.

    Attributes:
        slots: Overlapping slots that carried a price on both sides.
        mean_diff: Mean of ``actual - plan``.  Near zero unless a fee differs.
        stdev_diff: Spread of that difference.  Large with equal means is the
            signature of a granularity difference; a fee shows the opposite,
            a clear mean with almost no spread.
        hourly_max_diff: Largest gap once both sides are averaged per clock
            hour, which cancels intra-hour structure.
        worst_diff: Largest single-slot gap.
        outliers: Slots more than three spreads from the mean difference.  A
            run of these with no systematic offset means specific slots are
            wrong -- a stale price, a gap in the source -- which a mean over a
            whole day would otherwise absorb.  Always reported; only treated as
            a fault when it exceeds what chance explains, since three-sigma
            samples occur at a known rate.
        worst_at: Start of the slot holding :attr:`worst_diff`, so it can be
            looked at rather than guessed about.
        plan_is_hourly: Whether the plan repeats one price across each hour.
        actuals_are_subhourly: Whether the recorded prices vary within an hour.
        verdict: One line naming what the numbers show.
    """

    slots: int
    mean_diff: float
    stdev_diff: float
    hourly_max_diff: float
    worst_diff: float
    outliers: int
    worst_at: datetime | None
    plan_is_hourly: bool
    actuals_are_subhourly: bool
    verdict: str

    def describe(self) -> str:
        """Render the comparison for an analysis script."""
        if not self.slots:
            return f"prices: {self.verdict}"
        return (
            f"prices: {self.slots} slot(s) compared against the plan\n"
            f"  mean diff {self.mean_diff:+.4f}/kWh  (spread {self.stdev_diff:.4f})\n"
            f"  worst single slot {self.worst_diff:.4f}/kWh"
            f"{' at ' + self.worst_at.strftime('%Y-%m-%d %H:%M') if self.worst_at else ''}"
            f", {self.outliers} outlier(s)\n"
            f"  hourly-averaged worst gap {self.hourly_max_diff:.4f}/kWh\n"
            f"  {self.verdict}"
        )


def _constant_within_hours(by_hour: dict[datetime, list[float]]) -> bool:
    """Return ``True`` when every hour's values are identical."""
    return all(max(vals) - min(vals) <= 1e-9 for vals in by_hour.values())


def compare_prices(
    rows: Sequence[SlotActuals],
    slots: Sequence[PlannedSlot],
    tolerance: float = PRICE_OFFSET_TOLERANCE,
) -> PriceComparison:
    """Compare recorded prices against the prices a plan was built on.

    Args:
        rows: Aligned actuals, as returned by :func:`align_to_slots`.
        slots: The plan's slots, in the same order.
        tolerance: Largest mean difference treated as agreement.

    Returns:
        A :class:`PriceComparison` describing the relationship.
    """
    paired = [
        (row.start, row.import_price, slot.price.import_price)
        for row, slot in zip(rows, slots, strict=False)
        if row.import_price is not None
    ]
    if not paired:
        return PriceComparison(
            slots=0,
            mean_diff=0.0,
            stdev_diff=0.0,
            hourly_max_diff=0.0,
            worst_diff=0.0,
            outliers=0,
            worst_at=None,
            plan_is_hourly=False,
            actuals_are_subhourly=False,
            verdict="no overlapping slots carry prices -- re-run with --refresh",
        )

    diffs = [actual - planned for _t, actual, planned in paired]
    mean_diff = sum(diffs) / len(diffs)
    variance = sum((d - mean_diff) ** 2 for d in diffs) / len(diffs)
    stdev = variance**0.5

    actual_by_hour: dict[datetime, list[float]] = {}
    plan_by_hour: dict[datetime, list[float]] = {}
    for start, actual, planned in paired:
        hour = start.replace(minute=0, second=0, microsecond=0)
        actual_by_hour.setdefault(hour, []).append(actual)
        plan_by_hour.setdefault(hour, []).append(planned)
    hourly_max = max(
        abs(
            sum(actual_by_hour[h]) / len(actual_by_hour[h])
            - sum(plan_by_hour[h]) / len(plan_by_hour[h])
        )
        for h in actual_by_hour
    )

    plan_is_hourly = _constant_within_hours(plan_by_hour)
    actuals_are_subhourly = not _constant_within_hours(actual_by_hour)

    # A fee is the same on every slot, so its mean stands far clear of its own
    # spread.  Granularity noise averages to nothing but is wide, so judging the
    # mean against a fixed tolerance alone would call a short sample a fee.
    standard_error = stdev / len(diffs) ** 0.5
    significant = abs(mean_diff) > tolerance and abs(mean_diff) > 3 * standard_error

    worst_index = max(range(len(diffs)), key=lambda i: abs(diffs[i]))
    worst_diff = abs(diffs[worst_index])
    worst_at = paired[worst_index][0]
    outliers = (
        sum(1 for d in diffs if abs(d - mean_diff) > 3 * stdev) if stdev > 0 else 0
    )
    # Three-sigma samples occur at a known rate, so a lone one in a hundred
    # slots is what noise looks like, not a fault.  Require a clear excess.
    outliers_beyond_chance = outliers > max(2, 3 * _THREE_SIGMA_RATE * len(diffs))

    if significant:
        verdict = (
            f"systematic offset of {mean_diff:+.4f}/kWh -- a fee or tariff the "
            f"two sides do not share. Do not score until resolved."
        )
    elif outliers_beyond_chance:
        # Checked before the granularity and spread cases: a day-long mean
        # absorbs a handful of wrong slots, so without this they read as noise.
        verdict = (
            f"{outliers} slot(s) sit far outside the spread (worst "
            f"{worst_diff:.4f}/kWh) with no systematic offset -- individual "
            f"prices are wrong, not the whole series."
        )
    elif plan_is_hourly and actuals_are_subhourly:
        verdict = (
            "consistent: the plan carries hourly prices while the export is "
            "sub-hourly. Expected against a cycle from before the market moved "
            "to 15-minute periods; verify against a contemporary dump."
        )
    elif stdev > tolerance:
        verdict = (
            "means agree but slots do not -- check for a timing offset before scoring."
        )
    else:
        verdict = "consistent: same prices, slot for slot."
    return PriceComparison(
        slots=len(paired),
        mean_diff=mean_diff,
        stdev_diff=stdev,
        hourly_max_diff=hourly_max,
        worst_diff=worst_diff,
        outliers=outliers,
        worst_at=worst_at,
        plan_is_hourly=plan_is_hourly,
        actuals_are_subhourly=actuals_are_subhourly,
        verdict=verdict,
    )
