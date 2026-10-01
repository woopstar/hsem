"""Split a day's regret into forecast error and planner error (issue #1208).

Stage 2c of the planner backtest.  :mod:`tests.backtest.scoring` says how much
a day left on the table; this says why.  The day is replayed slot by slot
through the real planner, twice:

``forecast run``
    At every slot the planner gets the inputs HSEM recorded at that time —
    its price, PV and load forecasts — and the battery the replay has left.
    The decision for that slot is then executed against what really
    happened.  This is what HSEM's decisions cost.
``hindsight run``
    The same, with the realized prices, PV and house load put in the place of
    the forecasts.  This is what the same planner would have done knowing the
    future.

Each run's regret is its cost above the perfect-foresight oracle that ends
the day with the same stored energy.  Then:

``forecast error  = regret(forecast run) − regret(hindsight run)``
    What better forecasts would have saved.
``planner error   = regret(hindsight run)``
    What the planner leaves on the table with perfect inputs: floors,
    reserves, wear pricing, hysteresis, the terminal value, the MILP itself.
``execution error = regret(realized) − regret(forecast run)``
    What separates the replay from the meters: writes that failed or were
    skipped, manual overrides, the planner replanning inside a slot, and
    whatever the execution model below gets wrong.

The three add up to the realized regret.

**The execution model.**  A plan slot is a recommendation and a pair of battery
energies.  :func:`execute_decision` turns it into what the battery does with
the slot's real load, following the applier: forced modes move the planned
energy, the passive modes follow the load.  The battery never serves the EV —
only the house's own deficit — which is what the applier's discharge caps
enforce.  It is a model, and its error lands in ``execution error``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from custom_components.hsem.models.hourly_consumption_average import (
    HourlyConsumptionAverage,
)
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.price_point import PricePoint
from custom_components.hsem.models.solcast_slot import SolcastSlot
from custom_components.hsem.planner.engine_core import run_planner
from custom_components.hsem.planner.hindsight_oracle import (
    HindsightBattery,
    HindsightRun,
    grid_cost,
    stored_trajectory,
)
from custom_components.hsem.utils.datetime_utils import (
    physical_slot_grid,
    slot_key,
    slot_position,
)
from custom_components.hsem.utils.recommendations import Recommendations
from tests.backtest.actuals import Actuals
from tests.backtest.replay import planner_input_from_dict
from tests.backtest.scoring import RealizedDay, SiteLimits, realized_day
from tests.backtest.site import describe_site, same_site, site_of

#: Least share of a day's slots that must have a cycle recorded in them.  Below
#: it the replay would plan most of the day from forecasts HSEM did not have
#: at the time, and the split would describe the replay, not the day.
MIN_CYCLE_COVERAGE = 0.5

#: Recommendations that move the planned battery energy whatever the load is.
_CHARGE_FROM_GRID = Recommendations.BatteriesChargeGrid.value
_FORCED_DISCHARGE: frozenset[str] = frozenset(
    {
        Recommendations.ForceBatteriesDischarge.value,
        Recommendations.ForceExport.value,
    }
)
#: Recommendations executed as self-consumption: the battery follows the load.
_FOLLOW_LOAD: frozenset[str] = frozenset(
    {
        Recommendations.BatteriesDischargeMode.value,
        Recommendations.BatteriesDischargeWindowMode.value,
    }
)
#: Recommendations that only let a PV surplus into the battery.
_SURPLUS_ONLY: frozenset[str] = frozenset(
    {
        Recommendations.BatteriesChargeSolar.value,
        Recommendations.BatteriesWaitMode.value,
    }
)
_EV_CHARGING = Recommendations.EVSmartCharging.value


@dataclass(frozen=True)
class Decision:
    """What a plan asks of the battery in one slot.

    Attributes:
        recommendation: The slot's recommendation label.
        charged_kwh: Planned AC energy into the battery over a whole slot.
        discharged_kwh: Planned AC energy out of the battery over a whole slot.
    """

    recommendation: str | None
    charged_kwh: float = 0.0
    discharged_kwh: float = 0.0


def execute_decision(
    decision: Decision,
    *,
    net_load_kwh: float,
    house_net_kwh: float,
    stored_kwh: float,
    battery: HindsightBattery,
    hours: float,
) -> tuple[float, float]:
    """Return the AC charge and discharge a decision produces in a real slot.

    Follows the applier (``custom_sensors/applier.py``):

    - ``batteries_charge_grid`` charges the planned energy;
    - ``force_batteries_discharge`` and ``force_export`` discharge it;
    - ``batteries_discharge_mode`` and ``batteries_discharge_window_mode`` run
      as self-consumption: a house deficit is served, a surplus is stored;
    - ``batteries_charge_solar`` and ``batteries_wait_mode`` hold the battery
      at a 0 W discharge cap, so only a surplus gets in;
    - ``ev_smart_charging`` serves the house up to the planned discharge and
      stores a surplus;
    - anything else leaves the battery idle.

    Every flow is cut to the power limit and to the energy the battery can
    take or give.

    Args:
        decision: The plan's decision for the slot.
        net_load_kwh: The slot's real load without the battery, EV included;
            negative is a surplus.
        house_net_kwh: The house's own load minus PV.  The battery serves
            this, never the EV.
        stored_kwh: Stored energy at the start of the slot.
        battery: The battery's hard limits.
        hours: Slot duration in hours.

    Returns:
        ``(charged_kwh, discharged_kwh)`` on the AC side; at most one is
        positive.
    """
    room = max(battery.max_stored_kwh - stored_kwh, 0.0) / battery.charge_efficiency
    available = (
        max(stored_kwh - battery.min_stored_kwh, 0.0) * battery.discharge_efficiency
    )
    charge_cap = min(battery.max_charge_kw * hours, room)
    discharge_cap = min(battery.max_discharge_kw * hours, available)
    surplus = max(-net_load_kwh, 0.0)
    # The battery covers the house, not the EV: no more than the house's own
    # deficit, and no more than the site as a whole is short.
    house_deficit = max(min(house_net_kwh, net_load_kwh), 0.0)

    label = decision.recommendation
    if label == _CHARGE_FROM_GRID:
        return min(decision.charged_kwh, charge_cap), 0.0
    if label in _FORCED_DISCHARGE:
        return 0.0, min(decision.discharged_kwh, discharge_cap)
    if label in _FOLLOW_LOAD:
        if surplus > 0.0:
            return min(surplus, charge_cap), 0.0
        return 0.0, min(house_deficit, discharge_cap)
    if label in _SURPLUS_ONLY:
        return min(surplus, charge_cap), 0.0
    if label == _EV_CHARGING:
        if surplus > 0.0:
            return min(surplus, charge_cap), 0.0
        return 0.0, min(house_deficit, decision.discharged_kwh, discharge_cap)
    return 0.0, 0.0


@dataclass(frozen=True)
class RollingRun:
    """One slot-by-slot replay of a day.

    Attributes:
        run: The replay's grid and battery flows and their cost.
        recommendations: The recommendation executed in each slot.
        stale_slots: Slots planned from an input recorded in an earlier slot,
            because none was recorded in the slot itself.
        substituted_share: For a hindsight run, the average share of each
            plan's horizon that carried realized values; ``0.0`` for a
            forecast run.
    """

    run: HindsightRun
    recommendations: tuple[str | None, ...]
    stale_slots: int = 0
    substituted_share: float = 0.0


@dataclass(frozen=True)
class DayAttribution:
    """Where one day's regret came from.

    Every number is ``None`` on a day that could not be attributed;
    :attr:`unscorable` then says why.

    Attributes:
        day: The local calendar day.
        unscorable: Why the day has no attribution, or ``None``.
        realized_cost: Grid cost that was paid.
        forecast_run_cost: Grid cost of the replay on HSEM's forecasts.
        hindsight_run_cost: Grid cost of the replay on realized values.
        regret: Realized cost above its end-matched oracle.
        forecast_run_regret: The forecast run's cost above its own.
        hindsight_run_regret: The hindsight run's cost above its own.
        stale_slots: Slots of the forecast run planned from an older input.
        substituted_share: Share of the hindsight run's horizons that carried
            realized values.
        notes: Anything about the data that qualifies the result.
    """

    day: date
    unscorable: str | None = None
    realized_cost: float | None = None
    forecast_run_cost: float | None = None
    hindsight_run_cost: float | None = None
    regret: float | None = None
    forecast_run_regret: float | None = None
    hindsight_run_regret: float | None = None
    stale_slots: int = 0
    substituted_share: float = 0.0
    notes: tuple[str, ...] = ()

    @property
    def is_attributed(self) -> bool:
        """Return ``True`` when the day carries an attribution."""
        return self.unscorable is None

    @property
    def forecast_error(self) -> float | None:
        """Return what realized forecasts would have saved the same planner."""
        if self.forecast_run_regret is None or self.hindsight_run_regret is None:
            return None
        return self.forecast_run_regret - self.hindsight_run_regret

    @property
    def planner_error(self) -> float | None:
        """Return what the planner leaves on the table with realized inputs."""
        return self.hindsight_run_regret

    @property
    def execution_error(self) -> float | None:
        """Return what separates the replay on HSEM's forecasts from the meters."""
        if self.regret is None or self.forecast_run_regret is None:
            return None
        return self.regret - self.forecast_run_regret


# ---------------------------------------------------------------------------
# Which recorded input each slot is planned from
# ---------------------------------------------------------------------------


def _recorded_at(payload: Mapping[str, Any]) -> datetime | None:
    """Return the instant a cycle's planner input was built, if it says."""
    raw = (payload.get("planner_input") or {}).get("now_iso")
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def cycles_of_site(
    payloads: Iterable[Mapping[str, Any]], site_tag: str | None
) -> list[Mapping[str, Any]]:
    """Return the cycles recorded on the installation tagged *site_tag*.

    Every comparison of a plan with realized values goes through this: a
    cycle from another installation is left out, exactly like a cycle of
    another day (issue #1225).  Untagged cycles pair with untagged actuals.

    Args:
        payloads: Recorded cycles, in any order.
        site_tag: The tag of the actuals they are compared with.

    Returns:
        The cycles whose tag equals *site_tag*, in the order given.
    """
    return [payload for payload in payloads if same_site(site_of(payload), site_tag)]


def cycles_by_slot(
    payloads: Iterable[Mapping[str, Any]],
    day: date,
    zone: ZoneInfo,
    slot_minutes: int,
) -> dict[datetime, Mapping[str, Any]]:
    """Return the first cycle recorded in each slot of *day*.

    Only a cycle's own ``now_iso`` counts, not the time the dump was written:
    the planner does not replan on every dump, so several dumps carry the
    same input.

    Args:
        payloads: Recorded cycles, in any order.
        day: The local calendar day.
        zone: The site's time zone.
        slot_minutes: Slot width.

    Returns:
        Slot key → the earliest cycle whose input was built in that slot.
        Slots without a cycle are absent.
    """
    first: dict[datetime, tuple[datetime, Mapping[str, Any]]] = {}
    for payload in payloads:
        recorded = _recorded_at(payload)
        if recorded is None or recorded.tzinfo is None:
            continue
        if recorded.astimezone(zone).date() != day:
            continue
        key = slot_key(recorded, slot_minutes)
        if key not in first or recorded < first[key][0]:
            first[key] = (recorded, payload)
    return {key: payload for key, (_recorded, payload) in first.items()}


def site_cycles_by_slot(
    payloads: Iterable[Mapping[str, Any]],
    day: date,
    zone: ZoneInfo,
    slot_minutes: int,
    site_tag: str | None,
) -> dict[datetime, Mapping[str, Any]] | str:
    """Return *day*'s cycles of one installation, or why there are none.

    Args:
        payloads: Recorded cycles, in any order.
        day: The local calendar day.
        zone: The site's time zone.
        slot_minutes: Slot width.
        site_tag: The tag of the actuals the cycles are compared with.

    Returns:
        :func:`cycles_by_slot` of the cycles tagged *site_tag*; or, when that
        is empty, the reason as a string.
    """
    payloads = list(payloads)
    recorded = cycles_by_slot(
        cycles_of_site(payloads, site_tag), day, zone, slot_minutes
    )
    if recorded:
        return recorded
    if cycles_by_slot(payloads, day, zone, slot_minutes):
        return (
            f"the day's planner cycles are not from the installation of the "
            f"actuals ({describe_site(site_tag)})"
        )
    return "no planner cycle was recorded on this day"


def _cycle_for_slot(
    index: int,
    keys: Sequence[datetime],
    recorded: Mapping[datetime, Mapping[str, Any]],
) -> tuple[Mapping[str, Any] | None, bool]:
    """Return the cycle to plan slot *index* from, and whether it is stale.

    The cycle recorded in the slot itself when there is one; otherwise the
    latest earlier one of the same day, which is the input HSEM was still
    acting on; otherwise, at the very start of the day, the first later one.
    """
    if keys[index] in recorded:
        return recorded[keys[index]], False
    for earlier in range(index - 1, -1, -1):
        if keys[earlier] in recorded:
            return recorded[keys[earlier]], True
    for later in range(index + 1, len(keys)):
        if keys[later] in recorded:
            return recorded[keys[later]], True
    return None, True


# ---------------------------------------------------------------------------
# Putting realized values in the place of the forecasts
# ---------------------------------------------------------------------------


def with_realized_forecasts(
    inp: PlannerInput, actuals: Actuals, zone: ZoneInfo
) -> tuple[PlannerInput, float]:
    """Return *inp* with realized prices, PV and house load for its horizon.

    Every horizon slot the actuals cover gets its realized import and export
    price and its realized PV; every hour whose slots are all covered gets its
    realized house load.  A slot or hour the actuals do not cover keeps the
    forecast, so a horizon that reaches past the recorded days is only partly
    realized, and the share says by how much.

    The live readings that the planner blends into the current slot are
    switched off: they describe the instant the input was recorded, and the
    realized slot is already in the forecast's place.

    Args:
        inp: The recorded planner input.
        actuals: The loaded realized series.
        zone: The site's time zone.

    Returns:
        The rewritten input, and the share of its horizon slots that carry
        realized prices.
    """
    now = datetime.fromisoformat(inp.now_iso).astimezone(zone)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    minutes = inp.interval_minutes
    hours = minutes / 60.0
    grid = physical_slot_grid(now, minutes, inp.interval_length_hours)

    forecast_prices = {(p.day_offset, p.slot_in_day): p for p in inp.price_points}
    hourly_prices = {(p.day_offset, p.hour): p for p in inp.price_points}
    forecast_pv = {(s.day_offset, s.slot_in_day): s for s in inp.solcast_slots}
    hourly_pv = {(s.day_offset, s.hour): s for s in inp.solcast_slots}
    cyclic_pv = {s.hour: s for s in inp.solcast_slots if s.slot_in_day is None}

    prices: list[PricePoint] = []
    pv: list[SolcastSlot] = []
    house: dict[tuple[int, int], list[float | None]] = {}
    realized_slots = 0
    for start, _end in grid:
        key = slot_key(start, minutes)
        day_offset, slot_in_day = slot_position(start, midnight, minutes)
        position = (day_offset, slot_in_day)
        hour_position = (day_offset, start.hour)

        import_price = actuals._lookup("import_price", key)
        export_price = actuals._lookup("export_price", key)
        if import_price is not None and export_price is not None:
            realized_slots += 1
            prices.append(
                PricePoint(
                    hour=start.hour,
                    import_price=import_price,
                    export_price=export_price,
                    day_offset=day_offset,
                    slot_in_day=slot_in_day,
                )
            )
        elif (known := forecast_prices.get(position)) is not None or (
            known := hourly_prices.get(hour_position)
        ) is not None:
            prices.append(replace(known, slot_in_day=slot_in_day))

        produced = actuals._lookup("pv_produced", key)
        if produced is not None:
            pv_kw: float | None = produced / hours
        else:
            entry = (
                forecast_pv.get(position)
                or hourly_pv.get(hour_position)
                or cyclic_pv.get(start.hour)
            )
            pv_kw = None if entry is None else entry.pv_estimate
        if pv_kw is not None:
            pv.append(
                SolcastSlot(
                    hour=start.hour,
                    pv_estimate=pv_kw,
                    day_offset=day_offset,
                    slot_in_day=slot_in_day,
                )
            )

        house.setdefault(hour_position, []).append(actuals._lookup("house_load", key))

    forecast_house = {(c.day_offset, c.hour): c for c in inp.consumption_averages}
    cyclic_house = {c.hour: c for c in inp.consumption_averages if c.day_offset == 0}
    consumption: list[HourlyConsumptionAverage] = []
    for (day_offset, hour), values in house.items():
        if all(value is not None for value in values):
            # A partly covered clock hour (a horizon that starts or ends inside
            # it) is scaled to the whole hour the planner expects.
            total = math.fsum(v for v in values if v is not None)
            hourly = total * (60.0 / minutes) / len(values)
            consumption.append(
                HourlyConsumptionAverage(
                    hour=hour,
                    avg_1d=hourly,
                    avg_3d=hourly,
                    avg_7d=hourly,
                    avg_14d=hourly,
                    day_offset=day_offset,
                )
            )
        elif (known_load := forecast_house.get((day_offset, hour))) is not None:
            consumption.append(known_load)
        elif (known_load := cyclic_house.get(hour)) is not None:
            consumption.append(replace(known_load, day_offset=day_offset))

    return (
        replace(
            inp,
            price_points=prices,
            solcast_slots=pv,
            consumption_averages=consumption,
            live_solar_production_available=False,
            live_house_consumption_available=False,
        ),
        realized_slots / len(grid) if grid else 0.0,
    )


# ---------------------------------------------------------------------------
# The rolling replay
# ---------------------------------------------------------------------------


def _decision(inp: PlannerInput, slot_start: datetime, site: SiteLimits) -> Decision:
    """Plan from *inp* and return what the plan asks of the slot at *slot_start*."""
    plan = run_planner(inp)
    key = slot_key(slot_start, inp.interval_minutes)
    for slot in plan.slots:
        if slot_key(slot.start, inp.interval_minutes) == key:
            # The plan's battery energies are on the battery side.
            return Decision(
                recommendation=slot.recommendation,
                charged_kwh=slot.batteries_charged_kwh / site.charge_efficiency,
                discharged_kwh=slot.batteries_discharged_kwh
                * site.discharge_efficiency,
            )
    return Decision(recommendation=None)


def rolling_run(
    prepared: RealizedDay,
    recorded: Mapping[datetime, Mapping[str, Any]],
    actuals: Actuals,
    zone: ZoneInfo,
    site: SiteLimits,
    *,
    realized_forecasts: bool,
) -> RollingRun | None:
    """Replay one day slot by slot through the planner.

    At each slot the planner input recorded for it is replanned from the
    slot's start with the battery the replay has left, and the decision for
    that slot is executed against the slot's real load.

    Args:
        prepared: The day, from :func:`~tests.backtest.scoring.realized_day`.
        recorded: Slot key → recorded cycle, from :func:`cycles_by_slot`.
        actuals: The loaded realized series.
        zone: The site's time zone.
        site: The installation's hard limits.
        realized_forecasts: Replace each input's forecasts with realized
            values (the hindsight run) or leave them (the forecast run).

    Returns:
        The replay, or ``None`` when the day has no recorded cycle at all.
    """
    battery = prepared.configured
    stored = min(
        max(prepared.start_kwh, battery.min_stored_kwh), battery.max_stored_kwh
    )
    charged: list[float] = []
    discharged: list[float] = []
    labels: list[str | None] = []
    stale = 0
    shares: list[float] = []
    for index, (key, slot) in enumerate(zip(prepared.keys, prepared.slots)):
        payload, is_stale = _cycle_for_slot(index, prepared.keys, recorded)
        if payload is None:
            return None
        stale += int(is_stale)
        inp, _report = planner_input_from_dict(dict(payload))
        slot_start = key.astimezone(zone)
        inp = replace(
            inp,
            now_iso=slot_start.isoformat(),
            battery_soc_pct=stored / site.rated_kwh * 100.0,
        )
        if realized_forecasts:
            inp, share = with_realized_forecasts(inp, actuals, zone)
            shares.append(share)
        decision = _decision(inp, slot_start, site)
        charge, discharge = execute_decision(
            decision,
            net_load_kwh=slot.net_load_kwh,
            house_net_kwh=prepared.house_net_kwh[index],
            stored_kwh=stored,
            battery=battery,
            hours=slot.hours,
        )
        stored += charge * battery.charge_efficiency
        stored -= discharge / battery.discharge_efficiency
        charged.append(charge)
        discharged.append(discharge)
        labels.append(decision.recommendation)

    nets = [
        slot.net_load_kwh + c - d
        for slot, c, d in zip(prepared.slots, charged, discharged)
    ]
    grid_import = [max(net, 0.0) for net in nets]
    grid_export = [max(-net, 0.0) for net in nets]
    return RollingRun(
        run=HindsightRun(
            cost=grid_cost(prepared.slots, grid_import, grid_export),
            grid_import_kwh=tuple(grid_import),
            grid_export_kwh=tuple(grid_export),
            charged_kwh=tuple(charged),
            discharged_kwh=tuple(discharged),
            stored_kwh=tuple(
                stored_trajectory(battery, prepared.start_kwh, charged, discharged)
            ),
        ),
        recommendations=tuple(labels),
        stale_slots=stale,
        substituted_share=math.fsum(shares) / len(shares) if shares else 0.0,
    )


def _regret(prepared: RealizedDay, cost: float, end_kwh: float) -> float | None:
    """Return *cost* above the oracle that ends the day at *end_kwh*."""
    oracle = prepared.oracle(end_kwh)
    return None if oracle is None else cost - oracle.cost


def attribute_day(
    actuals: Actuals,
    payloads: Iterable[Mapping[str, Any]],
    day: date,
    zone: ZoneInfo,
    site: SiteLimits,
) -> DayAttribution:
    """Split one day's regret into execution, forecast and planner error.

    Args:
        actuals: The loaded realized series.
        payloads: Recorded cycles; those of other days and of other
            installations are ignored (issue #1225).
        day: The local calendar day.
        zone: The site's time zone.
        site: The installation's hard limits.  Must describe the installation
            that recorded both the cycles and the actuals.

    Returns:
        The attribution, or a :class:`DayAttribution` whose ``unscorable``
        says why there is none.
    """
    prepared = realized_day(actuals, day, zone, site)
    if isinstance(prepared, str):
        return DayAttribution(day=day, unscorable=prepared)
    recorded = site_cycles_by_slot(
        payloads, day, zone, actuals.slot_minutes, actuals.site_tag
    )
    if isinstance(recorded, str):
        return DayAttribution(day=day, unscorable=recorded)
    covered = sum(1 for key in prepared.keys if key in recorded)
    if covered < MIN_CYCLE_COVERAGE * len(prepared.keys):
        return DayAttribution(
            day=day,
            unscorable=(
                f"only {covered} of {len(prepared.keys)} slot(s) have a planner "
                f"cycle recorded in them"
            ),
        )
    widths = {
        (payload.get("planner_input") or {}).get("interval_minutes")
        for payload in recorded.values()
    }
    if widths != {actuals.slot_minutes}:
        return DayAttribution(
            day=day,
            unscorable=(
                f"cycles use {sorted(str(w) for w in widths)}-minute slots, "
                f"the actuals {actuals.slot_minutes}"
            ),
        )

    forecast_run = rolling_run(
        prepared, recorded, actuals, zone, site, realized_forecasts=False
    )
    hindsight_run = rolling_run(
        prepared, recorded, actuals, zone, site, realized_forecasts=True
    )
    if forecast_run is None or hindsight_run is None:  # pragma: no cover - guarded
        return DayAttribution(day=day, unscorable="the day could not be replayed")

    regret = _regret(prepared, prepared.realized_cost, prepared.stored_kwh[-1])
    forecast_regret = _regret(
        prepared, forecast_run.run.cost, forecast_run.run.stored_kwh[-1]
    )
    hindsight_regret = _regret(
        prepared, hindsight_run.run.cost, hindsight_run.run.stored_kwh[-1]
    )
    if regret is None or forecast_regret is None or hindsight_regret is None:
        return DayAttribution(
            day=day,
            unscorable="the oracle could not be solved (is scipy installed?)",
            notes=prepared.notes,
        )
    notes = list(prepared.notes)
    if forecast_run.stale_slots:
        notes.append(
            f"{forecast_run.stale_slots} of {len(prepared.keys)} slot(s) had no "
            f"cycle recorded in them and were planned from the nearest one"
        )
    if hindsight_run.substituted_share < 1.0 - 1e-9:
        notes.append(
            f"realized values cover {hindsight_run.substituted_share * 100:.0f} % of "
            f"the planning horizons; the rest of each horizon kept its forecast"
        )
    return DayAttribution(
        day=day,
        realized_cost=prepared.realized_cost,
        forecast_run_cost=forecast_run.run.cost,
        hindsight_run_cost=hindsight_run.run.cost,
        regret=regret,
        forecast_run_regret=forecast_regret,
        hindsight_run_regret=hindsight_regret,
        stale_slots=forecast_run.stale_slots,
        substituted_share=hindsight_run.substituted_share,
        notes=tuple(notes),
    )


def _money(value: float | None) -> str:
    return "       -" if value is None else f"{value:8.2f}"


def describe(results: Sequence[DayAttribution]) -> str:
    """Render attributions as a table.

    Args:
        results: Day attributions, in chronological order.

    Returns:
        A multi-line string for a terminal.  Costs are in the currency of the
        price sensors.
    """
    lines = [
        "day         realized forecast hindsight   regret = execution + forecast"
        " +  planner"
    ]
    for result in results:
        if not result.is_attributed:
            lines.append(f"{result.day}  not attributed: {result.unscorable}")
            continue
        lines.append(
            f"{result.day}  {_money(result.realized_cost)} "
            f"{_money(result.forecast_run_cost)}  {_money(result.hindsight_run_cost)} "
            f"{_money(result.regret)}    {_money(result.execution_error)}   "
            f"{_money(result.forecast_error)}  {_money(result.planner_error)}"
        )
        lines.extend(f"            note: {note}" for note in result.notes)
    return "\n".join(lines)
