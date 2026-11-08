"""Tests for blocked-start recovery (issue #1383).

Overnight 2026-11-07/08 a go-e Charger V4 accepted HSEM's start and profile
and sat in ``SuspendedEVSE`` for six hours: its local ``ForceState`` was
``Off``, set from the go-e app after HSEM last read the configuration. These
tests pin the fix: ``ForceState=Neutral`` is written before every start, and a
start the charger still ignores gets a bounded recovery and a repair issue.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.const import DOMAIN
from custom_components.hsem.custom_sensors.ocpp_blocked_start import (
    BLOCKED_GRACE_S,
    BLOCKED_ISSUE_KEY,
    BLOCKED_MAX_ATTEMPTS,
    BLOCKED_RETRY_S,
    blocked_issue_id,
)
from custom_components.hsem.custom_sensors.ocpp_flap_state import FlapState
from custom_components.hsem.custom_sensors.ocpp_server import OCPPServer
from custom_components.hsem.models.ocpp_session import ChargerSession

_CPID = "222819"
_IR = "custom_components.hsem.custom_sensors.ocpp_blocked_start.ir"
_T0 = datetime(2026, 11, 8, 0, 42, tzinfo=UTC)


@pytest.fixture
def server() -> OCPPServer:
    """Return an OCPPServer with zeroed anti-flap windows."""
    return OCPPServer(
        hass=MagicMock(),
        host="127.0.0.1",
        port=19005,
        start_window_s=0,
        stop_window_s=0,
    )


@pytest.fixture
def session(server: OCPPServer) -> ChargerSession:
    """Return a connected go-e session whose cache still says Neutral."""
    charger = ChargerSession(
        cpid=_CPID,
        websocket=AsyncMock(),
        connected_at=_T0,
        vendor="go-e",
        model="go-eCharger_V4",
        configuration_keys={"ForceState": "Neutral"},
    )
    charger.status = "Preparing"
    server._chargers[_CPID] = charger
    return charger


@pytest.fixture
def ir() -> Iterator[MagicMock]:
    """Patch the issue registry used by the recovery loop."""
    with patch(_IR) as mock:
        yield mock


def _calls(session: ChargerSession) -> list[list]:
    """Return every OCPP CALL sent, in order."""
    return [json.loads(c.args[0]) for c in session.websocket.send_str.call_args_list]


def _actions(session: ChargerSession) -> list[str]:
    """Return the OCPP action of every CALL sent, in order."""
    return [msg[2] for msg in _calls(session)]


async def _start_then_block(server: OCPPServer, session: ChargerSession) -> None:
    """Start a charge at _T0, then let the charger report SuspendedEVSE.

    The charger opens a transaction, as a go-e does even while forced off,
    so the paced RemoteStart retry stays out of the way.
    """
    await server.update_charge_target(_CPID, 11.04, now=_T0)
    assert server._flap_state == FlapState.Charging
    session.transaction_id = 2
    session.status = "SuspendedEVSE"
    session.status_changed_at = _T0
    session.websocket.send_str.reset_mock()


async def _cycle(server: OCPPServer, seconds: float) -> None:
    """Run one coordinator cycle *seconds* after _T0 with the same target."""
    await server.update_charge_target(
        _CPID, 11.04, now=_T0 + timedelta(seconds=seconds)
    )


@pytest.mark.asyncio
async def test_stale_neutral_cache_still_clears_force_state_before_start(
    server: OCPPServer, session: ChargerSession
) -> None:
    """The overnight case: ForceState=Neutral is written before RemoteStart."""
    await server.update_charge_target(_CPID, 11.04, now=_T0)

    calls = _calls(session)
    actions = [msg[2] for msg in calls]
    assert actions.index("ChangeConfiguration") < actions.index(
        "RemoteStartTransaction"
    )
    change = calls[actions.index("ChangeConfiguration")]
    assert change[3] == {"key": "ForceState", "value": "Neutral"}


@pytest.mark.asyncio
async def test_no_recovery_inside_the_grace_period(
    server: OCPPServer, session: ChargerSession, ir: MagicMock
) -> None:
    """A normal start passes through SuspendedEVSE without any recovery."""
    await _start_then_block(server, session)

    await _cycle(server, BLOCKED_GRACE_S - 1)

    assert "TriggerMessage" not in _actions(session)
    ir.async_create_issue.assert_not_called()


@pytest.mark.asyncio
async def test_blocked_start_runs_the_handler_recovery(
    server: OCPPServer, session: ChargerSession, ir: MagicMock
) -> None:
    """Past the grace period the go-e recovery runs once."""
    await _start_then_block(server, session)

    await _cycle(server, BLOCKED_GRACE_S)

    recovery = [a for a in _actions(session) if a != "RemoteStartTransaction"]
    # The paced RemoteStart retry (no transaction yet) also clears
    # ForceState, so the recovery ends with its own three calls.
    assert recovery[-3:] == [
        "ChangeConfiguration",
        "GetConfiguration",
        "TriggerMessage",
    ]
    trigger = _calls(session)[-1]
    assert trigger[3] == {"requestedMessage": "StatusNotification", "connectorId": 1}
    assert server._blocked_attempts == 1


@pytest.mark.asyncio
async def test_recovery_waits_for_the_cooldown(
    server: OCPPServer, session: ChargerSession, ir: MagicMock
) -> None:
    """Cycles inside the cooldown send nothing; the next attempt waits."""
    await _start_then_block(server, session)
    await _cycle(server, BLOCKED_GRACE_S)
    session.websocket.send_str.reset_mock()

    await _cycle(server, BLOCKED_GRACE_S + BLOCKED_RETRY_S - 1)
    assert "TriggerMessage" not in _actions(session)

    await _cycle(server, BLOCKED_GRACE_S + BLOCKED_RETRY_S)
    assert "TriggerMessage" in _actions(session)
    assert server._blocked_attempts == 2


@pytest.mark.asyncio
async def test_repair_issue_after_max_attempts_raised_once(
    server: OCPPServer, session: ChargerSession, ir: MagicMock
) -> None:
    """When every attempt fails, one repair issue is raised."""
    await _start_then_block(server, session)
    at = float(BLOCKED_GRACE_S)
    for _ in range(BLOCKED_MAX_ATTEMPTS):
        await _cycle(server, at)
        at += BLOCKED_RETRY_S
    ir.async_create_issue.assert_not_called()

    await _cycle(server, at)
    await _cycle(server, at + BLOCKED_RETRY_S)

    ir.async_create_issue.assert_called_once()
    args, kwargs = ir.async_create_issue.call_args
    assert args[1:] == (DOMAIN, blocked_issue_id(_CPID))
    assert kwargs["translation_key"] == BLOCKED_ISSUE_KEY
    assert kwargs["translation_placeholders"] == {
        "cpid": _CPID,
        "status": "SuspendedEVSE",
        "handler": "go-e",
    }


@pytest.mark.asyncio
async def test_charging_clears_the_repair_issue(
    server: OCPPServer, session: ChargerSession, ir: MagicMock
) -> None:
    """Once the charger delivers power, the issue goes and the loop resets."""
    await _start_then_block(server, session)
    server._blocked_attempts = BLOCKED_MAX_ATTEMPTS
    await _cycle(server, BLOCKED_GRACE_S)
    ir.async_create_issue.assert_called_once()

    session.status = "Charging"
    await _cycle(server, BLOCKED_GRACE_S + 1)

    ir.async_delete_issue.assert_called_once_with(
        server._hass, DOMAIN, blocked_issue_id(_CPID)
    )
    assert server._blocked_attempts == 0
    assert server._blocked_issue_raised is False

    # A later clean cycle does not delete again.
    await _cycle(server, BLOCKED_GRACE_S + 2)
    ir.async_delete_issue.assert_called_once()


@pytest.mark.asyncio
async def test_zero_target_resets_the_recovery(
    server: OCPPServer, session: ChargerSession, ir: MagicMock
) -> None:
    """A planned zero ends the blocked start without a repair issue."""
    await _start_then_block(server, session)
    await _cycle(server, BLOCKED_GRACE_S)
    assert server._blocked_attempts == 1

    await server.update_charge_target(
        _CPID, 0.0, now=_T0 + timedelta(seconds=BLOCKED_GRACE_S + 1)
    )

    assert server._blocked_attempts == 0
    assert server._last_blocked_attempt is None


@pytest.mark.asyncio
async def test_each_start_gets_its_own_grace(
    server: OCPPServer, session: ChargerSession, ir: MagicMock
) -> None:
    """Entering Charging restarts the grace period, even on an old status."""
    await _start_then_block(server, session)
    # SuspendedEVSE since long before, under HSEM's own 0 A profile.
    session.status_changed_at = _T0 - timedelta(hours=1)

    await _cycle(server, BLOCKED_GRACE_S - 1)
    assert "TriggerMessage" not in _actions(session)

    await _cycle(server, BLOCKED_GRACE_S)
    assert "TriggerMessage" in _actions(session)


@pytest.mark.asyncio
async def test_unknown_status_change_time_is_not_blocked(
    server: OCPPServer, session: ChargerSession, ir: MagicMock
) -> None:
    """Without a status timestamp the grace cannot be measured, so no action."""
    await _start_then_block(server, session)
    session.status_changed_at = None

    await _cycle(server, BLOCKED_GRACE_S * 10)

    assert server._blocked_attempts == 0


@pytest.mark.asyncio
async def test_management_off_clears_a_raised_issue(
    server: OCPPServer, session: ChargerSession, ir: MagicMock
) -> None:
    """Handing the charger back also removes the repair issue."""
    await _start_then_block(server, session)
    server._blocked_attempts = BLOCKED_MAX_ATTEMPTS
    await _cycle(server, BLOCKED_GRACE_S)

    await server.update_charge_target(
        _CPID,
        0.0,
        now=_T0 + timedelta(seconds=BLOCKED_GRACE_S + 1),
        managed=False,
        management_enabled=False,
    )

    ir.async_delete_issue.assert_called_once()


class TestSendTriggerMessage:
    """``TriggerMessage`` is a plain OCPP 1.6 Remote Trigger call."""

    @pytest.mark.asyncio
    async def test_whole_charge_point(
        self, server: OCPPServer, session: ChargerSession
    ) -> None:
        """``connector_id=None`` omits the connector."""
        assert await server.send_trigger_message(_CPID, "MeterValues", None)
        assert _calls(session)[-1][2:] == [
            "TriggerMessage",
            {"requestedMessage": "MeterValues"},
        ]

    @pytest.mark.asyncio
    async def test_unknown_charger(self, server: OCPPServer) -> None:
        """A charger that is not connected sends nothing."""
        assert await server.send_trigger_message("nope", "StatusNotification") is False


@pytest.mark.asyncio
async def test_blocked_without_transaction_still_recovers(
    server: OCPPServer, session: ChargerSession, ir: MagicMock
) -> None:
    """The 60 s RemoteStart retries must not keep restarting the grace."""
    await _start_then_block(server, session)
    session.transaction_id = None

    for at in (60, 120, 180):
        await _cycle(server, at)

    assert server._blocked_attempts == 1
    assert "TriggerMessage" in _actions(session)
