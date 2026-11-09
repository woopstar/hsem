"""Embedded OCPP 1.6 WebSocket server for EV charger control.

This module provides an optional, LAN-only OCPP 1.6 JSON WebSocket server
that listens for charger connections and dispatches
:class:`RemoteStartTransaction`/:class:`SetChargingProfile`/
:class:`RemoteStopTransaction` commands based on HSEM's EV charging plan.

Architecture::

    EV Charger ──WebSocket──▶ OCPPServer (asyncio task)
                                  │
                                  ├── Reads EV plan from CoordinatorData
                                  ├── Writes charger state to CoordinatorData
                                  └── Dispatches RemoteStart/SetChargingProfile/
                                      RemoteStop commands

.. important::
    This server binds to ``0.0.0.0`` by default.  The port MUST NOT be
    exposed to the public internet — it is LAN-only by design. Without TLS
    it performs no authentication on incoming connections; with mutual TLS
    (issue #1341) a charger must present a client certificate whose CN
    matches its charge point identity, unless ``allow_cn_mismatch`` is set.

Usage
-----
The server is managed by the HSEM coordinator:

- Created in :meth:`HSEMDataUpdateCoordinator.async_setup` when
  ``ocpp_enabled`` is ``True``.
- Stopped in :meth:`HSEMDataUpdateCoordinator.async_teardown`.
- Charge targets are updated after each planner cycle via
  :meth:`OCPPServer.update_charge_target`.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from collections import deque
from collections.abc import Callable, Coroutine
from datetime import datetime
from typing import Any

from aiohttp import web

from custom_components.hsem.custom_sensors.ocpp_anti_flap import OCPPAntiFlapMixin
from custom_components.hsem.custom_sensors.ocpp_commands import (
    CHARGER_STALL_THRESHOLD_S,
    OCPPCommandsMixin,
    charger_appears_stalled,
)
from custom_components.hsem.custom_sensors.ocpp_connection import (
    OCPPConnectionMixin,
)
from custom_components.hsem.custom_sensors.ocpp_control import (
    RATE_UNIT_PREF_AUTO,
    RATE_UNIT_PREFERENCES,
    OCPPControlMixin,
)
from custom_components.hsem.custom_sensors.ocpp_flap_state import FlapState
from custom_components.hsem.custom_sensors.ocpp_profiles import (
    HSEM_PROFILE_IDS,
    OCPPProfilesMixin,
)
from custom_components.hsem.custom_sensors.ocpp_session_log import MAX_SESSION_LOG
from custom_components.hsem.models.ocpp_session import ChargerSession

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Anti-flap defaults (seconds)
# ---------------------------------------------------------------------------
_DEFAULT_START_WINDOW_S = 60  # Sustained surplus required before starting
_DEFAULT_STOP_WINDOW_S = 180  # Sustained shortage required before stopping

# Per-slot epsilon for floating-point comparisons (kWh)
_SLOT_EPSILON = 1e-6


class OCPPServer(
    # Mixins that *define* the senders come first, so their real methods
    # win the MRO over the forward declarations the behavioural mixins
    # below carry purely so mypy can check them in isolation.
    OCPPCommandsMixin,
    OCPPControlMixin,
    OCPPProfilesMixin,
    OCPPAntiFlapMixin,
    OCPPConnectionMixin,
):
    """Embedded OCPP 1.6 WebSocket server for LAN-only EV charger control.

    Listens on a configurable TCP port and handles OCPP 1.6 JSON messages
    from one or more chargers.  Charge targets are pushed from the HSEM
    planner via :meth:`update_charge_target`. Charger-initiated message
    handlers (BootNotification, Heartbeat, etc.) live in
    :class:`OCPPMessageHandlersMixin`, the WebSocket connection and OCPP-J
    frame parsing in :class:`OCPPConnectionMixin` (issue #1402), and
    low-level outbound command senders in :class:`OCPPCommandsMixin`, all
    split out to satisfy the 30 KB / 1000-line file limit.

    Attributes:
        hass: The Home Assistant instance (used only for helper access).
        host: Bind address (default ``"0.0.0.0"``).
        port: TCP port (default ``9000``).
    """

    def __init__(
        self,
        hass: Any,
        host: str = "0.0.0.0",
        port: int = 9000,
        start_window_s: int = _DEFAULT_START_WINDOW_S,
        stop_window_s: int = _DEFAULT_STOP_WINDOW_S,
        on_significant_event: Callable[[], Coroutine[Any, Any, None]] | None = None,
        charging_rate_unit: str = RATE_UNIT_PREF_AUTO,
        ssl_context: ssl.SSLContext | None = None,
        allow_cn_mismatch: bool = False,
    ) -> None:
        """Initialise the OCPP server.

        Args:
            hass: The Home Assistant instance.
            host: Bind address.
            port: TCP port.
            start_window_s: Seconds of sustained surplus before starting
                a charge.
            stop_window_s: Seconds of sustained shortage before stopping
                a charge.
            on_significant_event: Optional async callback invoked (via
                :meth:`_notify_significant_event`) on a charger connect,
                disconnect, ``StatusNotification`` status change, or
                confirmed start/stop transaction (issue #908) — wired by
                the coordinator to a debounced refresh so
                ``sensor.hsem_ocpp_charger_status`` doesn't wait for the
                next scheduled cycle to reflect a live protocol event.
                Deliberately not invoked for ``MeterValues``/``Heartbeat``.
            charging_rate_unit: ``chargingRateUnit`` preference for charging
                profiles — one of ``auto`` (negotiate from the charger's
                reported capabilities, watts preferred), ``amps`` or
                ``watts`` (issue #1001). Unrecognised values fall back to
                ``auto``.
            ssl_context: Serve ``wss://`` with this context instead of
                plain ``ws://`` (issue #1341). When it requires client
                certificates, a charger whose certificate CN does not match
                its CPID is refused.
            allow_cn_mismatch: Accept such a charger anyway, for chargers
                whose certificate CN is their serial number rather than the
                CPID they connect with. The certificate must still chain to
                the client CA; only the identity binding is dropped.
        """
        self._hass = hass
        self._host = host
        self._port = port
        self._start_window_s = start_window_s
        self._stop_window_s = stop_window_s
        self._on_significant_event = on_significant_event
        self._ssl_context = ssl_context
        self._allow_cn_mismatch = allow_cn_mismatch
        self._charging_rate_unit: str = (
            charging_rate_unit
            if charging_rate_unit in RATE_UNIT_PREFERENCES
            else RATE_UNIT_PREF_AUTO
        )

        # Runtime state
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._chargers: dict[str, ChargerSession] = {}

        # Charge target tracking (anti-flap)
        self._target_entered_at: datetime | None = None
        self._zero_entered_at: datetime | None = None
        self._last_sent_target: float = -1.0  # Track last sent to avoid duplicates
        self._last_sent_current_a: int = -1  # Last requested amps, -1 = none sent
        # Last unit the profiles were built with ("W"/"A", "" = none sent):
        # a unit change at the same wattage must not be deduped away.
        self._last_sent_unit: str = ""
        # Last phase mode published in an amp profile (1/3, None = none or
        # not applicable) — replayed by the StartTransaction resend so a
        # TxProfile keeps the phase intent of the original send.
        self._last_sent_phases: int | None = None

        # Anti-flap state machine: "idle", "starting", "charging", "stopping"
        self._flap_state: FlapState = FlapState.Idle

        # RemoteStartTransaction retry tracking (issue #892). HSEM never
        # correlates OCPP CALLRESULTs to a specific outbound request, so the
        # only reliable "did it actually start" signal is the charger's own
        # subsequent StartTransaction call (session.transaction_id). While
        # that stays None after a start attempt, retry on a cooldown rather
        # than assuming the single attempt succeeded.
        self._last_remote_start_attempt: datetime | None = None

        # Same pacing for RemoteStopTransaction retries (issue #906) — see
        # _last_remote_start_attempt above; the ground-truth confirmation
        # that a stop actually happened is session.transaction_id becoming
        # None via the charger's own subsequent StopTransaction call.
        self._last_remote_stop_attempt: datetime | None = None

        # Pacing for SetChargingProfile retries triggered by a "Rejected"/
        # "NotSupported" CALLRESULT (issue #906), so a charger that keeps
        # rejecting the profile isn't resent it on every single coordinator
        # cycle while waiting for a fresh CALLRESULT to arrive.
        self._last_profile_retry_attempt: datetime | None = None

        # Monotonically increasing transaction ID allocator (issue #906).
        # OCPP 1.6 requires the CS to assign transaction IDs on
        # StartTransaction — a charger never sends one in its request.
        self._next_transaction_id: int = 1

        # Last transaction ID seen stopping, per CPID (issue #920
        # follow-up). Deliberately server-level, not per ChargerSession:
        # a session object is recreated on every reconnect, but this must
        # outlive that to stop _handle_meter_values() re-adopting a
        # transaction that has already ended.
        self._ended_transactions: dict[str, int] = {}

        # Charger-stall diagnostics (issue #894): whether the active
        # charging session currently appears stuck non-"Charging" despite
        # an open transaction, and whether the warning has already been
        # logged for the current stall (so it logs once, not every cycle).
        self._stalled: bool = False
        self._stall_logged: bool = False

        # Strong references for fire-and-forget background tasks (issue
        # #920 follow-up) — e.g. the post-StartTransaction profile resend,
        # which must not be awaited inline before the StartTransaction
        # CALLRESULT is sent (see OCPPMessageHandlersMixin._handle_start_transaction).
        # Without a strong reference, asyncio may garbage-collect a task
        # before it completes.
        self._background_tasks: set[asyncio.Task[Any]] = set()
        # Completed sessions, newest last (issue #1392).
        self._completed_sessions: deque[dict[str, Any]] = deque(maxlen=MAX_SESSION_LOG)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the aiohttp WebSocket server.

        Creates an :class:`aiohttp.web.Application` with a wildcard route
        that upgrades any path to WebSocket and delegates to
        :meth:`_handle_charger`, which derives the charge-point identifier
        from the connection path (``/`` → ``"default"``, ``/222819`` →
        ``"222819"``). A literal ``/`` route alone would 404 any charger
        connecting with its own CPID in the path (issue #892).
        """
        app = web.Application()
        app.router.add_get("/{tail:.*}", self._handle_charger)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(
            self._runner, self._host, self._port, ssl_context=self._ssl_context
        )
        await self._site.start()
        _LOGGER.info(
            "OCPP server started on %s://%s:%d%s (LAN-only — do not expose to "
            "internet)",
            "ws" if self._ssl_context is None else "wss",
            self._host,
            self._port,
            ", client certificates required" if self._requires_client_cert else "",
        )

    async def stop(self) -> None:
        """Stop the server and close all charger connections.

        Removes HSEM's own charging profiles first (issue #920): a
        ``TxDefaultProfile`` **persists on the charger** past HSEM's own
        lifetime, so an unloaded HSEM must not leave a connector throttled
        to 0 A with nothing left in HA to explain why. This runs before the
        sockets close, since it needs them.
        """
        self._cancel_window_timer()
        await self.release_charging_profiles(HSEM_PROFILE_IDS)

        # Close all charger sessions
        for cpid, session in list(self._chargers.items()):
            try:
                if session.websocket is not None:
                    await session.websocket.close()
            except Exception:
                _LOGGER.debug("Error closing charger %s WebSocket — ignoring", cpid)
        self._chargers.clear()

        # Stop the aiohttp site
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        _LOGGER.info("OCPP server stopped")

    @property
    def host(self) -> str:
        """Return the configured bind address."""
        return self._host

    @property
    def port(self) -> int:
        """Return the configured TCP port."""
        return self._port

    @property
    def is_listening(self) -> bool:
        """Return True while the aiohttp WebSocket site is bound and active."""
        return self._site is not None

    @property
    def last_requested_current_a(self) -> int | None:
        """Return the amperage in the last ``SetChargingProfile`` sent.

        ``None`` until the first profile has been sent to a connected
        charger this session.
        """
        return self._last_sent_current_a if self._last_sent_current_a >= 0 else None

    @property
    def anti_flap_state(self) -> str:
        """Return the anti-flap state machine's current state.

        One of the :class:`~custom_sensors.ocpp_flap_state.FlapState` values:
        ``"idle"``, ``"starting"``, ``"charging"``, ``"stopping"``.
        Exposed for diagnostics (issue #892) — without this, diagnosing why
        a charger isn't starting/stopping requires reading source code to
        understand HSEM's own internal state.

        Coerced with ``str()`` so the value published as an entity attribute
        stays a plain string rather than a ``FlapState`` member.  ``StrEnum``
        would serialise identically either way; this keeps it explicit.
        """
        return str(self._flap_state)

    @property
    def is_stalled(self) -> bool:
        """Return whether the active charging session appears stalled.

        ``True`` while :func:`charger_appears_stalled` has held for the
        connected charger during the most recent :meth:`update_charge_target`
        call (issue #894) — the charger is reporting a non-"Charging" status
        despite an open transaction. Diagnostics-only; never triggers a
        corrective OCPP call.
        """
        return self._stalled

    @property
    def charger_sessions(self) -> dict[str, ChargerSession]:
        """Return a copy of the current charger sessions dict."""
        return dict(self._chargers)

    @property
    def active_chargers(self) -> list[str]:
        """Return list of CPIDs for currently connected chargers.

        Manual/diagnostic accessor — no HA service currently exposes this
        (see issue #843). Kept as public API for direct/test use alongside
        :meth:`send_set_charging_profile` and :meth:`send_remote_stop`.
        """
        return list(self._chargers.keys())


# Re-exported so the stall diagnostic keeps its documented home here even
# though the helper itself lives in ocpp_commands and is applied by the
# anti-flap mixin — ocpp_sensors and coordinator_data both reference
# `ocpp_server.charger_appears_stalled` by that path.
__all__ = [
    "CHARGER_STALL_THRESHOLD_S",
    "OCPPServer",
    "charger_appears_stalled",
]
