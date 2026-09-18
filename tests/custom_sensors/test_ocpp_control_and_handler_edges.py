"""Edge cases in OCPP capability parsing and the message handlers.

HSEM talks to a charger it does not control the firmware of, so every reply it
parses can be malformed and every outbound call can fail. None of that may
raise out of a background task or leave a stale transaction belief behind.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.custom_sensors.ocpp_control import (
    CHARGING_RATE_UNIT_KEY,
    MAX_STACK_LEVEL_KEY,
    supported_charging_rate_units,
)
from custom_components.hsem.custom_sensors.ocpp_server import OCPPServer
from custom_components.hsem.models.ocpp_session import ChargerSession

_CONTROL_MODULE = "custom_components.hsem.custom_sensors.ocpp_control"
_HANDLERS_MODULE = "custom_components.hsem.custom_sensors.ocpp_message_handlers"


@pytest.fixture
def server() -> OCPPServer:
    """Return an OCPP server with no anti-flap delay."""
    return OCPPServer(
        hass=MagicMock(),
        host="127.0.0.1",
        port=19002,
        start_window_s=0,
        stop_window_s=0,
    )


@pytest.fixture
def session() -> ChargerSession:
    """Return a connected charger session."""
    return ChargerSession(
        cpid="CP-1", websocket=AsyncMock(), connected_at=datetime.now(UTC)
    )


class TestSupportedChargingRateUnits:
    """An unreadable capability reply falls back to amps-only."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("A", (True, False), id="amps"),
            pytest.param("W", (False, True), id="watts"),
            pytest.param("A,W", (True, True), id="both"),
            pytest.param("Current;Power", (True, True), id="long_names"),
        ],
    )
    def test_a_readable_reply_is_parsed(
        self, raw: str, expected: tuple[bool, bool]
    ) -> None:
        """Both OCPP separators and both spellings are accepted."""
        assert supported_charging_rate_units({CHARGING_RATE_UNIT_KEY: raw}) == expected

    @pytest.mark.parametrize(
        "keys",
        [
            pytest.param({}, id="key_absent"),
            pytest.param({CHARGING_RATE_UNIT_KEY: ""}, id="empty_value"),
            pytest.param(
                {CHARGING_RATE_UNIT_KEY: "Kelvin,Furlongs"}, id="unknown_units"
            ),
        ],
    )
    def test_an_unreadable_reply_assumes_amps(self, keys: dict[str, str]) -> None:
        """Amps is the OCPP-mandatory unit, so it is the safe assumption."""
        assert supported_charging_rate_units(keys) == (True, False)


class TestAbsorbConfigurationReply:
    """Only a reply with usable keys replaces the charger's known capabilities."""

    def test_non_dict_entries_are_skipped(
        self, server: OCPPServer, session: ChargerSession
    ) -> None:
        """A malformed list entry does not abort the whole reply."""
        server.absorb_configuration_reply(
            session,
            {
                "configurationKey": [
                    "not a dict",
                    {"key": MAX_STACK_LEVEL_KEY, "value": 8},
                ]
            },
        )

        assert session.configuration_keys == {MAX_STACK_LEVEL_KEY: "8"}

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({"configurationKey": "not a list"}, id="not_a_list"),
            pytest.param({}, id="key_absent"),
            pytest.param({"configurationKey": [{"value": "8"}]}, id="no_key_names"),
            pytest.param({"configurationKey": []}, id="empty_list"),
        ],
    )
    def test_an_unusable_reply_leaves_the_capabilities_untouched(
        self, server: OCPPServer, session: ChargerSession, payload: dict[str, Any]
    ) -> None:
        """A charger's previously-known keys survive a malformed reply."""
        session.configuration_keys = {MAX_STACK_LEVEL_KEY: "3"}

        server.absorb_configuration_reply(session, payload)

        assert session.configuration_keys == {MAX_STACK_LEVEL_KEY: "3"}


class TestProfileStackLevels:
    """HSEM installs its profiles at the top of the advertised range."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("8", (7, 8), id="reported_range"),
            pytest.param(None, (0, 1), id="unreported"),
            pytest.param("not a number", (0, 1), id="unparseable"),
            pytest.param("0", (0, 0), id="single_level_charger"),
            pytest.param("-2", (0, 0), id="negative"),
        ],
    )
    def test_the_levels_follow_the_reported_maximum(
        self,
        server: OCPPServer,
        session: ChargerSession,
        raw: str | None,
        expected: tuple[int, int],
    ) -> None:
        """A charger that advertises only level 0 gets 0/0, never a negative."""
        if raw is not None:
            session.configuration_keys = {MAX_STACK_LEVEL_KEY: raw}

        assert server.profile_stack_levels(session) == expected


class TestReleaseChargingProfiles:
    """Teardown clears HSEM's own profiles and is never blockable."""

    @pytest.mark.asyncio
    async def test_a_failing_clear_is_logged_and_swallowed(
        self, server: OCPPServer, session: ChargerSession
    ) -> None:
        """A charger that refuses the clear must not block the unload."""
        server._chargers["CP-1"] = session

        with (
            patch.object(
                server,
                "send_clear_charging_profile",
                AsyncMock(side_effect=RuntimeError("charger offline")),
            ),
            patch(f"{_CONTROL_MODULE}._LOGGER") as logger,
        ):
            await server.release_charging_profiles((1, 2))

        assert logger.exception.call_count == 2


class TestBackgroundTaskFailures:
    """A detached OCPP task logs its own failure — nothing else awaits it."""

    @pytest.mark.asyncio
    async def test_a_failing_configuration_request_is_logged(
        self, server: OCPPServer, session: ChargerSession
    ) -> None:
        """A charger that will not answer GetConfiguration is not fatal."""
        with (
            patch.object(
                server,
                "send_get_configuration",
                AsyncMock(side_effect=RuntimeError("timeout")),
            ),
            patch(f"{_HANDLERS_MODULE}._LOGGER") as logger,
        ):
            await server._request_configuration(session)

        logger.exception.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_failing_profile_resend_is_logged(
        self, server: OCPPServer, session: ChargerSession
    ) -> None:
        """A profile resend that fails after StartTransaction is not fatal."""
        with (
            patch.object(
                server,
                "_send_set_charging_profile",
                AsyncMock(side_effect=RuntimeError("rejected")),
            ),
            patch(f"{_HANDLERS_MODULE}._LOGGER") as logger,
        ):
            await server._resend_profile_after_start(session, 7400, 32)

        logger.exception.assert_called_once()


class TestTransactionIdParsing:
    """A transaction ID HSEM cannot parse is never remembered."""

    @pytest.mark.asyncio
    async def test_a_status_notification_without_a_status_is_ignored(
        self, server: OCPPServer, session: ChargerSession
    ) -> None:
        """An empty status carries no state transition."""
        before = session.status

        assert await server._handle_status_notification(session, {"status": ""}) == {}
        assert session.status == before

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "transaction_id",
        [
            pytest.param("abc", id="unparseable"),
            pytest.param(None, id="absent"),
            pytest.param(0, id="zero"),
            pytest.param(-5, id="negative"),
        ],
    )
    async def test_an_unusable_meter_transaction_is_not_adopted(
        self, server: OCPPServer, session: ChargerSession, transaction_id: Any
    ) -> None:
        """Only a positive, numeric transactionId may be adopted."""
        await server._adopt_transaction_from_meter_values(
            session, {"transactionId": transaction_id}, []
        )

        assert session.transaction_id is None

    @pytest.mark.asyncio
    async def test_an_unparseable_stop_transaction_is_not_recorded(
        self, server: OCPPServer, session: ChargerSession
    ) -> None:
        """A malformed stop is still accepted, but nothing is latched."""
        with patch(f"{_HANDLERS_MODULE}._LOGGER") as logger:
            response = await server._handle_stop_transaction(
                session, {"transactionId": "abc"}
            )

        assert response["idTagInfo"]["status"] == "Accepted"
        assert server._ended_transactions.get("CP-1") is None
        assert any(
            "non-numeric transactionId" in str(call.args[0])
            for call in logger.debug.call_args_list
        )
