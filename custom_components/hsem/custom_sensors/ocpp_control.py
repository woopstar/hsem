"""Charger administration and interrogation commands (OCPP 1.6).

Split out from :mod:`ocpp_commands` — which had reached the repository's
30 KB file limit — and cohesive in its own right: :mod:`ocpp_commands`
holds the commands that *drive a charge* (start, stop, charging profile),
while this module holds the ones that *ask the charger about itself* or
change how it is operated (``GetConfiguration``, ``GetCompositeSchedule``,
``ChangeAvailability``, ``ChangeConfiguration``). Like the other mixins it
composes into :class:`~ocpp_server.OCPPServer`, so ``self._chargers`` and
:meth:`~ocpp_commands.OCPPCommandsMixin._send_call` resolve there.

These exist because of issue #920: a charger can accept every charging
command HSEM sends — ``RemoteStartTransaction``, ``SetChargingProfile``
and ``RemoteStopTransaction`` all answering ``"status": "Accepted"``, with
the charger's own ``StartTransaction``/``StopTransaction`` confirming them
— and still deliver no power, sitting in ``SuspendedEVSE``. OCPP 1.6
defines that status as the *EVSE*, not the EV, withholding energy. When
that happens the useful next question is no longer "what else can HSEM
send?" but "what does the charger think its own state is?", which only
these calls can answer.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import Any

from custom_components.hsem.models.ocpp_session import ChargerSession

_LOGGER = logging.getLogger(__name__)

# Actions whose full CALLRESULT payload is worth surfacing above DEBUG
# (issue #920). These exist purely to interrogate or take control of a
# charger that is ignoring everything else, so their answers are the
# point — not incidental protocol chatter to be buried at debug level.
DIAGNOSTIC_ACTIONS = frozenset(
    {
        "GetConfiguration",
        "GetCompositeSchedule",
        "ChangeAvailability",
        "ChangeConfiguration",
    }
)


# go-e's vendor-specific key governing whether the charge point will
# deliver power at all, independent of any OCPP transaction (issue #920).
# Confirmed against a go-e Charger V4 (firmware 60.6): it reads back
# ``Neutral`` normally and flips to ``Off`` the moment "stop charge" is
# pressed in the go-e app — and while it is ``Off`` the charger accepts
# RemoteStart/RemoteStop/SetChargingProfile and ignores all of them,
# sitting in ``SuspendedEVSE``. It is writable, so a central system can
# take back control. Keyed by name because it is vendor-specific: HSEM
# only ever touches it on a charger that reports the key in its own
# GetConfiguration reply.
FORCE_STATE_KEY = "ForceState"

#: ``ForceState`` value meaning "no local override — follow normal rules",
#: which is what lets OCPP RemoteStart/RemoteStop actually govern.
FORCE_STATE_NEUTRAL = "Neutral"

#: ``ForceState`` value meaning "do not charge", which blocks everything.
FORCE_STATE_OFF = "Off"

# The charger's own physical current ceiling, reported read-only. Asking
# for more than this is silently a no-op — the cause of "it did not set
# the amps" against a station reporting 12 A while HSEM requested 16 A.
STATION_MAX_CURRENT_KEY = "Station-MaxCurrent"

# Highest charging-profile stack level the charger accepts. Higher wins,
# so a profile installed at a low level can be overridden by any other
# profile already present. lbbrhzn/ocpp reads this key and uses the top of
# the range; HSEM previously hardcoded 0/1 — the bottom.
MAX_STACK_LEVEL_KEY = "ChargeProfileMaxStackLevel"


class OCPPControlMixin:
    """Senders that interrogate or administer a connected charger."""

    # Declared (not assigned) so mypy resolves these against OCPPServer /
    # OCPPCommandsMixin rather than reporting missing attributes.
    _chargers: dict[str, ChargerSession]
    _send_call: Callable[[ChargerSession, str, dict], Coroutine[Any, Any, bool]]

    def _require_session(self, cpid: str, action: str) -> ChargerSession | None:
        """Return the session for *cpid*, logging a warning when absent.

        Args:
            cpid: Charge-point identifier.
            action: OCPP action name, for the warning message.

        Returns:
            The :class:`ChargerSession`, or ``None`` when not connected.
        """
        session = self._chargers.get(cpid)
        if session is None:
            _LOGGER.warning("Cannot send %s — charger %s not connected", action, cpid)
        return session

    async def send_get_configuration(self, cpid: str) -> bool:
        """Ask the charger to dump its full OCPP configuration.

        Diagnostic only (issue #920). The reply settles questions HSEM
        otherwise has to guess at when a charger accepts every command but
        never delivers power — most importantly ``SupportedFeatureProfiles``
        (does it implement SmartCharging at all?) and
        ``ChargingScheduleAllowedChargingRateUnit`` (does it want amps or
        watts?). HSEM hardcodes ``chargingRateUnit: "A"``, which a watt-only
        charger can accept as schema-valid and then apply as nothing;
        ``lbbrhzn/ocpp`` queries this key before choosing a unit rather than
        assuming. It also lists any *vendor-specific* keys, which is where a
        charger's "who is allowed to control charging" setting usually
        lives.

        Args:
            cpid: Charge-point identifier.

        Returns:
            ``True`` if the request was written to the socket.
        """
        session = self._require_session(cpid, "GetConfiguration")
        if session is None:
            return False
        # An empty/absent key list means "return every key" per OCPP 1.6.
        return await self._send_call(session, "GetConfiguration", {})

    async def send_get_composite_schedule(
        self, cpid: str, duration_s: int = 3600, connector_id: int = 1
    ) -> bool:
        """Ask the charger for the charging limit it has actually computed.

        Diagnostic only (issue #920), and the most direct question
        available: ``GetCompositeSchedule`` returns the charger's own
        combined view of every charging profile installed on a connector.
        It distinguishes the two possibilities a ``"status": "Accepted"`` on
        ``SetChargingProfile`` cannot — a profile accepted *and applied*
        (schedule reports the requested amps) versus one accepted and
        silently ignored or clamped to zero (schedule reports 0, or the
        call is rejected outright).

        Args:
            cpid: Charge-point identifier.
            duration_s: Length of schedule to report, in seconds.
            connector_id: Connector to report on.

        Returns:
            ``True`` if the request was written to the socket.
        """
        session = self._require_session(cpid, "GetCompositeSchedule")
        if session is None:
            return False
        return await self._send_call(
            session,
            "GetCompositeSchedule",
            {
                "connectorId": connector_id,
                "duration": duration_s,
                "chargingRateUnit": "A",
            },
        )

    async def send_change_availability(
        self, cpid: str, operative: bool = True, connector_id: int = 1
    ) -> bool:
        """Set a connector Operative or Inoperative.

        The standard OCPP 1.6 lever for a central system to take a
        connector into or out of service (issue #920) — the same command
        behind ``lbbrhzn/ocpp``'s "Availability" switch, which HSEM had no
        equivalent of.

        Worth knowing what this does *not* do: ``Inoperative`` maps to
        connector status ``"Unavailable"``, a different thing from
        ``"SuspendedEVSE"``. A charger already Operative but locally
        refusing to energise will answer ``"Accepted"`` here and change
        nothing, because the block isn't an availability block. That
        outcome is itself informative — it rules availability out and
        points at a charger-local setting instead.

        Args:
            cpid: Charge-point identifier.
            operative: ``True`` for Operative, ``False`` for Inoperative.
            connector_id: Connector to change; ``0`` means the whole
                charge point.

        Returns:
            ``True`` if the request was written to the socket.
        """
        session = self._require_session(cpid, "ChangeAvailability")
        if session is None:
            return False
        return await self._send_call(
            session,
            "ChangeAvailability",
            {
                "connectorId": connector_id,
                "type": "Operative" if operative else "Inoperative",
            },
        )

    def absorb_configuration_reply(
        self, session: ChargerSession, payload: dict
    ) -> None:
        """Record a ``GetConfiguration`` reply on the session.

        Lets HSEM read a charger's real capabilities instead of assuming
        them (issue #920) — see :attr:`ChargerSession.configuration_keys`.

        Args:
            session: The charger session.
            payload: The ``GetConfiguration`` CALLRESULT payload.
        """
        entries = payload.get("configurationKey")
        if not isinstance(entries, list):
            return
        found: dict[str, str] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            key = entry.get("key")
            if isinstance(key, str):
                found[key] = str(entry.get("value", ""))
        if not found:
            return
        session.configuration_keys = found
        _LOGGER.info(
            "OCPP %s: recorded %d configuration keys (%s=%s, %s=%s, %s=%s)",
            session.cpid,
            len(found),
            MAX_STACK_LEVEL_KEY,
            found.get(MAX_STACK_LEVEL_KEY, "?"),
            STATION_MAX_CURRENT_KEY,
            found.get(STATION_MAX_CURRENT_KEY, "?"),
            FORCE_STATE_KEY,
            found.get(FORCE_STATE_KEY, "n/a"),
        )

    async def ensure_charging_allowed(self, session: ChargerSession) -> None:
        """Clear a charger-local block that would veto remote control.

        HSEM "taking over" the charger (issue #920). A go-e parks
        :data:`FORCE_STATE_KEY` at :data:`FORCE_STATE_OFF` when charging is
        stopped from its own app, and while it sits there the charger
        accepts every OCPP command and obeys none of them. Nothing in the
        OCPP core profile clears that — ``ChangeAvailability`` addresses a
        different axis (Operative/Inoperative, i.e. connector status
        ``Unavailable``, not ``SuspendedEVSE``) — so it has to be written
        back through the vendor key.

        No-ops on any charger that doesn't report the key, and on one
        already in a permissive state, so this is safe to call before every
        start.

        Args:
            session: The charger session.
        """
        current = session.configuration_keys.get(FORCE_STATE_KEY)
        if current is None or current != FORCE_STATE_OFF:
            return
        _LOGGER.warning(
            "OCPP %s: charger is locally forced off (%s=%s) — clearing to "
            "%s so remote start can take effect",
            session.cpid,
            FORCE_STATE_KEY,
            current,
            FORCE_STATE_NEUTRAL,
        )
        if await self.send_change_configuration(
            session.cpid, FORCE_STATE_KEY, FORCE_STATE_NEUTRAL
        ):
            # Optimistic local update; the next GetConfiguration confirms it.
            session.configuration_keys[FORCE_STATE_KEY] = FORCE_STATE_NEUTRAL

    def profile_stack_levels(self, session: ChargerSession) -> tuple[int, int]:
        """Return ``(tx_default_level, tx_profile_level)`` for this charger.

        Higher stack levels win, so HSEM's profiles are installed at the
        top of the range the charger advertises rather than at the bottom
        (issue #920) — otherwise any profile already present outranks them.
        The transaction-scoped ``TxProfile`` sits one above the
        transaction-agnostic ``TxDefaultProfile``, matching
        ``lbbrhzn/ocpp``'s ordering.

        Falls back to the previous ``0``/``1`` when the charger hasn't
        reported :data:`MAX_STACK_LEVEL_KEY`.

        Args:
            session: The charger session.

        Returns:
            Stack levels for the TxDefaultProfile and TxProfile.
        """
        raw = session.configuration_keys.get(MAX_STACK_LEVEL_KEY)
        try:
            top = int(raw) if raw is not None else 1
        except ValueError:
            top = 1
        if top < 1:
            return 0, max(top, 0)
        return top - 1, top

    def station_max_current_a(self, session: ChargerSession) -> int | None:
        """Return the charger's own current ceiling, if it reports one.

        Args:
            session: The charger session.

        Returns:
            The cap in amperes, or ``None`` when unknown.
        """
        raw = session.configuration_keys.get(STATION_MAX_CURRENT_KEY)
        if raw is None:
            return None
        try:
            return int(float(raw))
        except ValueError:
            return None

    async def send_change_configuration(self, cpid: str, key: str, value: str) -> bool:
        """Write one OCPP configuration key on the charger.

        Deliberately generic (issue #920): rather than HSEM guessing which
        vendor-specific key governs a charger that ignores remote control,
        :meth:`send_get_configuration` lists the keys the charger actually
        exposes and this writes whichever one turns out to matter — no code
        change per charger model.

        A ``"Rejected"`` or ``"NotSupported"`` reply is as useful as an
        acceptance here, and either way the full response is logged.

        Args:
            cpid: Charge-point identifier.
            key: Configuration key name, exactly as the charger reports it.
            value: New value, as a string (OCPP 1.6 carries all
                configuration values as strings).

        Returns:
            ``True`` if the request was written to the socket.
        """
        session = self._require_session(cpid, "ChangeConfiguration")
        if session is None:
            return False
        return await self._send_call(
            session, "ChangeConfiguration", {"key": key, "value": value}
        )


__all__ = ["DIAGNOSTIC_ACTIONS", "OCPPControlMixin"]
