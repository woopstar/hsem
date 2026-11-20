"""Recorded days for the what-if tests (issue #1432).

Each day is what plain self-consumption would have left on the meters, so its
grid flows, SoC and PV are consistent with each other and with the site's
limits.
"""

from __future__ import annotations

import math
import random
from datetime import UTC, date, datetime, timedelta

from custom_components.hsem.models.what_if_history import RecordedDay
from custom_components.hsem.planner.hindsight_oracle import (
    HindsightSlot,
    simulate_self_consumption,
)
from custom_components.hsem.planner.hindsight_scoring import SiteLimits

SITE = SiteLimits(
    rated_kwh=10.0,
    min_soc_pct=5.0,
    max_soc_pct=100.0,
    max_charge_kw=5.0,
    max_discharge_kw=5.0,
    charge_efficiency=0.97,
    discharge_efficiency=0.97,
)

FIRST_DAY = date(2026, 6, 1)


def make_day(
    day: date,
    *,
    seed: int = 1,
    minutes: int = 60,
    start_soc_pct: float = 40.0,
    site: SiteLimits = SITE,
) -> RecordedDay:
    """Return one recorded day: evening peak, midday PV, cheap night.

    Args:
        day: The local day.
        seed: Seeds the load and price noise.
        minutes: Slot width.
        start_soc_pct: SoC the day starts with.
        site: The battery the day was recorded with.
    """
    rng = random.Random(seed)
    hours = minutes / 60.0
    slots: list[HindsightSlot] = []
    pv: list[float] = []
    for index in range(24 * 60 // minutes):
        clock = index * hours
        load = (0.3 + 0.4 * rng.random() + (1.6 if 17 <= clock < 21 else 0.0)) * hours
        sun = 5.0 * math.sin(math.pi * (clock - 6) / 12) if 6 <= clock <= 18 else 0.0
        price = (
            1.2 + 0.9 * math.sin(2 * math.pi * (clock - 13) / 24) + 0.1 * rng.random()
        )
        slots.append(HindsightSlot(hours, load - sun * hours, price, 0.4 * price))
        pv.append(sun * hours)
    run = simulate_self_consumption(
        slots, site.battery(), site.rated_kwh * start_soc_pct / 100.0
    )
    return RecordedDay(
        day=day.isoformat(),
        start=datetime(day.year, day.month, day.day, tzinfo=UTC),
        slot_minutes=minutes,
        soc_pct=(
            start_soc_pct,
            *(stored / site.rated_kwh * 100.0 for stored in run.stored_kwh),
        ),
        grid_import_kwh=run.grid_import_kwh,
        grid_export_kwh=run.grid_export_kwh,
        import_price=tuple(slot.import_price for slot in slots),
        export_price=tuple(slot.export_price for slot in slots),
        pv_kwh=tuple(pv),
    )


def make_days(count: int, *, minutes: int = 60) -> dict[str, RecordedDay]:
    """Return *count* consecutive recorded days from :data:`FIRST_DAY`."""
    days = [
        make_day(FIRST_DAY + timedelta(days=offset), seed=offset, minutes=minutes)
        for offset in range(count)
    ]
    return {day.day: day for day in days}
