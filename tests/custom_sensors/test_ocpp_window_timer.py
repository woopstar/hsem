"""Regression tests for issue #1381 — prompt OCPP starts and stops.

Force charge now is an explicit user command: it starts the charger on the
same call instead of waiting out the anti-flap start window. Any other wait in
``Starting`` or ``Stopping`` arms an ``async_call_later`` timer that re-applies
the latest target at window expiry, so the commands no longer wait for the
next coordinator cycle. The timer is cancelled when the target flips and when
the server stops.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.custom_sensors.ocpp_flap_state import FlapState
from custom_components.hsem.custom_sensors.ocpp_server import OCPPServer
from custom_components.hsem.custom_sensors.ocpp_window_timer import (
    WINDOW_TIMER_MARGIN_S,
)
from custom_components.hsem.models.ocpp_session import ChargerSession

_CPID = "test-cpid"
_TIMER = "custom_components.hsem.custom_sensors.ocpp_window_timer.async_call_later"
_START_S = 60
_STOP_S = 120


@pytest.fixture
def server() -> OCPPServer:
    """Return an OCPPServer with real (non-zero) anti-flap windows."""
    return OCPPServer(
        hass=MagicMock(),
        host="127.0.0.1",
        port=19004,
        start_window_s=_START_S,
        stop_window_s=_STOP_S,
    )


@pytest.fixture
def session(server: OCPPServer) -> ChargerSession:
    """Return a registered charger session with a mock WebSocket."""
    charger = ChargerSession(
        cpid=_CPID,
        websocket=AsyncMock(),
        connected_at=datetime.now(UTC),
    )
    charger.status = "Preparing"
    server._chargers[_CPID] = charger
    return charger


@pytest.fixture
def call_later() -> Iterator[MagicMock]:
    """Patch ``async_call_later``; each call returns its own unsub mock."""
    with patch(_TIMER, side_effect=lambda *_a, **_k: MagicMock()) as mock:
        yield mock


def _actions(session: ChargerSession) -> list[str]:
    """Return the OCPP action of every CALL sent, in order."""
    return [json.loads(c.args[0])[2] for c in session.websocket.send_str.call_args_list]


@pytest.mark.asyncio
async def test_force_charge_starts_without_waiting(
    server: OCPPServer, session: ChargerSession, call_later: MagicMock
) -> None:
    """``immediate`` sends RemoteStart + SetChargingProfile on the first call."""
    await server.update_charge_target(_CPID, 11.04, immediate=True)

    actions = _actions(session)
    assert "RemoteStartTransaction" in actions
    assert "SetChargingProfile" in actions
    assert server._flap_state == FlapState.Charging
    call_later.assert_not_called()


@pytest.mark.asyncio
async def test_surplus_start_still_waits_for_window(
    server: OCPPServer, session: ChargerSession, call_later: MagicMock
) -> None:
    """Without ``immediate`` the start window still holds the start."""
    await server.update_charge_target(_CPID, 11.04)

    assert "RemoteStartTransaction" not in _actions(session)
    assert server._flap_state == FlapState.Starting
    call_later.assert_called_once()
    assert call_later.call_args.args[1] == pytest.approx(
        _START_S + WINDOW_TIMER_MARGIN_S
    )


@pytest.mark.asyncio
async def test_timer_starts_at_window_expiry(
    server: OCPPServer, session: ChargerSession, call_later: MagicMock
) -> None:
    """The expiry callback re-applies the target and sends the start commands."""
    entered = datetime.now(UTC) - timedelta(seconds=_START_S + 1)
    await server.update_charge_target(_CPID, 11.04, now=entered)
    assert server._flap_state == FlapState.Starting

    # A later cycle inside the window keeps the armed timer.
    await server.update_charge_target(_CPID, 11.04, now=entered + timedelta(seconds=10))
    call_later.assert_called_once()

    callback = call_later.call_args.args[2]
    await callback(datetime.now(UTC))

    actions = _actions(session)
    assert "RemoteStartTransaction" in actions
    assert "SetChargingProfile" in actions
    assert server._flap_state == FlapState.Charging
    assert server._window_timer_unsub is None


@pytest.mark.asyncio
async def test_timer_stops_at_window_expiry(
    server: OCPPServer, session: ChargerSession, call_later: MagicMock
) -> None:
    """A zero target on a charging session stops it when the timer fires."""
    server._flap_state = FlapState.Charging
    session.transaction_id = 42
    entered = datetime.now(UTC) - timedelta(seconds=_STOP_S + 1)

    await server.update_charge_target(_CPID, 0.0, now=entered)
    assert server._flap_state == FlapState.Stopping
    assert "RemoteStopTransaction" not in _actions(session)
    assert call_later.call_args.args[1] == pytest.approx(
        _STOP_S + WINDOW_TIMER_MARGIN_S
    )

    await call_later.call_args.args[2](datetime.now(UTC))

    assert "RemoteStopTransaction" in _actions(session)


@pytest.mark.asyncio
async def test_target_flip_cancels_start_timer(
    server: OCPPServer, session: ChargerSession, call_later: MagicMock
) -> None:
    """Flipping back to zero cancels the start timer before it fires."""
    now = datetime.now(UTC)
    await server.update_charge_target(_CPID, 11.04, now=now)
    start_unsub = server._window_timer_unsub
    assert isinstance(start_unsub, MagicMock)

    await server.update_charge_target(_CPID, 0.0, now=now + timedelta(seconds=5))

    start_unsub.assert_called_once()
    # The flip waits on the stop window with a fresh timer of its own.
    assert server._flap_state == FlapState.Stopping
    assert server._window_timer_unsub is not start_unsub

    # A stale start callback that still fires does not start the charger.
    await call_later.call_args_list[0].args[2](datetime.now(UTC))
    assert "RemoteStartTransaction" not in _actions(session)


@pytest.mark.asyncio
async def test_stop_cancels_timer(
    server: OCPPServer, session: ChargerSession, call_later: MagicMock
) -> None:
    """Server stop (integration unload) cancels a pending window timer."""
    await server.update_charge_target(_CPID, 11.04)
    unsub = server._window_timer_unsub
    assert isinstance(unsub, MagicMock)

    await server.stop()

    unsub.assert_called_once()
    assert server._window_timer_unsub is None


@pytest.mark.asyncio
async def test_expiry_after_disconnect_is_a_noop(
    server: OCPPServer, session: ChargerSession, call_later: MagicMock
) -> None:
    """A timer firing after the charger left sends nothing."""
    await server.update_charge_target(_CPID, 11.04)
    server._chargers.clear()

    await call_later.call_args.args[2](datetime.now(UTC))

    assert "RemoteStartTransaction" not in _actions(session)
