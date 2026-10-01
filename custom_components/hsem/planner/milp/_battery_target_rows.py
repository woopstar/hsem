"""LP rows and bounds for the house-battery target stage-2 solve (issue #1109).

The stage-2 solve of :mod:`~custom_components.hsem.planner.milp._battery_target`
re-solves the MILP with four additions, all carried by
:class:`BatteryTargetRows`:

- a width-1 ``battery_target_penalty`` slack column and one soft row::

      −Σ_{k≤T} (ec[k] − ed[k]) − pen ≤ current_kwh − target_kwh

  priced at ``penalty_per_kwh`` in the objective (undiscounted);
- a per-slot grid-import cap (``gi[t] ≤ gi_stage1[t]``), applied to the
  ``grid_import`` upper bound *before* the grid-direction big-M rows are
  built so both use the same bound;
- a per-slot grid-import floor (``gi[t] ≥ gi_stage1[t]`` inside the build
  window, ``0`` elsewhere), applied as the ``grid_import`` lower bound;
- a per-slot grid-export floor (``ge[t] ≥`` the battery-origin export of
  stage 1 inside the build window, ``0`` elsewhere), applied as the
  ``grid_export`` lower bound (issue #1203).

With import pinned and the battery's own export kept, the only way left to
raise ``soc[T]`` is to export (or curtail) less PV, which is exactly the
agreed semantics.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

    from custom_components.hsem.planner.milp._layout import MilpOffsets

#: Name of the slack column declared in ``_layout.build_milp_column_layout``.
BATTERY_TARGET_PENALTY_BLOCK = "battery_target_penalty"

#: Diagnostics key holding the LP ``gi[t]`` solution of a solve.
LP_GRID_IMPORT_KEY = "lp_grid_import_kwh"

#: Diagnostics key holding the battery-origin AC export of a solve, per LP
#: slot: ``min(ge[t], η_dis · bx[t])`` from the LP solution (issue #1203).
LP_BATTERY_EXPORT_KEY = "lp_battery_export_ac_kwh"


@dataclass(frozen=True)
class BatteryTargetRows:
    """Everything the stage-2 LP needs on top of the stage-1 model.

    Attributes:
        target_index: LP index ``T`` of the last future slot ending at or
            before the target occurrence.
        target_kwh: Target inventory in model kWh (above the MILP SoC
            origin), already clamped to ``[0, usable_kwh]``.
        penalty_per_kwh: Undiscounted objective cost per kWh of shortfall.
        grid_import_floor: Per-LP-slot lower bound on ``gi[t]``.
        grid_import_cap: Per-LP-slot upper bound on ``gi[t]``.
        grid_export_floor: Per-LP-slot lower bound on ``ge[t]``: the
            battery-origin export of stage 1 inside the build window, ``0``
            after it (issue #1203).
    """

    target_index: int
    target_kwh: float
    penalty_per_kwh: float
    grid_import_floor: tuple[float, ...]
    grid_import_cap: tuple[float, ...]
    grid_export_floor: tuple[float, ...]


def lp_pin_flows(
    solution: np.ndarray,  # type: ignore[name-defined]
    m: int,
    offsets: MilpOffsets,
    discharge_eff: float,
) -> dict[str, list[float]]:
    """Return the raw LP flows a stage-2 solve pins against.

    The published slot fields are rounded to 3 decimals and re-derived after
    mutex resolution, so a pin built from them can make a fully determined
    slot infeasible.  These are the solver's own column values.

    The battery-origin export is ``min(ge[t], η_dis · bx[t])``: ``bx[t]`` is
    only an upper bound on the battery's share of ``ge[t]``, so the minimum
    keeps the value at or below what the solve really exported.
    """
    import numpy as np

    gi_off, ge_off = offsets.gi_off, offsets.ge_off
    bx_off = offsets.battery_export_off
    grid_export = solution[ge_off : ge_off + m]
    battery_export = solution[bx_off : bx_off + m]
    return {
        LP_GRID_IMPORT_KEY: solution[gi_off : gi_off + m].tolist(),
        LP_BATTERY_EXPORT_KEY: np.minimum(
            grid_export, battery_export * discharge_eff
        ).tolist(),
    }


def cap_grid_import(
    grid_import_ub_per_slot: np.ndarray,  # type: ignore[name-defined]
    grid_import_cap_per_slot: Sequence[float] | None,
) -> np.ndarray:  # type: ignore[name-defined]
    """Return the physical import bound tightened by an optional per-slot cap."""
    import numpy as np

    if grid_import_cap_per_slot is None:
        return grid_import_ub_per_slot
    capped: np.ndarray = np.minimum(
        grid_import_ub_per_slot,
        np.maximum(np.asarray(grid_import_cap_per_slot, dtype=float), 0.0),
    )
    return capped


def grid_import_bounds(
    grid_import_ub_per_slot: Sequence[float] | np.ndarray,  # type: ignore[name-defined]
    grid_import_floor_per_slot: Sequence[float] | None,
) -> list[tuple[float, float]]:
    """Return ``(lower, upper)`` bounds for every ``gi[t]`` column.

    The floor is clamped into ``[0, upper]`` so the pin can never make the
    bound pair itself infeasible.
    """
    bounds: list[tuple[float, float]] = []
    for t, raw_upper in enumerate(grid_import_ub_per_slot):
        upper = max(float(raw_upper), 0.0)
        lower = 0.0
        if grid_import_floor_per_slot is not None:
            lower = min(max(float(grid_import_floor_per_slot[t]), 0.0), upper)
        bounds.append((lower, upper))
    return bounds


def grid_export_bounds(
    grid_export_ub_per_slot: Sequence[float] | np.ndarray,  # type: ignore[name-defined]
    grid_export_floor_per_slot: Sequence[float] | None,
) -> list[tuple[float, float]]:
    """Return ``(lower, upper)`` bounds for every ``ge[t]`` column.

    The floor is clamped into ``[0, upper]`` so it can never make the bound
    pair itself infeasible.
    """
    bounds: list[tuple[float, float]] = []
    for t, raw_upper in enumerate(grid_export_ub_per_slot):
        upper = max(float(raw_upper), 0.0)
        lower = 0.0
        if grid_export_floor_per_slot is not None:
            lower = min(max(float(grid_export_floor_per_slot[t]), 0.0), upper)
        bounds.append((lower, upper))
    return bounds


def add_battery_target_row(
    a_ub: np.ndarray,  # type: ignore[name-defined]
    b_ub: np.ndarray,  # type: ignore[name-defined]
    rows: BatteryTargetRows,
    *,
    ec_off: int,
    ed_off: int,
    penalty_off: int,
    current_kwh: float,
) -> tuple[np.ndarray, np.ndarray]:  # type: ignore[name-defined]
    """Append the soft target row ``−Σ_{k≤T}(ec − ed) − pen ≤ E_0 − E_target``."""
    import numpy as np

    row = np.zeros((1, a_ub.shape[1]))
    for k in range(rows.target_index + 1):
        row[0, ec_off + k] = -1.0
        row[0, ed_off + k] = 1.0
    row[0, penalty_off] = -1.0
    return (
        np.vstack([a_ub, row]),
        np.append(b_ub, float(current_kwh) - float(rows.target_kwh)),
    )
