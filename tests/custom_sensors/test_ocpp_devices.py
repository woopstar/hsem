"""Tests for the per-charger OCPP device handlers (issue #1383)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from custom_components.hsem.custom_sensors.ocpp_devices import (
    ChargerHandler,
    resolve_handler,
)
from custom_components.hsem.custom_sensors.ocpp_devices.goe import (
    FORCE_STATE_KEY,
    FORCE_STATE_NEUTRAL,
    FORCE_STATE_OFF,
    GoEHandler,
)
from custom_components.hsem.models.ocpp_session import ChargerSession


def _session(
    vendor: str = "", model: str = "", keys: dict[str, str] | None = None
) -> ChargerSession:
    """Return a charger session with the given identity and config keys."""
    return ChargerSession(
        cpid="cp1",
        websocket=AsyncMock(),
        connected_at=datetime.now(UTC),
        vendor=vendor,
        model=model,
        configuration_keys=dict(keys or {}),
    )


class TestResolveHandler:
    """The handler is picked from vendor, model and reported keys."""

    @pytest.mark.parametrize(
        ("vendor", "model", "keys"),
        [
            ("go-e", "", {}),
            ("GO-E GmbH", "", {}),
            ("", "go-eCharger_V4", {}),
            ("", "", {FORCE_STATE_KEY: FORCE_STATE_NEUTRAL}),
        ],
    )
    def test_go_e_is_recognised(
        self, vendor: str, model: str, keys: dict[str, str]
    ) -> None:
        """Vendor, model or the ForceState key each select the go-e handler."""
        handler = resolve_handler(_session(vendor, model, keys))
        assert isinstance(handler, GoEHandler)
        assert handler.name == "go-e"

    def test_other_charger_gets_the_generic_handler(self) -> None:
        """A charger without a dedicated handler falls back to generic."""
        handler = resolve_handler(_session("ACME", "Wallbox 11"))
        assert type(handler) is ChargerHandler
        assert handler.name == "generic"

    def test_generic_handler_matches_every_charger(self) -> None:
        """The generic handler is a valid fallback for any session."""
        assert ChargerHandler.matches(_session("ACME", "Wallbox 11")) is True


class TestBeforeStart:
    """Only the go-e handler writes a vendor key, and only when blocked."""

    @pytest.mark.asyncio
    async def test_generic_handler_sends_nothing(self) -> None:
        """The generic handler never writes a configuration key."""
        commands = AsyncMock()
        await ChargerHandler().before_start(commands, _session("ACME"))
        commands.send_change_configuration.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_go_e_forced_off_is_cleared(self) -> None:
        """ForceState=Off is written back to Neutral and cached optimistically."""
        commands = AsyncMock()
        commands.send_change_configuration.return_value = True
        session = _session("go-e", keys={FORCE_STATE_KEY: FORCE_STATE_OFF})

        await GoEHandler().before_start(commands, session)

        commands.send_change_configuration.assert_awaited_once_with(
            "cp1", FORCE_STATE_KEY, FORCE_STATE_NEUTRAL
        )
        assert session.configuration_keys[FORCE_STATE_KEY] == FORCE_STATE_NEUTRAL

    @pytest.mark.asyncio
    async def test_go_e_failed_write_keeps_the_cached_value(self) -> None:
        """A write that never reached the socket leaves the cache at Off."""
        commands = AsyncMock()
        commands.send_change_configuration.return_value = False
        session = _session("go-e", keys={FORCE_STATE_KEY: FORCE_STATE_OFF})

        await GoEHandler().before_start(commands, session)

        assert session.configuration_keys[FORCE_STATE_KEY] == FORCE_STATE_OFF

    @pytest.mark.asyncio
    @pytest.mark.parametrize("keys", [{}, {FORCE_STATE_KEY: FORCE_STATE_NEUTRAL}])
    async def test_go_e_not_blocked_sends_nothing(self, keys: dict[str, str]) -> None:
        """No key, or a permissive value, means nothing to clear."""
        commands = AsyncMock()
        await GoEHandler().before_start(commands, _session("go-e", keys=keys))
        commands.send_change_configuration.assert_not_awaited()
