"""Dynamic self-learning discharge floor (issue #600).

Computes the reserve SoC needed to run the house until the next energy refill
(solar surplus, planned grid-charge, or an affordable grid refill), with a
self-correcting safety margin.

The stateless bridge arithmetic (what counts as a refill, how much energy the
bridge needs, the per-slot reserve) lives in
:mod:`custom_components.hsem.utils.dynamic_floor_bridge`.

Usage
-----
Instantiate once per entry and call :meth:`DynamicDischargeFloor.compute_floor`
after each planner run.  Call :meth:`DynamicDischargeFloor.correct_margin` every
cycle with the actual SoC; it files the evidence under the local day and lets
the safety margin self-correct at most once per day (issue #1141).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from custom_components.hsem.utils.dynamic_floor_bridge import (
    _cheap_window_slots,
    _cycle_cost_tolerance,
    _floor_profile,
    _planned_grid_charges,
    _scan_bridge,
    cheap_refill_price,
)
from custom_components.hsem.utils.logger import log_planner
from custom_components.hsem.utils.soc_bounds import reserve_floor_pct

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
        max_soc_pct: float = 100.0,
        export_buffer_kwh: float = 0.0,
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
            max_soc_pct=max_soc_pct,
            export_buffer_kwh=export_buffer_kwh,
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
        max_soc_pct: float = 100.0,
        export_buffer_kwh: float = 0.0,
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
           - The first slot a planned grid charge is credited to
           - The first slot of a cheap window: priced within one cycle
             cost of the cheapest price from there to the surplus, with
             dearer slots behind it (issue #1247, :func:`_cheap_window_slots`)
        3. Accumulate house consumption for every slot before the refill.
        4. That consumption is the reserve, whatever the size of the planned
           charge (issues #1214, #1220).
        5. If a reserve is left, scan again and
           credit every affordable slot (see :func:`cheap_refill_price`) with
           ``max_grid_charge_kw × hours``.  A covering credit ends the bridge
           as ``grid_available`` (issue #1156); otherwise the first scan
           stands.
        6. Reserve = net_consumption × safety_margin.
        7. The floor holds that reserve on top of the configured minimum SoC
           (:func:`reserve_floor_pct`, issue #1221), capped at *max_soc_pct*.

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
                Usable battery capacity: what the battery holds between
                *configured_min_soc_pct* and *max_soc_pct* (kWh).
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
            max_soc_pct:
                Configured maximum SoC (0-100); the floor never exceeds it.
            export_buffer_kwh:
                The MILP export reserve's checkpoint buffer (kWh); a planned
                charge that only buys it back after a battery export is no
                refill (issue #1239).  ``0`` (the default) credits every
                planned charge.

        Returns:
            A ``(effective_floor_pct, diagnostics, profile)`` tuple where
            *effective_floor_pct* is *configured_min_soc_pct* plus the SoC
            share of the reserve, *diagnostics* is a dict with
            ``reserve_kwh``, ``bridge_duration_hours``, ``next_refill_slot``,
            ``safety_margin``, ``refill_type`` and ``cheap_refill_price``,
            and *profile* is ``(slot start, floor SoC %)`` for every
            look-ahead slot.  The first profile entry, capped at
            *max_soc_pct*, equals *effective_floor_pct*; entries from the
            refill slot on equal *configured_min_soc_pct*.  The profile is
            empty when there are no future slots.
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
        # charges come first; when they leave a reserve, a second pass
        # credits affordable slots (issue #1156), which can only release it.
        planned = _planned_grid_charges(
            future, _cycle_cost_tolerance(cycle_cost_per_kwh), export_buffer_kwh
        )
        scan = _scan_bridge(
            future,
            planned,
            None,
            0.0,
            cheap_window=(
                _cheap_window_slots(future, cycle_cost_per_kwh)
                if max_grid_charge_kw > 1e-9
                else frozenset()
            ),
        )
        cheap_price = cheap_refill_price(
            (getattr(s, "import_price", math.nan) for s in future),
            cycle_cost_per_kwh,
        )
        if (
            (scan.reserve_kwh > 1e-9 or scan.refill_type == "cheap_window")
            and cheap_price is not None
            and max_grid_charge_kw > 1e-9
        ):
            cheap_scan = _scan_bridge(future, planned, cheap_price, max_grid_charge_kw)
            if cheap_scan.refill_type == "grid_available":
                scan = cheap_scan
        refill_slot = scan.refill_slot
        refill_type = scan.refill_type
        bridge_duration_hours = scan.duration_hours

        # The consumption bridged to the refill; 0 when the refill covers it.
        reserve_kwh = scan.reserve_kwh

        # Hold the reserve on top of the configured minimum (issue #1221).
        effective_floor_pct = reserve_floor_pct(
            reserve_kwh * self.safety_margin,
            usable_kwh,
            configured_min_soc_pct,
            max_soc_pct,
        )

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
            "refill=%s(%s)  cheap_refill_price=%s  margin=%.2f  "
            "effective=%.1f%%  configured_min=%.1f%%  max=%.1f%%  usable=%.3f",
            reserve_kwh,
            bridge_duration_hours,
            refill_type,
            diag["next_refill_slot"] or "none",
            diag["cheap_refill_price"],
            self.safety_margin,
            effective_floor_pct,
            configured_min_soc_pct,
            max_soc_pct,
            usable_kwh,
        )

        profile = _floor_profile(
            future,
            scan,
            usable_kwh,
            configured_min_soc_pct,
            max_soc_pct,
            self.safety_margin,
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
