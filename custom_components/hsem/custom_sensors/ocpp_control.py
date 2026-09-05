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
