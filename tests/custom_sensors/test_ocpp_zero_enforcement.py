"""Tests for enforcing a planned 0 W on an EV charger (issue #990).

A planned zero must be actuated as an *enforced* zero (a 0 A charging
profile), never as ``ClearChargingProfile`` — clearing is reserved for the
genuinely unmanaged cases (feature off, smart charging off, car unplugged)
so the transient connect gate (issue #969) cannot become the standing
0 A block issue #920 removed.

Covered here:

- A planned zero on a managed, plugged-in EV keeps the 0 A profile.
- A planned zero on an unmanaged EV releases HSEM's profiles (#920).
- A charger-initiated (free-vend) session HSEM never started is still
  driven to zero when the plan says zero — the zero branch must not be
  gated on HSEM's own anti-flap state having left ``"idle"``.
- ``StatusNotification`` respects ``connectorId``: connector 0 is the
  charge point itself per OCPP 1.6, never the car, so a charge-point-level
  ``Available`` must neither arm nor release the connect gate.
- A held enforced zero is released exactly once when the EV becomes
  unmanaged or disconnects — no per-cycle ``ClearChargingProfile`` spam.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.hsem.custom_sensors.ocpp_profiles import HSEM_PROFILE_IDS
from custom_components.hsem.custom_sensors.ocpp_server import OCPPServer
from custom_components.hsem.models.ocpp_session import ChargerSession


@pytest.fixture
def mock_hass():
    """Return a mock Home Assistant instance."""
    return MagicMock()


@pytest.fixture
def ocpp_server(mock_hass):
    """Return an OCPPServer with zeroed anti-flap windows for fast tests."""
    return OCPPServer(
        hass=mock_hass,
        host="127.0.0.1",
        port=19001,
        start_window_s=0,
        stop_window_s=0,
    )


@pytest.fixture
def charger_session():
    """Return a minimal charger session for testing handlers."""
    return ChargerSession(
        cpid="test-cpid",
        websocket=AsyncMock(),
        connected_at=datetime.now(UTC),
    )


def _sent_actions(charger_session: ChargerSession) -> list[str]:
    """Return the OCPP action names sent over the mock WebSocket, in order."""
    return [
        json.loads(call.args[0])[2]
        for call in charger_session.websocket.send_str.call_args_list
    ]


def _sent_messages(charger_session: ChargerSession, action: str) -> list[list]:
    """Return every OCPP message sent for *action*, in order."""
    return [
        json.loads(call.args[0])
        for call in charger_session.websocket.send_str.call_args_list
        if json.loads(call.args[0])[2] == action
    ]


def _cleared_profile_ids(charger_session: ChargerSession) -> set[int]:
    """Return the profile IDs HSEM cleared via ClearChargingProfile."""
    return {
        msg[3]["id"] for msg in _sent_messages(charger_session, "ClearChargingProfile")
    }


def _zero_limit_sent(charger_session: ChargerSession) -> bool:
    """Return whether a 0 A SetChargingProfile reached the socket."""
    return any(
        msg[3]["csChargingProfiles"]["chargingSchedule"]["chargingSchedulePeriod"][0][
            "limit"
        ]
        == 0
        for msg in _sent_messages(charger_session, "SetChargingProfile")
    )


async def _arm_gate(ocpp_server: OCPPServer, charger_session: ChargerSession) -> None:
    """Arm the connect gate via a connector-level status change."""
    ocpp_server._chargers["test-cpid"] = charger_session
    await ocpp_server._handle_status_notification(
        charger_session, {"status": "Preparing", "connectorId": 1}
    )
    await asyncio.sleep(0)  # let the detached gate task run
    assert charger_session.gate_pending_plan is True
    assert _zero_limit_sent(charger_session)


class TestPlannedZeroOnManagedEv:
    """A planned zero on a managed EV is an enforced zero, not a clear."""

    @pytest.mark.asyncio
    async def test_zero_first_decision_keeps_zero_profile(
        self, ocpp_server, charger_session
    ):
        """Pre-fix, this decision sent ClearChargingProfile for both HSEM IDs.

        That actuated "HSEM wants zero" identically to "HSEM has no
        opinion", leaving the charger unlimited right after connect.
        """
        await _arm_gate(ocpp_server, charger_session)
        charger_session.websocket.send_str.reset_mock()

        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=0.0, now=datetime.now(UTC)
        )

        assert charger_session.gate_pending_plan is False
        assert "ClearChargingProfile" not in _sent_actions(charger_session)
        # The enforced zero is re-affirmed at decision time so a failed or
        # in-flight arm-time send cannot leave the charger unlimited.
        assert _zero_limit_sent(charger_session)
        assert charger_session.hsem_zero_profile_active is True

    @pytest.mark.asyncio
    async def test_real_allocation_after_held_zero_replaces_profile(
        self, ocpp_server, charger_session
    ):
        """A later non-zero plan replaces the enforced zero — never clears."""
        await _arm_gate(ocpp_server, charger_session)
        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=0.0, now=datetime.now(UTC)
        )
        charger_session.websocket.send_str.reset_mock()

        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=7.2, now=datetime.now(UTC)
        )

        assert "ClearChargingProfile" not in _sent_actions(charger_session)
        profiles = _sent_messages(charger_session, "SetChargingProfile")
        assert profiles
        assert (
            profiles[-1][3]["csChargingProfiles"]["chargingSchedule"][
                "chargingSchedulePeriod"
            ][0]["limit"]
            == 16
        )
        assert charger_session.hsem_zero_profile_active is False

    @pytest.mark.asyncio
    async def test_held_zero_released_once_when_ev_becomes_unmanaged(
        self, ocpp_server, charger_session
    ):
        """Smart charging switched off relinquishes control — exactly once."""
        await _arm_gate(ocpp_server, charger_session)
        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=0.0, now=datetime.now(UTC)
        )
        assert charger_session.hsem_zero_profile_active is True
        charger_session.websocket.send_str.reset_mock()

        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=0.0, managed=False, now=datetime.now(UTC)
        )

        assert _cleared_profile_ids(charger_session) == set(HSEM_PROFILE_IDS)
        assert charger_session.hsem_zero_profile_active is False

        # Subsequent unmanaged zero cycles must not spam ClearChargingProfile.
        charger_session.websocket.send_str.reset_mock()
        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=0.0, managed=False, now=datetime.now(UTC)
        )
        charger_session.websocket.send_str.assert_not_called()

    @pytest.mark.asyncio
    async def test_held_zero_released_on_disconnect(self, ocpp_server, charger_session):
        """Unplugging the car releases a held enforced zero (#920 invariant)."""
        await _arm_gate(ocpp_server, charger_session)
        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=0.0, now=datetime.now(UTC)
        )
        assert charger_session.hsem_zero_profile_active is True
        charger_session.websocket.send_str.reset_mock()

        await ocpp_server._handle_status_notification(
            charger_session, {"status": "Available", "connectorId": 1}
        )
        await asyncio.sleep(0)

        assert _cleared_profile_ids(charger_session) == set(HSEM_PROFILE_IDS)
        assert charger_session.hsem_zero_profile_active is False


class TestPlannedZeroOnUnmanagedEv:
    """An unmanaged EV relinquishes the charger — issue #920 must not regress."""

    @pytest.mark.asyncio
    async def test_zero_first_decision_unmanaged_releases_gate(
        self, ocpp_server, charger_session
    ):
        """Feature off / smart charging off: the transient 0 A gate is cleared."""
        await _arm_gate(ocpp_server, charger_session)
        charger_session.websocket.send_str.reset_mock()

        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=0.0, managed=False, now=datetime.now(UTC)
        )

        assert charger_session.gate_pending_plan is False
        assert ocpp_server._flap_state == "idle"
        assert _cleared_profile_ids(charger_session) == set(HSEM_PROFILE_IDS)
        assert charger_session.hsem_zero_profile_active is False

    @pytest.mark.asyncio
    async def test_unmanaged_free_vend_is_not_stopped(
        self, ocpp_server, charger_session
    ):
        """With smart charging off, a locally started session is left alone."""
        ocpp_server._chargers["test-cpid"] = charger_session
        await ocpp_server._handle_start_transaction(charger_session, {})
        await asyncio.sleep(0)
        assert charger_session.transaction_id is not None
        charger_session.websocket.send_str.reset_mock()

        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=0.0, managed=False, now=datetime.now(UTC)
        )

        assert "RemoteStopTransaction" not in _sent_actions(charger_session)
        assert "SetChargingProfile" not in _sent_actions(charger_session)


class TestChargerInitiatedSessionDrivenToZero:
    """A free-vend session is stopped even though HSEM never started it."""

    @pytest.mark.asyncio
    async def test_free_vend_session_is_stopped_on_zero_plan(
        self, ocpp_server, charger_session
    ):
        """Precondition pins the pre-fix blind spot: flap idle, tx open.

        The zero-target branch used to require _flap_state to have left
        "idle", which only happens when HSEM itself started the session —
        so a charger-initiated StartTransaction was never stopped.
        """
        ocpp_server._chargers["test-cpid"] = charger_session
        await ocpp_server._handle_start_transaction(charger_session, {})
        await asyncio.sleep(0)

        # Explicit precondition: the exact state the old code ignored.
        assert ocpp_server._flap_state == "idle"
        assert charger_session.transaction_id is not None
        charger_session.websocket.send_str.reset_mock()

        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=0.0, now=datetime.now(UTC)
        )

        actions = _sent_actions(charger_session)
        assert "RemoteStopTransaction" in actions
        assert _zero_limit_sent(charger_session)
        assert charger_session.hsem_zero_profile_active is True

    @pytest.mark.asyncio
    async def test_free_vend_stop_waits_for_stop_window(
        self, mock_hass, charger_session
    ):
        """The free-vend stop still honours the anti-flap stop window."""
        server = OCPPServer(
            hass=mock_hass,
            host="127.0.0.1",
            port=19002,
            start_window_s=0,
            stop_window_s=60,
        )
        server._chargers["test-cpid"] = charger_session
        await server._handle_start_transaction(charger_session, {})
        await asyncio.sleep(0)
        charger_session.websocket.send_str.reset_mock()

        now = datetime.now(UTC)
        await server.update_charge_target("test-cpid", target_power_kw=0.0, now=now)
        assert "RemoteStopTransaction" not in _sent_actions(charger_session)

        await server.update_charge_target(
            "test-cpid", target_power_kw=0.0, now=now + timedelta(seconds=61)
        )
        assert "RemoteStopTransaction" in _sent_actions(charger_session)


class TestConnectorIdHandling:
    """connectorId=0 is the charge point, not the car (OCPP 1.6)."""

    @pytest.mark.asyncio
    async def test_charge_point_status_does_not_arm_gate(
        self, ocpp_server, charger_session
    ):
        """A connector-0 status change must not be read as 'car plugged in'."""
        ocpp_server._chargers["test-cpid"] = charger_session

        await ocpp_server._handle_status_notification(
            charger_session, {"status": "Preparing", "connectorId": 0}
        )
        await asyncio.sleep(0)

        assert charger_session.gate_pending_plan is False
        assert charger_session.status == "Available"
        assert charger_session.charge_point_status == "Preparing"
        charger_session.websocket.send_str.assert_not_called()

    @pytest.mark.asyncio
    async def test_charge_point_available_does_not_release_gate(
        self, ocpp_server, charger_session
    ):
        """connector-0 'Available' must not release a connector-level gate."""
        await _arm_gate(ocpp_server, charger_session)
        charger_session.websocket.send_str.reset_mock()

        await ocpp_server._handle_status_notification(
            charger_session, {"status": "Available", "connectorId": 0}
        )
        await asyncio.sleep(0)

        assert charger_session.gate_pending_plan is True
        assert charger_session.status == "Preparing"
        assert charger_session.charge_point_status == "Available"
        charger_session.websocket.send_str.assert_not_called()

        # The connector-level Available still releases it.
        await ocpp_server._handle_status_notification(
            charger_session, {"status": "Available", "connectorId": 1}
        )
        await asyncio.sleep(0)
        assert charger_session.gate_pending_plan is False
        assert _cleared_profile_ids(charger_session) == set(HSEM_PROFILE_IDS)


# ---------------------------------------------------------------------------
# Issue #1018 — a "connected" blip must not lose the enforced zero
# ---------------------------------------------------------------------------


async def _hold_zero_with_car_plugged_in(
    ocpp_server: OCPPServer, charger_session: ChargerSession
) -> None:
    """Reach the field state: a held 0 A profile, car plugged in, charger idle.

    Mirrors 2026-09-14 13:03 → 16:25: HSEM had stopped the session with a
    0 A profile and the connector sat at ``Finishing`` with the car attached.
    """
    await _arm_gate(ocpp_server, charger_session)
    await ocpp_server.update_charge_target(
        "test-cpid", target_power_kw=0.0, now=datetime.now(UTC)
    )
    await ocpp_server._handle_status_notification(
        charger_session, {"status": "Finishing", "connectorId": 1}
    )
    await asyncio.sleep(0)
    assert charger_session.hsem_zero_profile_active is True
    assert ocpp_server._flap_state == "idle"
    charger_session.websocket.send_str.reset_mock()


class TestConnectivityBlipKeepsEnforcedZero:
    """A one-cycle HA "connected" blip cannot release the 0 A limit (#1018)."""

    @pytest.mark.asyncio
    async def test_disconnect_blip_with_car_plugged_in_keeps_zero(
        self, ocpp_server, charger_session
    ):
        """The charger still reports a car: the zero stays enforced."""
        await _hold_zero_with_car_plugged_in(ocpp_server, charger_session)

        await ocpp_server.update_charge_target(
            "test-cpid",
            target_power_kw=0.0,
            managed=False,
            management_enabled=True,
            now=datetime.now(UTC),
        )

        assert "ClearChargingProfile" not in _sent_actions(charger_session)
        assert charger_session.hsem_zero_profile_active is True

    @pytest.mark.asyncio
    async def test_disconnect_the_charger_agrees_with_releases_zero(
        self, ocpp_server, charger_session
    ):
        """When the charger also reports no car, the release still happens."""
        await _hold_zero_with_car_plugged_in(ocpp_server, charger_session)
        charger_session.status = "Available"

        await ocpp_server.update_charge_target(
            "test-cpid",
            target_power_kw=0.0,
            managed=False,
            management_enabled=True,
            now=datetime.now(UTC),
        )

        assert _cleared_profile_ids(charger_session) == set(HSEM_PROFILE_IDS)
        assert charger_session.hsem_zero_profile_active is False

    @pytest.mark.asyncio
    async def test_smart_charging_off_releases_even_with_car_plugged_in(
        self, ocpp_server, charger_session
    ):
        """Switching management off is explicit — issue #920 must not regress."""
        await _hold_zero_with_car_plugged_in(ocpp_server, charger_session)

        await ocpp_server.update_charge_target(
            "test-cpid",
            target_power_kw=0.0,
            managed=False,
            management_enabled=False,
            now=datetime.now(UTC),
        )

        assert _cleared_profile_ids(charger_session) == set(HSEM_PROFILE_IDS)
        assert charger_session.hsem_zero_profile_active is False

    @pytest.mark.asyncio
    async def test_lost_zero_is_reinstalled_exactly_once(
        self, ocpp_server, charger_session
    ):
        """A managed, plugged-in EV without its 0 A profile gets it back once."""
        await _hold_zero_with_car_plugged_in(ocpp_server, charger_session)
        charger_session.hsem_zero_profile_active = False  # e.g. after a release

        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=0.0, now=datetime.now(UTC)
        )
        assert _zero_limit_sent(charger_session)
        assert charger_session.hsem_zero_profile_active is True

        charger_session.websocket.send_str.reset_mock()
        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=0.0, now=datetime.now(UTC)
        )
        charger_session.websocket.send_str.assert_not_called()

    @pytest.mark.asyncio
    async def test_zero_is_never_written_onto_an_idle_connector(
        self, ocpp_server, charger_session
    ):
        """No car plugged in: re-installing 0 A would be the #920 standing block."""
        ocpp_server._chargers["test-cpid"] = charger_session
        assert charger_session.status == "Available"

        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=0.0, now=datetime.now(UTC)
        )

        assert "SetChargingProfile" not in _sent_actions(charger_session)
        assert charger_session.hsem_zero_profile_active is False

    @pytest.mark.asyncio
    async def test_charging_without_a_transaction_is_driven_to_zero(
        self, ocpp_server, charger_session
    ):
        """A charger that resumes Charging with no StartTransaction is stopped."""
        ocpp_server._chargers["test-cpid"] = charger_session
        charger_session.status = "Charging"
        assert charger_session.transaction_id is None

        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=0.0, now=datetime.now(UTC)
        )

        assert _zero_limit_sent(charger_session)
        assert charger_session.hsem_zero_profile_active is True
        assert "ClearChargingProfile" not in _sent_actions(charger_session)

    @pytest.mark.asyncio
    async def test_charging_without_a_transaction_resends_an_ignored_zero(
        self, ocpp_server, charger_session
    ):
        """Even with 0 A believed held, a Charging connector is re-driven to zero."""
        await _hold_zero_with_car_plugged_in(ocpp_server, charger_session)
        await ocpp_server._handle_status_notification(
            charger_session, {"status": "Charging", "connectorId": 1}
        )
        charger_session.websocket.send_str.reset_mock()

        await ocpp_server.update_charge_target(
            "test-cpid", target_power_kw=0.0, now=datetime.now(UTC)
        )

        assert _zero_limit_sent(charger_session)

    @pytest.mark.asyncio
    async def test_field_sequence_never_loses_the_limit(
        self, ocpp_server, charger_session
    ):
        """2026-09-14 16:25, replayed: blip → Charging with no transaction → zero plan.

        Before the fix the blip cleared both HSEM profiles, the car drew
        ~10.9 kW from grid, and nothing re-armed the limit.
        """
        await _hold_zero_with_car_plugged_in(ocpp_server, charger_session)

        # 16:25:34 — one cycle with the HA entity reading disconnected.
        await ocpp_server.update_charge_target(
            "test-cpid",
            target_power_kw=0.0,
            managed=False,
            management_enabled=True,
            now=datetime.now(UTC),
        )
        # 16:25:48 — the charger resumes Charging without a StartTransaction.
        await ocpp_server._handle_status_notification(
            charger_session, {"status": "Charging", "connectorId": 1}
        )
        await asyncio.sleep(0)
        # 16:25:52 onwards — connected again, plan still zero.
        await ocpp_server.update_charge_target(
            "test-cpid",
            target_power_kw=0.0,
            managed=True,
            management_enabled=True,
            now=datetime.now(UTC),
        )

        assert "ClearChargingProfile" not in _sent_actions(charger_session)
        assert _zero_limit_sent(charger_session)
        assert charger_session.hsem_zero_profile_active is True
