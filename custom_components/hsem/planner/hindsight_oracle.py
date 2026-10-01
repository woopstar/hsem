"""Hindsight scoring of a battery over a window that has already happened.

Three numbers over the same slots and the same prices say whether a battery
was run well (issues #1208 and #1182):

- the **realized** grid cost — what was paid;
- a **self-consumption baseline** — what the inverter would have done alone;
- a **perfect-foresight oracle** — the cheapest the window could have been.

Everything here works on realized per-slot values, with **hard limits only**:
capacity, power and conversion losses.  No dynamic floor, reserve, hysteresis
or terminal value is modelled, because those are policy, and policy is what
the comparison measures.

The oracle must not be allowed to win by emptying the battery, so it has to
end the window with at least a given stored energy.  Hand it the energy the
battery really ended with.

Pure functions — no I/O, no Home Assistant imports.  The oracle needs scipy.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from custom_components.hsem.planner._scipy_probe import is_scipy_available

#: Relative MIP gap the oracle is solved to.  The oracle is quoted as a lower
#: bound, so "optimal within 0.01 %" would not do.
_ORACLE_MIP_GAP = 1e-9
#: Wall-clock limit for one oracle solve.  A day of 15-minute slots solves in
#: milliseconds; the limit only stops a pathological input from hanging.
_ORACLE_TIME_LIMIT_S = 60.0


@dataclass(frozen=True)
class HindsightSlot:
    """One slot of the window, as it actually happened.

    Attributes:
        hours: Slot duration in hours.
        net_load_kwh: What the site drew from (positive) or fed into
            (negative) everything that is not the battery: house, EV and other
            loads minus PV.  It is the grid flow the slot would have had with
            the battery idle.
        import_price: Price per kWh imported.
        export_price: Price per kWh exported, net of any export fee.
        curtailable_kwh: PV that could have been switched off instead of
            exported.  Only the oracle uses it, and only when exporting costs
            money.
    """

    hours: float
    net_load_kwh: float
    import_price: float
    export_price: float
    curtailable_kwh: float = 0.0


@dataclass(frozen=True)
class HindsightBattery:
    """The battery's hard limits.

    Charge and discharge are measured on the AC side: charging ``c`` kWh
    stores ``c × charge_efficiency`` and delivering ``d`` kWh removes
    ``d / discharge_efficiency``.  That is the planner's own convention
    (``planner/soc_simulation.py``).

    Attributes:
        min_stored_kwh: Stored energy at the hardware end-of-discharge SoC.
        max_stored_kwh: Stored energy at the charging cut-off SoC.
        max_charge_kw: AC charge power limit.
        max_discharge_kw: AC discharge power limit.
        charge_efficiency: Fraction of charged energy that is stored (0-1].
        discharge_efficiency: Fraction of removed energy delivered (0-1].
    """

    min_stored_kwh: float
    max_stored_kwh: float
    max_charge_kw: float
    max_discharge_kw: float
    charge_efficiency: float = 1.0
    discharge_efficiency: float = 1.0


@dataclass(frozen=True)
class HindsightRun:
    """Grid and battery flows of one way to run the window.

    Attributes:
        cost: Grid cost of the window: import paid minus export earned.
        grid_import_kwh: Grid import per slot.
        grid_export_kwh: Grid export per slot.
        charged_kwh: AC energy charged per slot.
        discharged_kwh: AC energy discharged per slot.
        stored_kwh: Stored energy at the end of each slot.
    """

    cost: float
    grid_import_kwh: tuple[float, ...]
    grid_export_kwh: tuple[float, ...]
    charged_kwh: tuple[float, ...]
    discharged_kwh: tuple[float, ...]
    stored_kwh: tuple[float, ...]

    @property
    def end_stored_kwh(self) -> float | None:
        """Return the stored energy after the last slot, if there is one."""
        return self.stored_kwh[-1] if self.stored_kwh else None


def grid_cost(
    slots: Sequence[HindsightSlot],
    grid_import_kwh: Sequence[float],
    grid_export_kwh: Sequence[float],
) -> float:
    """Return the grid cost of the given flows: import paid minus export earned.

    Args:
        slots: The window's slots, which carry the prices.
        grid_import_kwh: Grid import per slot.
        grid_export_kwh: Grid export per slot.

    Returns:
        The cost in the currency of the prices.  Negative when exports earned
        more than imports cost.
    """
    return math.fsum(
        imported * slot.import_price - exported * slot.export_price
        for slot, imported, exported in zip(
            slots, grid_import_kwh, grid_export_kwh, strict=True
        )
    )


def stored_trajectory(
    battery: HindsightBattery,
    start_stored_kwh: float,
    charged_kwh: Sequence[float],
    discharged_kwh: Sequence[float],
) -> list[float]:
    """Return the stored energy after each slot for the given battery flows.

    The result is not clamped to the battery's limits: a measured day run
    through an assumed efficiency can leave them, and the caller needs to see
    by how much.

    Args:
        battery: Supplies the conversion efficiencies.
        start_stored_kwh: Stored energy before the first slot.
        charged_kwh: AC energy charged per slot.
        discharged_kwh: AC energy discharged per slot.

    Returns:
        One stored-energy value per slot.
    """
    stored = start_stored_kwh
    trajectory: list[float] = []
    for charged, discharged in zip(charged_kwh, discharged_kwh, strict=True):
        stored += (
            charged * battery.charge_efficiency
            - discharged / battery.discharge_efficiency
        )
        trajectory.append(stored)
    return trajectory


def simulate_self_consumption(
    slots: Sequence[HindsightSlot],
    battery: HindsightBattery,
    start_stored_kwh: float,
) -> HindsightRun:
    """Simulate plain inverter self-consumption over the window.

    A surplus charges the battery and a deficit discharges it, each up to the
    power limit and the capacity.  Whatever is left goes to or comes from the
    grid.  Prices are not looked at: this is the hardware with nobody
    controlling it.

    Args:
        slots: The window's slots.
        battery: The battery's hard limits.
        start_stored_kwh: Stored energy before the first slot; clamped to the
            battery's limits.

    Returns:
        The flows and their cost.
    """
    stored = min(max(start_stored_kwh, battery.min_stored_kwh), battery.max_stored_kwh)
    grid_import: list[float] = []
    grid_export: list[float] = []
    charged: list[float] = []
    discharged: list[float] = []
    trajectory: list[float] = []
    for slot in slots:
        charge = discharge = 0.0
        if slot.net_load_kwh > 0.0:
            discharge = min(
                slot.net_load_kwh,
                battery.max_discharge_kw * slot.hours,
                (stored - battery.min_stored_kwh) * battery.discharge_efficiency,
            )
            stored -= discharge / battery.discharge_efficiency
        elif slot.net_load_kwh < 0.0:
            charge = min(
                -slot.net_load_kwh,
                battery.max_charge_kw * slot.hours,
                (battery.max_stored_kwh - stored) / battery.charge_efficiency,
            )
            stored += charge * battery.charge_efficiency
        net = slot.net_load_kwh + charge - discharge
        grid_import.append(max(net, 0.0))
        grid_export.append(max(-net, 0.0))
        charged.append(charge)
        discharged.append(discharge)
        trajectory.append(stored)
    return HindsightRun(
        cost=grid_cost(slots, grid_import, grid_export),
        grid_import_kwh=tuple(grid_import),
        grid_export_kwh=tuple(grid_export),
        charged_kwh=tuple(charged),
        discharged_kwh=tuple(discharged),
        stored_kwh=tuple(trajectory),
    )


def solve_hindsight_oracle(
    slots: Sequence[HindsightSlot],
    battery: HindsightBattery,
    start_stored_kwh: float,
    min_end_stored_kwh: float,
    *,
    max_charge_kwh: Sequence[float] | None = None,
    max_discharge_kwh: Sequence[float] | None = None,
    max_grid_import_kwh: Sequence[float] | None = None,
    max_grid_export_kwh: Sequence[float] | None = None,
) -> HindsightRun | None:
    """Return the cheapest way the battery could have been run, in hindsight.

    A mixed-integer program over the realized slots.  Per slot it chooses the
    AC charge, the AC discharge and how much PV to curtail; the grid takes the
    rest.  It minimises the grid cost subject to the battery's hard limits and
    to ending with at least *min_end_stored_kwh*.  A slot cannot both charge
    and discharge, nor both import and export, so the result is physically
    executable and not just a relaxation.

    The result is a lower bound on the realized cost of any run of the same
    window that is feasible under the same limits and ends with at least
    *min_end_stored_kwh*.  A caller that wants that guarantee for a measured
    day must hand in limits the measured day itself satisfies: the per-slot
    overrides exist for that.

    Args:
        slots: The window's slots.
        battery: The battery's hard limits.
        start_stored_kwh: Stored energy before the first slot.
        min_end_stored_kwh: Least stored energy after the last slot.
        max_charge_kwh: Per-slot AC charge limit, replacing
            ``max_charge_kw × hours``.
        max_discharge_kwh: Per-slot AC discharge limit, replacing
            ``max_discharge_kw × hours``.
        max_grid_import_kwh: Per-slot grid import limit; unlimited when
            ``None``.
        max_grid_export_kwh: Per-slot grid export limit; unlimited when
            ``None``.

    Returns:
        The optimal flows and their cost, or ``None`` when scipy is not
        available, the window is empty, or no run satisfies the limits (for
        example an end energy the battery cannot reach).
    """
    count = len(slots)
    if count == 0 or not is_scipy_available():
        return None

    import numpy as np
    from scipy import optimize

    # scipy's stubs type these bounds as scalars; arrays are what they take.
    linear_constraint: Any = optimize.LinearConstraint
    bounds: Any = optimize.Bounds

    charge_cap = np.array(
        max_charge_kwh
        if max_charge_kwh is not None
        else [battery.max_charge_kw * slot.hours for slot in slots],
        dtype=float,
    )
    discharge_cap = np.array(
        max_discharge_kwh
        if max_discharge_kwh is not None
        else [battery.max_discharge_kw * slot.hours for slot in slots],
        dtype=float,
    )
    net = np.array([slot.net_load_kwh for slot in slots], dtype=float)
    curtail_cap = np.array([max(slot.curtailable_kwh, 0.0) for slot in slots])
    # The most a slot can import or export, whatever the battery does.  Used
    # as the bound when no grid limit is given, and as the big-M that ties the
    # grid flows to their direction switch.
    import_cap = np.maximum(net, 0.0) + charge_cap + curtail_cap
    export_cap = np.maximum(-net, 0.0) + discharge_cap
    if max_grid_import_kwh is not None:
        import_cap = np.minimum(import_cap, np.array(max_grid_import_kwh, dtype=float))
    if max_grid_export_kwh is not None:
        export_cap = np.minimum(export_cap, np.array(max_grid_export_kwh, dtype=float))

    # Variable blocks, each ``count`` long: charge, discharge, import, export,
    # curtailment, then the battery and grid direction switches.
    (
        charge,
        discharge,
        grid_in,
        grid_out,
        curtail,
        charging,
        importing,
    ) = (slice(i * count, (i + 1) * count) for i in range(7))
    width = 7 * count
    eye = np.eye(count)

    def block(**parts: np.ndarray) -> np.ndarray:
        """Return a constraint matrix with *parts* placed in their columns."""
        columns = {
            "charge": charge,
            "discharge": discharge,
            "grid_in": grid_in,
            "grid_out": grid_out,
            "curtail": curtail,
            "charging": charging,
            "importing": importing,
        }
        matrix = np.zeros((count, width))
        for name, values in parts.items():
            matrix[:, columns[name]] = values
        return matrix

    # Energy balance: import − export = net load + charge − discharge + curtailed.
    balance = block(
        grid_in=eye, grid_out=-eye, charge=-eye, discharge=eye, curtail=-eye
    )
    # Stored energy after each slot, as a running sum of the battery flows.
    running = np.tril(np.ones((count, count)))
    stored = block(
        charge=running * battery.charge_efficiency,
        discharge=-running / battery.discharge_efficiency,
    )
    stored_low = np.full(count, battery.min_stored_kwh - start_stored_kwh)
    stored_low[-1] = max(battery.min_stored_kwh, min_end_stored_kwh) - start_stored_kwh
    stored_high = np.full(count, battery.max_stored_kwh - start_stored_kwh)

    objective = np.zeros(width)
    objective[grid_in] = [slot.import_price for slot in slots]
    objective[grid_out] = [-slot.export_price for slot in slots]

    upper = np.ones(width)
    upper[charge] = charge_cap
    upper[discharge] = discharge_cap
    upper[grid_in] = import_cap
    upper[grid_out] = export_cap
    upper[curtail] = curtail_cap
    integrality = np.zeros(width)
    integrality[charging] = 1
    integrality[importing] = 1

    result = optimize.milp(
        objective,
        constraints=[
            linear_constraint(balance, net, net),
            linear_constraint(stored, stored_low, stored_high),
            # charge ≤ cap × charging;  discharge ≤ cap × (1 − charging)
            linear_constraint(
                block(charge=eye, charging=-np.diag(charge_cap)), -np.inf, 0.0
            ),
            linear_constraint(
                block(discharge=eye, charging=np.diag(discharge_cap)),
                -np.inf,
                discharge_cap,
            ),
            # import ≤ cap × importing;  export ≤ cap × (1 − importing)
            linear_constraint(
                block(grid_in=eye, importing=-np.diag(import_cap)), -np.inf, 0.0
            ),
            linear_constraint(
                block(grid_out=eye, importing=np.diag(export_cap)),
                -np.inf,
                export_cap,
            ),
        ],
        integrality=integrality,
        bounds=bounds(np.zeros(width), upper),
        options={"mip_rel_gap": _ORACLE_MIP_GAP, "time_limit": _ORACLE_TIME_LIMIT_S},
    )
    if not result.success or result.x is None:
        return None

    charged = [max(float(v), 0.0) for v in result.x[charge]]
    discharged = [max(float(v), 0.0) for v in result.x[discharge]]
    grid_import = [max(float(v), 0.0) for v in result.x[grid_in]]
    grid_export = [max(float(v), 0.0) for v in result.x[grid_out]]
    return HindsightRun(
        cost=grid_cost(slots, grid_import, grid_export),
        grid_import_kwh=tuple(grid_import),
        grid_export_kwh=tuple(grid_export),
        charged_kwh=tuple(charged),
        discharged_kwh=tuple(discharged),
        stored_kwh=tuple(
            stored_trajectory(battery, start_stored_kwh, charged, discharged)
        ),
    )
