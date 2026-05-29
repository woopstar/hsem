"""Service handlers for the HSEM integration.

This module implements the four HSEM services:

- ``force_recalculation`` — Re-run the full planning pipeline immediately.
- ``set_temporary_override`` — Force a specific working mode on the select entity.
- ``clear_override`` — Reset the force-mode select to ``"auto"``.
- ``export_diagnostics`` — Return a structured diagnostics dump as service response.

All services expect the caller to provide the config-entry device ID (or the
coordinator is looked up from the only configured HSEM entry).  Service schemas
are defined in ``services.yaml``.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError

from custom_components.hsem.const import DOMAIN
from custom_components.hsem.coordinator import HSEMDataUpdateCoordinator
from custom_components.hsem.utils.diagnostics import build_diagnostics_dump
from custom_components.hsem.utils.logger import HSEM_LOGGER as _LOGGER
from custom_components.hsem.utils.sensornames import (
    get_force_working_mode_selector_entity_id,
)

# ---------------------------------------------------------------------------
# Supported override modes
# ---------------------------------------------------------------------------

SUPPORTED_OVERRIDE_MODES: list[str] = [
    "batteries_charge_grid",
    "batteries_charge_solar",
    "batteries_discharge_mode",
    "batteries_wait_mode",
    "ev_smart_charging",
    "force_batteries_discharge",
    "force_export",
]

# ---------------------------------------------------------------------------
# Service name constants
# ---------------------------------------------------------------------------

SERVICE_FORCE_RECALCULATION = "force_recalculation"
SERVICE_SET_TEMPORARY_OVERRIDE = "set_temporary_override"
SERVICE_CLEAR_OVERRIDE = "clear_override"
SERVICE_EXPORT_DIAGNOSTICS = "export_diagnostics"

# ---------------------------------------------------------------------------
# Voluptuous schemas for input validation
# ---------------------------------------------------------------------------

SCHEMA_FORCE_RECALCULATION = vol.Schema({})

SCHEMA_SET_TEMPORARY_OVERRIDE = vol.Schema(
    {
        vol.Required("working_mode"): vol.In(SUPPORTED_OVERRIDE_MODES),
        vol.Optional("duration_minutes"): vol.All(
            vol.Coerce(int),
            vol.Range(min=1, max=1440),
        ),
    }
)

SCHEMA_CLEAR_OVERRIDE = vol.Schema({})

SCHEMA_EXPORT_DIAGNOSTICS = vol.Schema({})

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_coordinator(hass: HomeAssistant) -> HSEMDataUpdateCoordinator | None:
    """Return the first available HSEM coordinator.

    HSEM only supports a single config entry, but looking up by the first
    entry is safer than assuming a fixed entry ID.

    Args:
        hass: The Home Assistant instance.

    Returns:
        The HSEM coordinator, or ``None`` if no entry is configured.
    """
    domain_data = hass.data.get(DOMAIN, {})
    for entry_data in domain_data.values():
        if isinstance(entry_data, dict):
            coordinator = entry_data.get("coordinator")
            if isinstance(coordinator, HSEMDataUpdateCoordinator):
                return coordinator
    return None


# ---------------------------------------------------------------------------
# Service handler implementations
# ---------------------------------------------------------------------------


async def async_handle_force_recalculation(
    hass: HomeAssistant,
    call: ServiceCall,  # noqa: ARG001
) -> None:
    """Trigger an immediate full planner recalculation.

    Args:
        hass: The Home Assistant instance.
        call: The service call (unused, schema is empty).
    """
    coordinator = _get_coordinator(hass)
    if coordinator is None:
        raise HomeAssistantError(
            "HSEM coordinator not found — integration may not be configured."
        )
    _LOGGER.info("HSEM service: force_recalculation called — triggering update cycle.")
    await coordinator._async_handle_update(None)  # noqa: SLF001
    _LOGGER.info("HSEM service: force_recalculation completed.")


async def async_handle_set_temporary_override(
    hass: HomeAssistant,
    call: ServiceCall,
) -> None:
    """Force a specific working mode via the force-mode select entity.

    The service writes ``call.data["working_mode"]`` to the
    ``select.hsem_force_working_mode`` entity, which the coordinator reads
    on the next cycle to bypass the planner and send the chosen mode
    directly to the inverter.

    Args:
        hass: The Home Assistant instance.
        call: The service call with ``working_mode`` key in ``data``
            and optional ``duration_minutes`` key.

    Raises:
        HomeAssistantError: When the select entity cannot be found or the
            service call fails.
    """
    working_mode: str = call.data["working_mode"]
    duration_minutes: int | None = call.data.get("duration_minutes")
    entity_id = get_force_working_mode_selector_entity_id()

    # Verify the entity exists before making the service call.
    if hass.states.get(entity_id) is None:
        raise HomeAssistantError(
            f"HSEM force working mode entity '{entity_id}' not found. "
            "Ensure the HSEM integration is fully configured."
        )

    _LOGGER.info(
        "HSEM service: set_temporary_override called — setting '%s' to '%s'.",
        entity_id,
        working_mode,
    )

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": entity_id, "option": working_mode},
        blocking=True,
    )

    # Store the override expiry on the coordinator if a duration was provided.
    coordinator = _get_coordinator(hass)
    if coordinator is not None:
        if duration_minutes is not None:
            from datetime import timedelta

            from custom_components.hsem.utils.datetime_utils import now as hsem_now

            coordinator._override_expiry = hsem_now() + timedelta(
                minutes=duration_minutes
            )
            _LOGGER.info(
                "HSEM service: set_temporary_override — mode='%s' will expire at %s.",
                working_mode,
                coordinator._override_expiry.isoformat(),
            )
        else:
            coordinator._override_expiry = None

        await coordinator._async_handle_update(None)  # noqa: SLF001

    _LOGGER.info(
        "HSEM service: set_temporary_override completed — mode='%s'.", working_mode
    )


async def async_handle_clear_override(
    hass: HomeAssistant,
    call: ServiceCall,  # noqa: ARG001
) -> None:
    """Clear any active working-mode override by setting the select to ``"auto"``.

    Args:
        hass: The Home Assistant instance.
        call: The service call (unused, schema is empty).

    Raises:
        HomeAssistantError: When the select entity cannot be found.
    """
    entity_id = get_force_working_mode_selector_entity_id()

    if hass.states.get(entity_id) is None:
        raise HomeAssistantError(
            f"HSEM force working mode entity '{entity_id}' not found. "
            "Ensure the HSEM integration is fully configured."
        )

    _LOGGER.info(
        "HSEM service: clear_override called — resetting '%s' to 'auto'.",
        entity_id,
    )

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": entity_id, "option": "auto"},
        blocking=True,
    )

    # Clear any stored override expiry so the override does not linger.
    coordinator = _get_coordinator(hass)
    if coordinator is not None:
        coordinator._override_expiry = None  # noqa: SLF001
        await coordinator._async_handle_update(None)  # noqa: SLF001

    _LOGGER.info("HSEM service: clear_override completed.")


async def async_handle_export_diagnostics(
    hass: HomeAssistant,
    call: ServiceCall,  # noqa: ARG001
) -> dict[str, Any]:
    """Export a structured diagnostics dump for the HSEM integration.

    The returned dictionary contains the most recent planner input, planner
    output, hardware-write summary, and integration version — with all
    entity-level identifiers redacted.  Suitable for debugging or attaching
    to GitHub issues.

    Args:
        hass: The Home Assistant instance.
        call: The service call (unused, schema is empty).

    Returns:
        A JSON-serialisable diagnostics dump dictionary.

    Raises:
        HomeAssistantError: When no planner cycle has completed yet or the
            coordinator is not found.
    """
    coordinator = _get_coordinator(hass)
    if coordinator is None:
        raise HomeAssistantError(
            "HSEM coordinator not found — integration may not be configured."
        )

    planner_input = getattr(coordinator, "_last_planner_input", None)
    planner_output = getattr(coordinator, "_last_planner_output", None)
    apply_summary = coordinator.data.apply_summary if coordinator.data else None

    if planner_input is None or planner_output is None:
        raise HomeAssistantError(
            "HSEM diagnostics: no planner cycle has completed yet. "
            "Wait for the first update cycle to finish."
        )

    try:
        from importlib.metadata import version as pkg_version

        integration_version = pkg_version("hsem")
    except Exception:  # noqa: BLE001
        integration_version = "unknown"

    dump = build_diagnostics_dump(
        planner_input,
        planner_output,
        apply_summary,
        integration_version=str(integration_version),
    )

    _LOGGER.info("HSEM service: export_diagnostics completed.")
    return dump


# ---------------------------------------------------------------------------
# Service registration
# ---------------------------------------------------------------------------

SERVICE_HANDLER_MAP: dict[str, tuple[vol.Schema, Any, SupportsResponse]] = {
    SERVICE_FORCE_RECALCULATION: (
        SCHEMA_FORCE_RECALCULATION,
        async_handle_force_recalculation,
        SupportsResponse.NONE,
    ),
    SERVICE_SET_TEMPORARY_OVERRIDE: (
        SCHEMA_SET_TEMPORARY_OVERRIDE,
        async_handle_set_temporary_override,
        SupportsResponse.NONE,
    ),
    SERVICE_CLEAR_OVERRIDE: (
        SCHEMA_CLEAR_OVERRIDE,
        async_handle_clear_override,
        SupportsResponse.NONE,
    ),
    SERVICE_EXPORT_DIAGNOSTICS: (
        SCHEMA_EXPORT_DIAGNOSTICS,
        async_handle_export_diagnostics,
        SupportsResponse.ONLY,
    ),
}


async def async_register_services(hass: HomeAssistant) -> None:
    """Register all HSEM services with Home Assistant.

    Called during :func:`~custom_components.hsem.__init__.async_setup_entry`.
    Services are unregistered automatically when the config entry is unloaded
    because they are tied to the integration domain.

    Args:
        hass: The Home Assistant instance.
    """
    for service_name, (
        schema,
        handler,
        supports_response,
    ) in SERVICE_HANDLER_MAP.items():
        if not hass.services.has_service(DOMAIN, service_name):
            hass.services.async_register(
                domain=DOMAIN,
                service=service_name,
                service_func=handler,
                schema=schema,
                supports_response=supports_response,
            )


async def async_unregister_services(hass: HomeAssistant) -> None:
    """Remove all HSEM services from Home Assistant.

    Called during :func:`~custom_components.hsem.__init__.async_unload_entry`.

    Args:
        hass: The Home Assistant instance.
    """
    for service_name in SERVICE_HANDLER_MAP:
        if hass.services.has_service(DOMAIN, service_name):
            hass.services.async_remove(DOMAIN, service_name)
