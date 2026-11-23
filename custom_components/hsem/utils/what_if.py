"""Replay recorded days with another battery (issue #1432).

``hsem.what_if`` answers "what would another battery have been worth?".  For
each recorded day the perfect-foresight oracle of
``planner/hindsight_oracle.py`` is solved once with the battery as configured
and once per scenario, over the same load, PV and prices.  Only the difference
of the two is meaningful: both are the cheapest the day could have been, not
what HSEM would have achieved.

The day's load is rebuilt from the meters exactly as the performance scoring
does it (``utils/performance_scoring.py``), and a day that scoring would not
stand behind is skipped here too.

Every run of a day starts and must end with the stored energy the battery
really had, counted from the end-of-discharge floor and capped at what the
battery in question can hold.  Extra capacity therefore starts the day empty
and is worth only what it shifts within the day.

Two figures are kept apart.  The *grid* savings are money not paid to the
grid, and they are what a payback time is counted from.  The savings *after
wear* also subtract the battery's depreciation per kWh cycled, which is the
purchase price spread over its life: dividing an investment by them would
charge the battery twice.

Pure functions with no Home Assistant imports: the coordinator runs
:func:`run_what_if` in the executor.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from custom_components.hsem.models.performance_record import (
    REASON_ORACLE_UNSOLVED,
    REASON_SCIPY_UNAVAILABLE,
)
from custom_components.hsem.models.what_if_history import RecordedDay
from custom_components.hsem.planner._scipy_probe import is_scipy_available
from custom_components.hsem.planner.hindsight_oracle import (
    ORACLE_TIME_LIMIT_S,
    HindsightBattery,
    solve_hindsight_oracle,
)
from custom_components.hsem.planner.hindsight_scoring import (
    RealizedWindow,
    SiteLimits,
    realized_window,
)
from custom_components.hsem.utils.performance_scoring import (
    meter_contradiction,
    realized_slots,
    unexplained_energy_kwh,
)

#: Why a day in the asked-for history has no comparison: HSEM holds no
#: complete record of it.
REASON_NOT_RECORDED = "not_recorded"

#: Why a day has no comparison: a solve ran into its time limit.
REASON_ORACLE_TIMEOUT = "oracle_timeout"

#: Why a day has no comparison: the whole run ran out of time before it.
REASON_RUN_TIME_LIMIT = "run_time_limit"

#: Most scenarios one call may compare: each costs one solve per day.
MAX_SCENARIOS = 10

#: Fewest compared days a yearly figure, and so a payback time, is given for.
MIN_DAYS_FOR_YEARLY = 7

#: What every response says about its own figures.
NOTE = (
    "Both costs are perfect-foresight bounds: the cheapest each day could have "
    "been with the battery's hard limits and full knowledge of load, PV and "
    "prices. Neither is what HSEM would have achieved. Read the difference as "
    "an estimate of what the hardware change is worth, not as a promise. "
    "Every day starts and ends with the stored energy the battery really had. "
    "Yearly figures and the payback time scale the compared days to 365 days: "
    "they carry the season of those days and are biased unless the days cover "
    f"a full year. They are left out below {MIN_DAYS_FOR_YEARLY} compared days. "
    "The payback time is counted from the grid savings alone, since battery "
    "wear is the purchase price spread over the battery's life."
)

_DAYS_PER_YEAR = 365.0


@dataclass(frozen=True)
class WhatIfScenario:
    """A battery to compare with the configured one.

    Every field left ``None`` keeps the configured value.

    Attributes:
        usable_kwh: Usable capacity, between the end-of-discharge SoC and the
            charging cut-off SoC.
        max_charge_kw: AC charge power limit.
        max_discharge_kw: AC discharge power limit.
        charge_efficiency: Charge efficiency as a fraction.
        discharge_efficiency: Discharge efficiency as a fraction.
        cycle_cost_per_kwh: Wear per kWh of battery throughput.
    """

    usable_kwh: float | None = None
    max_charge_kw: float | None = None
    max_discharge_kw: float | None = None
    charge_efficiency: float | None = None
    discharge_efficiency: float | None = None
    cycle_cost_per_kwh: float | None = None

    @property
    def changes_nothing(self) -> bool:
        """Return whether the scenario is the configured battery."""
        return self == WhatIfScenario()

    def site(self, current: SiteLimits) -> SiteLimits:
        """Return *current* with this scenario's battery in place.

        The usable capacity is set by scaling the rated capacity, so the SoC
        limits stay as configured.

        Args:
            current: The installation's hard limits, with a positive usable
                capacity.
        """
        scenario = current
        if self.usable_kwh is not None:
            scenario = replace(
                scenario,
                rated_kwh=current.rated_kwh
                * self.usable_kwh
                / usable_capacity_kwh(current),
            )
        if self.max_charge_kw is not None:
            scenario = replace(scenario, max_charge_kw=self.max_charge_kw)
        if self.max_discharge_kw is not None:
            scenario = replace(scenario, max_discharge_kw=self.max_discharge_kw)
        if self.charge_efficiency is not None:
            scenario = replace(scenario, charge_efficiency=self.charge_efficiency)
        if self.discharge_efficiency is not None:
            scenario = replace(scenario, discharge_efficiency=self.discharge_efficiency)
        return scenario


@dataclass(frozen=True)
class WhatIfRequest:
    """Everything one what-if needs, detached from the live coordinator.

    Attributes:
        days: The local days of the asked-for history as ISO dates, in order.
        recorded: The recorded days by ISO date.
        reasons: Why the performance scoring had no score for a day, by ISO
            date; used to say why a day that is not recorded is skipped.
        site: The installation's hard limits as configured today.  A day's
            load is rebuilt with the capacity and efficiencies recorded with
            that day; both sides of the comparison then use these limits.
        cycle_cost_per_kwh: The configured wear per kWh of battery throughput.
        scenarios: The batteries to compare, at least one.
        capacity_price_per_kwh: Price of one kWh of usable capacity, for the
            payback time; ``None`` for no payback.
        time_limit_s: Wall-clock limit for each oracle solve.
        budget_s: Wall-clock limit for the whole run, checked before every
            solve; days it does not reach or finish are skipped.  ``None`` for
            no limit.
        pause_s: Sleep after each replayed day, which hands the processor to
            whatever else is waiting for it.
    """

    days: tuple[str, ...]
    recorded: Mapping[str, RecordedDay]
    reasons: Mapping[str, str]
    site: SiteLimits
    cycle_cost_per_kwh: float
    scenarios: tuple[WhatIfScenario, ...]
    capacity_price_per_kwh: float | None = None
    time_limit_s: float | None = None
    budget_s: float | None = None
    pause_s: float = 0.0


@dataclass(frozen=True)
class _Cost:
    """What one run of a day cost: the grid bill and the battery wear."""

    grid: float
    wear: float

    @property
    def total(self) -> float:
        return self.grid + self.wear


def usable_capacity_kwh(site: SiteLimits) -> float:
    """Return the capacity between a site's two SoC limits.

    Args:
        site: The installation's hard limits.
    """
    return site.rated_kwh * (site.max_soc_pct - site.min_soc_pct) / 100.0


def capacity_range(start_kwh: float, end_kwh: float, step_kwh: float) -> list[float]:
    """Return the usable capacities from *start_kwh* to *end_kwh* in steps.

    Args:
        start_kwh: First capacity.
        end_kwh: Last capacity; included when the steps land on it.
        step_kwh: Distance between two capacities.

    Raises:
        ValueError: If the step is not positive, the end is below the start,
            or the range holds more than :data:`MAX_SCENARIOS` capacities.
    """
    if step_kwh <= 0.0 or end_kwh < start_kwh:
        raise ValueError("the capacity range needs start <= end and a positive step")
    count = int((end_kwh - start_kwh) / step_kwh + 1e-9) + 1
    if count > MAX_SCENARIOS:
        raise ValueError(
            f"the capacity range holds {count} sizes; at most {MAX_SCENARIOS} "
            "are compared in one call"
        )
    return [round(start_kwh + index * step_kwh, 6) for index in range(count)]


def _power_caps(
    battery_kw: float,
    configured_kw: float,
    measured_kwh: tuple[float, ...],
    window: RealizedWindow,
) -> list[float]:
    """Return a battery's per-slot power limit for a recorded day.

    The battery's own limit, plus whatever the meter read above the
    *configured* limit in that slot.  The same rule for both sides of a
    comparison: the configured battery can repeat what it really did, and a
    scenario with the configured power gets the configured battery's limits.
    """
    return [
        battery_kw * slot.hours + max(measured - configured_kw * slot.hours, 0.0)
        for slot, measured in zip(window.slots, measured_kwh, strict=True)
    ]


def _solve(
    window: RealizedWindow,
    battery: HindsightBattery,
    cycle_cost_per_kwh: float,
    time_limit_s: float,
) -> _Cost | None:
    """Return what a day costs with one battery, or ``None`` if unsolved."""
    floor = window.configured.min_stored_kwh

    def placed(stored_kwh: float) -> float:
        """Return a stored energy carried over to *battery*, above its floor."""
        return min(
            max(battery.min_stored_kwh + stored_kwh - floor, battery.min_stored_kwh),
            battery.max_stored_kwh,
        )

    run = solve_hindsight_oracle(
        window.slots,
        battery,
        placed(window.start_kwh),
        placed(window.stored_kwh[-1]),
        max_charge_kwh=_power_caps(
            battery.max_charge_kw,
            window.configured.max_charge_kw,
            window.charged_kwh,
            window,
        ),
        max_discharge_kwh=_power_caps(
            battery.max_discharge_kw,
            window.configured.max_discharge_kw,
            window.discharged_kwh,
            window,
        ),
        max_grid_import_kwh=window.caps["max_grid_import_kwh"],
        max_grid_export_kwh=window.caps["max_grid_export_kwh"],
        throughput_cost_per_kwh=cycle_cost_per_kwh,
        time_limit_s=time_limit_s,
    )
    if run is None:
        return None
    throughput = sum(run.charged_kwh) + sum(run.discharged_kwh)
    return _Cost(grid=run.cost, wear=cycle_cost_per_kwh * throughput)


def replay_day(
    day: RecordedDay,
    request: WhatIfRequest,
    sides: tuple[WhatIfScenario, ...],
    cancelled: Callable[[], bool] = lambda: False,
    out_of_time: Callable[[], bool] = lambda: False,
) -> list[_Cost] | str | None:
    """Return what a recorded day costs with each of *sides*.

    The day's load is rebuilt with the capacity and efficiencies the day was
    recorded with, so a battery that has been changed since does not distort
    the days before the change.

    Args:
        day: The recorded day.
        request: The what-if being run.
        sides: The batteries to run the day with.
        cancelled: Asked before each solve.
        out_of_time: Asked before each solve; the day is skipped as
            :data:`REASON_RUN_TIME_LIMIT` when it returns ``True``, so a run
            passes its budget by one solve at most (issue #1454).

    Returns:
        One cost per side, the reason the day cannot be compared, or ``None``
        when the run was cancelled.  A day counts only when every side could
        be solved, so that all scenarios of one call cover the same days.
    """
    if not is_scipy_available():
        return REASON_SCIPY_UNAVAILABLE
    recorded = replace(
        request.site,
        rated_kwh=day.rated_kwh,
        charge_efficiency=day.charge_efficiency,
        discharge_efficiency=day.discharge_efficiency,
    )
    realized = realized_slots(day.performance_slots(), recorded)
    window = realized_window(
        realized, recorded.rated_kwh * day.soc_pct[0] / 100.0, recorded
    )
    contradiction = meter_contradiction(
        window, realized, unexplained_energy_kwh(realized)
    )
    if contradiction is not None:
        return contradiction
    limit = (
        ORACLE_TIME_LIMIT_S if request.time_limit_s is None else request.time_limit_s
    )
    costs: list[_Cost] = []
    for side in sides:
        if cancelled():
            return None
        if out_of_time():
            return REASON_RUN_TIME_LIMIT
        started = time.monotonic()
        cost = _solve(
            window,
            side.site(request.site).battery(),
            (
                request.cycle_cost_per_kwh
                if side.cycle_cost_per_kwh is None
                else side.cycle_cost_per_kwh
            ),
            limit,
        )
        if cost is None:
            return (
                REASON_ORACLE_TIMEOUT
                if time.monotonic() - started >= limit
                else REASON_ORACLE_UNSOLVED
            )
        costs.append(cost)
    return costs


def _limits(site: SiteLimits, cycle_cost_per_kwh: float) -> dict[str, float]:
    """Return a battery's limits in the units the service takes them in."""
    return {
        "usable_capacity_kwh": round(usable_capacity_kwh(site), 3),
        "max_charge_power_w": round(site.max_charge_kw * 1000.0, 1),
        "max_discharge_power_w": round(site.max_discharge_kw * 1000.0, 1),
        "charge_efficiency_pct": round(site.charge_efficiency * 100.0, 2),
        "discharge_efficiency_pct": round(site.discharge_efficiency * 100.0, 2),
        "cycle_cost_per_kwh": round(cycle_cost_per_kwh, 5),
    }


def _row(
    request: WhatIfRequest,
    scenario: WhatIfScenario,
    current: list[_Cost],
    changed: list[_Cost],
) -> dict[str, Any]:
    """Return one scenario's totals over the compared days."""
    site = scenario.site(request.site)
    cycle_cost = (
        request.cycle_cost_per_kwh
        if scenario.cycle_cost_per_kwh is None
        else scenario.cycle_cost_per_kwh
    )
    current_grid = sum(cost.grid for cost in current)
    scenario_grid = sum(cost.grid for cost in changed)
    current_wear = sum(cost.wear for cost in current)
    scenario_wear = sum(cost.wear for cost in changed)
    grid_savings = current_grid - scenario_grid
    yearly = (
        grid_savings / len(current) * _DAYS_PER_YEAR
        if len(current) >= MIN_DAYS_FOR_YEARLY
        else None
    )
    row: dict[str, Any] = {
        **_limits(site, cycle_cost),
        "current_grid_cost": round(current_grid, 3),
        "scenario_grid_cost": round(scenario_grid, 3),
        "grid_savings": round(grid_savings, 3),
        "current_wear_cost": round(current_wear, 3),
        "scenario_wear_cost": round(scenario_wear, 3),
        "savings_after_wear": round(grid_savings + current_wear - scenario_wear, 3),
        "grid_savings_per_year": None if yearly is None else round(yearly, 2),
    }
    if request.capacity_price_per_kwh is not None:
        extra = usable_capacity_kwh(site) - usable_capacity_kwh(request.site)
        investment = extra * request.capacity_price_per_kwh
        row["extra_capacity_kwh"] = round(extra, 3)
        row["investment"] = round(investment, 2)
        row["payback_years"] = (
            round(investment / yearly, 1)
            if yearly is not None and yearly > 0.0 and investment > 0.0
            else None
        )
    return row


def run_what_if(
    request: WhatIfRequest, cancelled: Callable[[], bool] = lambda: False
) -> dict[str, Any] | None:
    """Compare the configured battery with each scenario over the history.

    Blocking: every compared day solves one mixed-integer program for the
    configured battery and one per scenario.  Call it from the executor.

    Args:
        request: The days, the limits and the scenarios.
        cancelled: Asked before each day and before each solve; the run stops
            when it returns ``True``.

    Returns:
        The JSON-serialisable response, or ``None`` when the run was
        cancelled.  ``days`` lists each compared day when there is exactly
        one scenario; a capacity range reports its totals per size only.
    """
    sides = (WhatIfScenario(), *request.scenarios)
    compared: list[tuple[str, list[_Cost]]] = []
    skipped: dict[str, int] = {}
    started = time.monotonic()

    def out_of_time() -> bool:
        """Return whether the run has used its budget."""
        return (
            request.budget_s is not None
            and time.monotonic() - started >= request.budget_s
        )

    for date in request.days:
        if cancelled():
            return None
        day = request.recorded.get(date)
        outcome: list[_Cost] | str | None
        if day is None:
            outcome = request.reasons.get(date, REASON_NOT_RECORDED)
        elif out_of_time():
            outcome = REASON_RUN_TIME_LIMIT
        else:
            outcome = replay_day(day, request, sides, cancelled, out_of_time)
            if request.pause_s > 0.0:
                time.sleep(request.pause_s)
        if outcome is None:
            return None
        if isinstance(outcome, str):
            skipped[outcome] = skipped.get(outcome, 0) + 1
        else:
            compared.append((date, outcome))

    current = [costs[0] for _, costs in compared]
    response: dict[str, Any] = {
        "history_days": len(request.days),
        "days_compared": len(compared),
        "first_day_compared": compared[0][0] if compared else None,
        "last_day_compared": compared[-1][0] if compared else None,
        "days_skipped": len(request.days) - len(compared),
        "days_timed_out": skipped.get(REASON_ORACLE_TIMEOUT, 0)
        + skipped.get(REASON_RUN_TIME_LIMIT, 0),
        "skipped_reasons": dict(sorted(skipped.items())),
        "current": _limits(request.site, request.cycle_cost_per_kwh),
        "scenarios": [
            _row(
                request,
                scenario,
                current,
                [costs[index + 1] for _, costs in compared],
            )
            for index, scenario in enumerate(request.scenarios)
        ],
        "note": NOTE,
    }
    if len(request.scenarios) == 1:
        response["days"] = [
            {
                "date": date,
                "current_grid_cost": round(costs[0].grid, 3),
                "scenario_grid_cost": round(costs[1].grid, 3),
                "grid_savings": round(costs[0].grid - costs[1].grid, 3),
                "savings_after_wear": round(costs[0].total - costs[1].total, 3),
            }
            for date, costs in compared
        ]
    return response
