"""The ``what_if`` service (issue #1432).

Replays the recorded days with another battery and returns what the change
would have been worth.  Read-only: it never writes to the hardware and never
touches the active plan.

Split from :mod:`services` to keep that module under the file-size limit.
"""

from __future__ import annotations

import time
from typing import Any

from homeassistant.core import ServiceCall, ServiceResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.hsem.coordinator_what_if import (
    WhatIfBusyError,
    WhatIfUnavailableError,
    async_run_what_if,
)
from custom_components.hsem.models.what_if_history import MAX_RECORDED_DAYS
from custom_components.hsem.utils.datetime_utils import now as hsem_now
from custom_components.hsem.utils.logger import HSEM_LOGGER as _LOGGER
from custom_components.hsem.utils.schema import vol
from custom_components.hsem.utils.what_if import WhatIfScenario, capacity_range

SERVICE_WHAT_IF = "what_if"

_RANGE_FIELDS = ("capacity_start_kwh", "capacity_end_kwh", "capacity_step_kwh")

_CAPACITY = vol.All(vol.Coerce(float), vol.Range(min=0.1, max=10000.0))
_POWER = vol.All(vol.Coerce(float), vol.Range(min=1.0, max=1000000.0))
_EFFICIENCY = vol.All(vol.Coerce(float), vol.Range(min=1.0, max=100.0))
_PRICE = vol.All(vol.Coerce(float), vol.Range(min=0.0))

SCHEMA_WHAT_IF = vol.Schema(
    {
        vol.Optional("history_days", default=30): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=MAX_RECORDED_DAYS)
        ),
        vol.Optional("usable_capacity_kwh"): _CAPACITY,
        vol.Optional("max_charge_power_w"): _POWER,
        vol.Optional("max_discharge_power_w"): _POWER,
        vol.Optional("charge_efficiency_pct"): _EFFICIENCY,
        vol.Optional("discharge_efficiency_pct"): _EFFICIENCY,
        vol.Optional("cycle_cost_per_kwh"): _PRICE,
        vol.Optional("capacity_start_kwh"): _CAPACITY,
        vol.Optional("capacity_end_kwh"): _CAPACITY,
        vol.Optional("capacity_step_kwh"): _CAPACITY,
        vol.Optional("capacity_price_per_kwh"): _PRICE,
    }
)


def scenarios_from_call(data: dict[str, Any]) -> tuple[WhatIfScenario, ...]:
    """Return the scenarios a service call asks for.

    One scenario from the overrides, or one per size when a capacity range is
    given: each size then carries the other overrides too.

    Args:
        data: The validated service data.

    Raises:
        ServiceValidationError: When the call changes nothing, gives only part
            of a capacity range, gives a range next to a single capacity, or
            gives a range that is empty or too long.
    """

    def scaled(name: str, factor: float) -> float | None:
        return None if data.get(name) is None else data[name] * factor

    base = WhatIfScenario(
        usable_kwh=data.get("usable_capacity_kwh"),
        max_charge_kw=scaled("max_charge_power_w", 0.001),
        max_discharge_kw=scaled("max_discharge_power_w", 0.001),
        charge_efficiency=scaled("charge_efficiency_pct", 0.01),
        discharge_efficiency=scaled("discharge_efficiency_pct", 0.01),
        cycle_cost_per_kwh=data.get("cycle_cost_per_kwh"),
    )
    given = [name for name in _RANGE_FIELDS if name in data]
    if not given:
        if base.changes_nothing:
            raise ServiceValidationError(
                "HSEM what_if: give at least one battery override or a capacity range."
            )
        return (base,)
    if len(given) != len(_RANGE_FIELDS):
        raise ServiceValidationError(
            "HSEM what_if: a capacity range needs capacity_start_kwh, "
            "capacity_end_kwh and capacity_step_kwh."
        )
    if base.usable_kwh is not None:
        raise ServiceValidationError(
            "HSEM what_if: give usable_capacity_kwh or a capacity range, not both."
        )
    try:
        sizes = capacity_range(*(data[name] for name in _RANGE_FIELDS))
    except ValueError as err:
        raise ServiceValidationError(f"HSEM what_if: {err}.") from err
    return tuple(
        WhatIfScenario(
            usable_kwh=size,
            max_charge_kw=base.max_charge_kw,
            max_discharge_kw=base.max_discharge_kw,
            charge_efficiency=base.charge_efficiency,
            discharge_efficiency=base.discharge_efficiency,
            cycle_cost_per_kwh=base.cycle_cost_per_kwh,
        )
        for size in sizes
    )


async def async_handle_what_if(call: ServiceCall) -> ServiceResponse:
    """Replay the recorded days with another battery.

    Args:
        call: The service call. ``history_days`` and the optional overrides
            are described in ``services.yaml``.

    Returns:
        The comparison: totals per scenario, each compared day for a single
        scenario, how many days were skipped and why, and a note on what the
        figures mean.

    Raises:
        ServiceValidationError: When the coordinator is not found, the data
            does not describe a scenario, or a what-if is already running.
        HomeAssistantError: Before the first planner cycle, and when the run
            is cancelled because the integration unloads.
    """
    # Imported here: ``services`` imports this module for its handler map.
    from custom_components.hsem.services import _get_coordinator

    coordinator = _get_coordinator(call.hass)
    if coordinator is None:
        raise ServiceValidationError(
            "HSEM coordinator not found — integration may not be configured."
        )
    scenarios = scenarios_from_call(dict(call.data))
    started = time.monotonic()
    try:
        response = await async_run_what_if(
            coordinator,
            hsem_now(),
            history_days=call.data["history_days"],
            scenarios=scenarios,
            capacity_price_per_kwh=call.data.get("capacity_price_per_kwh"),
        )
    except WhatIfBusyError as err:
        raise ServiceValidationError(
            "HSEM what_if: a what-if is already running. Wait for it to finish."
        ) from err
    except WhatIfUnavailableError as err:
        raise HomeAssistantError(f"HSEM what_if: {err}.") from err
    if response is None:
        raise HomeAssistantError("HSEM what_if: the run was cancelled.")
    response["duration_seconds"] = round(time.monotonic() - started, 2)
    _LOGGER.info(
        "HSEM service: what_if completed — %d scenario(s), %d day(s) compared, "
        "%d skipped, %.1f s",
        len(scenarios),
        response["days_compared"],
        response["days_skipped"],
        response["duration_seconds"],
    )
    return response
