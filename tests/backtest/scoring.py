"""Score realized days: cost, regret, savings and capture (issue #1208).

Stage 2b of the planner backtest.  For every complete local day in an actuals
file this compares three ways the same day could have gone, over the same
slots and the same prices:

``realized``
    What was paid: grid import × import price − grid export × export price.
``baseline``
    Plain inverter self-consumption — the hardware with nobody controlling it.
``oracle``
    The cheapest the day could have been with perfect foresight and **hard
    limits only**.

**Stored energy at the end of the day is part of the result.**  A run that
ends the day with a fuller battery has paid for energy it has not used yet, so
two costs are only comparable when both runs start and end at the same stored
energy.  Every number below is therefore a difference of two such runs:

``regret = realized − oracle``
    What was left on the table.  The oracle starts where the day started and
    must end with at least the stored energy the battery really ended with.
    Never negative: the oracle is a lower bound on the realized cost.
``potential = baseline − oracle``
    What the best control could have saved over none.  Both start where the
    day started; the oracle must end where self-consumption ends.
``savings = potential − regret``
    What HSEM's control was worth against self-consumption, ends equalised.
``capture = savings ÷ potential``
    The share of the day's potential that was realized.  ``None`` when the
    potential is too small to divide by.

Over a period the baseline is additionally run as **one** battery that carries
its own stored energy across midnight (:attr:`DayScore.carried_baseline_cost`),
which is the plain "what would the bill have been" comparison; its end-of-period
stored energy is reported next to it, because that one difference remains.

The site's load is everything that is not the battery — house, EV and losses
— taken from the meters as ``grid_import − grid_export − battery_charged +
battery_discharged``.  The EV is therefore a fixed load here: what its
scheduling saved is not measured.

The numerical work and the definitions live in the integration, in
``planner/hindsight_oracle.py`` and ``planner/hindsight_scoring.py``.  Both are
Home Assistant-free and shared with the on-device performance sensors of
issue #1182, so a day scored here and on the device gives the same numbers.
This module adds what only the backtest needs: reading days out of an actuals
file, and the baseline carried across midnight.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, fields
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from custom_components.hsem.planner.hindsight_oracle import simulate_self_consumption
from custom_components.hsem.planner.hindsight_scoring import (
    RealizedSlot,
    RealizedWindow,
    SiteLimits,
    WindowScore,
    battery_flows_from_soc,
    capture_share,
    realized_window,
    score_window,
)
from custom_components.hsem.utils.datetime_utils import slot_key
from tests.backtest.actuals import Actuals
from tests.backtest.site import describe_site

#: Series a day needs in every slot before it can be scored.
_REQUIRED_ENERGY: tuple[str, ...] = (
    "grid_import",
    "grid_export",
    "battery_charged",
    "battery_discharged",
)
_REQUIRED_VALUES: tuple[str, ...] = ("import_price", "export_price")


@dataclass(frozen=True)
class DayScore:
    """The score of one local day.

    Every cost is ``None`` on a day that could not be scored;
    :attr:`unscorable` then says why.

    Attributes:
        day: The local calendar day.
        slot_count: Slots in the day (92 or 100 on DST days at 15 minutes).
        unscorable: Why the day has no score, or ``None``.
        realized_cost: Grid cost that was paid.
        oracle_cost: Grid cost of the perfect-foresight oracle that ends the
            day with at least the stored energy the battery really ended with.
        baseline_cost: Grid cost of plain self-consumption from the stored
            energy the day really started with.
        potential: ``baseline_cost`` minus the cost of the oracle that ends
            where self-consumption ends.
        realized_end_kwh: Stored energy the realized day ends with, from the
            measured battery flows.
        baseline_end_kwh: Stored energy ``baseline_cost``'s run ends with.
        carried_baseline_cost: Grid cost of self-consumption from the stored
            energy the *baseline* ended the previous day with.  Equal to
            ``baseline_cost`` on the first day of a run of days.
        carried_baseline_end_kwh: Stored energy that run ends the day with.
        notes: Anything about the data that qualifies the score.
    """

    day: date
    slot_count: int = 0
    unscorable: str | None = None
    realized_cost: float | None = None
    oracle_cost: float | None = None
    baseline_cost: float | None = None
    potential: float | None = None
    realized_end_kwh: float | None = None
    baseline_end_kwh: float | None = None
    carried_baseline_cost: float | None = None
    carried_baseline_end_kwh: float | None = None
    notes: tuple[str, ...] = ()

    @property
    def is_scored(self) -> bool:
        """Return ``True`` when the day carries a score."""
        return self.unscorable is None

    @property
    def window_score(self) -> WindowScore | None:
        """Return the day's score in the shared form, or ``None`` if unscored."""
        if (
            self.realized_cost is None
            or self.oracle_cost is None
            or self.baseline_cost is None
            or self.potential is None
            or self.realized_end_kwh is None
            or self.baseline_end_kwh is None
        ):
            return None
        return WindowScore(
            realized_cost=self.realized_cost,
            oracle_cost=self.oracle_cost,
            baseline_cost=self.baseline_cost,
            potential=self.potential,
            realized_end_kwh=self.realized_end_kwh,
            baseline_end_kwh=self.baseline_end_kwh,
        )

    @property
    def regret(self) -> float | None:
        """Return ``realized − oracle``, or ``None`` on an unscored day."""
        score = self.window_score
        return None if score is None else score.regret

    @property
    def savings(self) -> float | None:
        """Return what the control was worth against self-consumption.

        ``potential − regret``: both runs start where the day started, and the
        difference in where they end is priced by the oracle.
        """
        score = self.window_score
        return None if score is None else score.savings

    @property
    def capture(self) -> float | None:
        """Return the share of the day's potential that was realized.

        ``savings ÷ potential``.  ``None`` on an unscored day and when the
        potential is below ``MIN_POTENTIAL``: a day on which no control could
        have saved anything has no share to report, and clamping it to 0 or
        100 % would invent one.
        """
        score = self.window_score
        return None if score is None else score.capture


@dataclass(frozen=True)
class ScoreSummary:
    """Totals over the scored days of a period.

    Attributes:
        scored_days: Days that carry a score.
        unscored_days: Days that do not.
        realized_cost: Sum of the realized costs.
        oracle_cost: Sum of the oracle costs.
        potential: Sum of the days' potentials.
        carried_baseline_cost: Sum of the carried baseline's costs: plain
            self-consumption as one battery across consecutive days.
        carried_baseline_end_delta_kwh: Stored energy that baseline holds at
            the end of the last scored day beyond what the battery really
            held.  Negative when HSEM left the battery fuller.
    """

    scored_days: int
    unscored_days: int
    realized_cost: float
    oracle_cost: float
    potential: float
    carried_baseline_cost: float
    carried_baseline_end_delta_kwh: float | None

    @property
    def regret(self) -> float:
        """Return the period's ``realized − oracle``."""
        return self.realized_cost - self.oracle_cost

    @property
    def savings(self) -> float:
        """Return the period's ``potential − regret``."""
        return self.potential - self.regret

    @property
    def capture(self) -> float | None:
        """Return the period's capture, or ``None`` when its potential is tiny."""
        return capture_share(self.savings, self.potential)

    @property
    def carried_savings(self) -> float:
        """Return ``carried baseline − realized`` over the period, ends not equalised."""
        return self.carried_baseline_cost - self.realized_cost


def day_slot_keys(day: date, zone: ZoneInfo, slot_minutes: int) -> list[datetime]:
    """Return the canonical slot keys of one local calendar day.

    Stepped in physical time, so a DST day has 92 or 100 quarter-hours and the
    repeated autumn hour keeps both of its occurrences.

    Args:
        day: The local calendar day.
        zone: The site's time zone, which defines the day.
        slot_minutes: Slot width.

    Returns:
        The day's slot keys in chronological order.
    """
    start = datetime.combine(day, time(0), zone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time(0), zone).astimezone(UTC)
    step = timedelta(minutes=slot_minutes)
    return [
        slot_key(start + index * step, slot_minutes)
        for index in range(int((end - start) / step))
    ]


def observed_days(actuals: Actuals, zone: ZoneInfo) -> list[date]:
    """Return every local day the actuals touch, in order.

    Args:
        actuals: The loaded realized series.
        zone: The site's time zone.

    Returns:
        Each local day with at least one grid-import observation.  Whether a
        day is complete enough to score is :func:`score_day`'s decision.
    """
    observed = actuals.energy_kwh.get("grid_import", {})
    return sorted({key.astimezone(zone).date() for key in observed})


def merge_actuals(parts: Sequence[Actuals]) -> Actuals:
    """Combine several actuals files into one, for example one file per day.

    Args:
        parts: The loaded files.  A slot present in more than one keeps the
            value of the last file that carries it.

    Returns:
        One :class:`Actuals` holding every series of every part.

    Raises:
        ValueError: If there are no parts, they differ in slot width, or they
            are from different installations (issue #1225).
    """
    if not parts:
        raise ValueError("no actuals to merge")
    widths = {part.slot_minutes for part in parts}
    if len(widths) != 1:
        raise ValueError(f"actuals differ in slot width: {sorted(widths)}")
    tags = {part.site_tag for part in parts}
    if len(tags) != 1:
        raise ValueError(
            "actuals are from different installations: "
            + ", ".join(sorted(describe_site(tag) for tag in tags))
        )
    merged = Actuals(
        slot_minutes=parts[0].slot_minutes,
        source=", ".join(part.source for part in parts),
        site_tag=parts[0].site_tag,
    )
    for part in parts:
        for name, values in part.energy_kwh.items():
            merged.energy_kwh.setdefault(name, {}).update(values)
        for name, values in part.values.items():
            merged.values.setdefault(name, {}).update(values)
    return merged


def _observed(actuals: Actuals, series: str, key: datetime) -> float:
    """Return one observation of a day that was checked to be complete."""
    value = actuals._lookup(series, key)
    if value is None:
        raise ValueError(f"{series} has no observation at {key.isoformat()}")
    return value


def _missing(
    actuals: Actuals, keys: Sequence[datetime], *, flows_from_soc: bool = False
) -> str | None:
    """Return why the day cannot be scored, or ``None`` when it can."""
    gaps: list[str] = []
    energy = _REQUIRED_ENERGY[:2] if flows_from_soc else _REQUIRED_ENERGY
    for series in (*energy, *_REQUIRED_VALUES):
        absent = sum(1 for key in keys if actuals._lookup(series, key) is None)
        if absent:
            gaps.append(f"{series} missing in {absent}/{len(keys)} slot(s)")
    if flows_from_soc:
        edges = [*keys, keys[-1] + timedelta(minutes=actuals.slot_minutes)]
        absent = sum(
            1 for key in edges if actuals._lookup("battery_soc_pct", key) is None
        )
        if absent:
            gaps.append(
                f"battery_soc_pct missing at {absent}/{len(edges)} slot boundaries"
            )
    elif actuals._lookup("battery_soc_pct", keys[0]) is None:
        gaps.append("battery_soc_pct missing at the start of the day")
    return "; ".join(gaps) or None


def _battery_flows(
    actuals: Actuals, key: datetime, site: SiteLimits, *, flows_from_soc: bool
) -> tuple[float, float]:
    """Return a slot's battery charge and discharge."""
    if not flows_from_soc:
        return (
            _observed(actuals, "battery_charged", key),
            _observed(actuals, "battery_discharged", key),
        )
    following = key + timedelta(minutes=actuals.slot_minutes)
    return battery_flows_from_soc(
        _observed(actuals, "battery_soc_pct", key),
        _observed(actuals, "battery_soc_pct", following),
        site,
    )


@dataclass(frozen=True, kw_only=True)
class RealizedDay(RealizedWindow):
    """One complete local day, prepared for the hindsight functions.

    Attributes:
        day: The local calendar day.
        keys: The day's canonical slot keys.
        house_net_kwh: House load minus PV per slot, without the EV, where
            both meters were observed; the slot's whole net load otherwise.
    """

    day: date
    keys: tuple[datetime, ...]
    house_net_kwh: tuple[float, ...]


def realized_day(
    actuals: Actuals,
    day: date,
    zone: ZoneInfo,
    site: SiteLimits,
    *,
    flows_from_soc: bool = False,
) -> RealizedDay | str:
    """Prepare one local day of actuals for scoring.

    The oracle's limits are the site's, widened wherever the realized day
    itself went beyond them: see
    :func:`~custom_components.hsem.planner.hindsight_scoring.realized_window`.

    Args:
        actuals: The loaded realized series.
        day: The local calendar day.
        zone: The site's time zone.
        site: The installation's hard limits.
        flows_from_soc: Take the battery's flows from the change in its SoC
            over each slot instead of from the battery energy series.  That
            is what the on-device performance sensors do (issue #1182), so
            this is the mode that reproduces their numbers.  It needs the SoC
            at every slot boundary, including the one that ends the day.

    Returns:
        The prepared day, or the reason it cannot be scored.  Missing actuals
        are never read as zero.
    """
    keys = day_slot_keys(day, zone, actuals.slot_minutes)
    if not keys:
        return "the day has no slots"
    reason = _missing(actuals, keys, flows_from_soc=flows_from_soc)
    if reason is not None:
        return reason

    hours = actuals.slot_minutes / 60.0
    flows = [
        _battery_flows(actuals, key, site, flows_from_soc=flows_from_soc)
        for key in keys
    ]
    slots = [
        RealizedSlot(
            hours=hours,
            grid_import_kwh=_observed(actuals, "grid_import", key),
            grid_export_kwh=_observed(actuals, "grid_export", key),
            charged_kwh=charged,
            discharged_kwh=discharged,
            import_price=_observed(actuals, "import_price", key),
            export_price=_observed(actuals, "export_price", key),
            pv_kwh=actuals._lookup("pv_produced", key),
        )
        for key, (charged, discharged) in zip(keys, flows, strict=True)
    ]
    house = [actuals._lookup("house_load", key) for key in keys]
    start_kwh = site.rated_kwh * _observed(actuals, "battery_soc_pct", keys[0]) / 100.0
    window = realized_window(slots, start_kwh, site)
    return RealizedDay(
        **{spec.name: getattr(window, spec.name) for spec in fields(window)},
        day=day,
        keys=tuple(keys),
        house_net_kwh=tuple(
            slot.net_load_kwh
            if load is None or slot.pv_kwh is None
            else load - slot.pv_kwh
            for slot, load in zip(slots, house, strict=True)
        ),
    )


def score_day(
    actuals: Actuals,
    day: date,
    zone: ZoneInfo,
    site: SiteLimits,
    *,
    baseline_start_kwh: float | None = None,
    flows_from_soc: bool = False,
) -> DayScore:
    """Score one local day of realized actuals.

    Args:
        actuals: The loaded realized series.
        day: The local calendar day to score.
        zone: The site's time zone.
        site: The installation's hard limits.
        baseline_start_kwh: Stored energy the carried baseline starts the
            day with, from its own previous day.  ``None`` starts it where the
            battery really was.
        flows_from_soc: Take the battery's flows from its SoC, as the
            on-device performance sensors do: see :func:`realized_day`.

    Returns:
        The day's score, or a :class:`DayScore` whose ``unscorable`` says why
        there is none.  Missing actuals are never read as zero.
    """
    prepared = realized_day(actuals, day, zone, site, flows_from_soc=flows_from_soc)
    if isinstance(prepared, str):
        slot_count = len(day_slot_keys(day, zone, actuals.slot_minutes))
        return DayScore(day=day, slot_count=slot_count, unscorable=prepared)

    slot_count = len(prepared.keys)
    baseline = simulate_self_consumption(
        prepared.slots, prepared.configured, prepared.start_kwh
    )
    score = score_window(prepared, baseline=baseline)
    if score is None:
        return DayScore(
            day=day,
            slot_count=slot_count,
            unscorable="the oracle could not be solved (is scipy installed?)",
            notes=prepared.notes,
        )
    carried = (
        baseline
        if baseline_start_kwh is None
        else simulate_self_consumption(
            prepared.slots, prepared.configured, baseline_start_kwh
        )
    )
    return DayScore(
        day=day,
        slot_count=slot_count,
        realized_cost=score.realized_cost,
        oracle_cost=score.oracle_cost,
        baseline_cost=score.baseline_cost,
        potential=score.potential,
        realized_end_kwh=score.realized_end_kwh,
        baseline_end_kwh=score.baseline_end_kwh,
        carried_baseline_cost=carried.cost,
        carried_baseline_end_kwh=carried.stored_kwh[-1],
        notes=prepared.notes,
    )


def score_days(
    actuals: Actuals,
    days: Iterable[date],
    zone: ZoneInfo,
    site: SiteLimits,
    *,
    flows_from_soc: bool = False,
) -> list[DayScore]:
    """Score several days, carrying the baseline's battery across midnight.

    On consecutive scored days the carried baseline starts each day with the
    stored energy it ended the previous one with: it is one counterfactual
    battery, not a new one every midnight.  After a gap, or after an unscored
    day, it starts where the battery really was.

    Args:
        actuals: The loaded realized series.
        days: The local days to score.
        zone: The site's time zone.
        site: The installation's hard limits.
        flows_from_soc: Take the battery's flows from its SoC, as the
            on-device performance sensors do: see :func:`realized_day`.

    Returns:
        One :class:`DayScore` per day, in chronological order.
    """
    scores: list[DayScore] = []
    previous: DayScore | None = None
    for day in sorted(set(days)):
        carried = None
        if (
            previous is not None
            and previous.is_scored
            and previous.day + timedelta(days=1) == day
        ):
            carried = previous.carried_baseline_end_kwh
        previous = score_day(
            actuals,
            day,
            zone,
            site,
            baseline_start_kwh=carried,
            flows_from_soc=flows_from_soc,
        )
        scores.append(previous)
    return scores


def summarize(scores: Sequence[DayScore]) -> ScoreSummary:
    """Total the scored days of a period.

    Args:
        scores: Day scores, in chronological order.

    Returns:
        The totals.  Unscored days are counted, never treated as zero cost.
    """
    scored = [score for score in scores if score.is_scored]
    end_delta = None
    if scored:
        last = scored[-1]
        if (
            last.carried_baseline_end_kwh is not None
            and last.realized_end_kwh is not None
        ):
            end_delta = last.carried_baseline_end_kwh - last.realized_end_kwh
    return ScoreSummary(
        scored_days=len(scored),
        unscored_days=len(scores) - len(scored),
        realized_cost=sum(score.realized_cost or 0.0 for score in scored),
        oracle_cost=sum(score.oracle_cost or 0.0 for score in scored),
        potential=sum(score.potential or 0.0 for score in scored),
        carried_baseline_cost=sum(
            score.carried_baseline_cost or 0.0 for score in scored
        ),
        carried_baseline_end_delta_kwh=end_delta,
    )


def _money(value: float | None) -> str:
    return "       -" if value is None else f"{value:8.2f}"


def _share(value: float | None) -> str:
    return " unknown" if value is None else f"{value * 100:7.1f}%"


def describe(scores: Sequence[DayScore]) -> str:
    """Render day scores and their total as a table.

    Args:
        scores: Day scores, in chronological order.

    Returns:
        A multi-line string for a terminal.  Costs are in the currency of the
        price sensors.
    """
    lines = ["day         realized   oracle   regret potential  savings  capture"]
    for score in scores:
        if not score.is_scored:
            lines.append(f"{score.day}  not scored: {score.unscorable}")
            continue
        lines.append(
            f"{score.day}  {_money(score.realized_cost)} {_money(score.oracle_cost)} "
            f"{_money(score.regret)}  {_money(score.potential)} "
            f"{_money(score.savings)} {_share(score.capture)}"
        )
        lines.extend(f"            note: {note}" for note in score.notes)
    total = summarize(scores)
    if total.scored_days:
        lines.append(
            f"{total.scored_days:4d} day(s) {_money(total.realized_cost)} "
            f"{_money(total.oracle_cost)} {_money(total.regret)}  "
            f"{_money(total.potential)} {_money(total.savings)} {_share(total.capture)}"
        )
        delta = total.carried_baseline_end_delta_kwh
        lines.append(
            f"self-consumption as one battery across consecutive days: "
            f"{total.carried_baseline_cost:.2f}, {total.carried_savings:+.2f} "
            f"against realized"
            + (
                ""
                if delta is None
                else f"; it ends with {delta:+.2f} kWh against what the battery held"
            )
        )
    if total.unscored_days:
        lines.append(f"{total.unscored_days} day(s) not scored")
    return "\n".join(lines)
