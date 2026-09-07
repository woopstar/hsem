"""OCPP 1.6 charger-initiated message handlers.

Originally extracted from :mod:`ocpp_server` to satisfy the repository's
30 KB / 1000-line file limit; these handlers mix into
:class:`~ocpp_server.OCPPServer`. Most only read/write
``session``/``payload``, but :meth:`_handle_start_transaction` also
allocates from :attr:`~ocpp_server.OCPPServer._next_transaction_id`
(issue #906) to assign each transaction a real, CS-issued ID rather than
trusting whatever (if anything) the charger sent, and the status-change,
start-transaction, and stop-transaction handlers call
:meth:`~ocpp_server.OCPPServer._notify_significant_event` (issue #908) to
trigger a debounced coordinator refresh promptly rather than waiting for
the next scheduled cycle. :meth:`_handle_start_transaction` also schedules
a charging-profile resend once the transaction ID is known (issue #920
follow-up) — a profile sent alongside ``RemoteStartTransaction`` can only
ever be a transaction-agnostic ``TxDefaultProfile``, since the transaction
ID isn't known yet at that point. The resend runs as a background task
(:attr:`~ocpp_server.OCPPServer._background_tasks`), never awaited inline
before returning — :meth:`~ocpp_server.OCPPServer._dispatch` sends this
handler's returned dict to the charger as the StartTransaction CALLRESULT
only *after* the handler coroutine returns, so awaiting extra
``SetChargingProfile`` sends first would put two unsolicited CALLs on the
wire before the charger receives the response to its own still-pending
StartTransaction request. Some charger firmware handles messages strictly
request/response and can misbehave when that ordering is violated.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

from custom_components.hsem.models.ocpp_session import ChargerSession

_LOGGER = logging.getLogger(__name__)


class OCPPMessageHandlersMixin:
    """Handlers for OCPP messages initiated by a connected charger."""

    # Declared (not assigned) so mypy uses OCPPServer.__init__'s type rather
    # than inferring one from _handle_start_transaction's usage below.
    _next_transaction_id: int

    # Declared (not assigned) so mypy resolves this against
    # OCPPCommandsMixin._notify_significant_event() rather than reporting a
    # missing attribute (issue #908).
    _notify_significant_event: Callable[[], Coroutine[Any, Any, None]]

    # Declared (not assigned) so mypy resolves these against
    # OCPPServer/OCPPCommandsMixin's attributes/method rather than reporting
    # missing attributes — used by _handle_start_transaction to re-send the
    # charging profile once the transaction ID is known (issue #920
    # follow-up).
    _last_sent_current_a: int
    _last_sent_target: float
    _send_set_charging_profile: Callable[
        [ChargerSession, int, int], Coroutine[Any, Any, bool]
    ]
    _background_tasks: set[asyncio.Task[Any]]

    # Declared (not assigned) so mypy resolves this against
    # OCPPServer.__init__'s type — last transaction ID seen stopping per
    # CPID, so _adopt_transaction_from_meter_values() never revives a
    # transaction that has already ended (issue #920 follow-up).
    _ended_transactions: dict[str, int]

    # Declared (not assigned) so mypy resolves this against
    # OCPPControlMixin, which composes into the same OCPPServer.
    send_get_configuration: Callable[[str], Coroutine[Any, Any, bool]]

    async def _handle_boot_notification(
        self, session: ChargerSession, payload: dict
    ) -> dict:
        """Handle a ``BootNotification`` request.

        Records charger identity and returns an ``Accepted`` response with
        a 300-second heartbeat interval.

        Args:
            session: The charger session.
            payload: BootNotification payload.

        Returns:
            Response dict with status, interval, and currentTime.
        """
        session.vendor = payload.get("chargePointVendor", "")
        session.model = payload.get("chargePointModel", "")
        session.firmware = payload.get("firmwareVersion", "")
        session.serial = payload.get("chargePointSerialNumber", "")
        _LOGGER.info(
            "OCPP BootNotification from %s: vendor=%s, model=%s, fw=%s, serial=%s",
            session.cpid,
            session.vendor,
            session.model,
            session.firmware,
            session.serial,
        )
        # Learn what this charger can actually do, rather than assuming
        # (issue #920): the reply drives charge-profile stack levels, the
        # station current cap, and whether a vendor "don't charge" key
        # needs clearing before a remote start can take effect. Scheduled
        # detached for the same reason as the profile resend — this
        # handler's return value is only sent as the BootNotification
        # CALLRESULT once it returns.
        task = asyncio.create_task(self._request_configuration(session))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

        return {
            "status": "Accepted",
            "interval": 300,
            "currentTime": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    async def _request_configuration(self, session: ChargerSession) -> None:
        """Background task: ask a newly-booted charger for its configuration.

        Args:
            session: The charger session.
        """
        try:
            await self.send_get_configuration(session.cpid)
        except Exception:
            _LOGGER.exception(
                "OCPP %s: GetConfiguration request after BootNotification failed",
                session.cpid,
            )

    async def _handle_heartbeat(self, session: ChargerSession, payload: dict) -> dict:
        """Handle a ``Heartbeat`` request.

        Args:
            session: The charger session.
            payload: Heartbeat payload (unused).

        Returns:
            Response dict with currentTime.
        """
        session.last_heartbeat = datetime.now(UTC)
        return {
            "currentTime": session.last_heartbeat.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    async def _handle_status_notification(
        self, session: ChargerSession, payload: dict
    ) -> dict:
        """Handle a ``StatusNotification`` request.

        Updates the charger's status based on connector status, and records
        when the status actually changed (issue #894) — a repeated
        StatusNotification carrying the same status leaves
        ``status_changed_at`` untouched, so it reflects how long the
        charger has been stuck in its current state rather than the time
        of the most recent heartbeat-style repeat.

        Args:
            session: The charger session.
            payload: StatusNotification payload.

        Returns:
            Empty dict (CALLRESULT per OCPP spec).
        """
        new_status = payload.get("status", "")
        if new_status:
            if new_status != session.status:
                session.status_changed_at = datetime.now(UTC)
                session.status = new_status
                _LOGGER.debug(
                    "OCPP charger %s status changed to '%s'", session.cpid, new_status
                )
                # Notify promptly (issue #908) — only on an actual change,
                # not a repeated StatusNotification carrying the same
                # status.
                await self._notify_significant_event()
            else:
                session.status = new_status
        return {}

    async def _handle_meter_values(
        self, session: ChargerSession, payload: dict
    ) -> dict:
        """Handle a ``MeterValues`` request.

        Parses power and energy readings from the meter values and updates
        the session state. Also adopts the charger's own ``transactionId``
        when HSEM has none recorded — see
        :meth:`_adopt_transaction_from_meter_values`.

        Args:
            session: The charger session.
            payload: MeterValues payload.

        Returns:
            ``None`` (empty response per OCPP spec).
        """
        connector_id = payload.get("connectorId", 0)
        meter_values = payload.get("meterValue", [])

        await self._adopt_transaction_from_meter_values(session, payload, meter_values)

        for mv in meter_values:
            sampled_values = mv.get("sampledValue", [])
            for sv in sampled_values:
                measurand = sv.get("measurand", "")
                value = sv.get("value", "0")
                try:
                    numeric_value = float(value)
                except ValueError, TypeError:
                    continue

                if measurand == "Power.Active.Import":
                    session.current_power_w = numeric_value
                elif measurand == "Energy.Active.Import.Register":
                    session.current_energy_wh = numeric_value
                elif measurand == "":
                    # Many chargers send power in an unlabelled field
                    unit = sv.get("unit", "")
                    if unit == "W" or unit == "":
                        session.current_power_w = numeric_value

        _LOGGER.debug(
            "OCPP MeterValues from %s (connector %d): power=%.0fW, energy=%.0fWh",
            session.cpid,
            connector_id,
            session.current_power_w,
            session.current_energy_wh,
        )
        return {}

    async def _adopt_transaction_from_meter_values(
        self, session: ChargerSession, payload: dict, meter_values: list
    ) -> None:
        """Adopt the charger's open ``transactionId`` when HSEM has none.

        Self-heal after a restart or reconnect (issue #920 follow-up).
        ``ChargerSession`` is recreated on every WebSocket connect with
        ``transaction_id = None``, and only :meth:`_handle_start_transaction`
        ever set it — so a transaction the charger opened before HSEM
        restarted was invisible to HSEM forever. That deadlocks both
        directions: ``RemoteStopTransaction`` is skipped entirely ("nothing
        to stop") so the stale transaction is never closed, and
        ``RemoteStartTransaction`` is rejected by the charger because that
        connector already has a transaction in progress — observed exactly
        this way against a go-e Charger V4, which kept reporting
        ``transactionId: 2`` on every ``MeterValues`` for 45 minutes while
        HSEM believed nothing was running.

        ``MeterValues`` is the only inbound message that routinely carries
        the live ``transactionId``, which makes it the natural recovery
        point — the same approach ``lbbrhzn/ocpp`` uses (its
        "Self-heal after restart: adopt incoming txId" branch).

        Guarded against reviving a transaction that has just ended: a
        charger's closing meter values arrive *after* its
        ``StopTransaction``, so adopting them would immediately resurrect
        the session HSEM just stopped. Both signals ``lbbrhzn/ocpp`` uses
        are checked — an explicit ``Transaction.End`` reading context, and
        the last transaction ID seen stopping on this CPID.

        Args:
            session: The charger session.
            payload: The full MeterValues payload.
            meter_values: The ``meterValue`` list from the payload.
        """
        if session.transaction_id is not None:
            return

        raw_transaction_id = payload.get("transactionId")
        if raw_transaction_id is None:
            return
        try:
            transaction_id = int(raw_transaction_id)
        except ValueError, TypeError:
            return
        if transaction_id <= 0:
            return

        if transaction_id == self._ended_transactions.get(session.cpid):
            return

        for mv in meter_values:
            for sv in mv.get("sampledValue", []):
                if sv.get("context") == "Transaction.End":
                    return

        session.transaction_id = transaction_id
        # Never hand out an ID at or below one the charger is already using:
        # _next_transaction_id restarts at 1 on every HSEM restart, so
        # without this the next StartTransaction would be assigned an ID the
        # charger has already seen (observed: HSEM assigned tx=1 moments
        # after adopting the charger's live tx=2), leaving RemoteStop
        # targeting an ambiguous ID.
        self._next_transaction_id = max(self._next_transaction_id, transaction_id + 1)
        _LOGGER.warning(
            "OCPP %s: adopted charger's already-open transaction %d from "
            "MeterValues — HSEM had none recorded (restart/reconnect). "
            "Stop commands can now target it.",
            session.cpid,
            transaction_id,
        )
        # Rare by construction (at most once per discovered stale
        # transaction), unlike MeterValues itself — so unlike the rest of
        # this handler it is worth an out-of-band refresh, on the same
        # "only on a real transition" rule as _handle_status_notification.
        await self._notify_significant_event()

    async def _handle_authorize(self, session: ChargerSession, payload: dict) -> dict:
        """Handle an ``Authorize`` request.

        Always accepts — this is a LAN-only server with no authentication.

        Args:
            session: The charger session.
            payload: Authorize payload.

        Returns:
            Response dict with idTagInfo status.
        """
        id_tag = payload.get("idTag", "unknown")
        _LOGGER.debug("OCPP Authorize from %s: idTag=%s", session.cpid, id_tag)
        return {"idTagInfo": {"status": "Accepted"}}

    async def _handle_start_transaction(
        self, session: ChargerSession, payload: dict
    ) -> dict:
        """Handle a ``StartTransaction`` request.

        Allocates a fresh transaction ID and returns it in an ``Accepted``
        response.

        Per OCPP 1.6 §5.14, ``StartTransaction.req`` (charger → CS) has no
        ``transactionId`` field — allocating one is the CS's job, returned
        in ``StartTransaction.conf``. Echoing back
        ``payload.get("transactionId", 0)`` (issue #906) meant every real
        charger, which never sends this field, got assigned ``0``. Chargers
        that treat ``0`` as an unset/sentinel value would then reject or
        ignore a subsequent ``RemoteStopTransaction`` carrying
        ``transactionId: 0``, since it never numerically matched a
        transaction they considered active.

        Args:
            session: The charger session.
            payload: StartTransaction payload (unused — see above).

        Returns:
            Response dict with the CS-assigned transactionId and idTagInfo.
        """
        transaction_id = self._next_transaction_id
        self._next_transaction_id += 1
        session.transaction_id = transaction_id
        # A fresh transaction supersedes any "already ended" marker for this
        # charger (issue #920 follow-up) — that marker only exists to stop
        # trailing meter values from reviving a closed transaction.
        self._ended_transactions.pop(session.cpid, None)
        _LOGGER.info(
            "OCPP StartTransaction from %s: assigned tx=%d",
            session.cpid,
            transaction_id,
        )
        await self._notify_significant_event()

        # Schedule a charging-profile resend now that the transaction ID is
        # known (issue #920 follow-up). A profile sent moments earlier
        # alongside RemoteStartTransaction — the normal case, since HSEM
        # authorizes before it can know what transaction ID the charger
        # will assign — could only ever be a transaction-agnostic
        # TxDefaultProfile. _send_set_charging_profile() also attaches a
        # TxProfile bound to session.transaction_id whenever it's set,
        # which some chargers require to actually throttle an
        # already-running session; this is the first point after
        # StartTransaction where that's possible.
        # _last_sent_current_a stays -1 (never set) if no profile has been
        # requested for this charger yet, in which case there's nothing to
        # reapply.
        #
        # Deliberately NOT awaited inline: this coroutine's return value is
        # sent to the charger as the StartTransaction CALLRESULT only after
        # it returns (OCPPServer._dispatch). Awaiting the profile resend
        # here first would put two unsolicited SetChargingProfile CALLs on
        # the wire before the charger gets the response to its own
        # still-pending StartTransaction request — some charger firmware
        # expects strict request/response ordering and can stop responding
        # to anything once that's violated.
        if self._last_sent_current_a >= 0:
            task = asyncio.create_task(
                self._resend_profile_after_start(
                    session, int(self._last_sent_target), self._last_sent_current_a
                )
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

        return {
            "transactionId": transaction_id,
            "idTagInfo": {"status": "Accepted"},
        }

    async def _resend_profile_after_start(
        self, session: ChargerSession, max_power_w: int, max_current_a: int
    ) -> None:
        """Background task: resend a charging profile after StartTransaction.

        Runs detached from :meth:`_handle_start_transaction` (issue #920
        follow-up) — see that method's docstring for why it must not be
        awaited inline. Exceptions are caught and logged here rather than
        left to asyncio's default handler, since nothing else awaits or
        retrieves this task's result.

        Args:
            session: The charger session.
            max_power_w: Maximum charging power in watts to re-request.
            max_current_a: Maximum charging current in amperes to re-request.
        """
        try:
            await self._send_set_charging_profile(session, max_power_w, max_current_a)
        except Exception:
            _LOGGER.exception(
                "OCPP %s: background profile resend after StartTransaction failed",
                session.cpid,
            )

    async def _handle_stop_transaction(
        self, session: ChargerSession, payload: dict
    ) -> dict:
        """Handle a ``StopTransaction`` request.

        Clears the transaction ID, records it as ended, and returns an
        ``Accepted`` response.

        The ended ID is remembered per CPID (issue #920 follow-up) so the
        closing ``MeterValues`` that arrive right after a stop can't be
        adopted by :meth:`_adopt_transaction_from_meter_values` and revive
        the transaction HSEM just closed.

        Args:
            session: The charger session.
            payload: StopTransaction payload.

        Returns:
            Response dict with idTagInfo.
        """
        transaction_id = payload.get("transactionId")
        _LOGGER.info(
            "OCPP StopTransaction from %s: tx=%s",
            session.cpid,
            transaction_id,
        )
        try:
            if transaction_id is not None:
                self._ended_transactions[session.cpid] = int(transaction_id)
        except ValueError, TypeError:
            _LOGGER.debug(
                "OCPP %s: non-numeric transactionId %r on StopTransaction — "
                "not recorded as ended",
                session.cpid,
                transaction_id,
            )
        session.transaction_id = None
        await self._notify_significant_event()
        return {"idTagInfo": {"status": "Accepted"}}

    async def _handle_unknown(
        self, session: ChargerSession, payload: dict
    ) -> dict | None:
        """Handle an unknown/unsupported OCPP action.

        Logs a warning and returns ``None`` so no CALLERROR is sent.

        Args:
            session: The charger session.
            payload: Message payload.

        Returns:
            ``None``.
        """
        _LOGGER.debug(
            "OCPP unknown action from charger %s: payload=%s",
            session.cpid,
            payload,
        )
        return None


__all__ = ["OCPPMessageHandlersMixin"]
