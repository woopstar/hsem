"""OCPP charger debug services.

Split from :mod:`services` to stay inside the repository's 30 KB file
limit, and cohesive on its own: every service here talks directly to a
connected OCPP charger, bypassing the planner and the anti-flap state
machine, and exists to answer a question about the charger rather than to
run the product (issue #920).

They came out of a charger that accepted ``RemoteStartTransaction``,
``SetChargingProfile`` and ``RemoteStopTransaction`` — all answering
``"Accepted"``, all confirmed by the charger's own
``StartTransaction``/``StopTransaction`` — while delivering no power at
all. Diagnosing that needed the ability to send one command at a time and
read what the charger says back about itself.

``_get_coordinator`` is imported inside the function rather than at module
scope: :mod:`services` imports these handlers to build its registration
map, so a module-level import back into it would be circular.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.hsem.coordinator import HSEMDataUpdateCoordinator
from custom_components.hsem.utils.logger import HSEM_LOGGER as _LOGGER

# ---------------------------------------------------------------------------
# Service name constants
# ---------------------------------------------------------------------------

SERVICE_OCPP_DEBUG_START_CHARGING = "ocpp_debug_start_charging"
SERVICE_OCPP_DEBUG_STOP_CHARGING = "ocpp_debug_stop_charging"
SERVICE_OCPP_DEBUG_DIAGNOSTICS = "ocpp_debug_diagnostics"
SERVICE_OCPP_DEBUG_SET_AVAILABILITY = "ocpp_debug_set_availability"
SERVICE_OCPP_DEBUG_SET_CONFIGURATION = "ocpp_debug_set_configuration"
SERVICE_OCPP_DEBUG_SET_CURRENT = "ocpp_debug_set_current"

# These services target the primary EV's server by default, or the second
# EV's server (only relevant when configured/enabled) — mirrors the
# "charger_index" convention used throughout ocpp_sensors.py.
SUPPORTED_OCPP_CHARGERS: list[str] = ["primary", "second"]

# ---------------------------------------------------------------------------
# Voluptuous schemas for input validation
# ---------------------------------------------------------------------------

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

SCHEMA_OCPP_DEBUG_SET_AVAILABILITY = vol.Schema(
    {
        vol.Optional("charger", default="primary"): vol.In(SUPPORTED_OCPP_CHARGERS),
        vol.Optional("operative", default=True): vol.Coerce(bool),
        vol.Optional("connector_id", default=1): vol.All(
            vol.Coerce(int),
            vol.Range(min=0, max=8),
        ),
    }
)

SCHEMA_OCPP_DEBUG_SET_CURRENT = vol.Schema(
    {
        vol.Optional("charger", default="primary"): vol.In(SUPPORTED_OCPP_CHARGERS),
        # 0 is meaningful — it is the generic OCPP way to say "draw
        # nothing" — but 1-5 A is not: no charger delivers below its
        # MinChargingCurrent, which is 6 A on every unit seen so far.
        vol.Required("current_a"): vol.Any(
            0, vol.All(vol.Coerce(int), vol.Range(min=6, max=32))
        ),
    }
)

SCHEMA_OCPP_DEBUG_SET_CONFIGURATION = vol.Schema(
    {
        vol.Optional("charger", default="primary"): vol.In(SUPPORTED_OCPP_CHARGERS),
        vol.Required("key"): vol.All(vol.Coerce(str), vol.Length(min=1)),
        vol.Required("value"): vol.Coerce(str),
    }
)


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
    # Deferred to avoid a circular import — see the module docstring.
    from custom_components.hsem.services import _get_coordinator

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


async def async_handle_ocpp_debug_set_current(call: ServiceCall) -> None:
    """Send only a charging profile, at a given current.

    Diagnostics-only (issue #920), and deliberately the narrowest of the
    OCPP debug services: it sends a ``SetChargingProfile`` and nothing
    else. No ``RemoteStartTransaction``, no vendor force-state write. That
    isolation is the point — it answers "does this charger honour charging
    profiles at all?", which nothing else can, because every other path
    changes more than one thing at once.

    ``current_a: 0`` is the interesting case: a 0 A profile is the generic,
    standards-only way an energy-management system says "draw nothing". If
    it stops a charge on its own, HSEM's stop needs no vendor-specific
    handling for that charger.

    Remember a request above the charger's own ``Station-MaxCurrent`` is
    accepted but cannot raise the limit, so it will look like nothing
    happened — HSEM logs a warning naming both numbers when that is why.

    Args:
        call: The service call with a required ``current_a`` key (``0``, or
            6-32 A) and an optional ``charger`` key
            (``"primary"``/``"second"``, default ``"primary"``).
            ``call.hass`` provides the Home Assistant instance.

    Raises:
        ServiceValidationError: When the coordinator or selected OCPP
            server is unavailable, or no charger is currently connected.
        HomeAssistantError: When the profile fails to reach the charger.
    """
    charger_choice: str = call.data["charger"]
    current_a: int = call.data["current_a"]
    ocpp_server, cpid = _resolve_connected_charger(call.hass, charger_choice)

    _LOGGER.warning(
        "HSEM service: ocpp_debug_set_current called for %s charger "
        "(cpid=%s) — sending a %d A charging profile and nothing else",
        charger_choice,
        cpid,
        current_a,
    )
    if not await ocpp_server.send_set_charging_profile(
        cpid, current_a * 230, current_a
    ):
        raise HomeAssistantError(
            f"HSEM service: failed to send charging profile to charger "
            f"'{cpid}' — see the log for details."
        )
    _LOGGER.info(
        "HSEM service: ocpp_debug_set_current sent %d A for cpid=%s",
        current_a,
        cpid,
    )


async def async_handle_ocpp_debug_set_availability(call: ServiceCall) -> None:
    """Set the charger's connector Operative or Inoperative.

    Diagnostics-only (issue #920). The standard OCPP 1.6 lever for a
    central system to take a connector into or out of service — HSEM had
    no equivalent of ``lbbrhzn/ocpp``'s "Availability" switch.

    Note ``Inoperative`` maps to connector status ``"Unavailable"``, which
    is a different thing from ``"SuspendedEVSE"``. A charger that is
    already Operative but locally refusing to energise will accept this
    and change nothing — an informative result in itself, since it rules
    availability out as the cause.

    Args:
        call: The service call with an optional ``charger`` key
            (``"primary"``/``"second"``, default ``"primary"``), an
            optional ``operative`` key (default ``True``), and an optional
            ``connector_id`` key (default 1; 0 means the whole charge
            point). ``call.hass`` provides the Home Assistant instance.

    Raises:
        ServiceValidationError: When the coordinator or selected OCPP
            server is unavailable, or no charger is currently connected.
        HomeAssistantError: When the command fails to reach the charger.
    """
    charger_choice: str = call.data["charger"]
    operative: bool = call.data["operative"]
    connector_id: int = call.data["connector_id"]
    ocpp_server, cpid = _resolve_connected_charger(call.hass, charger_choice)

    _LOGGER.warning(
        "HSEM service: ocpp_debug_set_availability called for %s charger "
        "(cpid=%s, connector=%d) — setting %s",
        charger_choice,
        cpid,
        connector_id,
        "Operative" if operative else "Inoperative",
    )
    if not await ocpp_server.send_change_availability(
        cpid, operative=operative, connector_id=connector_id
    ):
        raise HomeAssistantError(
            f"HSEM service: failed to send ChangeAvailability to charger "
            f"'{cpid}' — see the log for details."
        )
    _LOGGER.info("HSEM service: ocpp_debug_set_availability sent for cpid=%s", cpid)


async def async_handle_ocpp_debug_set_configuration(call: ServiceCall) -> None:
    """Write one OCPP configuration key on the charger.

    Diagnostics-only (issue #920), and deliberately generic: rather than
    HSEM guessing which vendor-specific key governs a charger that ignores
    remote control, ``ocpp_debug_diagnostics`` lists the keys the charger
    actually exposes and this writes whichever one turns out to matter —
    no code change needed per charger model.

    Args:
        call: The service call with required ``key`` and ``value`` keys,
            and an optional ``charger`` key (``"primary"``/``"second"``,
            default ``"primary"``). ``call.hass`` provides the Home
            Assistant instance.

    Raises:
        ServiceValidationError: When the coordinator or selected OCPP
            server is unavailable, or no charger is currently connected.
        HomeAssistantError: When the command fails to reach the charger.
    """
    charger_choice: str = call.data["charger"]
    key: str = call.data["key"]
    value: str = call.data["value"]
    ocpp_server, cpid = _resolve_connected_charger(call.hass, charger_choice)

    _LOGGER.warning(
        "HSEM service: ocpp_debug_set_configuration called for %s charger "
        "(cpid=%s) — setting %s=%s",
        charger_choice,
        cpid,
        key,
        value,
    )
    if not await ocpp_server.send_change_configuration(cpid, key, value):
        raise HomeAssistantError(
            f"HSEM service: failed to send ChangeConfiguration to charger "
            f"'{cpid}' — see the log for details."
        )
    _LOGGER.info("HSEM service: ocpp_debug_set_configuration sent for cpid=%s", cpid)
