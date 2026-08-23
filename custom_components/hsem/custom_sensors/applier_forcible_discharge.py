"""Forcible-discharge write sequence.

Extracted from ``applier.py`` to satisfy the repository's 30 KB /
1000-line file limit. Pure move: no behaviour change.
"""

from __future__ import annotations

from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

from custom_components.hsem.custom_sensors.applier_caps import (
    _configured_battery_device_ids,
)
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.huawei import (
    async_set_forcible_discharge,
)
from custom_components.hsem.utils.inverter_verify import (
    ApplyResult,
    ApplyStatus,
    async_write_and_verify,
)
from custom_components.hsem.utils.logger import HSEM_LOGGER as _LOGGER

# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


async def _async_apply_forcible_discharge(
    sensor: Any,  # NOSONAR -- HA internal type; circular import risk
    cfg: SensorConfig,
    live: LiveState,
    current_required_kwh: float,
    max_discharge_power: int,
) -> list[ApplyResult]:
    """Issue a forcible-discharge command to the battery pack and verify acceptance.

    Returns:
        List of :class:`ApplyResult` entries (one per configured battery device).
        Returns an empty list if preconditions are not met and no write is attempted.
    """
    battery_device_ids = _configured_battery_device_ids(cfg)
    if (
        live.battery_usable_capacity_kwh <= 0
        or current_required_kwh < 0
        or not battery_device_ids
    ):
        return []

    target_soc = int(
        live.huawei_batteries_end_of_discharge_soc_pct
    )  # discharge to floor
    target_soc = max(5, min(100, target_soc))  # clamp 5-100 for safety

    bat_fc_entity = cfg.huawei_solar_batteries_forcible_charge

    def _read_fc_accepted() -> float | None:
        """Return 1.0 if forcible charge state is active (not stopped/empty),
        None otherwise.  The forcible_charge sensor reports a string like
        'Discharging at 5000W until 5.0%' when active, or 'Stopped' when idle."""
        if not bat_fc_entity:
            return None
        state = sensor.hass.states.get(bat_fc_entity)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE, ""):
            return None
        if state.state.lower() == "stopped":
            return None
        return 1.0

    results: list[ApplyResult] = []
    for device_id in battery_device_ids:

        async def _write_fc(_dev: str = device_id) -> None:
            await async_set_forcible_discharge(
                sensor,
                _dev,
                target_soc,
                max_discharge_power,
            )

        result = await async_write_and_verify(
            entity_id=(bat_fc_entity or "forcible_charge") + f":{device_id}",
            desired=1.0,
            writer=_write_fc,
            reader=_read_fc_accepted,
            # The forcible_charge sensor changes state immediately when the
            # command is accepted — no need for wide tolerance or retries.
            tolerance=0.0,
            max_retries=3,
        )
        results.append(result)
        _LOGGER.debug(
            "Excess battery export: Set forcible discharge for device %s to %d%% "
            "SOC at %dW power. Verify result: %s",
            device_id,
            target_soc,
            max_discharge_power,
            result.status.value,
        )
        if result.status == ApplyStatus.FAILED:
            break
    return results
