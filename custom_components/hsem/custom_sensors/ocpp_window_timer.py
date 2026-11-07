"""Expiry timer for the anti-flap start and stop windows (issue #1381).

The anti-flap state machine in :mod:`ocpp_anti_flap` only evaluates its
windows when :meth:`~ocpp_anti_flap.OCPPAntiFlapMixin.update_charge_target`
is called, which normally happens once per coordinator cycle. Without a timer,
a target that entered ``Starting`` or ``Stopping`` waited for the window *and*
for the next cycle, up to ``hsem_update_interval`` later.

This mixin schedules one Home Assistant ``async_call_later`` per waiting
window. When it fires, the latest target the coordinator gave is re-applied,
so the start or stop commands go out at window expiry. The timer is cancelled
when the state machine leaves the waiting state, when the target flips, and
when the server stops.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from datetime import datetime
from functools import partial
from typing import Any

from homeassistant.helpers.event import async_call_later

from custom_components.hsem.custom_sensors.ocpp_flap_state import FlapState

_LOGGER = logging.getLogger(__name__)

#: Slack added to the remaining window so the re-applied call measures an
#: elapsed time at or past the window, never a few milliseconds short.
WINDOW_TIMER_MARGIN_S = 0.5


class OCPPWindowTimerMixin:
    """Re-applies the latest charge target when a start/stop window expires."""

    _hass: Any
    _flap_state: FlapState
    _chargers: dict[str, Any]

    # Class-level defaults, so OCPPServer.__init__ needs no extra state.
    _window_timer_unsub: Callable[[], None] | None = None
    _window_timer_state: FlapState | None = None
    # Identifies the armed timer, so a superseded callback does nothing.
    _window_timer_token: object | None = None
    _last_target_call: dict[str, Any] | None = None

    # Implemented by OCPPAntiFlapMixin, which subclasses this mixin.
    update_charge_target: Callable[..., Coroutine[Any, Any, None]]

    def _sync_window_timer(self, wait_s: float | None) -> None:
        """Arm, keep or cancel the window timer after a target update.

        Args:
            wait_s: Seconds left of the window the state machine is waiting
                on, or ``None`` when it is not waiting. A timer already armed
                for the current state is kept, so repeated cycles inside one
                window do not push the expiry back.
        """
        if wait_s is None:
            self._cancel_window_timer()
            return
        if (
            self._window_timer_unsub is not None
            and self._window_timer_state == self._flap_state
        ):
            return
        self._cancel_window_timer()
        token = object()
        self._window_timer_token = token
        self._window_timer_state = self._flap_state
        self._window_timer_unsub = async_call_later(
            self._hass,
            max(wait_s, 0.0) + WINDOW_TIMER_MARGIN_S,
            partial(self._async_window_expired, token),
        )

    def _cancel_window_timer(self) -> None:
        """Cancel a pending window timer, if any."""
        if self._window_timer_unsub is not None:
            self._window_timer_unsub()
        self._window_timer_unsub = None
        self._window_timer_state = None
        self._window_timer_token = None

    async def _async_window_expired(self, token: object, _now: datetime) -> None:
        """Re-apply the latest charge target once the waited-on window expires.

        Args:
            token: The token of the arming that scheduled this call. A call
                from a cancelled or superseded timer is ignored.
            _now: Fire time passed by ``async_call_later`` (unused).
        """
        if token is not self._window_timer_token:
            return
        armed_for = self._window_timer_state
        self._window_timer_unsub = None
        self._window_timer_state = None
        self._window_timer_token = None
        last = self._last_target_call
        if last is None or armed_for != self._flap_state:
            return
        if last["cpid"] not in self._chargers:
            return
        _LOGGER.debug(
            "OCPP %s: anti-flap '%s' window expired — re-applying the target",
            last["cpid"],
            armed_for,
        )
        await self.update_charge_target(**last)


__all__ = ["WINDOW_TIMER_MARGIN_S", "OCPPWindowTimerMixin"]
