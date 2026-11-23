"""LP rows for the pre-peak load-forecast reserve (issue #1361).

Per reserve one soft row and one ``load_reserve_slack`` column per tier::

    −Σ_{k≤T} (ec[k] − ed[k]) − Σ_j slack_j ≤ current_kwh − level_kwh

``T`` is the last slot of the expensive window and ``level_kwh`` the energy
the battery holds there with the full reserve.  Each tier's slack is priced,
undiscounted, at what that part of the reserve is worth, and bounded by the
tier's width, so the solver gives up the least valuable part first and holds
only what costs less to carry than it is worth.  The dearest tier is
unbounded, which keeps the row feasible whatever the battery holds.  The
house load in the energy balance is untouched.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from custom_components.hsem.planner.load_reserve import LoadReservePlan
from custom_components.hsem.planner.milp._battery_target_rows import (
    add_soft_soc_target_row,
)
from custom_components.hsem.planner.milp._layout import (
    LOAD_RESERVE_SLACK_BLOCK,
    Bound,
    MilpColumnLayout,
)
from custom_components.hsem.utils.datetime_utils import utc_key

if TYPE_CHECKING:
    import numpy as np

    from custom_components.hsem.models.planned_slot import PlannedSlot


@dataclass(frozen=True)
class LoadReserveRows:
    """What the LP needs for one reserve row.

    Attributes:
        target_index: LP index ``T`` of the last slot of the window.
        target_kwh: Model kWh the battery holds at the end of ``T`` with the
            full reserve.
        tiers: ``(width_kwh, price_per_kwh)`` per slack column, cheapest
            first; the last one is unbounded.
    """

    target_index: int
    target_kwh: float
    tiers: tuple[tuple[float, float], ...]


def load_reserve_rows(
    plan: LoadReservePlan | None,
    slots: Sequence[PlannedSlot],
    future_idx: Sequence[int],
) -> tuple[LoadReserveRows, ...]:
    """Return the LP row data of every applied reserve.

    Args:
        plan: The reserve plan of this run, or ``None`` when off.
        slots: The slot list of the solve.
        future_idx: Slot index per LP index.
    """
    if plan is None:
        return ()
    lp_index = {utc_key(slots[i].end): t for t, i in enumerate(future_idx)}
    return tuple(
        LoadReserveRows(lp_index[key], spec.level_kwh, spec.tiers)
        for spec in plan.windows
        if spec.window_end is not None and (key := utc_key(spec.window_end)) in lp_index
    )


def load_reserve_column_count(rows: Sequence[LoadReserveRows]) -> int:
    """Return the number of slack columns the reserves need."""
    return sum(len(row.tiers) for row in rows)


def load_reserve_slack_bounds(rows: Sequence[LoadReserveRows]) -> list[Bound]:
    """Return ``(lower, upper)`` per slack column, in column order."""
    bounds: list[Bound] = []
    for row in rows:
        bounds.extend((0.0, width) for width, _price in row.tiers[:-1])
        bounds.append((0.0, None))
    return bounds


def price_load_reserve_slack(
    c_obj: np.ndarray,  # type: ignore[name-defined]
    column_layout: MilpColumnLayout,
    rows: Sequence[LoadReserveRows],
) -> None:
    """Write each tier's undiscounted slack price into the objective."""
    prices = [price for row in rows for _width, price in row.tiers]
    for k, price in enumerate(prices):
        c_obj[column_layout.offset(LOAD_RESERVE_SLACK_BLOCK) + k] = price


def add_load_reserve_rows(
    a_ub: np.ndarray,  # type: ignore[name-defined]
    b_ub: np.ndarray,  # type: ignore[name-defined]
    rows: Sequence[LoadReserveRows],
    *,
    column_layout: MilpColumnLayout,
    ec_off: int,
    ed_off: int,
    current_kwh: float,
) -> tuple[np.ndarray, np.ndarray]:  # type: ignore[name-defined]
    """Append one soft row per reserve, on its own slack columns."""
    column = column_layout.offset(LOAD_RESERVE_SLACK_BLOCK) if rows else 0
    for row in rows:
        a_ub, b_ub = add_soft_soc_target_row(
            a_ub,
            b_ub,
            target_index=row.target_index,
            target_kwh=row.target_kwh,
            ec_off=ec_off,
            ed_off=ed_off,
            penalty_off=column,
            current_kwh=current_kwh,
        )
        a_ub[-1, column : column + len(row.tiers)] = -1.0
        column += len(row.tiers)
    return a_ub, b_ub
