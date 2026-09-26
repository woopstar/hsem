"""Anti-flap charge-target state machine (OCPP 1.6).

Split from :mod:`ocpp_server` to stay inside the repository's 30 KB file
limit, and cohesive on its own: this is the layer that turns the
planner's continuously-updated charge target into *discrete* start and
stop commands, holding each one until the target has been stable long
enough to be worth acting on. Without it, a target oscillating around
zero would start and stop a physical charger every planner cycle.

The state machine is :class:`FlapState` — ``Idle`` → ``Starting`` →
``Charging`` → ``Stopping`` → ``Idle`` — with the start and stop windows
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
    connector_has_car,
)
from custom_components.hsem.custom_sensors.ocpp_flap_state import FlapState
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
    _flap_state: FlapState
    _target_entered_at: datetime | None
    _zero_entered_at: datetime | None
    _last_sent_target: float
    _last_sent_current_a: int
    _last_sent_unit: str
    _last_sent_phases: int | None
    _last_profile_retry_attempt: datetime | None
    _stalled: bool
    _stall_logged: bool
    _send_remote_start: Callable[..., Coroutine[Any, Any, bool]]
    _send_remote_stop: Callable[..., Coroutine[Any, Any, bool]]
    _send_set_charging_profile: Callable[
        [ChargerSession, int, int, int | None], Coroutine[Any, Any, bool]
    ]
    _effective_rate_unit: Callable[[ChargerSession], str]
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
        managed: bool = True,
        number_phases: int | None = None,
        management_enabled: bool | None = None,
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
            managed: ``True`` while HSEM is responsible for this EV —
                planned-load feature enabled, car connected, and smart
                charging switched on (issue #990). A planned zero on a
                managed EV is an *enforced* zero (a 0 A profile is held or
                installed), while a zero on an unmanaged EV means "HSEM
                has no opinion" and releases HSEM's profiles exactly once
                so no standing 0 A block remains (issue #920).
            number_phases: Intended phase mode for an amp-unit profile
                (1 or 3, issue #1001) — included as the schedule period's
                ``numberPhases`` so an amp-only auto-phase-switching
                charger is told whether the amp limit is a one-phase or a
                three-phase command.  ``None`` omits the field (unknown,
                or a watt-unit profile where phases are irrelevant).
            management_enabled: ``True`` while the planned-load feature is
                enabled and smart charging is on — *managed* without the
                car-connected reading (issue #1018). While it holds and the
                charger itself reports a car plugged in, the EV stays
                managed even if the Home Assistant "connected" entity says
                otherwise, so a one-cycle blip cannot release an enforced
                zero. ``None`` treats it as equal to *managed*.
        """
        if cpid not in self._chargers:
            return

        session = self._chargers[cpid]
        if now is None:
            now = datetime.now(UTC)

        target_w = target_power_kw * 1000.0

        # A car the charger itself reports plugged in is still managed while
        # the user has HSEM managing this EV, whatever a single Home Assistant
        # "connected" reading says (issue #1018). Otherwise a one-cycle blip
        # of that entity releases the enforced 0 A profile, and the car
        # immediately charges at full power from grid. Turning the feature
        # or smart charging off still relinquishes the charger, and a real
        # unplug is released by the StatusNotification "Available" path.
        if management_enabled is None:
            management_enabled = managed
        # Turning the feature or smart charging off is an explicit hand-back
        # (issue #1105): with nothing to command, release the charger at once,
        # even mid-session, instead of stopping the car first. A positive
        # target (force charge with smart charging off) still commands it.
        if (
            not management_enabled
            and target_w <= _SLOT_EPSILON
            and self._holds_charger_control(session)
        ):
            _LOGGER.info(
                "OCPP %s: EV management is off (flap state '%s') — releasing "
                "the charger without stopping the session",
                session.cpid,
                self._flap_state,
            )
            await self._relinquish_charger_control(session)
            return

        if management_enabled and not managed and connector_has_car(session):
            _LOGGER.debug(
                "OCPP %s: EV reads disconnected but the connector reports '%s' "
                "— keeping it managed",
                session.cpid,
                session.status,
            )
            managed = True

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
                if managed:
                    # "HSEM wants zero" is not "HSEM has no opinion"
                    # (issue #990): on a managed, plugged-in EV the planned
                    # zero is an *enforced* zero — hold the 0 A profile
                    # instead of clearing it. Re-affirm the profile here
                    # so an arm-time send that failed or is still in flight
                    # cannot leave the charger unlimited.
                    _LOGGER.info(
                        "OCPP %s: planner's first cycle since connect "
                        "allocated no charge — holding the enforced 0 A "
                        "profile for a managed EV",
                        session.cpid,
                    )
                    await self._send_zero_current_profile(session)
                else:
                    _LOGGER.info(
                        "OCPP %s: planner's first cycle since connect "
                        "allocated no charge for an unmanaged EV — "
                        "releasing the transient pending-plan gate",
                        session.cpid,
                    )
                    await self._release_connect_gate(session)
            # target_w > 0 needs no special handling here — the normal
            # "starting" branch below installs the real profile, replacing
            # the transient 0 A one.

        # Anti-flap state machine
        if target_w > _SLOT_EPSILON:
            # Target is non-zero — handle start window
            if self._flap_state in (
                FlapState.Idle,
                FlapState.Stopping,
                FlapState.Starting,
            ):
                # Not yet charging — the stall diagnostic only applies once
                # a session is confirmed "charging" (issue #894).
                self._stalled = False
                self._stall_logged = False
                if self._flap_state != FlapState.Starting:
                    self._target_entered_at = now
                    self._flap_state = FlapState.Starting
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
                        session, int(target_w), max_current_a, number_phases
                    )
                    if remote_start_ok and profile_ok:
                        self._flap_state = FlapState.Charging
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
            elif self._flap_state == FlapState.Charging:
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
                # A unit renegotiation (the charger's GetConfiguration reply
                # arriving after the first send, or a config change) at the
                # same wattage is still a material change: the A-profile must
                # be replaced by the W-profile or vice versa (issue #1001).
                # An empty _last_sent_unit means no successful profile send
                # has been recorded yet — never a reason to resend.
                material_change = abs(target_w - self._last_sent_target) > 50.0 or (
                    bool(self._last_sent_unit)
                    and self._effective_rate_unit(session) != self._last_sent_unit
                )
                rejected_retry = profile_status in (
                    "Rejected",
                    "NotSupported",
                ) and self._profile_retry_due(now)
                if material_change or rejected_retry:
                    self._last_profile_retry_attempt = now
                    await self._send_set_charging_profile(
                        session, int(target_w), max_current_a, number_phases
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
            #
            # A charger-initiated session (free-vend) is admitted via
            # ``free_vend`` (issue #990): _flap_state only leaves Idle
            # when HSEM itself starts a session, but a charger that opens
            # its own StartTransaction still carries an open
            # transaction_id — and a managed EV whose plan says zero must
            # be driven to zero regardless of who started the session.
            # Gated on ``managed`` so a locally started charge on an
            # unmanaged EV (smart charging off) is left alone.
            #
            # Some chargers resume ``Charging`` with no StartTransaction at
            # all (issue #1018: a go-eCharger after its profile was cleared),
            # so a connector reporting ``Charging`` counts as a session too —
            # mirroring ``_send_remote_stop``, which already sends the 0 A
            # profile in exactly that case.
            #
            # A managed EV whose zero is not currently enforced gets its 0 A
            # profile back immediately (issue #1018) — without waiting for the
            # stop window, which exists to damp HSEM's own sessions, not one
            # it never started. Only on a connector with a car plugged in: a
            # TxDefaultProfile persists, and writing 0 A onto an idle connector
            # would be the standing block issue #920 removed.
            if (
                managed
                and self._flap_state == FlapState.Idle
                and not session.hsem_zero_profile_active
                and connector_has_car(session)
            ):
                _LOGGER.info(
                    "OCPP %s: plugged-in managed EV has no enforced 0 A profile "
                    "while the plan allocates zero — re-installing it",
                    session.cpid,
                )
                await self._send_zero_current_profile(session)
            free_vend = (
                managed
                and self._flap_state == FlapState.Idle
                and (session.transaction_id is not None or session.status == "Charging")
            )
            if (
                self._flap_state
                in (FlapState.Charging, FlapState.Starting, FlapState.Stopping)
                or free_vend
            ):
                if free_vend and self._flap_state == FlapState.Idle:
                    _LOGGER.info(
                        "OCPP %s: charger-initiated transaction %s is "
                        "running while the plan allocates zero — driving "
                        "it to zero",
                        session.cpid,
                        session.transaction_id,
                    )
                if self._flap_state != FlapState.Stopping:
                    self._zero_entered_at = now
                    self._flap_state = FlapState.Stopping
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
                        self._flap_state = FlapState.Idle
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
            elif not managed and session.hsem_zero_profile_active:
                # The EV became unmanaged (feature off, smart charging
                # off, or the car reads disconnected and the charger agrees
                # there is no car — see issue #1018 above) while HSEM was
                # holding an enforced zero. Release it
                # exactly once — leaving the 0 A profile standing would be
                # the issue #920 regression, and repeating the clear every
                # cycle would spam the charger (issue #990).
                _LOGGER.info(
                    "OCPP %s: EV is no longer managed — releasing the held 0 A profile",
                    session.cpid,
                )
                await self._release_connect_gate(session)
            self._target_entered_at = None

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
        """Release HSEM's zero block from a message-handler context.

        Called when the car disconnects (connector-level status returns to
        ``"Available"``) while either the pending-plan gate is still armed
        — the planner never got a chance to decide anything for this
        connection (issue #969) — or HSEM was holding an enforced zero the
        car will no longer consume (issue #990). In both cases there is
        nothing left to gate, and leaving the 0 A profile standing on an
        idle connector would be the issue #920 regression. Scheduled as a
        detached task for the same ordering reason as
        :meth:`_arm_connect_gate`.

        Args:
            session: The charger session that returned to ``"Available"``.
        """
        was_pending = session.gate_pending_plan
        session.gate_pending_plan = False
        _LOGGER.info(
            "OCPP %s: disconnected %s — releasing the 0 A block",
            session.cpid,
            "before the planner decided"
            if was_pending
            else "while held at an enforced zero",
        )
        self._spawn_gate_task(self._release_connect_gate(session))

    def _holds_charger_control(self, session: ChargerSession) -> bool:
        """Return whether HSEM has a profile installed or a session in flight."""
        return (
            self._flap_state != FlapState.Idle
            or session.hsem_zero_profile_active
            or session.gate_pending_plan
            or self._last_sent_current_a >= 0
        )

    async def _relinquish_charger_control(self, session: ChargerSession) -> None:
        """Clear HSEM's profiles and reset the state machine to ``Idle``.

        Used when the user switches management off (issue #1105). Unlike the
        stop path it sends no 0 A profile and no ``RemoteStopTransaction``:
        the session, if any, continues under the charger's own control.
        Resetting the last-sent bookkeeping also stops the post-
        ``StartTransaction`` resend from re-installing an HSEM limit.
        """
        session.gate_pending_plan = False
        await self._release_connect_gate(session)
        self._flap_state = FlapState.Idle
        self._target_entered_at = None
        self._zero_entered_at = None
        self._last_sent_target = -1.0
        self._last_sent_current_a = -1
        self._last_sent_unit = ""
        self._last_sent_phases = None
        self._stalled = False
        self._stall_logged = False

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
        session.hsem_zero_profile_active = False


__all__ = ["OCPPAntiFlapMixin"]
