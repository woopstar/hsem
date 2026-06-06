"""Hourly data populator package for HSEMWorkingModeSensor.

Single responsibility: populate a list of :class:`HourlyRecommendation` slots
with prices, Solcast PV estimates, and weighted house-consumption averages.

Sub-modules:
    prices_solcast — Price and Solcast PV population (async + snapshot)
    consumption   — House consumption average population (async + snapshot)
"""

from __future__ import annotations

from typing import Any

from custom_components.hsem.utils.ha_helpers import (
    async_resolve_entity_id_from_unique_id,
)

# ---------------------------------------------------------------------------
# Shared helper — used by both sub-modules
# ---------------------------------------------------------------------------


async def _resolve_cached(
    sensor: Any,  # NOSONAR -- HA internal type; circular import risk
    cache: dict[str, str],
    unique_id: str,
) -> str | None:
    """Return the entity_id for ``unique_id``, resolving and caching if needed."""
    if unique_id not in cache:
        entity_id = await async_resolve_entity_id_from_unique_id(sensor, unique_id)
        if entity_id is not None:
            cache[unique_id] = entity_id
    return cache.get(unique_id)


# ---------------------------------------------------------------------------
# Re-export all public functions from sub-modules
# ---------------------------------------------------------------------------

from .consumption import (  # noqa: E402
    async_populate_avg_house_consumption,
    populate_avg_house_consumption_from_snapshot,
)
from .prices_solcast import (  # noqa: E402
    async_populate_price_and_solcast,
    populate_price_and_solcast_from_snapshot,
)

__all__ = [
    "async_populate_avg_house_consumption",
    "async_populate_price_and_solcast",
    "populate_avg_house_consumption_from_snapshot",
    "populate_price_and_solcast_from_snapshot",
]
