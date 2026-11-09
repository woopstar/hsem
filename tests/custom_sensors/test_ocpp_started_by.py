"""Tests for recording who started an OCPP transaction (issue #1393)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.hsem.custom_sensors.ocpp_server import OCPPServer
from custom_components.hsem.models.ocpp_session import ChargerSession

_HANDLERS_LOGGER = "custom_components.hsem.custom_sensors.ocpp_transactions"


@pytest.fixture
def ocpp_server() -> OCPPServer:
    """Return an OCPPServer that is never started."""
    return OCPPServer(hass=MagicMock(), host="127.0.0.1", port=19000)


@pytest.fixture
def charger_session() -> ChargerSession:
    """Return a charger session with a mocked websocket."""
    return ChargerSession(
        cpid="cp1", websocket=AsyncMock(), connected_at=datetime.now(UTC)
    )


class TestStartedBy:
    """transaction_started_by follows the StartTransaction idTag."""

    @pytest.mark.asyncio
    async def test_hsem_tag_is_hsem(self, ocpp_server, charger_session):
        """HSEM's own idTag echoed back means HSEM started it."""
        await ocpp_server._handle_start_transaction(
            charger_session, {"connectorId": 1, "idTag": "HSEM", "meterStart": 0}
        )
        assert charger_session.transaction_started_by == "hsem"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("id_tag", ["04A2B3C4D5", "", None])
    async def test_other_tag_is_local(
        self, ocpp_server, charger_session, caplog, id_tag
    ):
        """Any other idTag is a local start; the tag is only logged at debug."""
        with caplog.at_level(logging.INFO, logger=_HANDLERS_LOGGER):
            await ocpp_server._handle_start_transaction(
                charger_session, {"connectorId": 1, "idTag": id_tag, "meterStart": 0}
            )
        assert charger_session.transaction_started_by == "local"
        assert "started locally" in caplog.text
        assert "04A2B3C4D5" not in caplog.text

    @pytest.mark.asyncio
    async def test_stop_clears_started_by(self, ocpp_server, charger_session):
        """A stop clears the field."""
        reply = await ocpp_server._handle_start_transaction(
            charger_session, {"connectorId": 1, "idTag": "HSEM", "meterStart": 0}
        )
        await ocpp_server._handle_stop_transaction(
            charger_session,
            {"transactionId": reply["transactionId"], "meterStop": 0},
        )
        assert charger_session.transaction_started_by is None

    @pytest.mark.asyncio
    async def test_adopted_transaction_is_unknown(self, ocpp_server, charger_session):
        """A transaction adopted from MeterValues has an unknown starter."""
        await ocpp_server._handle_meter_values(
            charger_session,
            {
                "connectorId": 1,
                "transactionId": 9,
                "meterValue": [{"sampledValue": [{"value": "100"}]}],
            },
        )
        assert charger_session.transaction_id == 9
        assert charger_session.transaction_started_by == "unknown"

    @pytest.mark.asyncio
    async def test_local_start_does_not_change_control(
        self, ocpp_server, charger_session
    ):
        """A local start is still managed: the start response is unchanged."""
        reply = await ocpp_server._handle_start_transaction(
            charger_session, {"connectorId": 1, "idTag": "card", "meterStart": 0}
        )
        assert reply["idTagInfo"] == {"status": "Accepted"}
        assert charger_session.transaction_id == reply["transactionId"]
