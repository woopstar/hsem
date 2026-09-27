"""Working-mode option resolution for the applier (LUNA2000 vs. EMMA).

Extracted from ``applier.py`` to keep it under the repository's 30 KB /
1000-line file limit. Pure helpers only: the actual select write stays in
``applier.py`` so it runs through the same write-and-verify path as every
other battery write.
"""

from __future__ import annotations

from typing import Any

from custom_components.hsem.custom_sensors.applier_state_readers import (
    _read_select_options,
)
from custom_components.hsem.utils.inverter_verify import ApplyResult, ApplyStatus
from custom_components.hsem.utils.logger import HSEM_LOGGER as _LOGGER
from custom_components.hsem.utils.workingmodes import resolve_working_mode_option


def _resolve_working_mode_write(
    sensor: Any,  # NOSONAR -- HA internal type; circular import risk
    entity_id: str,
    intent: str,
    live_mode: str | None,
) -> str | ApplyResult:
    """Return the select option to write for *intent*, or a FAILED result.

    The configured select entity is authoritative: the intent is mapped to
    whichever option it advertises (LUNA2000 or EMMA naming). When it has no
    matching option, a ``FAILED`` :class:`ApplyResult` is returned so the
    applier status sensor surfaces the misconfiguration instead of the write
    being skipped silently.

    Args:
        sensor: HSEM sensor instance with a ``hass`` attribute.
        entity_id: Working-mode select entity ID.
        intent: ``WorkingModes`` value the applier wants to apply.
        live_mode: Current live option, recorded as ``actual`` on failure.

    Returns:
        The option string to write, or a ``FAILED`` :class:`ApplyResult`.
    """
    option = resolve_working_mode_option(
        intent, _read_select_options(sensor, entity_id)
    )
    if option is not None:
        return option
    _LOGGER.warning("%s has no option for working mode %s", entity_id, intent)
    return ApplyResult(
        entity_id=entity_id,
        desired=intent,
        actual=live_mode,
        status=ApplyStatus.FAILED,
        error_message="Working mode not supported by the selected entity",
    )
