"""Dynamic self-learning discharge floor (issue #600).

Computes the reserve SoC needed to run the house until the next energy refill
(solar surplus or planned grid-charge), with a self-correcting safety margin.

Usage
-----
Instantiate once per entry and call :meth:`DynamicDischargeFloor.compute_floor`
after each planner run.  Call :meth:`DynamicDischargeFloor.correct_margin` with
the actual SoC to let the safety margin self-correct over time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from custom_components.hsem.utils.logger import HSEM_LOGGER as _LOGGER

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


# ---------------------------------------------------------------------------
# DynamicDischargeFloor
# ---------------------------------------------------------------------------


@dataclass
class DynamicDischargeFloor:
    """Computes a dynamic discharge floor based on bridge-to-refill energy.

    Scans future planner output slots to find the next energy *refill* slot
    (solar surplus or planned grid-charge), sums the house consumption between
    now and that refill, and applies a self-learning safety margin.

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
    _last_floor_pct: float | None = field(default=None, init=False, repr=False)
    _days_below_floor: int = field(default=0, init=False, repr=False)
    _days_above_floor: int = field(default=0, init=False, repr=False)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compute_floor(
        self,
        now: datetime,
        slots: list,  # list[PlannedSlot] — kept typing-free for pure-Python testability
        current_kwh: float,
        usable_kwh: float,
        configured_min_soc_pct: float,
        hours_ahead: int = 48,
    ) -> tuple[float, dict]:
        """Compute the effective discharge floor as SoC percentage.

        Algorithm
        ---------
        1. Scan slots from *now* forward looking for the first refill slot.
        2. A refill slot is one of:
           - Solar surplus (net_consumption_kwh < 0)
           - Grid-charge planned AND the charged energy ≥ accumulated reserve
        3. Accumulate house consumption for every non-refill future slot.
        4. Subtract solar surplus (negative net) between now and the refill.
        5. Subtract grid-charge energy planned between now and the refill.
        6. Reserve = net_consumption × safety_margin.
        7. Convert reserve to SoC pct and return max(configured_min, reserve).

        Args:
            now:
                Timezone-aware current datetime.
            slots:
                Future planner output slots (list of objects with
                ``start``, ``end``, ``estimated_net_consumption_kwh``,
                ``batteries_charged_kwh``, and ``recommendation`` attributes).
            current_kwh:
                Current usable battery energy above the absolute floor (kWh).
            usable_kwh:
                Maximum usable battery capacity (kWh).
            configured_min_soc_pct:
                User-configured minimum SoC for export (0-100).  This is the
                absolute floor — the dynamic floor can only be higher.
            hours_ahead:
                Look-ahead window in hours.  Defaults to 48.

        Returns:
            A ``(effective_floor_pct, diagnostics)`` tuple where
            *effective_floor_pct* is the greater of *configured_min_soc_pct*
            and the computed reserve SoC, and *diagnostics* is a dict with
            ``reserve_kwh``, ``bridge_duration_hours``, ``next_refill_slot``,
            and ``safety_margin``.
        """
        # Default diagnostics when no slots or no refill is found.
        diag: dict = {
            "reserve_kwh": 0.0,
            "bridge_duration_hours": 0.0,
            "next_refill_slot": None,
            "safety_margin": self.safety_margin,
            "refill_type": "none",
        }

        if not slots:
            _LOGGER.debug(
                "[dynamic_floor] No slots provided — using configured min %.1f%%",
                configured_min_soc_pct,
            )
            return configured_min_soc_pct, diag

        # Filter to future slots only, ordered chronologically.
        future = [s for s in slots if s.end > now]
        if not future:
            _LOGGER.debug(
                "[dynamic_floor] No future slots — using configured min %.1f%%",
                configured_min_soc_pct,
            )
            return configured_min_soc_pct, diag

        # Scan forward to find refill and accumulate consumption.
        accumulated_consumption = 0.0
        accumulated_solar = 0.0
        accumulated_grid_charge = 0.0
        refill_slot = None
        refill_type = "none"
        bridge_duration_hours = 0.0

        for s in future:
            slot_hours = (s.end - s.start).total_seconds() / 3600.0

            net = getattr(s, "estimated_net_consumption_kwh", 0.0) or 0.0

            # Check for solar surplus refill.
            if net < -1e-9:
                refill_slot = s
                refill_type = "solar_surplus"
                break

            # Check for grid-charge refill.
            # A grid-charge slot is one where batteries_charged_kwh > 0 and the
            # recommendation indicates grid charging.
            charged = getattr(s, "batteries_charged_kwh", 0.0) or 0.0
            rec = getattr(s, "recommendation", None)
            if charged > 1e-9 and rec == "batteries_charge_grid":
                # This is a planned grid charge slot — credit the energy delivered.
                accumulated_grid_charge += charged
                # Check if the accumulated grid charge covers the reserve need.
                # The reserve need is accumulated_consumption - accumulated_solar.
                reserve_need = max(accumulated_consumption - accumulated_solar, 0.0)
                if accumulated_grid_charge >= reserve_need:
                    refill_slot = s
                    refill_type = "grid_charge"
                    break
                # If it doesn't cover the full reserve yet, continue scanning.
                # The grid charge energy will be counted in the final
                # reserve calculation (subtracted from consumption).
                bridge_duration_hours += slot_hours
                continue

            # Regular consumption slot.
            if net > 1e-9:
                accumulated_consumption += net
            elif net < -1e-9:
                # Solar surplus that we didn't catch above (shouldn't happen due to
                # the break above, but be safe).
                accumulated_solar += abs(net)

            bridge_duration_hours += slot_hours

        # Compute reserve: net consumption minus solar contribution and grid charge.
        reserve_kwh = (
            accumulated_consumption - accumulated_solar - accumulated_grid_charge
        )
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
        }

        _LOGGER.debug(
            "[dynamic_floor] compute_floor: reserve=%.3f kWh  bridge=%.1f h  "
            "refill=%s(%s)  margin=%.2f  raw_soc=%.1f%%  effective=%.1f%%  "
            "configured_min=%.1f%%  usable=%.3f",
            reserve_kwh,
            bridge_duration_hours,
            refill_type,
            diag["next_refill_slot"] or "none",
            self.safety_margin,
            reserve_soc_pct,
            effective_floor_pct,
            configured_min_soc_pct,
            usable_kwh,
        )

        # Store the computed floor for later margin correction.
        self._last_floor_pct = effective_floor_pct

        return effective_floor_pct, diag

    def correct_margin(self, actual_soc_pct: float, floor_pct: float) -> None:
        """Self-correct the safety margin based on whether the reserve was sufficient.

        Called once per coordinator cycle (or at midnight).  Compares the actual
        SoC against the previously computed floor:

        - If actual SoC < floor: increment ``_days_below_floor``.
          After 2 consecutive days below floor, increase margin by 0.05.
        - If actual SoC > floor × 1.3: increment ``_days_above_floor``.
          After 7 consecutive days well above floor, decrease margin by 0.02.

        Args:
            actual_soc_pct:
                Current actual battery SoC as a percentage (0-100).
            floor_pct:
                The dynamic floor computed earlier today.  Used as the
                comparison baseline.
        """
        if actual_soc_pct < floor_pct:
            self._days_below_floor += 1
            self._days_above_floor = 0
            _LOGGER.debug(
                "[dynamic_floor] SoC %.1f%% < floor %.1f%% — below-floor days: %d",
                actual_soc_pct,
                floor_pct,
                self._days_below_floor,
            )
            if self._days_below_floor >= _DAYS_BELOW_FLOOR_TRIGGER:
                old_margin = self.safety_margin
                self.safety_margin = min(
                    self.max_margin, self.safety_margin + _MARGIN_INCREASE
                )
                self._days_below_floor = 0
                _LOGGER.info(
                    "[dynamic_floor] Increasing safety margin from %.2f to %.2f "
                    "(SoC %.1f%% dropped below floor %.1f%% for %d days)",
                    old_margin,
                    self.safety_margin,
                    actual_soc_pct,
                    floor_pct,
                    _DAYS_BELOW_FLOOR_TRIGGER,
                )
        elif actual_soc_pct > floor_pct * _WELL_ABOVE_FACTOR:
            self._days_above_floor += 1
            self._days_below_floor = 0
            _LOGGER.debug(
                "[dynamic_floor] SoC %.1f%% > floor %.1f%% × %.1f — above-floor days: %d",
                actual_soc_pct,
                floor_pct,
                _WELL_ABOVE_FACTOR,
                self._days_above_floor,
            )
            if self._days_above_floor >= _DAYS_ABOVE_FLOOR_TRIGGER:
                old_margin = self.safety_margin
                self.safety_margin = max(
                    self.min_margin, self.safety_margin - _MARGIN_DECREASE
                )
                self._days_above_floor = 0
                _LOGGER.info(
                    "[dynamic_floor] Decreasing safety margin from %.2f to %.2f "
                    "(SoC %.1f%% stayed above floor %.1f%% for %d days)",
                    old_margin,
                    self.safety_margin,
                    actual_soc_pct,
                    floor_pct,
                    _DAYS_ABOVE_FLOOR_TRIGGER,
                )
        else:
            # SoC is between floor and floor × 1.3 — steady state, reset counters.
            self._days_below_floor = 0
            self._days_above_floor = 0
