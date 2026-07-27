"""Opportunistic grid charging (A2/H28/H29)."""

from __future__ import annotations

from datetime import datetime

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.planner.charging._charge_helpers import (
    _already_planned_charge_kwh,
)
from custom_components.hsem.utils.datetime_utils import as_tz
from custom_components.hsem.utils.logger import log_planner
from custom_components.hsem.utils.recommendations import Recommendations

# ---------------------------------------------------------------------------
# Opportunistic grid charging (A2/H28/H29)
# ---------------------------------------------------------------------------


def apply_opportunistic_charge(
    slots: list[PlannedSlot],
    now: datetime,
    current_capacity: float,
    usable_capacity: float,
    max_charge_per_interval: float,
    depreciation_threshold: float,
    cycle_cost_per_kwh: float = 0.0,
) -> None:
    """Charge the battery opportunistically when import prices are very low.

    This is a *schedule-independent* charge pass: it runs even when no
    discharge window is configured.  It covers two cases:

    1. **Negative import price** — the grid pays the consumer.  Every
       negative-price future slot is eligible regardless of the battery level.
    2. **Below-(depreciation − cycle cost) import price** — import is cheap
       enough that charging is economically sound.  The effective ceiling is
       ``max(depreciation_threshold − cycle_cost_per_kwh, 0)`` so that battery
       wear *reduces* the eligible price window — the planner only charges
       opportunistically when the price is low enough to cover both
       depreciation and cycle wear.

    Slots already assigned (by schedule pre-charge or prior passes) are
    skipped.  Energy is limited to what the battery can still absorb.

    Args:
        slots: Mutable list of planned slots (modified in-place).
        now: Timezone-aware current datetime.
        current_capacity: Current available battery energy in kWh.
        usable_capacity: Maximum usable battery energy in kWh.
        max_charge_per_interval: Maximum energy chargeable per slot (kWh).
        depreciation_threshold: Price ceiling below which grid charging is
            considered economically justified (local currency / kWh).
            This is the depreciation threshold from
            :func:`~custom_components.hsem.utils.misc.calculate_recommended_threshold`.
        cycle_cost_per_kwh: Additional per-kWh wear cost *subtracted* from the
            depreciation threshold.  Only slots with import price below
            ``max(depreciation_threshold - cycle_cost_per_kwh, 0)`` are eligible.
            Defaults to 0.0 (backwards compatible).
    """
    log_planner(
        "debug",
        "[chg] apply_opportunistic_charge  current=%.3f  usable=%.3f  "
        "depr_threshold=%.4f  cycle_cost=%.4f",
        current_capacity,
        usable_capacity,
        depreciation_threshold,
        cycle_cost_per_kwh,
    )
    if max_charge_per_interval <= 0:
        return

    already_planned = _already_planned_charge_kwh(slots)
    remaining_capacity = max(usable_capacity - current_capacity - already_planned, 0.0)
    if remaining_capacity <= 0:
        log_planner(
            "debug",
            "[chg] apply_opportunistic_charge  skipped — no remaining capacity "
            "(already_planned=%.3f)",
            already_planned,
        )
        return

    charged = 0.0

    # Priority 1: negative import price — charge as much as possible
    for s in sorted(
        (
            slot
            for slot in slots
            if as_tz(slot.end, now.tzinfo) > now
            and slot.recommendation is None
            and slot.price.import_price < 0
        ),
        key=lambda x: (x.price.import_price, x.start),
    ):
        if charged >= remaining_capacity:
            break
        energy = min(max_charge_per_interval, remaining_capacity - charged)
        if energy > 0:
            s.recommendation = Recommendations.BatteriesChargeGrid.value
            s.batteries_charged_kwh = round(energy, 3)
            charged += energy

    # Priority 2: below-(depreciation − cycle cost) price
    # Cycle wear cost is subtracted from the depreciation threshold to make
    # the planner more conservative: the effective ceiling is reduced so that
    # only prices that are cheap enough to justify *both* the depreciation and
    # the wear cost qualify for opportunistic charging.
    effective_threshold = max(depreciation_threshold - cycle_cost_per_kwh, 0.0)
    if abs(effective_threshold) > 1e-9:
        for s in sorted(
            (
                slot
                for slot in slots
                if as_tz(slot.end, now.tzinfo) > now
                and slot.recommendation is None
                and 0 <= slot.price.import_price < effective_threshold
            ),
            key=lambda x: (x.price.import_price, x.start),
        ):
            if charged >= remaining_capacity:
                break
            energy = min(max_charge_per_interval, remaining_capacity - charged)
            if energy > 0:
                s.recommendation = Recommendations.BatteriesChargeGrid.value
                s.batteries_charged_kwh = round(energy, 3)
                charged += energy
