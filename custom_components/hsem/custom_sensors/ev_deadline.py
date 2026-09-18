"""Resolve configured EV charging deadlines."""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from typing import Any

from homeassistant.exceptions import HomeAssistantError

from custom_components.hsem.utils.ha_helpers import (
    EntityNotFoundError,
    ha_get_entity_state_and_convert,
)
from custom_components.hsem.utils.logger import HSEM_LOGGER as _LOGGER


def resolve_ev_deadline_from_params(
    sensor: Any,
    deadline_entity: str | None,
    deadline_fixed: str | None,
) -> datetime | None:
    """Resolve an EV charging deadline from an entity or fixed config string.

    Args:
        sensor: Working-mode sensor instance (provides ``hass``).
        deadline_entity: Optional HA entity whose state is a time string.
        deadline_fixed: Fallback HH:MM string from config.

    Returns:
        A timezone-aware ``datetime`` for the deadline, or ``None``.
    """
    time_str: str | None = None

    if deadline_entity:
        try:
            raw = ha_get_entity_state_and_convert(sensor, deadline_entity, None)
            from homeassistant.core import State as _State  # noqa: PLC0415

            if isinstance(raw, _State):
                time_str = raw.state
            elif isinstance(raw, str):
                time_str = raw
        except (EntityNotFoundError, HomeAssistantError) as exc:
            _LOGGER.warning(
                "Could not read EV deadline entity '%s': %s. Falling back to default.",
                deadline_entity,
                exc,
            )

    if not time_str:
        time_str = deadline_fixed or "07:00"

    match = re.match(
        r"^([01]?\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?$",
        (time_str or "").strip(),
    )
    if not match:
        _LOGGER.debug("Ignoring invalid EV deadline time '%s'", time_str)
        return None

    hour, minute = int(match.group(1)), int(match.group(2))
    from custom_components.hsem.utils.datetime_utils import now as hsem_now

    now_local = hsem_now()
    deadline_naive = datetime.combine(now_local.date(), time(hour, minute))
    deadline = deadline_naive.replace(tzinfo=now_local.tzinfo)

    if deadline <= now_local:
        deadline += timedelta(days=1)

    return deadline
