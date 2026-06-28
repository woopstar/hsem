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

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.hsem.const import DOMAIN
from custom_components.hsem.coordinator import HSEMDataUpdateCoordinator
from custom_components.hsem.utils.diagnostics import build_diagnostics_dump
from custom_components.hsem.utils.logger import HSEM_LOGGER as _LOGGER
from custom_components.hsem.utils.sensornames.diagnostics import (
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
SERVICE_CREATE_DASHBOARD = "create_dashboard"

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

SCHEMA_CREATE_DASHBOARD = vol.Schema({vol.Optional("force", default=False): bool})

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_coordinator(hass: HomeAssistant) -> HSEMDataUpdateCoordinator | None:
    """Return the first available HSEM coordinator from any LOADED config entry.

    HSEM only supports a single config entry, but looking up by the first
    loaded entry is safer than assuming a fixed entry ID.  Uses
    ``entry.runtime_data`` (Bronze rule: runtime-data).

    Args:
        hass: The Home Assistant instance.

    Returns:
        The HSEM coordinator, or ``None`` if no entry is configured/loaded.
    """
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is not ConfigEntryState.LOADED:
            continue
        if entry.runtime_data and hasattr(entry.runtime_data, "coordinator"):
            coordinator = entry.runtime_data.coordinator
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

    Raises:
        ServiceValidationError: When the coordinator is not found.
    """
    coordinator = _get_coordinator(hass)
    if coordinator is None:
        raise ServiceValidationError(
            "HSEM coordinator not found — integration may not be configured."
        )
    _LOGGER.info("HSEM service: force_recalculation called — triggering update cycle")
    await coordinator._async_handle_update(None)  # noqa: SLF001
    _LOGGER.info("HSEM service: force_recalculation completed")


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
        ServiceValidationError: When the select entity cannot be found or the
            service call fails.
    """
    working_mode: str = call.data["working_mode"]
    duration_minutes: int | None = call.data.get("duration_minutes")
    entity_id = get_force_working_mode_selector_entity_id()

    # Verify the entity exists before making the service call.
    if hass.states.get(entity_id) is None:
        raise ServiceValidationError(
            f"HSEM force working mode entity '{entity_id}' not found. "
            "Ensure the HSEM integration is fully configured."
        )

    _LOGGER.info(
        "HSEM service: set_temporary_override called — setting '%s' to '%s'",
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
                "HSEM service: set_temporary_override — mode='%s' will expire at %s",
                working_mode,
                coordinator._override_expiry.isoformat(),
            )
        else:
            coordinator._override_expiry = None

        await coordinator._async_handle_update(None)  # noqa: SLF001

    _LOGGER.info(
        "HSEM service: set_temporary_override completed — mode='%s'", working_mode
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
        ServiceValidationError: When the select entity cannot be found.
    """
    entity_id = get_force_working_mode_selector_entity_id()

    if hass.states.get(entity_id) is None:
        raise ServiceValidationError(
            f"HSEM force working mode entity '{entity_id}' not found. "
            "Ensure the HSEM integration is fully configured."
        )

    _LOGGER.info(
        "HSEM service: clear_override called — resetting '%s' to 'auto'",
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

    _LOGGER.info("HSEM service: clear_override completed")


async def async_handle_export_diagnostics(  # NOSONAR
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
        ServiceValidationError: When the coordinator is not found.
        HomeAssistantError: When no planner cycle has completed yet.
    """
    coordinator = _get_coordinator(hass)
    if coordinator is None:
        raise ServiceValidationError(
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
        integration_version = STATE_UNKNOWN

    dump = build_diagnostics_dump(
        planner_input,
        planner_output,
        apply_summary,
        integration_version=str(integration_version),
    )

    _LOGGER.info("HSEM service: export_diagnostics completed")
    return dump


async def async_handle_create_dashboard(
    hass: HomeAssistant,
    call: ServiceCall,
) -> None:
    """Create or update the HSEM Lovelace dashboard.

    Reads the bundled dashboard YAML from the integration directory and
    creates a dashboard at URL ``/hsem-dashboard``.  When ``force`` is
    ``False`` (default) and the dashboard already exists, the call is a
    no-op.  When ``force`` is ``True`` the existing dashboard is replaced.

    Args:
        hass: The Home Assistant instance.
        call: The service call with optional ``force`` boolean.
    """
    from pathlib import Path

    import yaml

    force: bool = call.data.get("force", False)
    dashboard_url = "hsem-dashboard"

    # Check if dashboard already exists.
    for dash in hass.data.get("lovelace", {}).get("dashboards", {}).values():
        if isinstance(dash, dict) and dash.get("url_path") == dashboard_url:
            if not force:
                _LOGGER.info(
                    "HSEM dashboard already exists at /%s — use force: true to overwrite",
                    dashboard_url,
                )
                return
            break

    # Read the bundled dashboard YAML.
    dash_path = Path(__file__).parent / "dashboards" / "dashboard_en.yaml"
    if not dash_path.exists():
        _LOGGER.error("HSEM dashboard YAML not found at %s", dash_path)
        return

    with open(dash_path, encoding="utf-8") as f:
        dashboard_yaml = yaml.safe_load(f)

    # Store the dashboard via HA's storage.
    store = hass.helpers.storage.Store(1, "lovelace.dashboards")
    data = await store.async_load() or {}
    content = data.get("content", {})

    existing = next(
        (
            d
            for d in content.values()
            if isinstance(d, dict) and d.get("url_path") == dashboard_url
        ),
        None,
    )
    if existing and not force:
        _LOGGER.info("HSEM dashboard already exists — use force: true to overwrite")
        return

    # Build the dashboard entry.
    import uuid

    dash_id = existing.get("id", str(uuid.uuid4())) if existing else str(uuid.uuid4())
    content[dash_id] = {
        "id": dash_id,
        "url_path": dashboard_url,
        "title": "HSEM",
        "icon": "mdi:solar-power",
        "show_in_sidebar": True,
        "require_admin": False,
        "mode": "storage",
    }

    data["content"] = content
    await store.async_save(data)

    # Also store the dashboard config itself.
    config_store = hass.helpers.storage.Store(1, f"lovelace.{dash_id}")
    await config_store.async_save(dashboard_yaml)

    _LOGGER.info("HSEM dashboard created at /%s", dashboard_url)


# ---------------------------------------------------------------------------
# Service registration
# ---------------------------------------------------------------------------

SERVICE_HANDLER_MAP: dict[str, tuple[vol.Schema, Any, SupportsResponse]] = {
    SERVICE_CLEAR_OVERRIDE: (
        SCHEMA_CLEAR_OVERRIDE,
        async_handle_clear_override,
        SupportsResponse.NONE,
    ),
    SERVICE_CREATE_DASHBOARD: (
        SCHEMA_CREATE_DASHBOARD,
        async_handle_create_dashboard,
        SupportsResponse.NONE,
    ),
    SERVICE_EXPORT_DIAGNOSTICS: (
        SCHEMA_EXPORT_DIAGNOSTICS,
        async_handle_export_diagnostics,
        SupportsResponse.ONLY,
    ),
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
}


async def async_register_services(
    hass: HomeAssistant,
) -> None:  # NOSONAR
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


async def async_unregister_services(
    hass: HomeAssistant,
) -> None:  # NOSONAR
    """Remove all HSEM services from Home Assistant.

    Called during :func:`~custom_components.hsem.__init__.async_unload_entry`.

    Args:
        hass: The Home Assistant instance.
    """
    for service_name in SERVICE_HANDLER_MAP:
        if hass.services.has_service(DOMAIN, service_name):
            hass.services.async_remove(DOMAIN, service_name)
