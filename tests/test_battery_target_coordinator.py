"""House-battery target diagnostics through the coordinator (issue #1109).

The planner's next-occurrence record must reach the published
``CoordinatorData`` snapshot (and from there the working-mode sensor), and a
stale or failed cycle must not leave a newer record behind.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from custom_components.hsem.coordinator import CoordinatorData
from custom_components.hsem.models.planner_output import PlannerOutput
from tests.test_ha_mock_integration import (
    _BASE_ENTITY_STATES,
    _patch_all_ha_helpers,
    make_bare_coordinator,
    make_fake_config_entry,
    make_fake_hass,
)

_REPORT = {
    "target_time": "2026-09-14T17:00:00+02:00",
    "target_kwh": 9.0,
    "stage2_ran": True,
    "stage2_status": "solved",
}


def test_planner_output_record_is_kept_for_the_next_snapshot() -> None:
    """Applying a planner output stores its battery-target record."""
    coord = make_bare_coordinator()
    assert coord._battery_target_diagnostics is None

    coord._apply_planner_output(PlannerOutput(slots=[], battery_target=_REPORT))
    assert coord._battery_target_diagnostics == _REPORT

    # A later plan with the target disabled clears it again.
    coord._apply_planner_output(PlannerOutput(slots=[]))
    assert coord._battery_target_diagnostics is None


def test_record_is_part_of_the_accepted_plan_state() -> None:
    """A discarded cycle restores the previously accepted record."""
    coord = make_bare_coordinator()
    coord._battery_target_diagnostics = _REPORT
    accepted = coord._capture_accepted_plan_state()

    coord._battery_target_diagnostics = {"stage2_status": "failed"}
    coord._restore_accepted_plan_state(accepted)

    assert coord._battery_target_diagnostics == _REPORT


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_published_snapshot_carries_the_planner_record(enabled: bool) -> None:
    """``CoordinatorData.battery_target`` is whatever the planner last reported."""
    config_entry = make_fake_config_entry(
        {"hsem_read_only": True, "hsem_batteries_target_soc_enabled": enabled}
    )
    coord = make_bare_coordinator(
        hass=make_fake_hass(_BASE_ENTITY_STATES), config_entry=config_entry
    )
    coord._set_update_interval = AsyncMock()  # type: ignore[method-assign]  # test monkey-patch
    captured: list[CoordinatorData] = []
    coord.async_set_updated_data = captured.append  # type: ignore[method-assign, assignment]  # test monkey-patch

    with _patch_all_ha_helpers():
        await coord._async_run_update_cycle()

    assert len(captured) == 1
    assert captured[0].battery_target == coord._battery_target_diagnostics
    assert coord._cfg.batteries_target_soc_enabled is enabled
    if not enabled:
        assert captured[0].battery_target is None
