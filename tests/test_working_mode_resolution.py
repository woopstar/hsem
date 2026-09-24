"""Tests for topology-independent Huawei working-mode resolution."""

import pytest

from custom_components.hsem.utils.workingmodes import (
    WorkingModes,
    resolve_working_mode_option,
)


@pytest.mark.parametrize(
    ("intent", "options", "expected"),
    [
        (
            WorkingModes.TimeOfUse.value,
            ["time_of_use_luna2000", "maximise_self_consumption"],
            "time_of_use_luna2000",
        ),
        (
            WorkingModes.MaximizeSelfConsumption.value,
            ["time_of_use", "maximum_self_consumption"],
            "maximum_self_consumption",
        ),
        (
            WorkingModes.FullyFedToGrid.value,
            ["fully_fed_to_grid"],
            "fully_fed_to_grid",
        ),
        (WorkingModes.TimeOfUse.value, ["maximum_self_consumption"], None),
    ],
)
def test_resolve_working_mode_option_uses_supported_value(
    intent: str, options: list[str], expected: str | None
) -> None:
    """HSEM maps a planner intent only to an advertised select option."""
    assert resolve_working_mode_option(intent, options) == expected


def test_resolve_working_mode_option_preserves_legacy_value_without_options() -> None:
    """Startup without select metadata retains the existing LUNA behavior."""
    assert (
        resolve_working_mode_option(WorkingModes.TimeOfUse.value, None)
        == WorkingModes.TimeOfUse.value
    )
