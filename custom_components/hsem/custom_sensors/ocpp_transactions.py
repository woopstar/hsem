"""OCPP 1.6 transaction lifecycle for the OCPP server (issue #1410).

Split out of :mod:`ocpp_message_handlers` to keep it under the 30 KB file
limit; :class:`OCPPTransactionsMixin` is inherited by
:class:`~ocpp_message_handlers.OCPPMessageHandlersMixin` and so mixes into
:class:`~ocpp_server.OCPPServer`.

:meth:`_handle_start_transaction` allocates from
:attr:`~ocpp_server.OCPPServer._next_transaction_id` (issues #906, #1404) to
assign each transaction a real, CS-issued ID rather than trusting whatever
(if anything) the charger sent, and the start and stop handlers call
:meth:`~ocpp_server.OCPPServer._notify_significant_event` (issue #908).
:meth:`_handle_start_transaction` also schedules a charging-profile resend
once the transaction ID is known (issue #920 follow-up) — a profile sent
alongside ``RemoteStartTransaction`` can only ever be a
transaction-agnostic ``TxDefaultProfile``, since the transaction ID isn't
known yet at that point. The resend runs as a background task
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
from typing import Any

from custom_components.hsem.custom_sensors.ocpp_commands import (
    _REMOTE_START_ID_TAG,
)
from custom_components.hsem.custom_sensors.ocpp_session_log import (
    OCPPSessionLogMixin,
)
from custom_components.hsem.custom_sensors.ocpp_transaction_ids import (
    is_stale_stop,
    next_transaction_id,
)
from custom_components.hsem.models.ocpp_session import ChargerSession

_LOGGER = logging.getLogger(__name__)

# Values for ChargerSession.transaction_started_by (issue #1393).
STARTED_BY_HSEM = "hsem"
STARTED_BY_LOCAL = "local"
STARTED_BY_UNKNOWN = "unknown"


class OCPPTransactionsMixin(OCPPSessionLogMixin):
    """Start, stop and adopt OCPP transactions."""

    # Declared (not assigned) so mypy uses OCPPServer.__init__'s types and
    # resolves the senders against OCPPCommandsMixin, which composes into
    # the same OCPPServer.
    _next_transaction_id: int
    _notify_significant_event: Callable[[], Coroutine[Any, Any, None]]
    _last_sent_current_a: int
    _last_sent_target: float
    _last_sent_phases: int | None
    _send_set_charging_profile: Callable[
        [ChargerSession, int, int, int | None], Coroutine[Any, Any, bool]
    ]
    _background_tasks: set[asyncio.Task[Any]]
    # Last transaction ID seen stopping per CPID, so
    # _adopt_transaction_from_meter_values() never revives a transaction
    # that has already ended (issue #920 follow-up).
    _ended_transactions: dict[str, int]

    def _drop_transaction_on_available(self, session: ChargerSession) -> None:
        """Forget a transaction the charger no longer has (issue #1404).

        A connector reporting ``Available`` has no transaction in the OCPP
        1.6 state machine. Reaching here means HSEM missed or ignored the
        ``StopTransaction``, for example one it took for stale. The ID is
        recorded as ended so trailing MeterValues cannot adopt it again.

        Args:
            session: The charger session.
        """
        transaction_id = session.transaction_id
        _LOGGER.warning(
            "OCPP %s: connector is Available but HSEM still held transaction "
            "%s; treating it as ended",
            session.cpid,
            transaction_id,
        )
        if transaction_id is not None:
            self._ended_transactions[session.cpid] = transaction_id
        session.transaction_id = None
        session.transaction_started_by = None
        session.current_power_w = 0.0
        session.phase_currents_a = {}

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
        session.transaction_started_by = STARTED_BY_UNKNOWN
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
            payload: StartTransaction payload. Only ``meterStart`` is read
                (issue #1392); the transaction ID is HSEM's own, see above.

        Returns:
            Response dict with the CS-assigned transactionId and idTagInfo.
        """
        transaction_id = next_transaction_id(self._next_transaction_id)
        self._next_transaction_id = transaction_id + 1
        session.transaction_id = transaction_id
        self._record_transaction_start(session, payload)
        self._record_started_by(session, payload.get("idTag"))
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
                    session,
                    int(self._last_sent_target),
                    self._last_sent_current_a,
                    self._last_sent_phases,
                )
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

        return {
            "transactionId": transaction_id,
            "idTagInfo": {"status": "Accepted"},
        }

    @staticmethod
    def _record_started_by(session: ChargerSession, id_tag: Any) -> None:
        """Record whether HSEM or the charger started the transaction.

        HSEM's own ``RemoteStartTransaction`` always carries
        :data:`~ocpp_commands._REMOTE_START_ID_TAG`, and the charger echoes
        it back. Any other idTag means a local start (issue #1393). This is
        diagnostic only. HSEM keeps control while smart charging is on, so
        a start or stop from the app during the day cannot cancel the
        night's planned charge, and a free-vend charger sends its own idTag
        on every plug-in anyway.

        The idTag itself is logged at debug level only, since it can be an
        RFID card number.

        Args:
            session: The charger session.
            id_tag: ``idTag`` from the StartTransaction payload.
        """
        if id_tag == _REMOTE_START_ID_TAG:
            session.transaction_started_by = STARTED_BY_HSEM
            return
        session.transaction_started_by = STARTED_BY_LOCAL
        _LOGGER.info(
            "OCPP %s: transaction started locally, not by HSEM. HSEM keeps "
            "control while smart charging is on",
            session.cpid,
        )
        _LOGGER.debug("OCPP %s: local start idTag=%s", session.cpid, id_tag)

    async def _resend_profile_after_start(
        self,
        session: ChargerSession,
        max_power_w: int,
        max_current_a: int,
        number_phases: int | None = None,
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
            await self._send_set_charging_profile(
                session, max_power_w, max_current_a, number_phases
            )
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
        if is_stale_stop(session.transaction_id, transaction_id):
            # Answered, as every CALL must be, but the running transaction
            # and its session-log data are kept (issue #1404).
            _LOGGER.warning(
                "OCPP %s: StopTransaction for tx=%s is older than the open "
                "tx=%s; ignored",
                session.cpid,
                transaction_id,
                session.transaction_id,
            )
            return {"idTagInfo": {"status": "Accepted"}}
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
        self._record_completed_session(session, payload)
        session.transaction_id = None
        # Many chargers send no MeterValues once the transaction has ended,
        # so the last reading would otherwise stay on the power sensor until
        # the next session (issue #1405). The energy register is kept.
        session.current_power_w = 0.0
        session.phase_currents_a = {}
        session.transaction_started_by = None
        await self._notify_significant_event()
        return {"idTagInfo": {"status": "Accepted"}}


__all__ = [
    "STARTED_BY_HSEM",
    "STARTED_BY_LOCAL",
    "STARTED_BY_UNKNOWN",
    "OCPPTransactionsMixin",
]
