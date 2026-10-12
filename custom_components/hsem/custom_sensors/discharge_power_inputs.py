"""Live readings of the Huawei ``maximum_discharging_power`` number entity.

The entity carries two different values, and they must stay apart:

- its **state** is the limit HSEM last commanded. The applier lowers it for a
  hold, an EV or the wait-mode reserve, so it is not a capability (issue #592).
- its ``max`` **attribute** is the highest value the hardware accepts. Home
  Assistant rejects a write above it (issue #1291).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.conversion import convert_to_float
from custom_components.hsem.utils.ha_helpers import entity_attribute_float


def read_max_discharge_power(
    sensor: Any,  # NOSONAR -- HA internal type; circular import risk
    cfg: SensorConfig,
    state: LiveState,
    reader: Callable[..., Any],
) -> None:
    """Read the commanded discharge limit and the entity's own maximum.

    Args:
        sensor: The working-mode sensor, used for ``hass`` access.
        cfg: Current sensor configuration.
        state: Live state to fill. A missing commanded limit is recorded as a
            critical missing entity; a missing ``max`` attribute is not, since
            the capacity table still gives a limit.
        reader: Read closure ``(entity_id, "float", label=...) -> Any`` that
            returns ``None`` for an unconfigured or unreadable entity.
    """
    entity_id = cfg.huawei_solar_batteries_maximum_discharging_power
    max_discharge_w = convert_to_float(
        reader(entity_id, "float", label="max_discharging_power")
    )
    if max_discharge_w is None:
        state.add_missing_entity(
            "Critical: battery max discharge power returned None (unavailable/invalid)"
        )
    state.huawei_batteries_max_discharge_power_w = max_discharge_w
    state.huawei_batteries_max_discharge_power_limit_w = entity_attribute_float(
        sensor, entity_id, "max"
    )
