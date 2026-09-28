"""Regression tests for issue #1105 — management off releases the charger at once.

Turning smart charging (or the planned-load feature) off is an explicit
hand-back of the charger. It used to release HSEM's profiles only while HSEM
was *holding* a 0 A zero. If HSEM was actively charging, the zero target went
through the normal stop path instead: a 0 A profile plus
``RemoteStopTransaction``, with the release deferred until the charger
confirmed the stop (and never, if it didn't).

Now any zero target with management off clears HSEM's profile IDs exactly once
and resets the anti-flap state machine, without stopping the session.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.hsem.custom_sensors.ocpp_flap_state import FlapState
from custom_components.hsem.custom_sensors.ocpp_profiles import HSEM_PROFILE_IDS
from custom_components.hsem.custom_sensors.ocpp_server import OCPPServer
from custom_components.hsem.models.ocpp_session import ChargerSession

_CPID = "test-cpid"


@pytest.fixture
def ocpp_server() -> OCPPServer:
    """Return an OCPPServer with zeroed anti-flap windows for fast tests."""
    return OCPPServer(
        hass=MagicMock(),
        host="127.0.0.1",
        port=19003,
        start_window_s=0,
        stop_window_s=0,
    )


@pytest.fixture
def charger_session() -> ChargerSession:
    """Return a charger session with a mock WebSocket."""
    return ChargerSession(
        cpid=_CPID,
        websocket=AsyncMock(),
        connected_at=datetime.now(UTC),
    )


def _messages(session: ChargerSession) -> list[list]:
    """Return every OCPP CALL sent over the mock WebSocket, in order."""
    return [json.loads(c.args[0]) for c in session.websocket.send_str.call_args_list]


def _actions(session: ChargerSession) -> list[str]:
    return [msg[2] for msg in _messages(session)]


def _cleared_ids(session: ChargerSession) -> set[int]:
    return {
        msg[3]["id"] for msg in _messages(session) if msg[2] == "ClearChargingProfile"
    }


def _zero_limit_sent(session: ChargerSession) -> bool:
    return any(
        msg[3]["csChargingProfiles"]["chargingSchedule"]["chargingSchedulePeriod"][0][
            "limit"
        ]
        == 0
        for msg in _messages(session)
        if msg[2] == "SetChargingProfile"
    )


async def _charge(server: OCPPServer, session: ChargerSession, now: datetime) -> None:
    """Reach a managed, HSEM-started session actively charging at 10 A."""
    server._chargers[_CPID] = session
    session.status = "Charging"
    session.transaction_id = 42
    await server.update_charge_target(_CPID, 7.0, max_current_a=10, now=now)
    assert server._flap_state == FlapState.Charging
    session.websocket.send_str.reset_mock()


async def _smart_off(
    server: OCPPServer, now: datetime, *, target_kw: float = 0.0
) -> None:
    await server.update_charge_target(
        _CPID,
        target_kw,
        max_current_a=0 if target_kw <= 0 else 10,
        now=now,
        managed=False,
        management_enabled=False,
    )


def _assert_released_without_stop(server: OCPPServer, session: ChargerSession) -> None:
    actions = _actions(session)
    assert _cleared_ids(session) == set(HSEM_PROFILE_IDS)
    assert "RemoteStopTransaction" not in actions
    assert not _zero_limit_sent(session)
    assert server._flap_state == FlapState.Idle
    assert session.hsem_zero_profile_active is False
    assert server.last_requested_current_a is None


class TestManagementOffMidSession:
    """Management off releases the charger instead of stopping the car."""

    @pytest.mark.asyncio
    async def test_charging_session_is_released_not_stopped(
        self, ocpp_server: OCPPServer, charger_session: ChargerSession
    ) -> None:
        now = datetime.now(UTC)
        await _charge(ocpp_server, charger_session, now)

        await _smart_off(ocpp_server, now + timedelta(seconds=10))

        _assert_released_without_stop(ocpp_server, charger_session)
        # The session is left running under the charger's own control.
        assert charger_session.transaction_id == 42

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", [FlapState.Starting, FlapState.Stopping])
    async def test_starting_and_stopping_states_are_released(
        self,
        ocpp_server: OCPPServer,
        charger_session: ChargerSession,
        state: FlapState,
    ) -> None:
        now = datetime.now(UTC)
        await _charge(ocpp_server, charger_session, now)
        ocpp_server._flap_state = state

        await _smart_off(ocpp_server, now + timedelta(seconds=10))

        _assert_released_without_stop(ocpp_server, charger_session)

    @pytest.mark.asyncio
    async def test_stop_window_does_not_delay_the_release(
        self, charger_session: ChargerSession
    ) -> None:
        """The release is immediate even with a long anti-flap stop window."""
        server = OCPPServer(
            hass=MagicMock(),
            host="127.0.0.1",
            port=19004,
            start_window_s=0,
            stop_window_s=600,
        )
        now = datetime.now(UTC)
        await _charge(server, charger_session, now)

        await _smart_off(server, now + timedelta(seconds=1))

        _assert_released_without_stop(server, charger_session)

    @pytest.mark.asyncio
    async def test_release_is_sent_exactly_once(
        self, ocpp_server: OCPPServer, charger_session: ChargerSession
    ) -> None:
        now = datetime.now(UTC)
        await _charge(ocpp_server, charger_session, now)
        await _smart_off(ocpp_server, now + timedelta(seconds=10))
        charger_session.websocket.send_str.reset_mock()

        for i in range(3):
            await _smart_off(ocpp_server, now + timedelta(seconds=20 + 10 * i))

        charger_session.websocket.send_str.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_transaction_after_release_resends_nothing(
        self, ocpp_server: OCPPServer, charger_session: ChargerSession
    ) -> None:
        """The post-StartTransaction resend must not re-install an HSEM limit."""
        now = datetime.now(UTC)
        await _charge(ocpp_server, charger_session, now)
        await _smart_off(ocpp_server, now + timedelta(seconds=10))
        charger_session.transaction_id = None
        charger_session.websocket.send_str.reset_mock()

        await ocpp_server._handle_start_transaction(charger_session, {})
        await asyncio.sleep(0)

        assert "SetChargingProfile" not in _actions(charger_session)

    @pytest.mark.asyncio
    async def test_re_enabling_smart_charging_resumes_management(
        self, ocpp_server: OCPPServer, charger_session: ChargerSession
    ) -> None:
        now = datetime.now(UTC)
        await _charge(ocpp_server, charger_session, now)
        await _smart_off(ocpp_server, now + timedelta(seconds=10))
        charger_session.websocket.send_str.reset_mock()

        await ocpp_server.update_charge_target(
            _CPID, 7.0, max_current_a=10, now=now + timedelta(seconds=20)
        )

        assert "SetChargingProfile" in _actions(charger_session)
        assert ocpp_server._flap_state == FlapState.Charging
        assert ocpp_server.last_requested_current_a == 10

    @pytest.mark.asyncio
    async def test_force_charge_with_smart_off_still_commands_the_charger(
        self, ocpp_server: OCPPServer, charger_session: ChargerSession
    ) -> None:
        """A positive target (force charge, smart off) is a command, not a release."""
        now = datetime.now(UTC)
        await _charge(ocpp_server, charger_session, now)

        await _smart_off(ocpp_server, now + timedelta(seconds=10), target_kw=11.0)

        assert "ClearChargingProfile" not in _actions(charger_session)
        assert ocpp_server._flap_state == FlapState.Charging


class TestUnchangedBehaviour:
    """The existing managed / unmanaged guarantees are untouched."""

    @pytest.mark.asyncio
    async def test_managed_zero_still_stops_the_session(
        self, ocpp_server: OCPPServer, charger_session: ChargerSession
    ) -> None:
        """Smart charging on + plan says zero: the enforced stop path runs."""
        now = datetime.now(UTC)
        await _charge(ocpp_server, charger_session, now)

        await ocpp_server.update_charge_target(
            _CPID, 0.0, max_current_a=0, now=now + timedelta(seconds=10)
        )

        actions = _actions(charger_session)
        assert "RemoteStopTransaction" in actions
        assert _zero_limit_sent(charger_session)
        assert "ClearChargingProfile" not in actions

    @pytest.mark.asyncio
    async def test_connected_blip_keeps_the_stop_path(
        self, ocpp_server: OCPPServer, charger_session: ChargerSession
    ) -> None:
        """management_enabled=True with a car plugged in is still managed (#1018)."""
        now = datetime.now(UTC)
        await _charge(ocpp_server, charger_session, now)

        await ocpp_server.update_charge_target(
            _CPID,
            0.0,
            max_current_a=0,
            now=now + timedelta(seconds=10),
            managed=False,
            management_enabled=True,
        )

        assert "ClearChargingProfile" not in _actions(charger_session)
        assert "RemoteStopTransaction" in _actions(charger_session)

    @pytest.mark.asyncio
    async def test_untouched_charger_is_left_alone(
        self, ocpp_server: OCPPServer, charger_session: ChargerSession
    ) -> None:
        """Management off on a charger HSEM never commanded sends nothing."""
        ocpp_server._chargers[_CPID] = charger_session
        charger_session.status = "Charging"
        charger_session.transaction_id = 7

        await _smart_off(ocpp_server, datetime.now(UTC))

        charger_session.websocket.send_str.assert_not_called()
