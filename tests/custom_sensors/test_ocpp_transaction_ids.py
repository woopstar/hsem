"""Tests for transaction IDs and stale StopTransaction (issue #1404)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.custom_sensors.ocpp_server import OCPPServer
from custom_components.hsem.custom_sensors.ocpp_transaction_ids import (
    TX_ID_CEILING,
    is_stale_stop,
    next_transaction_id,
)
from custom_components.hsem.models.ocpp_session import ChargerSession

_CLOCK = "custom_components.hsem.custom_sensors.ocpp_transaction_ids.time.time"
_HANDLERS = "custom_components.hsem.custom_sensors.ocpp_message_handlers"


def _server() -> OCPPServer:
    """Return an OCPPServer that is never started."""
    return OCPPServer(hass=MagicMock(), host="127.0.0.1", port=19000)


def _session() -> ChargerSession:
    """Return a charger session with a mocked websocket."""
    return ChargerSession(
        cpid="cp1", websocket=AsyncMock(), connected_at=datetime.now(UTC)
    )


class TestNextTransactionId:
    """The clock is a floor for new IDs."""

    def test_clock_wins_over_a_low_counter(self):
        """A fresh counter after a restart jumps to the clock."""
        assert next_transaction_id(1, now=1_800_000_000.7) == 1_800_000_000

    def test_counter_wins_when_higher(self):
        """Two starts in the same second still get distinct IDs."""
        assert next_transaction_id(1_800_000_001, now=1_800_000_000) == 1_800_000_001

    def test_counter_only_past_the_int32_ceiling(self):
        """After January 2038 the counter alone is used."""
        assert next_transaction_id(5, now=TX_ID_CEILING + 1) == 5

    def test_reads_the_clock_when_no_time_is_given(self):
        """Without ``now`` the current time is used."""
        with patch(_CLOCK, return_value=1_234_567_890.0):
            assert next_transaction_id(1) == 1_234_567_890


class TestIsStaleStop:
    """Only a lower ID than the open one is stale."""

    @pytest.mark.parametrize(
        ("open_id", "stop_id", "stale"),
        [
            (None, 5, False),
            (10, 9, True),
            (10, "9", True),
            (10, 10, False),
            (10, 11, False),
            (10, None, False),
            (10, "abc", False),
        ],
    )
    def test_cases(self, open_id, stop_id, stale):
        """Lower is stale; idle, equal, higher and unusable IDs are not."""
        assert is_stale_stop(open_id, stop_id) is stale


class TestServerTransactions:
    """The OCPP server applies the rules."""

    @pytest.mark.asyncio
    async def test_ids_rise_across_a_restart(self):
        """A restarted server does not reuse an ID handed out before."""
        with patch(_CLOCK, return_value=1_800_000_000.0):
            first = await _server()._handle_start_transaction(_session(), {})
        with patch(_CLOCK, return_value=1_800_000_060.0):
            second = await _server()._handle_start_transaction(_session(), {})
        assert second["transactionId"] > first["transactionId"]

    @pytest.mark.asyncio
    async def test_a_stale_stop_keeps_the_open_transaction(self, caplog):
        """A late stop for an older transaction is answered and ignored."""
        server, session = _server(), _session()
        with patch(_CLOCK, return_value=1_800_000_000.0):
            reply = await server._handle_start_transaction(
                session, {"meterStart": 1000}
            )
        with caplog.at_level(logging.WARNING, logger=_HANDLERS):
            result = await server._handle_stop_transaction(
                session, {"transactionId": 3, "meterStop": 2000}
            )
        assert result == {"idTagInfo": {"status": "Accepted"}}
        assert session.transaction_id == reply["transactionId"]
        assert session.meter_start_wh == pytest.approx(1000.0)
        assert server.completed_sessions == []
        assert "older than the open" in caplog.text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("offset", [0, 1])
    async def test_a_matching_or_higher_stop_ends_it(self, offset):
        """The open ID, or a higher one, ends the transaction as before."""
        server, session = _server(), _session()
        reply = await server._handle_start_transaction(session, {"meterStart": 0})
        await server._handle_stop_transaction(
            session,
            {"transactionId": reply["transactionId"] + offset, "meterStop": 500},
        )
        assert session.transaction_id is None
        assert len(server.completed_sessions) == 1

    @pytest.mark.asyncio
    async def test_available_drops_a_held_transaction(self, caplog):
        """A connector that is Available cannot have a transaction."""
        server, session = _server(), _session()
        session.status = "Charging"
        reply = await server._handle_start_transaction(session, {"idTag": "HSEM"})
        with caplog.at_level(logging.WARNING, logger=_HANDLERS):
            await server._handle_status_notification(
                session, {"connectorId": 1, "status": "Available"}
            )
        assert session.transaction_id is None
        assert session.transaction_started_by is None
        assert "treating it as ended" in caplog.text
        # Trailing MeterValues for that ID must not adopt it again.
        await server._handle_meter_values(
            session,
            {
                "connectorId": 1,
                "transactionId": reply["transactionId"],
                "meterValue": [
                    {
                        "sampledValue": [
                            {"measurand": "Power.Active.Import", "value": "0"}
                        ]
                    }
                ],
            },
        )
        assert session.transaction_id is None

    @pytest.mark.asyncio
    async def test_available_without_a_transaction_changes_nothing(self):
        """No transaction, nothing to drop."""
        server, session = _server(), _session()
        session.status = "Preparing"
        await server._handle_status_notification(
            session, {"connectorId": 1, "status": "Available"}
        )
        assert session.transaction_id is None
        assert server._ended_transactions == {}

    @pytest.mark.asyncio
    async def test_repeated_available_also_drops_it(self):
        """A held transaction is dropped even without a status change."""
        server, session = _server(), _session()
        session.status = "Available"
        session.transaction_id = 42
        await server._handle_status_notification(
            session, {"connectorId": 1, "status": "Available"}
        )
        assert session.transaction_id is None
        assert server._ended_transactions["cp1"] == 42
