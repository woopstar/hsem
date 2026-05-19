"""Immutable snapshot of all Home Assistant states collected once per cycle.

The coordinator collects this at the start of each update cycle and passes it to
all downstream population functions, guaranteeing that every slot is populated
from the same frozen data.  No function that receives a :class:`StateSnapshot`
should ever call ``hass.states.get()`` or similar HA state lookups.

The snapshot carries **no** Home Assistant imports and can be constructed freely
in unit tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from custom_components.hsem.models.live_state import LiveState


@dataclass(frozen=True)
class StateSnapshot:
    """Immutable snapshot of all HA states collected once per update cycle.

    Attributes:
        live: Live entity states (battery, power, EV, working mode, etc.).
        energy_average_values: Mapping of entity_id → float value (kWh) for
            the 1d/3d/7d/14d average consumption sensors (24 hours × 4 periods
            = 96 entries).  Populated once; read by the hourly-data populator.
        sensor_attributes: Mapping of entity_id → dict of attributes for
            EDS (import/export price) and Solcast PV forecast sensors.
            Pre-read so that :func:`~custom_sensors.hourly_data_populator.async_populate_price_and_solcast`
            can populate slots without additional HA state lookups.
    """

    live: LiveState
    energy_average_values: dict[str, float] = field(default_factory=dict)
    sensor_attributes: dict[str, dict[str, Any]] = field(default_factory=dict)
