"""Charging-profile construction and dispatch (OCPP 1.6).

Split from :mod:`ocpp_commands` to stay inside the repository's 30 KB
file limit, and cohesive on its own: everything here is about expressing
a *current limit* to a charger, whether that limit is the planner's real
target or the ``0 A`` used to stop a charge. :mod:`ocpp_commands` keeps
the transaction-level commands (start, stop, the raw CALL/CALLRESULT
plumbing); :mod:`ocpp_control` keeps the ones that interrogate or
administer the charger.

Mixes into :class:`~ocpp_server.OCPPServer` like the other mixins, so
``self._send_call`` and the capability lookups on
:class:`~ocpp_control.OCPPControlMixin` resolve there.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import Any

from custom_components.hsem.custom_sensors.ocpp_control import (
    CHARGING_RATE_UNIT_KEY,
    RATE_UNIT_AMPS,
    RATE_UNIT_PREF_AMPS,
    RATE_UNIT_PREF_WATTS,
    RATE_UNIT_WATTS,
    STATION_MAX_CURRENT_KEY,
    supported_charging_rate_units,
)
from custom_components.hsem.models.ocpp_session import ChargerSession

_LOGGER = logging.getLogger(__name__)

# Stable charging-profile IDs HSEM owns. Reused on every send so each new
# profile replaces HSEM's previous one rather than accumulating, and so
# ClearChargingProfile can remove exactly HSEM's own profiles on teardown
# without disturbing any installed by another system (issue #920).
_TX_DEFAULT_PROFILE_ID = 1
_TX_PROFILE_ID = 2

#: The charging-profile IDs HSEM owns, for teardown to clear.
HSEM_PROFILE_IDS = (_TX_DEFAULT_PROFILE_ID, _TX_PROFILE_ID)


class OCPPProfilesMixin:
    """Builds and sends the charging profiles that carry a current limit."""

    # Declared (not assigned) so mypy resolves these against OCPPServer and
    # the sibling mixins they actually live on.
    _last_sent_target: float
    _last_sent_current_a: int
    _last_sent_unit: str
    _last_sent_phases: int | None
    _charging_rate_unit: str
    _chargers: dict[str, ChargerSession]
    _send_call: Callable[[ChargerSession, str, dict], Coroutine[Any, Any, bool]]
    profile_stack_levels: Callable[[ChargerSession], tuple[int, int]]
    station_max_current_a: Callable[[ChargerSession], int | None]

    def _effective_rate_unit(self, session: ChargerSession) -> str:
        """Return the ``chargingRateUnit`` (``"W"`` or ``"A"``) to use.

        Resolves the configured preference (``hsem_ocpp_charging_rate_unit``)
        against the charger's own reported
        ``ChargingScheduleAllowedChargingRateUnit`` capability (issue #1001):

        * ``amps`` — always amps.
        * ``watts`` — always watts (a charger that never reported watt
          support gets a warning: per OCPP 1.6 it may schema-validate the
          profile and then apply nothing).
        * ``auto`` (default) — watts when the charger reports watt support,
          otherwise amps.  Watts are preferred because a watt ceiling is
          phase-agnostic: an auto-phase-switching charger maps the wattage
          to its own 1/3-phase decision with no amp ambiguity.
        """
        preference = self._charging_rate_unit
        if preference == RATE_UNIT_PREF_AMPS:
            return RATE_UNIT_AMPS
        _amps_ok, watts_ok = supported_charging_rate_units(session.configuration_keys)
        if preference == RATE_UNIT_PREF_WATTS:
            if not watts_ok:
                _LOGGER.warning(
                    "OCPP %s: watts forced but the charger never reported "
                    "watt support (%s) — the profile may be accepted and "
                    "silently ignored",
                    session.cpid,
                    CHARGING_RATE_UNIT_KEY,
                )
            return RATE_UNIT_WATTS
        return RATE_UNIT_WATTS if watts_ok else RATE_UNIT_AMPS

    def _charging_profiles(
        self,
        session: ChargerSession,
        max_current_a: int,
        *,
        target_power_w: float = 0.0,
        number_phases: int | None = None,
    ) -> tuple[dict, dict | None, str]:
        """Build the charging profile(s) expressing a charge limit.

        Shared by the planner-driven limit and by the zero-limit stop
        (issue #920) so the two can never drift in stack level, profile ID
        or kind — the only difference between "charge at 8 A" and "do not
        charge" is the number in the schedule.

        The schedule's unit is negotiated per charger (issue #1001): watts
        when the charger reports watt support (default ``auto``
        preference), amps otherwise.  Amp schedules additionally carry
        ``numberPhases`` when the caller knows the intended phase mode, so
        an amp-only auto-phase-switching charger is told whether an amp
        limit is a one-phase or a three-phase command.

        Args:
            session: The charger session.
            max_current_a: Current limit in amperes; ``0`` means "draw
                nothing".
            target_power_w: The same limit expressed in watts — used
                verbatim as the schedule limit when the negotiated unit is
                watts.
            number_phases: Intended phase count for an amp schedule
                (1 or 3), or ``None`` when unknown/irrelevant.

        Returns:
            The ``TxDefaultProfile``, a ``TxProfile`` bound to the live
            transaction when one is open (``None`` otherwise), and the
            ``chargingRateUnit`` the profiles were built with.
        """
        unit = self._effective_rate_unit(session)
        period: dict[str, Any] = {"startPeriod": 0}
        if unit == RATE_UNIT_WATTS:
            period["limit"] = max(0, int(round(target_power_w)))
        else:
            period["limit"] = max_current_a
            if number_phases is not None:
                period["numberPhases"] = number_phases
        schedule = {
            "chargingRateUnit": unit,
            "chargingSchedulePeriod": [period],
        }
        # Install at the top of the stack-level range the charger reports,
        # not the bottom (issue #920): higher levels win, so a profile at
        # level 0 loses to anything already installed.
        default_level, tx_level = self.profile_stack_levels(session)

        tx_default_profile = {
            "chargingProfileId": _TX_DEFAULT_PROFILE_ID,
            "stackLevel": default_level,
            "chargingProfilePurpose": "TxDefaultProfile",
            "chargingProfileKind": "Relative",
            "chargingSchedule": schedule,
        }
        tx_profile: dict | None = None
        if session.transaction_id is not None:
            tx_profile = {
                "chargingProfileId": _TX_PROFILE_ID,
                "stackLevel": tx_level,
                "chargingProfilePurpose": "TxProfile",
                "chargingProfileKind": "Relative",
                "transactionId": session.transaction_id,
                "chargingSchedule": schedule,
            }
        return tx_default_profile, tx_profile, unit

    async def _send_zero_current_profile(self, session: ChargerSession) -> bool:
        """Tell the charger to draw nothing, using only standard OCPP.

        The generic half of stopping a charge (issue #920). A charging
        profile with a ``0 A`` limit is the idiomatic OCPP 1.6 way for an
        energy-management system to say "do not draw power right now" — the
        same mechanism as any other limit, just at zero — and it needs no
        vendor-specific knowledge at all.

        Sent alongside ``RemoteStopTransaction`` rather than instead of it:
        ending the transaction is the correct protocol action, and this
        covers a charger that keeps delivering power afterwards.

        Deliberately does not touch :attr:`_last_sent_target` /
        :attr:`_last_sent_current_a`. Those track the *charge target* the
        anti-flap state machine is working toward; a stop already resets
        them, and recording 0 A here would let the material-change filter
        mistake the next real target for a continuation.

        Records :attr:`~models.ocpp_session.ChargerSession.
        hsem_zero_profile_active` on success so the anti-flap layer can
        later release the block exactly once if the EV becomes unmanaged
        or disconnects (issue #990).

        Args:
            session: The charger session.

        Returns:
            ``True`` if at least one profile reached the socket.
        """
        tx_default_profile, tx_profile, _unit = self._charging_profiles(
            session, 0, target_power_w=0.0
        )
        sent = await self._send_call(
            session,
            "SetChargingProfile",
            {"connectorId": 1, "csChargingProfiles": tx_default_profile},
        )
        if tx_profile is not None:
            sent = (
                await self._send_call(
                    session,
                    "SetChargingProfile",
                    {"connectorId": 1, "csChargingProfiles": tx_profile},
                )
                or sent
            )
        if sent:
            session.hsem_zero_profile_active = True
            _LOGGER.debug("Sent 0-limit charging profile to %s", session.cpid)
        return sent

    async def _send_set_charging_profile(
        self,
        session: ChargerSession,
        max_power_w: int,
        max_current_a: int = 16,
        number_phases: int | None = None,
    ) -> bool:
        """Send ``SetChargingProfile`` request(s) to limit charging.

        Always sends a ``TxDefaultProfile`` (applies to whichever transaction
        becomes active on this connector). When a transaction is already
        confirmed (:attr:`ChargerSession.transaction_id` is not ``None``),
        also sends a ``TxProfile`` bound to that transaction — mirroring the
        dual-profile strategy used by the mature ``lbbrhzn/ocpp`` Home
        Assistant integration (issue #920 follow-up), after cross-checking
        its ``ocppv16.py::set_charge_rate()``. Some chargers only actually
        throttle an *ongoing* session via a transaction-scoped ``TxProfile``,
        treating a bare ``TxDefaultProfile`` as a lower-priority default that
        doesn't override a session already running under the charger's own
        local decision — this was observed as a real charger accepting
        (``"status": "Accepted"``) a `TxDefaultProfile`-only request without
        the amp limit ever actually taking effect.

        ``chargingProfileKind`` is ``"Relative"`` for both profiles —
        matching ``lbbrhzn/ocpp``, which uses `"Relative"` universally
        (`ChargePointMaxProfile`, `TxProfile`, and `TxDefaultProfile` alike)
        across the very wide range of real charger models it's tested
        against. An earlier attempt here to use `"Absolute"` instead, on the
        theory that OCPP 1.6 §3.11 restricts `"Relative"` to `TxProfile`
        only, was not supported by this real-world evidence and has been
        reverted.

        Args:
            session: The charger session.
            max_power_w: Maximum charging power in watts.
            max_current_a: Maximum current in amperes.

        Returns:
            ``True`` if at least one profile was written to the socket.
            Bookkeeping (:attr:`_last_sent_target`, :attr:`_last_sent_current_a`)
            is only updated when at least one send succeeds (issue #892) — a
            fully failed send must not be remembered as the charger's
            current ceiling, or the material-change dedup filter would
            wrongly suppress a rightful retry.
        """
        station_max_a = self.station_max_current_a(session)
        prospective_unit = self._effective_rate_unit(session)
        if (
            prospective_unit == RATE_UNIT_AMPS
            and station_max_a is not None
            and max_current_a > station_max_a
        ):
            _LOGGER.warning(
                "OCPP %s: requested %d A but the charger caps itself at %d A "
                "(%s) — the request is valid but cannot raise the limit "
                "above the station maximum, so no change will be visible",
                session.cpid,
                max_current_a,
                station_max_a,
                STATION_MAX_CURRENT_KEY,
            )

        tx_default_profile, tx_profile, unit = self._charging_profiles(
            session,
            max_current_a,
            target_power_w=float(max_power_w),
            number_phases=number_phases,
        )
        default_sent = await self._send_call(
            session,
            "SetChargingProfile",
            {"connectorId": 1, "csChargingProfiles": tx_default_profile},
        )

        # Also bind a TxProfile to the live transaction once known, at a
        # higher stackLevel — a charger that only honours transaction-scoped
        # profiles for an already-running session still gets the limit.
        # `tx_sent` stays `None` (not `True`) when no transaction is active
        # and nothing was attempted — `default_sent or True` would otherwise
        # always evaluate to `True` even when the sole attempt failed.
        tx_sent: bool | None = None
        if tx_profile is not None:
            tx_sent = await self._send_call(
                session,
                "SetChargingProfile",
                {"connectorId": 1, "csChargingProfiles": tx_profile},
            )

        sent = default_sent or bool(tx_sent)
        if sent:
            self._last_sent_target = float(max_power_w)
            self._last_sent_current_a = max_current_a
            self._last_sent_unit = unit
            self._last_sent_phases = number_phases
            # A real limit replaces any held zero on the same profile IDs;
            # a 0-limit send through this path *is* a zero profile
            # (issue #990).
            session.hsem_zero_profile_active = max_current_a == 0
            _LOGGER.debug(
                "Sent SetChargingProfile to %s: max %d A (~%d W, unit %s)",
                session.cpid,
                max_current_a,
                max_power_w,
                unit,
            )
        return sent

    async def send_set_charging_profile(
        self,
        cpid: str,
        max_power_w: int,
        max_current_a: int = 16,
        number_phases: int | None = None,
    ) -> bool:
        """Directly send a ``SetChargingProfile`` to a charger.

        Bypasses the anti-flap state machine.  Use
        :meth:`~ocpp_server.OCPPServer.update_charge_target` for normal
        planner-driven operation.

        Wired to the ``ocpp_debug_start_charging`` service (issue #920) as a
        manual override for diagnosing a charger that won't start over
        OCPP — not used by the planner's own normal-operation path,
        which goes through :meth:`~ocpp_server.OCPPServer.update_charge_target`
        instead. Kept as public API for direct/test use.

        Args:
            cpid: Charge-point identifier.
            max_power_w: Maximum charging power in watts.
            max_current_a: Maximum current in amperes.

        Returns:
            ``True`` if the message was written to the socket.
        """
        if cpid not in self._chargers:
            _LOGGER.warning(
                "Cannot send SetChargingProfile — charger %s not connected", cpid
            )
            return False
        return await self._send_set_charging_profile(
            self._chargers[cpid], max_power_w, max_current_a, number_phases
        )


__all__ = ["HSEM_PROFILE_IDS", "OCPPProfilesMixin"]
