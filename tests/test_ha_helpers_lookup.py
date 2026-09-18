"""Tests for entity-id resolution, state conversion, and device lookup.

``async_resolve_entity_id_from_unique_id`` caches registry lookups but must
drop a cached id once that entity disappears, or HSEM would keep reading a
deleted entity. ``ha_get_entity_state_and_convert`` must distinguish a
genuinely missing entity from an unconvertible reading.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from homeassistant.exceptions import HomeAssistantError

from custom_components.hsem.utils.ha_helpers import (
    EntityNotFoundError,
    async_device_exists,
    async_resolve_entity_id_from_unique_id,
    ha_get_entity_state_and_convert,
)

_MODULE = "custom_components.hsem.utils.ha_helpers"
_UNIQUE_ID = "hsem_test_unique_id"
_ENTITY_ID = "sensor.hsem_resolved"


class _FakeState:
    """Minimal stand-in for an HA state object."""

    def __init__(self, state: str, unit: str | None = None) -> None:
        self.state = state
        self.attributes = {"unit_of_measurement": unit} if unit else {}


def _caller(states: dict[str, _FakeState] | None = None) -> MagicMock:
    """Return a caller whose ``hass.states`` holds *states*."""
    caller = MagicMock()
    caller.hass.states.get.side_effect = (states or {}).get
    return caller


@pytest.fixture(autouse=True)
def _clear_resolution_cache() -> Any:
    """Isolate the module-level resolution cache between tests."""
    with patch(f"{_MODULE}._entity_id_from_unique_id_cache", {}):
        yield


class TestResolveEntityIdFromUniqueId:
    """Resolution is cached, but a cached id is never trusted blindly."""

    @pytest.mark.asyncio
    async def test_resolves_and_caches_the_entity_id(self) -> None:
        """A registry hit is returned and reused without a second lookup."""
        caller = _caller({_ENTITY_ID: _FakeState("1")})
        registry = MagicMock()
        registry.async_get_entity_id.return_value = _ENTITY_ID

        with patch(f"{_MODULE}.er.async_get", return_value=registry) as get_registry:
            first = await async_resolve_entity_id_from_unique_id(
                caller, _UNIQUE_ID, "sensor"
            )
            second = await async_resolve_entity_id_from_unique_id(
                caller, _UNIQUE_ID, "sensor"
            )

        assert first == _ENTITY_ID
        assert second == _ENTITY_ID
        get_registry.assert_called_once()

    @pytest.mark.asyncio
    async def test_cached_id_is_dropped_once_the_entity_disappears(self) -> None:
        """A cached id for a deleted entity is evicted, not returned."""
        # Seed the cache as a previous successful resolution would have.
        caller = _caller({_ENTITY_ID: _FakeState("1")})
        registry = MagicMock()
        registry.async_get_entity_id.return_value = _ENTITY_ID

        with patch(f"{_MODULE}.er.async_get", return_value=registry):
            assert (
                await async_resolve_entity_id_from_unique_id(
                    caller, _UNIQUE_ID, "sensor"
                )
                == _ENTITY_ID
            )

        # The entity is then removed from HA and from the registry.
        caller = _caller()
        registry.async_get_entity_id.return_value = None

        with patch(f"{_MODULE}.er.async_get", return_value=registry) as get_registry:
            resolved = await async_resolve_entity_id_from_unique_id(
                caller, _UNIQUE_ID, "sensor"
            )

        assert resolved is None
        # The stale cache entry did not short-circuit the lookup.
        get_registry.assert_called_once()

    @pytest.mark.asyncio
    async def test_unknown_unique_id_resolves_to_nothing(self) -> None:
        """An unregistered unique id is reported as missing."""
        caller = _caller()
        registry = MagicMock()
        registry.async_get_entity_id.return_value = None

        with patch(f"{_MODULE}.er.async_get", return_value=registry):
            assert (
                await async_resolve_entity_id_from_unique_id(
                    caller, _UNIQUE_ID, "sensor"
                )
                is None
            )

    @pytest.mark.asyncio
    async def test_without_hass_nothing_can_be_resolved(self) -> None:
        """Before the entity is attached to HA there is no registry to read."""
        caller = MagicMock()
        caller.hass = None

        assert (
            await async_resolve_entity_id_from_unique_id(caller, _UNIQUE_ID, "sensor")
            is None
        )


class TestGetEntityStateAndConvert:
    """Conversion separates "no entity" from "unusable reading"."""

    def test_missing_entity_id_returns_nothing(self) -> None:
        """An unconfigured entity is not an error."""
        assert ha_get_entity_state_and_convert(_caller(), None, "float") is None

    def test_unknown_entity_raises(self) -> None:
        """An entity absent from the state machine is an error."""
        with pytest.raises(EntityNotFoundError):
            ha_get_entity_state_and_convert(_caller(), _ENTITY_ID, "float")

    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            pytest.param("2.5", 2.5, id="numeric"),
            pytest.param("unavailable", None, id="unavailable"),
            pytest.param("unknown", None, id="unknown"),
            pytest.param("not a number", None, id="non_numeric"),
        ],
    )
    def test_float_conversion(self, state: str, expected: float | None) -> None:
        """Unusable float readings are reported as missing, not as zero."""
        caller = _caller({_ENTITY_ID: _FakeState(state)})

        result = ha_get_entity_state_and_convert(caller, _ENTITY_ID, "float")

        if expected is None:
            assert result is None
        else:
            assert result == pytest.approx(expected)

    def test_float_precision_is_applied(self) -> None:
        """The requested number of decimals is honoured."""
        caller = _caller({_ENTITY_ID: _FakeState("2.34567")})

        assert ha_get_entity_state_and_convert(
            caller, _ENTITY_ID, "float", 2
        ) == pytest.approx(2.35)

    def test_string_conversion_passes_the_raw_state(self) -> None:
        """String reads are returned verbatim."""
        caller = _caller({_ENTITY_ID: _FakeState("time_of_use_luna2000")})

        assert (
            ha_get_entity_state_and_convert(caller, _ENTITY_ID, "string")
            == "time_of_use_luna2000"
        )

    def test_boolean_conversion_resolves_switch_states(self) -> None:
        """Boolean reads use the shared state mapping."""
        caller = _caller({_ENTITY_ID: _FakeState("on")})

        assert ha_get_entity_state_and_convert(caller, _ENTITY_ID, "boolean") is True

    def test_unknown_state_raises_for_boolean_reads(self) -> None:
        """An unknown state is an error rather than a fabricated ``False``."""
        caller = _caller({_ENTITY_ID: _FakeState("unknown")})

        with pytest.raises(EntityNotFoundError):
            ha_get_entity_state_and_convert(caller, _ENTITY_ID, "boolean")

    def test_int_conversion(self) -> None:
        """Integer reads convert through the shared helper."""
        caller = _caller({_ENTITY_ID: _FakeState("42")})

        assert ha_get_entity_state_and_convert(caller, _ENTITY_ID, "int") == 42

    def test_unsupported_output_type_returns_nothing(self) -> None:
        """An unknown conversion target is logged and yields nothing."""
        caller = _caller({_ENTITY_ID: _FakeState("42")})

        with patch(f"{_MODULE}._LOGGER") as logger:
            assert (
                ha_get_entity_state_and_convert(caller, _ENTITY_ID, "duration") is None
            )

        logger.error.assert_called_once()

    def test_raw_state_object_is_returned_without_a_type(self) -> None:
        """Without an output type the state object itself is returned."""
        state = _FakeState("42")
        caller = _caller({_ENTITY_ID: state})

        assert ha_get_entity_state_and_convert(caller, _ENTITY_ID, None) is state

    def test_unknown_raw_state_raises(self) -> None:
        """An unknown state has no usable state object to return."""
        caller = _caller({_ENTITY_ID: _FakeState("unknown")})

        with pytest.raises(EntityNotFoundError):
            ha_get_entity_state_and_convert(caller, _ENTITY_ID, None)

    def test_conversion_failure_is_wrapped_as_a_ha_error(self) -> None:
        """An unexpected conversion failure surfaces as a HA error."""
        caller = _caller({_ENTITY_ID: _FakeState("42")})

        with (
            patch(f"{_MODULE}.convert_to_int", side_effect=TypeError("boom")),
            pytest.raises(HomeAssistantError, match="Error converting state"),
        ):
            ha_get_entity_state_and_convert(caller, _ENTITY_ID, "int")


class TestDeviceExists:
    """Device existence is answered from the device registry."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("device_entry", "expected"),
        [
            pytest.param(MagicMock(), True, id="known"),
            pytest.param(None, False, id="unknown"),
        ],
    )
    async def test_lookup(self, device_entry: Any, expected: bool) -> None:
        """A registered device exists; anything else does not."""
        registry = MagicMock()
        registry.async_get.return_value = device_entry

        with patch(f"{_MODULE}.dr.async_get", return_value=registry):
            assert await async_device_exists(MagicMock(), "device_id") is expected
