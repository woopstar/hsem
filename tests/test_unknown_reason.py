"""Tests for the classification of unknown-sensor reasons (issue #1482)."""

from __future__ import annotations

from types import ModuleType

import pytest

from custom_components.hsem import coordinator_performance
from custom_components.hsem.models import (
    energy_round_trip,
    energy_tracker,
    performance_record,
    performance_tracker,
)
from custom_components.hsem.utils import efficiency_learner, unknown_reason
from custom_components.hsem.utils.unknown_reason import (
    LOG_LEVELS,
    UNKNOWN_REASONS,
    UNMAPPED_REASON,
    UnknownKind,
    classify,
    reason_kind,
    unknown_reason_attributes,
)

#: Every module that defines a reason code of a sensor.
_MODULES: tuple[ModuleType, ...] = (
    energy_round_trip,
    energy_tracker,
    performance_record,
    performance_tracker,
    coordinator_performance,
    efficiency_learner,
    unknown_reason,
)

#: Constants that match the pattern but are no reason for an unknown value:
#: with the status ``ok`` the battery efficiency sensor has a value.
_NOT_A_REASON = {"STATUS_OK"}


def _constants() -> dict[str, str]:
    """Return every ``REASON_*`` / ``STATUS_*`` string constant by its name."""
    found: dict[str, str] = {}
    for module in _MODULES:
        for name, value in vars(module).items():
            if (
                name.startswith(("REASON_", "STATUS_"))
                and name not in _NOT_A_REASON
                and isinstance(value, str)
            ):
                found[f"{module.__name__}.{name}"] = value
    return found


class TestRegistry:
    def test_every_reason_constant_is_in_the_registry(self) -> None:
        """A new reason code must be given a kind before it ships."""
        missing = {
            name: value
            for name, value in _constants().items()
            if value not in UNKNOWN_REASONS
        }

        assert missing == {}

    def test_every_registry_entry_has_a_constant(self) -> None:
        """A code that no module sets any more must leave the registry."""
        assert set(UNKNOWN_REASONS) - set(_constants().values()) == set()

    def test_the_scan_finds_the_codes_of_every_module(self) -> None:
        """The scan is not vacuous: each module contributes a constant."""
        names = _constants()

        for module in _MODULES:
            assert any(name.startswith(f"{module.__name__}.") for name in names)

    def test_every_entry_has_a_kind_and_a_sentence(self) -> None:
        for code, reason in UNKNOWN_REASONS.items():
            assert isinstance(reason.kind, UnknownKind), code
            assert reason.sentence.endswith("."), code

    def test_every_kind_has_a_log_level(self) -> None:
        assert LOG_LEVELS == {
            UnknownKind.WAITING: "info",
            UnknownKind.NOT_CONFIGURED: "info",
            UnknownKind.ACTION_NEEDED: "warning",
            UnknownKind.FAULT: "error",
        }


class TestClassify:
    @pytest.mark.parametrize(
        ("code", "kind"),
        [
            ("throughput_below_minimum", UnknownKind.WAITING),
            ("soc_change_too_large", UnknownKind.WAITING),
            ("learning", UnknownKind.WAITING),
            ("not_evaluated", UnknownKind.WAITING),
            ("ac_battery_power_not_configured", UnknownKind.NOT_CONFIGURED),
            ("cycle_cost_unknown", UnknownKind.NOT_CONFIGURED),
            ("ac_battery_power_unit_not_power", UnknownKind.ACTION_NEEDED),
            ("capacity_unknown", UnknownKind.ACTION_NEEDED),
            ("implausible_ratio", UnknownKind.FAULT),
            ("implausible", UnknownKind.FAULT),
            ("evaluation_failed", UnknownKind.FAULT),
        ],
    )
    def test_kind_of_a_code(self, code: str, kind: UnknownKind) -> None:
        assert classify(code).kind is kind
        assert reason_kind(code) == kind.value

    def test_an_unmapped_code_is_a_fault(self) -> None:
        assert classify("a_code_nobody_mapped") is UNMAPPED_REASON
        assert UNMAPPED_REASON.kind is UnknownKind.FAULT
        assert reason_kind("a_code_nobody_mapped") == "fault"

    def test_no_code_has_no_kind(self) -> None:
        assert reason_kind(None) is None

    def test_attributes_of_a_reason(self) -> None:
        assert unknown_reason_attributes("learning") == {
            "reason": "learning",
            "reason_kind": "waiting",
        }
        assert unknown_reason_attributes(None) == {
            "reason": None,
            "reason_kind": None,
        }
