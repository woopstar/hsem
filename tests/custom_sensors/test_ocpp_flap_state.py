"""Tests for the anti-flap FlapState enum (issue #1024).

The state machine was previously driven by bare string literals compared in 16
places across 7 files, with ``_flap_state`` typed only as ``str`` — so a typo in
any comparison failed silently while driving physical EV charger commands.

These tests pin the values (the no-migration guarantee) and the exposure
boundary, so the refactor stays behaviour-preserving.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.hsem.coordinator_data import CoordinatorData
from custom_components.hsem.custom_sensors.ocpp_flap_state import FlapState

_SOURCE_FILES = (
    "custom_sensors/ocpp_anti_flap.py",
    "custom_sensors/ocpp_server.py",
    "custom_sensors/ocpp_message_handlers.py",
    "custom_sensors/ocpp_commands.py",
    "coordinator_cycle.py",
    "coordinator_data.py",
)
_COMPONENT_DIR = Path(__file__).parent.parent.parent / "custom_components" / "hsem"


class TestFlapStateValues:
    """Values must be byte-identical to the previous raw strings."""

    @pytest.mark.parametrize(
        ("member", "expected"),
        [
            (FlapState.Idle, "idle"),
            (FlapState.Starting, "starting"),
            (FlapState.Charging, "charging"),
            (FlapState.Stopping, "stopping"),
        ],
    )
    def test_value_is_unchanged(self, member: FlapState, expected: str) -> None:
        assert member.value == expected

    def test_no_extra_members(self) -> None:
        """A fifth state would need handling in every transition branch."""
        assert {m.value for m in FlapState} == {
            "idle",
            "starting",
            "charging",
            "stopping",
        }

    def test_compares_equal_to_raw_string(self) -> None:
        """StrEnum keeps any remaining string comparison working."""
        assert FlapState.Charging == "charging"
        assert FlapState.Idle != "charging"

    def test_str_renders_the_value_not_the_member(self) -> None:
        """``str()`` on a StrEnum yields the value — unlike a plain Enum."""
        assert str(FlapState.Stopping) == "stopping"

    def test_serialises_as_a_plain_string(self) -> None:
        """The value reaches HA as an entity attribute, so JSON must be flat."""
        assert json.dumps({"anti_flap_state": FlapState.Starting}) == (
            '{"anti_flap_state": "starting"}'
        )


class TestFlapStateExposure:
    """The published attribute must stay a plain string."""

    def test_coordinator_data_defaults_to_idle(self) -> None:
        data = CoordinatorData()
        assert data.ocpp_anti_flap_state == "idle"
        assert data.ocpp_second_anti_flap_state == "idle"

    def test_anti_flap_state_property_returns_a_plain_str(self) -> None:
        """``anti_flap_state`` coerces with ``str()`` — not a FlapState member."""
        from custom_components.hsem.custom_sensors.ocpp_server import OCPPServer

        server = object.__new__(OCPPServer)
        server._flap_state = FlapState.Charging
        value = server.anti_flap_state
        assert value == "charging"
        assert type(value) is str


class TestNoRawFlapStateLiterals:
    """Guard against a future re-hardcode of the state values."""

    @pytest.mark.parametrize("filename", _SOURCE_FILES)
    def test_flap_state_is_never_compared_to_a_literal(self, filename: str) -> None:
        """No ``_flap_state`` line may contain a quoted string."""
        offenders = [
            line.strip()
            for line in (_COMPONENT_DIR / filename)
            .read_text(encoding="utf-8")
            .splitlines()
            if "_flap_state" in line
            and '"' in line
            and not line.lstrip().startswith("#")
        ]
        assert offenders == []
