"""Dynamic self-learning discharge floor (issue #600).

Computes the reserve SoC needed to run the house until the next energy refill
(solar surplus, planned grid-charge, or an affordable grid refill), with a
self-correcting safety margin.

Usage
-----
Instantiate once per entry and call :meth:`DynamicDischargeFloor.compute_floor`
after each planner run.  Call :meth:`DynamicDischargeFloor.correct_margin` every
cycle with the actual SoC; it files the evidence under the local day and lets
the safety margin self-correct at most once per day (issue #1141).
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, NamedTuple

from custom_components.hsem.utils.logger import log_planner
from custom_components.hsem.utils.recommendations import Recommendations
from custom_components.hsem.utils.units import slot_duration_hours

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default safety margin: 15 % buffer above the computed reserve.
_DEFAULT_SAFETY_MARGIN = 1.15
# Floor for the safety margin — never below 5 % buffer.
_MIN_SAFETY_MARGIN = 1.05
# Ceiling for the safety margin — never above 50 % buffer.
_MAX_SAFETY_MARGIN = 1.50
# Margin increase step (absolute multiplier) when SoC drops below floor.
_MARGIN_INCREASE = 0.05
# Margin decrease step (absolute multiplier) when SoC stays well above floor.
_MARGIN_DECREASE = 0.02
# Number of days below floor before increasing margin.
_DAYS_BELOW_FLOOR_TRIGGER = 2
# Number of days above floor before decreasing margin.
_DAYS_ABOVE_FLOOR_TRIGGER = 7
# Threshold multiplier: SoC is "well above" floor when it exceeds floor by 30 %.
_WELL_ABOVE_FACTOR = 1.3
# SoC points below the floor in force before a dip counts as a shortfall.
# Absorbs the plan landing exactly on its floor and SoC-reading resolution.
_SHORTFALL_TOLERANCE_PCT = 1.0


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
    tolerance = (
        cycle_cost_per_kwh
        if math.isfinite(cycle_cost_per_kwh) and cycle_cost_per_kwh > 0.0
        else 0.0
    )
    lowest = min(finite)
    if max(finite) - lowest <= tolerance + 1e-9:
        return None
    return lowest + tolerance


class _BridgeScan(NamedTuple):
    """Result of one walk from now to the next refill.

    ``deltas`` holds, for every slot before the refill, what that slot adds
    to the reserve: its net consumption, or minus the grid charge credited in
    it.  Their sum is the bridge's reserve before the safety margin.
    """

    refill_slot: Any
    refill_type: str
    consumption_kwh: float
    solar_kwh: float
    grid_charge_kwh: float
    duration_hours: float
    deltas: tuple[float, ...] = ()


def _scan_bridge(
    future: list, cheap_price: float | None, max_grid_charge_kw: float
) -> _BridgeScan:
    """Walk *future* to the first refill and total the energy bridged.

    With *cheap_price* ``None`` only the reference plan's grid charges are
    credited, and a covering one is a ``grid_charge`` refill.  Otherwise a
    slot priced at or below *cheap_price* is also credited with what the
    battery can take at *max_grid_charge_kw*, and a covering credit is a
    ``grid_available`` refill (issue #1156).

    Args:
        future: Chronological look-ahead slots.
        cheap_price: Affordable-refill threshold from
            :func:`cheap_refill_price`, or ``None``.
        max_grid_charge_kw: Battery charge power limit (kW).

    Returns:
        The refill slot and type (``None`` / ``"none"`` when no refill is
        found) and the bridge's consumption, solar, grid credit and hours.
    """
    consumption = 0.0
    solar = 0.0
    grid_charge = 0.0
    hours = 0.0
    deltas: list[float] = []
    for s in future:
        slot_hours = slot_duration_hours(s.start, s.end)

        net = getattr(s, "estimated_net_consumption_kwh", 0.0) or 0.0

        # Check for solar surplus refill.
        if net < -1e-9:
            return _BridgeScan(
                s,
                "solar_surplus",
                consumption,
                solar,
                grid_charge,
                hours,
                tuple(deltas),
            )

        # Check for grid-charge refill: a slot where the reference plan grid
        # charges, or (second pass) an affordable slot it could charge in.
        charged = getattr(s, "batteries_charged_kwh", 0.0) or 0.0
        planned = (
            charged
            if charged > 1e-9
            and getattr(s, "recommendation", None)
            == Recommendations.BatteriesChargeGrid.value
            else 0.0
        )
        price = getattr(s, "import_price", math.nan)
        affordable = cheap_price is not None and price <= cheap_price + 1e-9
        credit = (
            max(planned, max_grid_charge_kw * slot_hours) if affordable else planned
        )
        if credit > 1e-9:
            grid_charge += credit
            # Check if the accumulated grid charge covers the reserve need.
            # The reserve need is consumption - solar so far.
            if grid_charge >= max(consumption - solar, 0.0):
                refill_type = "grid_charge" if cheap_price is None else "grid_available"
                return _BridgeScan(
                    s,
                    refill_type,
                    consumption,
                    solar,
                    grid_charge,
                    hours,
                    tuple(deltas),
                )
            # If it doesn't cover the full reserve yet, continue scanning.
            # The grid charge energy will be counted in the final
            # reserve calculation (subtracted from consumption).
            deltas.append(-credit)
            hours += slot_hours
            continue

        # Regular consumption slot.
        if net > 1e-9:
            consumption += net
        elif net < -1e-9:
            # Solar surplus that we didn't catch above (shouldn't happen due to
            # the return above, but be safe).
            solar += abs(net)
        deltas.append(max(net, 0.0))

        hours += slot_hours
    return _BridgeScan(
        None, "none", consumption, solar, grid_charge, hours, tuple(deltas)
    )


def _floor_profile(
    future: list,
    scan: _BridgeScan,
    usable_kwh: float,
    configured_min_soc_pct: float,
    safety_margin: float,
) -> list[tuple[datetime, float]]:
    """Return the floor at the start of every look-ahead slot (issue #1188).

    The reserve at the start of a bridge slot is what remains of the bridge
    from that slot on: the sum of the remaining ``scan.deltas``, clamped at
    zero and converted exactly as the scalar floor is.  The first entry is
    therefore the scalar floor.  From the refill slot on the reserve is no
    longer needed and the floor is the configured minimum.  A refill that
    covers the bridge (``grid_charge`` / ``grid_available``) leaves no reserve
    at all, as for the scalar.

    Args:
        future: Chronological look-ahead slots the scan walked.
        scan: The bridge scan whose reserve becomes the scalar floor.
        usable_kwh: Maximum usable battery capacity (kWh).
        configured_min_soc_pct: Configured minimum SoC (0-100).
        safety_margin: The margin applied to the reserve.

    Returns:
        ``(slot start, floor SoC %)`` for every slot in *future*.
    """
    remaining = [0.0] * len(future)
    if scan.refill_type in ("solar_surplus", "none") and usable_kwh > 1e-9:
        total = 0.0
        for index in range(len(scan.deltas) - 1, -1, -1):
            total += scan.deltas[index]
            remaining[index] = max(total, 0.0)
    return [
        (
            slot.start,
            max(
                configured_min_soc_pct,
                (reserve_kwh / usable_kwh) * 100.0 * safety_margin
                if usable_kwh > 1e-9
                else 0.0,
            ),
        )
        for slot, reserve_kwh in zip(future, remaining)
    ]


# ---------------------------------------------------------------------------
# DynamicDischargeFloor
# ---------------------------------------------------------------------------


@dataclass
class DynamicDischargeFloor:
    """Computes a dynamic discharge floor based on bridge-to-refill energy.

    Scans future planner output slots to find the next energy *refill* slot
    (solar surplus, planned grid-charge, or affordable grid refill), sums the
    house consumption between now and that refill, and applies a self-learning
    safety margin.

    Attributes:
        safety_margin:
            Self-learning multiplier (≥ 1.0).  Starts at 1.15 (15 % buffer).
        min_margin:
            Floor for the safety margin — never below 1.05.
        max_margin:
            Ceiling for the safety margin — never above 1.50.
    """

    safety_margin: float = _DEFAULT_SAFETY_MARGIN
    min_margin: float = _MIN_SAFETY_MARGIN
    max_margin: float = _MAX_SAFETY_MARGIN

    # Margin correction tracking (non-dataclass, mutable)
    _days_below_floor: int = field(default=0, init=False, repr=False)
    _days_above_floor: int = field(default=0, init=False, repr=False)
    # Evidence for the local day being observed (issue #1141).
    _observed_day: date | None = field(default=None, init=False, repr=False)
    _day_evaluated: bool = field(default=False, init=False, repr=False)
    _day_shortfall: bool = field(default=False, init=False, repr=False)
    _day_well_above: bool = field(default=True, init=False, repr=False)
    # SoC and floor of the previous call — that floor is the one in force.
    _prev_soc_pct: float | None = field(default=None, init=False, repr=False)
    _prev_floor_pct: float | None = field(default=None, init=False, repr=False)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compute_floor(
        self,
        now: datetime,
        slots: list,  # list[PlannedSlot] — kept typing-free for pure-Python testability
        usable_kwh: float,
        configured_min_soc_pct: float,
        hours_ahead: int = 48,
        *,
        cycle_cost_per_kwh: float = 0.0,
        max_grid_charge_kw: float = 0.0,
    ) -> tuple[float, dict]:
        """Compute the effective discharge floor as SoC percentage.

        The scalar part of :meth:`compute_floor_profile`; see there for the
        algorithm and the arguments.

        Returns:
            ``(effective_floor_pct, diagnostics)``.
        """
        floor_pct, diag, _profile = self.compute_floor_profile(
            now,
            slots,
            usable_kwh,
            configured_min_soc_pct,
            hours_ahead,
            cycle_cost_per_kwh=cycle_cost_per_kwh,
            max_grid_charge_kw=max_grid_charge_kw,
        )
        return floor_pct, diag

    def compute_floor_profile(
        self,
        now: datetime,
        slots: list,  # list[PlannedSlot] — kept typing-free for pure-Python testability
        usable_kwh: float,
        configured_min_soc_pct: float,
        hours_ahead: int = 48,
        *,
        cycle_cost_per_kwh: float = 0.0,
        max_grid_charge_kw: float = 0.0,
    ) -> tuple[float, dict, list[tuple[datetime, float]]]:
        """Compute the discharge floor now and for every look-ahead slot.

        The reserve shrinks as the bridge to the refill gets shorter and is
        gone once the refill has happened (issue #1188), so next to the
        scalar floor this returns the floor at the start of each slot.

        Algorithm
        ---------
        1. Scan slots from *now* forward looking for the first refill slot.
        2. A refill slot is one of:
           - Solar surplus (net_consumption_kwh < 0)
           - Grid-charge planned AND the charged energy ≥ accumulated reserve
        3. Accumulate house consumption for every non-refill future slot.
        4. Subtract solar surplus (negative net) between now and the refill.
        5. Subtract grid-charge energy planned between now and the refill.
        6. If no planned grid charge covers the bridge, scan again and also
           credit every affordable slot (see :func:`cheap_refill_price`) with
           ``max_grid_charge_kw × hours``.  A covering credit ends the bridge
           as ``grid_available`` (issue #1156); otherwise the first scan
           stands.
        7. Reserve = net_consumption × safety_margin.
        8. Convert reserve to SoC pct and return max(configured_min, reserve).

        Args:
            now:
                Timezone-aware current datetime.
            slots:
                Future planner output slots (list of objects with
                ``start``, ``end``, ``estimated_net_consumption_kwh``,
                ``batteries_charged_kwh``, and ``recommendation`` attributes,
                and optionally ``import_price``; a slot without a finite
                price is never an affordable refill).
            usable_kwh:
                Maximum usable battery capacity (kWh).
            configured_min_soc_pct:
                User-configured minimum SoC for export (0-100).  This is the
                absolute floor — the dynamic floor can only be higher.
            hours_ahead:
                Look-ahead window in hours.  Defaults to 48.
            cycle_cost_per_kwh:
                Battery wear per kWh of throughput; the affordable-refill
                tolerance above the look-ahead's cheapest import price.
            max_grid_charge_kw:
                Battery charge power limit (kW).  ``0`` (the default)
                disables affordable refills.

        Returns:
            A ``(effective_floor_pct, diagnostics, profile)`` tuple where
            *effective_floor_pct* is the greater of *configured_min_soc_pct*
            and the computed reserve SoC, *diagnostics* is a dict with
            ``reserve_kwh``, ``bridge_duration_hours``, ``next_refill_slot``,
            ``safety_margin``, ``refill_type`` and ``cheap_refill_price``,
            and *profile* is ``(slot start, floor SoC %)`` for every
            look-ahead slot.  The first profile entry equals
            *effective_floor_pct*; entries from the refill slot on equal
            *configured_min_soc_pct*.  The profile is empty when there are no
            future slots.
        """
        # Default diagnostics when no slots or no refill is found.
        diag: dict = {
            "reserve_kwh": 0.0,
            "bridge_duration_hours": 0.0,
            "next_refill_slot": None,
            "safety_margin": self.safety_margin,
            "refill_type": "none",
            "cheap_refill_price": None,
        }

        if not slots:
            log_planner(
                "debug",
                "[dynamic_floor] No slots provided — using configured min %.1f%%",
                configured_min_soc_pct,
            )
            return configured_min_soc_pct, diag, []

        # Filter to future slots only, ordered chronologically, bounded to
        # the look-ahead window — low-confidence day+2/day+3 forecasts
        # beyond hours_ahead must not extend the bridge scan.
        horizon_end = now + timedelta(hours=hours_ahead)
        future = [s for s in slots if s.end > now and s.start < horizon_end]
        if not future:
            log_planner(
                "debug",
                "[dynamic_floor] No future slots — using configured min %.1f%%",
                configured_min_soc_pct,
            )
            return configured_min_soc_pct, diag, []

        # Scan forward to the first refill.  The reference plan's own grid
        # charges come first; only if they do not cover the bridge does a
        # second pass also credit affordable slots it does not charge in
        # (issue #1156), so a covering planned charge keeps its refill.
        scan = _scan_bridge(future, None, 0.0)
        cheap_price = cheap_refill_price(
            (getattr(s, "import_price", math.nan) for s in future),
            cycle_cost_per_kwh,
        )
        if (
            scan.refill_type != "grid_charge"
            and cheap_price is not None
            and max_grid_charge_kw > 1e-9
        ):
            cheap_scan = _scan_bridge(future, cheap_price, max_grid_charge_kw)
            if cheap_scan.refill_type == "grid_available":
                scan = cheap_scan
        refill_slot = scan.refill_slot
        refill_type = scan.refill_type
        bridge_duration_hours = scan.duration_hours

        # Compute reserve: net consumption minus solar contribution and grid charge.
        reserve_kwh = scan.consumption_kwh - scan.solar_kwh - scan.grid_charge_kwh
        reserve_kwh = max(reserve_kwh, 0.0)

        # Convert reserve to SoC percentage.
        if usable_kwh > 1e-9:
            reserve_soc_pct = (reserve_kwh / usable_kwh) * 100.0 * self.safety_margin
        else:
            reserve_soc_pct = 0.0

        effective_floor_pct = max(configured_min_soc_pct, reserve_soc_pct)

        diag = {
            "reserve_kwh": round(reserve_kwh, 3),
            "bridge_duration_hours": round(bridge_duration_hours, 2),
            "next_refill_slot": refill_slot.start.isoformat() if refill_slot else None,
            "safety_margin": self.safety_margin,
            "refill_type": refill_type,
            "cheap_refill_price": (
                round(cheap_price, 5) if cheap_price is not None else None
            ),
        }

        log_planner(
            "debug",
            "[dynamic_floor] compute_floor: reserve=%.3f kWh  bridge=%.1f h  "
            "refill=%s(%s)  cheap_refill_price=%s  margin=%.2f  raw_soc=%.1f%%  "
            "effective=%.1f%%  configured_min=%.1f%%  usable=%.3f",
            reserve_kwh,
            bridge_duration_hours,
            refill_type,
            diag["next_refill_slot"] or "none",
            diag["cheap_refill_price"],
            self.safety_margin,
            reserve_soc_pct,
            effective_floor_pct,
            configured_min_soc_pct,
            usable_kwh,
        )

        profile = _floor_profile(
            future, scan, usable_kwh, configured_min_soc_pct, self.safety_margin
        )
        return effective_floor_pct, diag, profile

    def correct_margin(
        self, actual_soc_pct: float, floor_pct: float, *, now: datetime
    ) -> None:
        """Record one cycle's SoC-vs-floor evidence and learn once per day.

        Called every coordinator cycle.  Each call is judged against the floor
        *in force* since the previous call and filed under the local day of
        *now*:

        - **Shortfall:** the battery was at or above that floor and is now more
          than 1 SoC point below it, so the reserve did not hold.
        - **Well above:** the SoC is above that floor × 1.3.
        - A floor the battery was already below is no evidence either way.
          The planner caps it at the live SoC (issue #1094), and missing a
          floor the battery never reached says nothing about the margin.

        A day is classified on the first call of a later day (issue #1141):
        any shortfall makes it a *below* day; a day whose every evaluated call
        was well above is an *above* day; any other day resets both counters,
        and so does a gap between observed days.  Two consecutive below days
        raise the margin by 0.05; seven consecutive above days lower it by
        0.02.  The margin therefore changes at most once per day, however
        often this is called.  It lives in memory only, so a restart resets
        it to 1.15.

        Args:
            actual_soc_pct:
                Current actual battery SoC as a percentage (0-100).
            floor_pct:
                The floor :meth:`compute_floor` just returned, uncapped.  It
                is the floor in force for the next call.
            now:
                Timezone-aware local datetime; its date files the evidence.
        """
        today = now.date()
        if self._observed_day is not None and today != self._observed_day:
            self._close_day(today)
        if self._observed_day != today:
            self._observed_day = today
            self._day_evaluated = False
            self._day_shortfall = False
            self._day_well_above = True

        prev_soc, prev_floor = self._prev_soc_pct, self._prev_floor_pct
        self._prev_soc_pct, self._prev_floor_pct = actual_soc_pct, floor_pct
        if prev_soc is None or prev_floor is None:
            return
        if prev_soc < prev_floor - 1e-9:
            # The battery never reached this floor — not a margin signal.
            self._day_well_above = False
            return
        self._day_evaluated = True
        if actual_soc_pct < prev_floor - _SHORTFALL_TOLERANCE_PCT:
            self._day_shortfall = True
            log_planner(
                "debug",
                "[dynamic_floor] SoC %.1f%% fell below floor %.1f%% — %s is a "
                "below-floor day",
                actual_soc_pct,
                prev_floor,
                today,
            )
        if actual_soc_pct <= prev_floor * _WELL_ABOVE_FACTOR:
            self._day_well_above = False

    def _close_day(self, today: date) -> None:
        """Classify the observed day and apply the margin triggers (issue #1141).

        Args:
            today: The local date of the call that ended the observed day.
        """
        day = self._observed_day
        if self._day_shortfall:
            verdict = "below"
            self._days_below_floor += 1
            self._days_above_floor = 0
        elif self._day_evaluated and self._day_well_above:
            verdict = "well above"
            self._days_above_floor += 1
            self._days_below_floor = 0
        else:
            verdict = "neutral"
            self._days_below_floor = 0
            self._days_above_floor = 0
        log_planner(
            "debug",
            "[dynamic_floor] day %s closed: %s — below-floor days: %d, "
            "above-floor days: %d",
            day,
            verdict,
            self._days_below_floor,
            self._days_above_floor,
        )

        old_margin = self.safety_margin
        if self._days_below_floor >= _DAYS_BELOW_FLOOR_TRIGGER:
            self.safety_margin = min(
                self.max_margin, self.safety_margin + _MARGIN_INCREASE
            )
            self._days_below_floor = 0
            log_planner(
                "info",
                "[dynamic_floor] Increasing safety margin from %.2f to %.2f "
                "(SoC dropped below the floor on %d consecutive days)",
                old_margin,
                self.safety_margin,
                _DAYS_BELOW_FLOOR_TRIGGER,
            )
        elif self._days_above_floor >= _DAYS_ABOVE_FLOOR_TRIGGER:
            self.safety_margin = max(
                self.min_margin, self.safety_margin - _MARGIN_DECREASE
            )
            self._days_above_floor = 0
            log_planner(
                "info",
                "[dynamic_floor] Decreasing safety margin from %.2f to %.2f "
                "(SoC stayed well above the floor on %d consecutive days)",
                old_margin,
                self.safety_margin,
                _DAYS_ABOVE_FLOOR_TRIGGER,
            )

        if day is not None and (today - day).days != 1:
            # Days are only consecutive when observed back to back.
            self._days_below_floor = 0
            self._days_above_floor = 0
