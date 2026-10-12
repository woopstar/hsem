"""Bridge arithmetic of the dynamic discharge floor (issues #600, #1156 ff.).

The stateless half of the dynamic floor: given the future slots of a
reference plan, how much energy the battery has to hold to bridge the house
to its next refill, which planned or affordable grid charges count as one,
and how that reserve is laid out per slot.
:class:`~custom_components.hsem.utils.dynamic_floor.DynamicDischargeFloor`
owns the state (the learned safety margin) and calls these.

Extracted from :mod:`custom_components.hsem.utils.dynamic_floor` to keep it
under the 30 KB / 1000-line limit (issue #1274).  Pure functions, no I/O.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import datetime
from typing import Any, NamedTuple

from custom_components.hsem.utils.recommendations import Recommendations
from custom_components.hsem.utils.soc_bounds import reserve_floor_pct
from custom_components.hsem.utils.units import slot_duration_hours


def cheap_refill_price(
    import_prices: Iterable[float], cycle_cost_per_kwh: float
) -> float | None:
    """Return the highest import price that counts as an affordable refill.

    A grid refill is *affordable* when its import price is within one battery
    cycle cost of the cheapest import price in the look-ahead (issue #1156).
    The cycle cost is the smallest price spread the planner treats as worth
    moving energy for, so a slot that close to the look-ahead's minimum is as
    cheap a refill as the horizon offers.  A night that is not the cheapest
    time of the look-ahead, such as a 0.15 night before a 0.12 day, is not.

    Args:
        import_prices: Import prices of the look-ahead slots.  Non-finite
            values (slots without a price) are ignored.
        cycle_cost_per_kwh: Battery wear per kWh of throughput; a negative or
            non-finite value counts as 0.

    Returns:
        The threshold price, or ``None`` when no price is finite or when the
        prices spread by no more than the cycle cost.  Without a price valley
        no slot is cheaper than any other, so none is an affordable refill.
    """
    finite = [p for p in import_prices if math.isfinite(p)]
    if not finite:
        return None
    tolerance = _cycle_cost_tolerance(cycle_cost_per_kwh)
    lowest = min(finite)
    if max(finite) - lowest <= tolerance + 1e-9:
        return None
    return lowest + tolerance


def _cycle_cost_tolerance(cycle_cost_per_kwh: float) -> float:
    """Return the price tolerance of one battery cycle; 0 when unusable."""
    if math.isfinite(cycle_cost_per_kwh) and cycle_cost_per_kwh > 0.0:
        return cycle_cost_per_kwh
    return 0.0


#: Resolution of the stored energy the plan publishes per slot (1 Wh): what is
#: left of a planned charge after the buffer buy-back within it is rounding.
_STORED_ENERGY_RESOLUTION_KWH = 1e-3


def _export_buffer_buy_back_kwh(bridge: list, export_buffer_kwh: float) -> float:
    """Return the part of the bridge's grid charges that only restores the export buffer.

    A plan that exports battery energy before the PV surplus must hold
    *export_buffer_kwh* at the checkpoint before that surplus (the MILP
    export reserve).  What it sold below the buffer it has to buy back, and
    that purchase supplies the checkpoint, not the house: it is no refill
    for the bridge (issue #1239).  The buy-back is the buffer minus the
    least the plan holds from its last battery export in the bridge on.

    Args:
        bridge: The look-ahead slots before the first solar surplus.
        export_buffer_kwh: The export reserve's checkpoint buffer (kWh);
            0 when excess export is disabled.

    Returns:
        kWh of the bridge's planned grid charges that are not a refill.
    """
    if export_buffer_kwh <= 1e-9:
        return 0.0
    last_export = None
    for index, slot in enumerate(bridge):
        if (getattr(slot, "battery_export_kwh", 0.0) or 0.0) > 1e-9:
            last_export = index
    if last_export is None:
        return 0.0
    stored = [
        getattr(slot, "stored_kwh_at_end", math.nan) for slot in bridge[last_export:]
    ]
    finite = [value for value in stored if math.isfinite(value)]
    if not finite:
        return 0.0
    return max(export_buffer_kwh - min(finite), 0.0)


def _planned_grid_charges(
    future: list, price_tolerance: float, export_buffer_kwh: float = 0.0
) -> list[float]:
    """Return the reference plan's grid charge per slot, placement-invariant.

    The reference solve is free to put a grid charge in any of several
    equally priced slots, and it does not pick the same one on every replan.
    The bridge scan tests each charge against the consumption bridged *up to
    its slot*, so the same charge released the floor when it sat early in the
    night and left a reserve when it sat late (issue #1198).

    Each planned charge is therefore credited at the **earliest bridge slot
    priced within** *price_tolerance* **of its own slot**: the plan could have
    bought the same energy there at the same cost.  The bridge is every slot
    before the first solar surplus; charges behind it are never read.  A
    charge whose slot has no finite price stays where it is.

    A charge that only buys back the export reserve's buffer after a battery
    export is not credited (issue #1239, :func:`_export_buffer_buy_back_kwh`);
    it is taken off the latest credits first, since the buffer is what the
    plan still holds at the end of the bridge.

    Args:
        future: Chronological look-ahead slots.
        price_tolerance: Largest price difference between two slots that
            still counts as equally priced (the battery cycle cost).
        export_buffer_kwh: The export reserve's checkpoint buffer (kWh).

    Returns:
        One credit in kWh per slot of *future*.
    """
    credits = [0.0] * len(future)
    prices: list[float] = []
    charges: list[tuple[int, float]] = []
    for index, slot in enumerate(future):
        if (getattr(slot, "estimated_net_consumption_kwh", 0.0) or 0.0) < -1e-9:
            break
        prices.append(getattr(slot, "import_price", math.nan))
        charged = getattr(slot, "batteries_charged_kwh", 0.0) or 0.0
        if (
            charged > 1e-9
            and getattr(slot, "recommendation", None)
            == Recommendations.BatteriesChargeGrid.value
        ):
            charges.append((index, charged))
    # The buffer is what the plan still holds at the end of the bridge, so
    # it is the energy bought last: take the buy-back off the latest charges.
    buy_back = _export_buffer_buy_back_kwh(future[: len(prices)], export_buffer_kwh)
    for position in reversed(range(len(charges))):
        if buy_back <= 1e-9:
            break
        index, charged = charges[position]
        taken = min(charged, buy_back)
        charges[position] = (index, charged - taken)
        buy_back -= taken
    for index, charged in charges:
        if charged <= 1e-9 + (
            _STORED_ENERGY_RESOLUTION_KWH if export_buffer_kwh > 1e-9 else 0.0
        ):
            continue
        price = prices[index]
        target = index
        if math.isfinite(price):
            target = next(
                earlier
                for earlier, earlier_price in enumerate(prices)
                if abs(earlier_price - price) <= price_tolerance + 1e-9
            )
        credits[target] += charged
    return credits


def _cheap_window_slots(future: list, cycle_cost_per_kwh: float) -> frozenset[int]:
    """Return the indices of the slots where the plan can refill for the dearer slots behind them.

    Inside a cheap window the reference plan's last small grid charge comes
    and goes with the live SoC, and with it the floor flipped between the
    configured minimum (a charge credited to the live slot) and the bridge to
    the solar surplus (issue #1238).  Whether the plan *does* buy depends on
    the SoC; whether it *can* does not (issue #1247).  A slot is part of a
    cheap window when its import price is within one battery cycle cost of
    the cheapest price from that slot to the next solar surplus, and the
    prices of that remaining bridge spread by more than the cycle cost: the
    plan can buy there for the slots behind it that cost more.  With flat
    prices nothing is cheaper to buy now than to import later, so a flat
    bridge keeps its reserve, and so does a cheap tail with nothing dearer
    behind it.

    Args:
        future: Chronological look-ahead slots, the live slot first.
        cycle_cost_per_kwh: Battery wear per kWh of throughput.

    Returns:
        Indices into *future* of the cheap-window slots before the first
        solar surplus; empty when no slot has a price.
    """
    bridge: list[float] = []
    for slot in future:
        if (getattr(slot, "estimated_net_consumption_kwh", 0.0) or 0.0) < -1e-9:
            break
        bridge.append(getattr(slot, "import_price", math.nan))
    tolerance = _cycle_cost_tolerance(cycle_cost_per_kwh)
    cheap: set[int] = set()
    lowest, highest = math.inf, -math.inf
    for index in range(len(bridge) - 1, -1, -1):
        price = bridge[index]
        if not math.isfinite(price):
            continue
        lowest, highest = min(lowest, price), max(highest, price)
        if highest - lowest > tolerance + 1e-9 and price <= lowest + tolerance + 1e-9:
            cheap.add(index)
    return frozenset(cheap)


class _BridgeScan(NamedTuple):
    """Result of one walk from now to the next refill.

    ``deltas`` holds, for every slot before the refill, what that slot adds
    to the reserve: its net consumption.  ``covered`` is true for an
    affordable refill (``grid_available``); such a refill leaves no reserve.
    """

    refill_slot: Any
    refill_type: str
    consumption_kwh: float
    solar_kwh: float
    covered: bool
    duration_hours: float
    deltas: tuple[float, ...] = ()

    @property
    def reserve_kwh(self) -> float:
        """Return the bridge's reserve before the safety margin."""
        if self.covered:
            return 0.0
        return max(self.consumption_kwh - self.solar_kwh, 0.0)


def _scan_bridge(
    future: list,
    planned_charges: list[float],
    cheap_price: float | None,
    max_grid_charge_kw: float,
    cheap_window: frozenset[int] = frozenset(),
) -> _BridgeScan:
    """Walk *future* to the first refill and total the energy bridged.

    With *cheap_price* ``None`` only the reference plan's grid charges are
    credited, and the first credited slot is a ``grid_charge`` refill: the
    bridge ends where the plan refills, and the reserve is the consumption
    before it (issue #1214) whatever the size of the charge (issue #1220: a
    covering charge used to release the floor, and the amount the plan buys
    depends on the live SoC).  The reserve never reaches past a planned
    charge, so holding it cannot shrink that charge on the next replan.

    Otherwise a slot priced at or below *cheap_price* is credited with what
    the battery can take at *max_grid_charge_kw*, credits add up, and a
    covering total is a ``grid_available`` refill (issue #1156).  A planned
    charge in a slot that is not affordable ends this scan too.

    A slot in *cheap_window* (see :func:`_cheap_window_slots`) ends the
    bridge like a planned charge, as a ``cheap_window`` refill with the
    consumption before it as the reserve, whether or not the plan charges
    there (issue #1247).  A planned charge in the same slot is read first and
    keeps its label.

    Args:
        future: Chronological look-ahead slots.
        planned_charges: The reference plan's grid charge credited to each
            slot, from :func:`_planned_grid_charges`.
        cheap_price: Affordable-refill threshold from
            :func:`cheap_refill_price`, or ``None``.
        max_grid_charge_kw: Battery charge power limit (kW).
        cheap_window: Indices into *future* of the cheap-window slots.

    Returns:
        The refill slot and type (``None`` / ``"none"`` when no refill is
        found), the bridge's consumption, solar and hours, and whether the
        refill covers the bridge.
    """
    consumption = 0.0
    solar = 0.0
    grid_charge = 0.0
    hours = 0.0
    deltas: list[float] = []
    for index, (s, planned) in enumerate(zip(future, planned_charges, strict=True)):
        slot_hours = slot_duration_hours(s.start, s.end)

        net = getattr(s, "estimated_net_consumption_kwh", 0.0) or 0.0

        # Check for solar surplus refill.
        if net < -1e-9:
            return _BridgeScan(
                s, "solar_surplus", consumption, solar, False, hours, tuple(deltas)
            )

        # Check for grid-charge refill: a slot the reference plan's grid
        # charge is credited to, or (second pass) an affordable slot it
        # could charge in.
        price = getattr(s, "import_price", math.nan)
        affordable = cheap_price is not None and price <= cheap_price + 1e-9
        credit = (
            max(planned, max_grid_charge_kw * slot_hours) if affordable else planned
        )
        if credit > 1e-9:
            if not affordable:
                # A planned charge ends the bridge whatever its size: the
                # reserve is what is consumed before it (issues #1214, #1220).
                # In the affordable pass that leaves the first scan standing.
                refill_type = "grid_charge" if cheap_price is None else "none"
                return _BridgeScan(
                    s, refill_type, consumption, solar, False, hours, tuple(deltas)
                )
            grid_charge += credit
            # The credit covers the bridge when it is at least what was
            # consumed up to this slot (consumption - solar so far).
            if grid_charge >= max(consumption - solar, 0.0):
                return _BridgeScan(
                    s, "grid_available", consumption, solar, True, hours, tuple(deltas)
                )
            # An affordable credit that does not cover the bridge yet: keep
            # scanning, later affordable slots add to it.
            deltas.append(0.0)
            hours += slot_hours
            continue

        # A cheap-window slot is where the plan can refill for the dearer
        # slots behind it, whether or not it does; the bridge ends there
        # (issue #1247).  A planned charge in the same slot was read first.
        if index in cheap_window:
            return _BridgeScan(
                s, "cheap_window", consumption, solar, False, hours, tuple(deltas)
            )

        # Regular consumption slot.
        if net > 1e-9:
            consumption += net
        elif net < -1e-9:
            # Solar surplus that we didn't catch above (shouldn't happen due to
            # the return above, but be safe).
            solar += abs(net)
        deltas.append(max(net, 0.0))

        hours += slot_hours
    return _BridgeScan(None, "none", consumption, solar, False, hours, tuple(deltas))


def _floor_profile(
    future: list,
    scan: _BridgeScan,
    usable_kwh: float,
    configured_min_soc_pct: float,
    max_soc_pct: float,
    safety_margin: float,
) -> list[tuple[datetime, float]]:
    """Return the floor at the start of every look-ahead slot (issue #1188).

    The reserve at the start of a bridge slot is what remains of the bridge
    from that slot on: the sum of the remaining ``scan.deltas``, clamped at
    zero and converted as the scalar floor is, except that an entry is not
    capped at the maximum SoC: the planner needs the whole reserve to decide
    which bridge slots a battery that cannot hold it serves (issue #1222).
    The first entry, capped, is therefore the scalar floor.  From the refill
    slot on the reserve is no longer needed and the floor is the configured
    minimum.  A refill that covers the bridge leaves no reserve at all, as
    for the scalar.

    Args:
        future: Chronological look-ahead slots the scan walked.
        scan: The bridge scan whose reserve becomes the scalar floor.
        usable_kwh: Capacity between the minimum and the maximum SoC (kWh).
        configured_min_soc_pct: Configured minimum SoC (0-100).
        max_soc_pct: Configured maximum SoC (0-100).
        safety_margin: The margin applied to the reserve.

    Returns:
        ``(slot start, floor SoC %)`` for every slot in *future*.
    """
    remaining = [0.0] * len(future)
    if not scan.covered:
        total = 0.0
        for index in range(len(scan.deltas) - 1, -1, -1):
            total += scan.deltas[index]
            remaining[index] = max(total, 0.0)
    return [
        (
            slot.start,
            reserve_floor_pct(
                reserve_kwh * safety_margin,
                usable_kwh,
                configured_min_soc_pct,
                max_soc_pct,
                capped=False,
            ),
        )
        for slot, reserve_kwh in zip(future, remaining)
    ]
