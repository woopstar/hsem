"""House load derived from the Huawei inverters and power meter (issue #1310).

Many Huawei installations have no house consumption sensor: only EMMA and
SDongle devices expose a load power.  A plain inverter + power meter setup has
the inverter's active power and the meter's active power, and the house load
is

    house load = Σ inverter active power − power meter active power

because the Huawei power meter reports export as positive.  While the battery
charges from the grid the inverter's active power is negative and the meter's
is more negative, so the difference stays the house's own draw.

HSEM publishes this as ``sensor.hsem_house_load_power`` and uses it as the
house consumption power when no working house sensor is configured.  A
configured sensor that exists always wins, so nothing changes for
installations that already have one.

Not seen by the formula: generation that does not pass through a Huawei
inverter (for example AC-coupled micro-inverters behind the meter).  An EV
charger behind the meter is included, which
``hsem_house_power_includes_ev_charger_power`` already describes.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from typing import Any

from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.misc import get_config_value, optional_entity_id
from custom_components.hsem.utils.sensornames.energy import (
    get_house_load_power_sensor_entity_id,
)

#: Config keys of the inverters' active power sensors, inverter 1 first.
HOUSE_LOAD_INVERTER_KEYS: tuple[str, ...] = (
    "hsem_huawei_solar_inverter_active_power",
    "hsem_huawei_solar_inverter_2_active_power",
)
#: Config key of the power meter's active power sensor.
HOUSE_LOAD_METER_KEY = "hsem_huawei_solar_power_meter_active_power"


def house_load_inputs(
    inverter_active_power: str | None,
    inverter_2_active_power: str | None,
    power_meter_active_power: str | None,
) -> tuple[list[str], str] | None:
    """Return the derived sensor's inputs, or ``None`` when it cannot be built.

    The first inverter and the power meter are required; the second inverter
    is added when configured.

    Args:
        inverter_active_power: Entity ID of inverter 1's active power.
        inverter_2_active_power: Entity ID of inverter 2's active power.
        power_meter_active_power: Entity ID of the power meter's active power.

    Returns:
        ``(inverter entity IDs, meter entity ID)``, or ``None``.
    """
    if not inverter_active_power or not power_meter_active_power:
        return None
    inverters = [inverter_active_power]
    if inverter_2_active_power:
        inverters.append(inverter_2_active_power)
    return inverters, power_meter_active_power


def derived_house_load_w(
    inverters_w: Sequence[float | None], meter_w: float | None
) -> float | None:
    """Return the house load in W, or ``None`` when any input is missing.

    A missing or non-finite input makes the result unknown, never 0: a 0 W
    house load would be learned as real consumption.

    Args:
        inverters_w: Each inverter's active power in W (positive = producing).
        meter_w: The power meter's active power in W (positive = exporting).

    Returns:
        ``Σ inverters_w − meter_w``, or ``None``.
    """
    values = [*inverters_w, meter_w]
    if not inverters_w or any(v is None or not math.isfinite(v) for v in values):
        return None
    return sum(v for v in inverters_w if v is not None) - (meter_w or 0.0)


def effective_house_power_entity(
    configured: str | None,
    derived_configured: bool,
    entity_exists: Callable[[str], bool],
) -> str | None:
    """Return the entity HSEM reads as the house consumption power.

    A configured sensor that exists is always used.  When it is empty or
    points to a missing entity and the derived sensor's inputs are set, the
    derived ``sensor.hsem_house_load_power`` is used instead.  Otherwise the
    configured value is returned unchanged, so a missing sensor is still
    reported as missing.

    Args:
        configured: The ``hsem_house_consumption_power`` entity ID, if any.
        derived_configured: Whether the derived sensor's inputs are set.
        entity_exists: Returns whether an entity ID exists in Home Assistant.

    Returns:
        The entity ID to read, or ``None``.
    """
    if configured and entity_exists(configured):
        return configured
    if derived_configured:
        return get_house_load_power_sensor_entity_id()
    return configured or None


def cfg_house_load_inputs(cfg: SensorConfig) -> tuple[list[str], str] | None:
    """Return the derived sensor's inputs from *cfg* (see :func:`house_load_inputs`)."""
    return house_load_inputs(
        cfg.huawei_solar_inverter_active_power,
        cfg.huawei_solar_inverter_2_active_power,
        cfg.huawei_solar_power_meter_active_power,
    )


def resolve_house_power(hass: Any, cfg: SensorConfig) -> None:
    """Point ``cfg.house_consumption_power`` at the entity HSEM should read.

    Applies :func:`effective_house_power_entity` with Home Assistant's state
    machine, once per update cycle, so every reader of the house power (the
    state snapshot, the live power timer, the missing-entity checks) sees the
    same entity.

    Args:
        hass: The Home Assistant instance.
        cfg: The cycle's sensor configuration; updated in place.
    """
    cfg.house_consumption_power = effective_house_power_entity(
        cfg.house_consumption_power,
        cfg_house_load_inputs(cfg) is not None,
        lambda entity_id: hass.states.get(entity_id) is not None,
    )


def entry_house_power_entity(hass: Any, config_entry: Any) -> str | None:
    """Return the house power entity to read for *config_entry*.

    The per-hour consumption sensors read the config entry directly rather
    than the coordinator's :class:`SensorConfig`; this applies the same
    choice as :func:`resolve_house_power`.

    Args:
        hass: The Home Assistant instance.
        config_entry: The HSEM config entry.

    Returns:
        The entity ID to read, or ``None``.
    """
    inputs = house_load_inputs(
        *(
            optional_entity_id(get_config_value(config_entry, key))
            for key in (*HOUSE_LOAD_INVERTER_KEYS, HOUSE_LOAD_METER_KEY)
        )
    )
    return effective_house_power_entity(
        optional_entity_id(
            get_config_value(config_entry, "hsem_house_consumption_power")
        ),
        inputs is not None,
        lambda entity_id: hass.states.get(entity_id) is not None,
    )
