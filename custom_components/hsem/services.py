"""Service handlers for the HSEM integration.

This module implements the HSEM services:

- ``force_recalculation`` — Re-run the full planning pipeline immediately.
- ``set_temporary_override`` — Force a specific working mode on the select entity.
- ``clear_override`` — Reset the force-mode select to ``"auto"``.
- ``export_diagnostics`` — Return a structured diagnostics dump as service response.
- ``create_dashboard`` — Log the path to the bundled dashboard YAML.
- ``ocpp_debug_start_charging`` — Manually send RemoteStartTransaction +
  SetChargingProfile, bypassing the anti-flap state machine (issue #920).
- ``ocpp_debug_stop_charging`` — Manually send RemoteStopTransaction, bypassing
  the anti-flap state machine (issue #920).
- ``ocpp_debug_diagnostics`` — Query the charger's own configuration and the
  charging limit it has actually computed (issue #920).

All services are integration-level actions; the coordinator is looked up from
the only configured HSEM entry.  Service schemas are defined in ``services.yaml``.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.hsem.const import DOMAIN
from custom_components.hsem.coordinator import HSEMDataUpdateCoordinator
from custom_components.hsem.utils.dashboard import async_ensure_hsem_dashboard
from custom_components.hsem.utils.diagnostics import build_diagnostics_dump
from custom_components.hsem.utils.integration_version import (
    async_get_hsem_integration_version,
)
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

# OCPP debug services (issue #920) target the primary EV's server by default,
# or the second EV's server (only relevant when configured/enabled) — mirrors
# the "charger_index" convention used throughout ocpp_sensors.py.
SUPPORTED_OCPP_CHARGERS: list[str] = ["primary", "second"]

# ---------------------------------------------------------------------------
# Service name constants
# ---------------------------------------------------------------------------

SERVICE_FORCE_RECALCULATION = "force_recalculation"
SERVICE_SET_TEMPORARY_OVERRIDE = "set_temporary_override"
SERVICE_CLEAR_OVERRIDE = "clear_override"
SERVICE_EXPORT_DIAGNOSTICS = "export_diagnostics"
SERVICE_CREATE_DASHBOARD = "create_dashboard"
SERVICE_OCPP_DEBUG_START_CHARGING = "ocpp_debug_start_charging"
SERVICE_OCPP_DEBUG_STOP_CHARGING = "ocpp_debug_stop_charging"
SERVICE_OCPP_DEBUG_DIAGNOSTICS = "ocpp_debug_diagnostics"

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

SCHEMA_CREATE_DASHBOARD = vol.Schema(
    {
        vol.Optional("dashboard_path"): vol.All(vol.Coerce(str), vol.Length(min=1)),
    }
)

SCHEMA_OCPP_DEBUG_START_CHARGING = vol.Schema(
    {
        vol.Optional("charger", default="primary"): vol.In(SUPPORTED_OCPP_CHARGERS),
        vol.Optional("max_current_a", default=16): vol.All(
            vol.Coerce(int),
            vol.Range(min=6, max=32),
        ),
    }
)

SCHEMA_OCPP_DEBUG_STOP_CHARGING = vol.Schema(
    {
        vol.Optional("charger", default="primary"): vol.In(SUPPORTED_OCPP_CHARGERS),
    }
)

SCHEMA_OCPP_DEBUG_DIAGNOSTICS = vol.Schema(
    {
        vol.Optional("charger", default="primary"): vol.In(SUPPORTED_OCPP_CHARGERS),
    }
)

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


def _get_ocpp_server(coordinator: HSEMDataUpdateCoordinator, charger: str) -> Any:
    """Return the running OCPP server for the selected EV, or ``None``.

    Args:
        coordinator: The HSEM coordinator.
        charger: ``"primary"`` or ``"second"`` — selects which EV's embedded
            OCPP server to target (issue #920). Mirrors the
            ``charger_index`` convention in ``ocpp_sensors.py``.

    Returns:
        The :class:`~custom_components.hsem.custom_sensors.ocpp_server.OCPPServer`
        instance, or ``None`` if that server isn't enabled/running.
    """
    attr = "_ocpp_second_server" if charger == "second" else "_ocpp_server"
    return getattr(coordinator, attr, None)


def _resolve_connected_charger(hass: HomeAssistant, charger: str) -> tuple[Any, str]:
    """Return the running OCPP server and its connected CPID, or raise.

    Shared by every ``ocpp_debug_*`` service (issue #920) so the three of
    them can't drift in how they validate — all of them need exactly the
    same three things to be true before they can talk to a charger.

    Args:
        hass: The Home Assistant instance.
        charger: ``"primary"`` or ``"second"``.

    Returns:
        A ``(server, cpid)`` tuple for the connected charger.

    Raises:
        ServiceValidationError: When the coordinator or the selected EV's
            OCPP server is unavailable, or no charger is connected to it.
    """
    coordinator = _get_coordinator(hass)
    if coordinator is None:
        raise ServiceValidationError(
            "HSEM coordinator not found — integration may not be configured."
        )

    ocpp_server = _get_ocpp_server(coordinator, charger)
    if ocpp_server is None:
        raise ServiceValidationError(
            f"OCPP server for the '{charger}' charger is not running "
            "— check that OCPP is enabled in the config for this EV."
        )

    active = ocpp_server.active_chargers
    if not active:
        raise ServiceValidationError(
            f"No charger currently connected to the '{charger}' OCPP server."
        )
    return ocpp_server, active[0]


# ---------------------------------------------------------------------------
# Service handler implementations
# ---------------------------------------------------------------------------


async def async_handle_force_recalculation(call: ServiceCall) -> None:
    """Trigger an immediate full planner recalculation.

    Args:
        call: The service call (schema is empty). ``call.hass`` provides the
            Home Assistant instance.

    Raises:
        ServiceValidationError: When the coordinator is not found.
    """
    coordinator = _get_coordinator(call.hass)
    if coordinator is None:
        raise ServiceValidationError(
            "HSEM coordinator not found — integration may not be configured."
        )
    _LOGGER.info("HSEM service: force_recalculation called — triggering update cycle")
    await coordinator._async_handle_update(None)  # noqa: SLF001
    _LOGGER.info("HSEM service: force_recalculation completed")


async def async_handle_set_temporary_override(call: ServiceCall) -> None:
    """Force a specific working mode via the force-mode select entity.

    The service writes ``call.data["working_mode"]`` to the
    ``select.hsem_force_working_mode`` entity, which the coordinator reads
    on the next cycle to bypass the planner and send the chosen mode
    directly to the inverter.

    Args:
        call: The service call with ``working_mode`` key in ``data``
            and optional ``duration_minutes`` key. ``call.hass`` provides the
            Home Assistant instance.

    Raises:
        ServiceValidationError: When the select entity cannot be found or the
            service call fails.
    """
    working_mode: str = call.data["working_mode"]
    duration_minutes: int | None = call.data.get("duration_minutes")
    entity_id = get_force_working_mode_selector_entity_id()

    # Verify the entity exists before making the service call.
    if call.hass.states.get(entity_id) is None:
        raise ServiceValidationError(
            f"HSEM force working mode entity '{entity_id}' not found. "
            "Ensure the HSEM integration is fully configured."
        )

    _LOGGER.info(
        "HSEM service: set_temporary_override called — setting '%s' to '%s'",
        entity_id,
        working_mode,
    )

    await call.hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": entity_id, "option": working_mode},
        blocking=True,
    )

    # Store the override expiry on the coordinator if a duration was provided.
    coordinator = _get_coordinator(call.hass)
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


async def async_handle_clear_override(call: ServiceCall) -> None:
    """Clear any active working-mode override by setting the select to ``"auto"``.

    Args:
        call: The service call (schema is empty). ``call.hass`` provides the
            Home Assistant instance.

    Raises:
        ServiceValidationError: When the select entity cannot be found.
    """
    entity_id = get_force_working_mode_selector_entity_id()

    if call.hass.states.get(entity_id) is None:
        raise ServiceValidationError(
            f"HSEM force working mode entity '{entity_id}' not found. "
            "Ensure the HSEM integration is fully configured."
        )

    _LOGGER.info(
        "HSEM service: clear_override called — resetting '%s' to 'auto'",
        entity_id,
    )

    await call.hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": entity_id, "option": "auto"},
        blocking=True,
    )

    # Clear any stored override expiry so the override does not linger.
    coordinator = _get_coordinator(call.hass)
    if coordinator is not None:
        coordinator._override_expiry = None  # noqa: SLF001
        await coordinator._async_handle_update(None)  # noqa: SLF001

    _LOGGER.info("HSEM service: clear_override completed")


async def async_handle_export_diagnostics(
    call: ServiceCall,
) -> dict[str, Any]:  # NOSONAR
    """Export a structured diagnostics dump for the HSEM integration.

    The returned dictionary contains the most recent planner input, planner
    output, hardware-write summary, and integration version — with all
    entity-level identifiers redacted.  Suitable for debugging or attaching
    to GitHub issues.

    Args:
        call: The service call (schema is empty). ``call.hass`` provides the
            Home Assistant instance.

    Returns:
        A JSON-serialisable diagnostics dump dictionary.

    Raises:
        ServiceValidationError: When the coordinator is not found.
        HomeAssistantError: When no planner cycle has completed yet.
    """
    coordinator = _get_coordinator(call.hass)
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

    integration_version = await async_get_hsem_integration_version(call.hass)

    dump = build_diagnostics_dump(
        planner_input,
        planner_output,
        apply_summary,
        integration_version=str(integration_version),
    )

    _LOGGER.info("HSEM service: export_diagnostics completed")
    return dump


async def async_handle_create_dashboard(
    call: ServiceCall,
) -> dict[str, Any]:  # NOSONAR
    """Create or update the bundled HSEM Lovelace dashboard.

    The bundled dashboard YAML is copied to the default path
    ``<config>/hsem_dashboard.yaml`` (or the ``dashboard_path`` override) and
    a storage-mode Lovelace dashboard is registered in Home Assistant so the
    dashboard appears in the sidebar.

    Args:
        call: The service call with optional ``dashboard_path`` key in
            ``data``. ``call.hass`` provides the Home Assistant instance.

    Returns:
        A dict with ``dashboard_path`` and ``dashboard_url`` keys.

    Raises:
        HomeAssistantError: When the dashboard cannot be created.
    """
    from pathlib import Path

    dashboard_path_arg: str | None = call.data.get("dashboard_path")
    dashboard_path = Path(dashboard_path_arg) if dashboard_path_arg else None

    result = await async_ensure_hsem_dashboard(
        call.hass,
        dashboard_path=dashboard_path,
    )
    _LOGGER.info(
        "HSEM service: create_dashboard completed — path=%s url=%s",
        result["dashboard_path"],
        result["dashboard_url"],
    )
    return result


async def async_handle_ocpp_debug_start_charging(call: ServiceCall) -> None:
    """Manually send RemoteStartTransaction + SetChargingProfile for debugging.

    Bypasses the anti-flap state machine entirely and talks directly to the
    connected charger via :meth:`OCPPServer.send_remote_start` and
    :meth:`OCPPServer.send_set_charging_profile` (issue #920) — for
    diagnosing why a charger won't start over OCPP, not for normal
    operation. The planner's own anti-flap-gated target still runs on the
    next coordinator cycle and may countermand this immediately if the plan
    calls for zero power.

    Args:
        call: The service call with an optional ``charger`` key
            (``"primary"``/``"second"``, default ``"primary"``) and optional
            ``max_current_a`` key (default 16). ``call.hass`` provides the
            Home Assistant instance.

    Raises:
        ServiceValidationError: When the coordinator or selected OCPP
            server is unavailable, or no charger is currently connected.
        HomeAssistantError: When the commands fail to reach the charger.
    """
    charger_choice: str = call.data["charger"]
    max_current_a: int = call.data["max_current_a"]

    ocpp_server, cpid = _resolve_connected_charger(call.hass, charger_choice)

    _LOGGER.warning(
        "HSEM service: ocpp_debug_start_charging called for %s charger "
        "(cpid=%s, max_current_a=%d) — bypassing anti-flap for debugging",
        charger_choice,
        cpid,
        max_current_a,
    )
    start_ok = await ocpp_server.send_remote_start(cpid)
    profile_ok = await ocpp_server.send_set_charging_profile(
        cpid, max_current_a * 230, max_current_a
    )
    if not (start_ok and profile_ok):
        raise HomeAssistantError(
            f"HSEM service: failed to send start commands to charger '{cpid}' "
            "— see the log for details."
        )
    _LOGGER.info("HSEM service: ocpp_debug_start_charging completed for cpid=%s", cpid)


async def async_handle_ocpp_debug_stop_charging(call: ServiceCall) -> None:
    """Manually send RemoteStopTransaction for debugging.

    Bypasses the anti-flap state machine entirely via
    :meth:`OCPPServer.send_remote_stop` (issue #920) — for diagnosing why a
    charger won't stop over OCPP, not for normal operation.

    Args:
        call: The service call with an optional ``charger`` key
            (``"primary"``/``"second"``, default ``"primary"``).
            ``call.hass`` provides the Home Assistant instance.

    Raises:
        ServiceValidationError: When the coordinator or selected OCPP
            server is unavailable, or no charger is currently connected.
        HomeAssistantError: When the command fails to reach the charger.
    """
    charger_choice: str = call.data["charger"]

    ocpp_server, cpid = _resolve_connected_charger(call.hass, charger_choice)

    _LOGGER.warning(
        "HSEM service: ocpp_debug_stop_charging called for %s charger "
        "(cpid=%s) — bypassing anti-flap for debugging",
        charger_choice,
        cpid,
    )
    stopped = await ocpp_server.send_remote_stop(cpid)
    if not stopped:
        raise HomeAssistantError(
            f"HSEM service: failed to send RemoteStopTransaction to charger "
            f"'{cpid}' — see the log for details."
        )
    _LOGGER.info("HSEM service: ocpp_debug_stop_charging completed for cpid=%s", cpid)


async def async_handle_ocpp_debug_diagnostics(call: ServiceCall) -> None:
    """Interrogate the charger about its own configuration and limits.

    Diagnostics-only (issue #920). Sends ``GetConfiguration`` and
    ``GetCompositeSchedule`` and logs the charger's replies at warning
    level, to answer the questions a ``"status": "Accepted"`` on
    ``SetChargingProfile`` cannot:

    - Does the charger implement SmartCharging at all
      (``SupportedFeatureProfiles``)?
    - Does it want amps or watts
      (``ChargingScheduleAllowedChargingRateUnit``)? HSEM always sends
      amps, which a watt-only charger can accept and then apply as
      nothing.
    - What limit has it actually computed from the profiles installed on
      the connector (``GetCompositeSchedule``)? A charger accepting a
      16 A profile and then reporting a composite schedule of 0 A is the
      signature of a profile that was accepted and silently ignored — and
      of ``SuspendedEVSE``, which OCPP 1.6 defines as the EVSE withholding
      energy, explicitly listing "a smart charging restriction" as a cause.

    Replies arrive asynchronously and are logged by the OCPP server as
    they come in, so this returns as soon as both requests are sent.

    Args:
        call: The service call with an optional ``charger`` key
            (``"primary"``/``"second"``, default ``"primary"``).
            ``call.hass`` provides the Home Assistant instance.

    Raises:
        ServiceValidationError: When the coordinator or selected OCPP
            server is unavailable, or no charger is currently connected.
        HomeAssistantError: When the requests fail to reach the charger.
    """
    charger_choice: str = call.data["charger"]
    ocpp_server, cpid = _resolve_connected_charger(call.hass, charger_choice)

    _LOGGER.warning(
        "HSEM service: ocpp_debug_diagnostics called for %s charger "
        "(cpid=%s) — querying GetConfiguration + GetCompositeSchedule; "
        "replies are logged as they arrive",
        charger_choice,
        cpid,
    )
    config_ok = await ocpp_server.send_get_configuration(cpid)
    schedule_ok = await ocpp_server.send_get_composite_schedule(cpid)
    if not (config_ok and schedule_ok):
        raise HomeAssistantError(
            f"HSEM service: failed to send diagnostic queries to charger "
            f"'{cpid}' — see the log for details."
        )
    _LOGGER.info("HSEM service: ocpp_debug_diagnostics sent for cpid=%s", cpid)


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
        SupportsResponse.ONLY,
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
    SERVICE_OCPP_DEBUG_DIAGNOSTICS: (
        SCHEMA_OCPP_DEBUG_DIAGNOSTICS,
        async_handle_ocpp_debug_diagnostics,
        SupportsResponse.NONE,
    ),
    SERVICE_OCPP_DEBUG_START_CHARGING: (
        SCHEMA_OCPP_DEBUG_START_CHARGING,
        async_handle_ocpp_debug_start_charging,
        SupportsResponse.NONE,
    ),
    SERVICE_OCPP_DEBUG_STOP_CHARGING: (
        SCHEMA_OCPP_DEBUG_STOP_CHARGING,
        async_handle_ocpp_debug_stop_charging,
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
