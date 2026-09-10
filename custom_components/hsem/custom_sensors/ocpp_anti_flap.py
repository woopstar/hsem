"""Anti-flap charge-target state machine (OCPP 1.6).

Split from :mod:`ocpp_server` to stay inside the repository's 30 KB file
limit, and cohesive on its own: this is the layer that turns the
planner's continuously-updated charge target into *discrete* start and
stop commands, holding each one until the target has been stable long
enough to be worth acting on. Without it, a target oscillating around
zero would start and stop a physical charger every planner cycle.

The state machine is ``"idle"`` → ``"starting"`` → ``"charging"`` →
``"stopping"`` → ``"idle"``, with the start and stop windows
(:attr:`~ocpp_server.OCPPServer._start_window_s` /
``_stop_window_s``) gating each transition. Mixes into
:class:`~ocpp_server.OCPPServer`, so the timing state and the senders on
the sibling mixins resolve there.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

from custom_components.hsem.custom_sensors.ocpp_commands import (
    CHARGER_STALL_THRESHOLD_S,
    charger_appears_stalled,
)
from custom_components.hsem.custom_sensors.ocpp_profiles import HSEM_PROFILE_IDS
from custom_components.hsem.models.ocpp_session import ChargerSession

_LOGGER = logging.getLogger(__name__)

# Per-slot epsilon for floating-point comparisons (kWh)
_SLOT_EPSILON = 1e-6


class OCPPAntiFlapMixin:
    """Turns a continuous charge target into stable start/stop commands."""

    # Declared (not assigned) so mypy resolves these against OCPPServer and
    # the sibling mixins they actually live on.
    _chargers: dict[str, ChargerSession]
    _start_window_s: int
    _stop_window_s: int
    _flap_state: str
    _target_entered_at: datetime | None
    _zero_entered_at: datetime | None
    _target_power_w: float
    _last_sent_target: float
    _last_profile_retry_attempt: datetime | None
    _stalled: bool
    _stall_logged: bool
    _send_remote_start: Callable[..., Coroutine[Any, Any, bool]]
    _send_remote_stop: Callable[..., Coroutine[Any, Any, bool]]
    _send_set_charging_profile: Callable[
        [ChargerSession, int, int], Coroutine[Any, Any, bool]
    ]
    _remote_start_due: Callable[[datetime], bool]
    _remote_stop_due: Callable[[datetime], bool]
    _profile_retry_due: Callable[[datetime], bool]

    # Declared (not assigned) so mypy resolves these against
    # OCPPProfilesMixin / OCPPControlMixin, which compose into the same
    # OCPPServer — used by the connect-time pending-plan gate (issue #969).
    _send_zero_current_profile: Callable[[ChargerSession], Coroutine[Any, Any, bool]]
    send_clear_charging_profile: Callable[[str, int], Coroutine[Any, Any, bool]]
    _background_tasks: set[asyncio.Task[Any]]

    async def update_charge_target(
        self,
        cpid: str,
        target_power_kw: float,
        max_current_a: int = 16,
        now: datetime | None = None,
    ) -> None:
        """Update the charge target for a charger with anti-flap logic.

        When *target_power_kw* > 0 for longer than the start window, a
        ``SetChargingProfile`` message is sent to begin charging.  When
        *target_power_kw* == 0 for longer than the stop window, a
        ``RemoteStopTransaction`` message is sent.

        Args:
            cpid: Charge-point identifier.
            target_power_kw: Desired charging power in kW (0 = stop).
            max_current_a: Maximum charging current in amperes (used to
                build the charging profile).  Default 16 A.
            now: Current timestamp (injected for testability).
        """
        if cpid not in self._chargers:
            return

        session = self._chargers[cpid]
        if now is None:
            now = datetime.now(UTC)

        target_w = target_power_kw * 1000.0

        if session.gate_pending_plan:
            # This call is itself the signal that the planner has now had
            # its first real look at this connection — update_charge_target()
            # runs once per full coordinator cycle, and the connect-time
            # gate (issue #969) exists only to bridge the gap before that
            # first cycle. It must not outlive this one decision either
            # way, or a legitimate long-term zero allocation (smart
            # charging disabled, feature off, ...) would land right back
            # in the standing-block regression issue #920 fixed.
            session.gate_pending_plan = False
            if target_w <= _SLOT_EPSILON:
                _LOGGER.info(
                    "OCPP %s: planner's first cycle since connect allocated "
                    "no charge — releasing the transient pending-plan gate",
                    session.cpid,
                )
                await self._release_connect_gate(session)
            # target_w > 0 needs no special handling here — the normal
            # "starting" branch below installs the real profile, replacing
            # the transient 0 A one.

        # Anti-flap state machine
        if target_w > _SLOT_EPSILON:
            # Target is non-zero — handle start window
            if self._flap_state in ("idle", "stopping", "starting"):
                # Not yet charging — the stall diagnostic only applies once
                # a session is confirmed "charging" (issue #894).
                self._stalled = False
                self._stall_logged = False
                if self._flap_state != "starting":
                    self._target_entered_at = now
                    self._flap_state = "starting"
                target_at = self._target_entered_at
                if target_at is None:
                    target_at = now
                elapsed = (now - target_at).total_seconds()
                if elapsed >= self._start_window_s:
                    remote_start_ok = True
                    if session.transaction_id is None:
                        remote_start_ok = await self._send_remote_start(
                            session, now=now
                        )
                    profile_ok = await self._send_set_charging_profile(
                        session, int(target_w), max_current_a
                    )
                    if remote_start_ok and profile_ok:
                        self._flap_state = "charging"
                    else:
                        # Stay "starting" so the next cycle retries — the
                        # start window has already elapsed, so elapsed
                        # will still satisfy the threshold immediately
                        # (issue #892).
                        _LOGGER.warning(
                            "OCPP %s: failed to send start commands — "
                            "will retry next cycle",
                            session.cpid,
                        )
                else:
                    _LOGGER.debug(
                        "OCPP anti-flap: waiting for start window "
                        "(elapsed=%.1fs, needed=%ds)",
                        elapsed,
                        self._start_window_s,
                    )
            elif self._flap_state == "charging":
                # Still no confirmed transaction from the charger — the
                # first RemoteStartTransaction may have been rejected,
                # dropped, or simply never answered. Retry on a cooldown
                # rather than leaving the session stuck (issue #892).
                if session.transaction_id is None and self._remote_start_due(now):
                    await self._send_remote_start(session, now=now)
                # Already charging — update if target changed materially,
                # or retry on a cooldown if the charger's last CALLRESULT
                # rejected the profile (issue #906): a material-change
                # check alone would otherwise never resend a limit the
                # charger has already refused.
                profile_status = session.last_call_status.get("SetChargingProfile")
                material_change = abs(target_w - self._last_sent_target) > 50.0
                rejected_retry = profile_status in (
                    "Rejected",
                    "NotSupported",
                ) and self._profile_retry_due(now)
                if material_change or rejected_retry:
                    self._last_profile_retry_attempt = now
                    await self._send_set_charging_profile(
                        session, int(target_w), max_current_a
                    )

                # Stall diagnostics (issue #894): a charger stuck reporting
                # SuspendedEVSE/Faulted/Unavailable despite an open
                # transaction and a valid profile already sent is a silent
                # fault. Diagnostics-only — no corrective OCPP call.
                if charger_appears_stalled(session, now, CHARGER_STALL_THRESHOLD_S):
                    if not self._stall_logged:
                        _LOGGER.warning(
                            "OCPP %s: charger appears stalled — status "
                            "'%s' unchanged for over %ds with transaction "
                            "%s open",
                            session.cpid,
                            session.status,
                            CHARGER_STALL_THRESHOLD_S,
                            session.transaction_id,
                        )
                        self._stall_logged = True
                    self._stalled = True
                else:
                    self._stalled = False
                    self._stall_logged = False
            self._zero_entered_at = None
        else:
            # Target is zero — handle stop window
            self._stalled = False
            self._stall_logged = False
            # "stopping" must stay in this guard alongside "charging" and
            # "starting" (issue #906) — without it, once the state machine
            # entered "stopping" below, this whole block was skipped on
            # every subsequent cycle, so a stop that failed to send (or
            # that the charger silently ignored) was never retried despite
            # the "will retry next cycle" comment below.
            if self._flap_state in ("charging", "starting", "stopping"):
                if self._flap_state != "stopping":
                    self._zero_entered_at = now
                    self._flap_state = "stopping"
                zero_at = self._zero_entered_at
                if zero_at is None:
                    zero_at = now
                elapsed = (now - zero_at).total_seconds()
                if elapsed >= self._stop_window_s:
                    if session.transaction_id is None:
                        # Ground truth: the charger has already confirmed
                        # the stop via its own StopTransaction call (or
                        # there was never anything to stop) — mirrors how
                        # transaction_id becoming non-None confirms a
                        # start (issue #906). Still call _send_remote_stop()
                        # to reset its target-tracking bookkeeping; it
                        # no-ops the actual socket write in this case.
                        await self._send_remote_stop(session, now=now)
                        self._flap_state = "idle"
                    elif self._remote_stop_due(now):
                        if not await self._send_remote_stop(session, now=now):
                            _LOGGER.warning(
                                "OCPP %s: failed to send RemoteStopTransaction "
                                "— will retry next cycle",
                                session.cpid,
                            )
                else:
                    _LOGGER.debug(
                        "OCPP anti-flap: waiting for stop window "
                        "(elapsed=%.1fs, needed=%ds)",
                        elapsed,
                        self._stop_window_s,
                    )
            self._target_entered_at = None
            self._target_power_w = 0.0

    # ------------------------------------------------------------------
    # Connect-time "pending plan" gate (issue #969)
    # ------------------------------------------------------------------

    def _spawn_gate_task(self, coro: Coroutine[Any, Any, Any]) -> None:
        """Fire-and-forget a gate-related OCPP call.

        Mirrors the detached-task pattern already used for the
        post-``StartTransaction`` profile resend (issue #920 follow-up):
        never awaited inline, so a caller invoked synchronously from a
        message handler (e.g. :meth:`~ocpp_message_handlers.
        OCPPMessageHandlersMixin._handle_status_notification`) doesn't risk
        putting an extra unsolicited ``CALL`` on the wire before that
        handler's own CALLRESULT is returned.

        Args:
            coro: The coroutine to run detached.
        """
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _arm_connect_gate(self, session: ChargerSession) -> None:
        """Arm the transient pending-plan gate for a freshly connected EV.

        Called from :meth:`~ocpp_message_handlers.OCPPMessageHandlersMixin.
        _handle_status_notification` the moment a charger's own status
        leaves ``"Available"`` while HSEM's anti-flap state is still
        ``"idle"`` — the earliest signal available that a car has just been
        plugged in, ahead of some chargers' own ``StartTransaction``, which
        they send without ever asking HSEM (issue #969). Installs a 0 A
        block immediately so the charger cannot free-vend at its default
        rate before the planner's next cycle decides a real target.

        Reuses :meth:`~ocpp_profiles.OCPPProfilesMixin._send_zero_current_profile`
        — the same generic mechanism :meth:`~ocpp_commands.OCPPCommandsMixin.
        _send_remote_stop` uses — rather than a second code path. Unlike
        that stop path, this fires unconditionally on the transition since
        the whole point is to get ahead of a charger that hasn't opened a
        transaction yet.

        Args:
            session: The charger session that just left ``"Available"``.
        """
        session.gate_pending_plan = True
        _LOGGER.info(
            "OCPP %s: status left 'Available' with no plan yet — gating at "
            "0 A pending the planner's next decision",
            session.cpid,
        )
        self._spawn_gate_task(self._send_zero_current_profile(session))

    def _schedule_release_connect_gate(self, session: ChargerSession) -> None:
        """Release the pending-plan gate from a message-handler context.

        Called when the car disconnects (status returns to ``"Available"``)
        while the gate is still armed — the planner never got a chance to
        decide anything for this connection, so there is nothing left to
        gate (issue #969). Scheduled as a detached task for the same
        ordering reason as :meth:`_arm_connect_gate`.

        Args:
            session: The charger session that returned to ``"Available"``.
        """
        session.gate_pending_plan = False
        _LOGGER.info(
            "OCPP %s: disconnected before the planner decided — releasing "
            "the transient pending-plan gate",
            session.cpid,
        )
        self._spawn_gate_task(self._release_connect_gate(session))

    async def _release_connect_gate(self, session: ChargerSession) -> None:
        """Clear HSEM's own charging profiles, giving control back.

        Removes exactly the two profile IDs HSEM owns
        (:data:`~ocpp_profiles.HSEM_PROFILE_IDS`) via ``ClearChargingProfile``
        — never a blanket clear — so a profile another system installed is
        never touched (same scoping as :meth:`~ocpp_control.
        OCPPControlMixin.release_charging_profiles`, but for one charger
        instead of every connected one). Without this, the transient 0 A
        block installed by :meth:`_arm_connect_gate` would linger as a
        standing limit once the gate lifts with nothing to replace it —
        exactly the issue #920 regression this gate must not reintroduce.

        Args:
            session: The charger session to release.
        """
        for profile_id in HSEM_PROFILE_IDS:
            await self.send_clear_charging_profile(session.cpid, profile_id)


__all__ = ["OCPPAntiFlapMixin"]
