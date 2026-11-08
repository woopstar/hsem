"""Recovery for a charger that ignores a start (issue #1383).

A charger can accept ``RemoteStartTransaction`` and ``SetChargingProfile``
and still deliver no power, sitting in ``SuspendedEVSE`` (the EVSE, not the
EV, withholding energy). On a go-e this happens while its local
``ForceState`` is ``Off``: overnight 2026-11-07/08 a planned charge sat there
for six hours with nothing but a log line to show for it.

This mixin turns that into a bounded recovery loop, generic across chargers:

1. While the anti-flap state machine is ``Charging`` with a positive profile
   sent, a connector still ``SuspendedEVSE`` after :data:`BLOCKED_GRACE_S`
   counts as a blocked start.
2. Every :data:`BLOCKED_RETRY_S` the charger's device handler runs
   :meth:`~ocpp_devices.base.ChargerHandler.recover_blocked_start`.
3. After :data:`BLOCKED_MAX_ATTEMPTS` failed attempts, a Home Assistant
   repair issue is raised so the problem is visible outside ``hsem.log``.
4. The repair issue is removed as soon as the charger leaves
   ``SuspendedEVSE`` or the target drops to zero.

``ChangeAvailability`` is deliberately not part of it: it controls
Operative/Inoperative (connector ``Unavailable``), not ``SuspendedEVSE``,
and cycling it can interrupt a running session.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.helpers import issue_registry as ir

from custom_components.hsem.const import DOMAIN
from custom_components.hsem.custom_sensors.ocpp_devices import (
    ChargerCommands,
    resolve_handler,
)
from custom_components.hsem.models.ocpp_session import ChargerSession

_LOGGER = logging.getLogger(__name__)

#: Seconds ``SuspendedEVSE`` is tolerated after a start. A healthy go-e
#: passes through it for about 15 s on a normal start.
BLOCKED_GRACE_S = 90

#: Seconds between recovery attempts.
BLOCKED_RETRY_S = 300

#: Failed attempts before the repair issue is raised.
BLOCKED_MAX_ATTEMPTS = 3

#: Translation key of the repair issue (``issues`` in translations/*.json).
BLOCKED_ISSUE_KEY = "ocpp_charger_blocked"

_BLOCKED_STATUS = "SuspendedEVSE"


def blocked_issue_id(cpid: str) -> str:
    """Return the repair-issue id for the charger *cpid*."""
    return f"{BLOCKED_ISSUE_KEY}_{cpid}"


class OCPPBlockedStartMixin:
    """Detects a start the charger ignores and drives a bounded recovery."""

    _hass: Any
    _last_sent_target: float

    # Class-level defaults, so OCPPServer.__init__ needs no extra state.
    _blocked_attempts: int = 0
    _last_blocked_attempt: datetime | None = None
    _blocked_issue_raised: bool = False
    #: When the anti-flap state machine last entered ``Charging``.
    _charging_since: datetime | None = None

    def _start_is_blocked(self, session: ChargerSession, now: datetime) -> bool:
        """Return whether a positive start has sat in ``SuspendedEVSE`` too long.

        The grace period counts from the later of the status change and the
        moment the state machine entered ``Charging``, so every start gets
        its own grace even on a connector that was already ``SuspendedEVSE``
        under HSEM's 0 A profile. It deliberately ignores the paced
        ``RemoteStartTransaction`` retries, which would restart it forever.
        """
        if session.status != _BLOCKED_STATUS or self._last_sent_target <= 0.0:
            return False
        since = session.status_changed_at
        if since is None:
            return False
        started = self._charging_since
        if started is not None and started > since:
            since = started
        return (now - since).total_seconds() >= BLOCKED_GRACE_S

    async def _check_blocked_start(
        self, session: ChargerSession, now: datetime
    ) -> None:
        """Run one blocked-start check from the ``Charging`` branch.

        Args:
            session: The charger session.
            now: Current timestamp.
        """
        if not self._start_is_blocked(session, now):
            self._clear_blocked_start(session)
            return
        last = self._last_blocked_attempt
        if last is not None and (now - last).total_seconds() < BLOCKED_RETRY_S:
            return
        if self._blocked_attempts >= BLOCKED_MAX_ATTEMPTS:
            self._raise_blocked_issue(session)
            return
        self._blocked_attempts += 1
        self._last_blocked_attempt = now
        handler = resolve_handler(session)
        _LOGGER.warning(
            "OCPP %s: charger stays '%s' despite a %.0f W start — running "
            "'%s' recovery (attempt %d of %d)",
            session.cpid,
            session.status,
            self._last_sent_target,
            handler.name,
            self._blocked_attempts,
            BLOCKED_MAX_ATTEMPTS,
        )
        commands: ChargerCommands = self  # type: ignore[assignment]  # OCPPServer implements the senders
        await handler.recover_blocked_start(commands, session)

    def _clear_blocked_start(self, session: ChargerSession) -> None:
        """Reset the recovery loop and remove the repair issue, if raised."""
        self._blocked_attempts = 0
        self._last_blocked_attempt = None
        if self._blocked_issue_raised:
            self._blocked_issue_raised = False
            ir.async_delete_issue(self._hass, DOMAIN, blocked_issue_id(session.cpid))
            _LOGGER.info("OCPP %s: blocked start resolved", session.cpid)

    def _raise_blocked_issue(self, session: ChargerSession) -> None:
        """Raise the Home Assistant repair issue once per blocked start."""
        if self._blocked_issue_raised:
            return
        self._blocked_issue_raised = True
        handler = resolve_handler(session)
        _LOGGER.error(
            "OCPP %s: charger still '%s' after %d recovery attempts — raising "
            "a repair issue",
            session.cpid,
            session.status,
            BLOCKED_MAX_ATTEMPTS,
        )
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            blocked_issue_id(session.cpid),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=BLOCKED_ISSUE_KEY,
            translation_placeholders={
                "cpid": session.cpid,
                "status": session.status,
                "handler": handler.name,
            },
        )


__all__ = [
    "BLOCKED_GRACE_S",
    "BLOCKED_ISSUE_KEY",
    "BLOCKED_MAX_ATTEMPTS",
    "BLOCKED_RETRY_S",
    "OCPPBlockedStartMixin",
    "blocked_issue_id",
]
