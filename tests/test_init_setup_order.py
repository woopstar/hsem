"""Tests for HSEM's config-entry setup ordering (issue #926).

Regression for the intermittent "select.hsem_force_working_mode not found in
Home Assistant" warning: ``async_setup_entry`` used to run the coordinator's
first update cycle (which reads HSEM's own select/number/switch/time entities
back via ``hass.states``) *before* forwarding platform setups — so those
entities did not exist yet in the state machine. The fix moves the first
cycle to run only after platform setups are forwarded.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem import HSEMRuntimeData, async_setup_entry


@pytest.fixture
def mock_hass() -> MagicMock:
    """Return a mocked HomeAssistant with a recordable platform-forward call."""
    hass = MagicMock()
    hass.config_entries = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)
    return hass


@pytest.mark.asyncio
async def test_first_refresh_runs_after_platforms_are_forwarded(
    mock_hass: MagicMock,
) -> None:
    """The coordinator's first cycle must run after platforms are forwarded.

    Before the fix, ``coordinator.async_setup()`` itself ran the first cycle,
    so it always preceded ``async_forward_entry_setups``. Now the first cycle
    lives in ``coordinator.async_run_first_refresh()``, called only after
    platforms are forwarded.
    """
    call_order: list[str] = []

    mock_coordinator = MagicMock()

    async def _record_setup() -> None:
        call_order.append("coordinator.async_setup")

    async def _record_forward(*_args: object, **_kwargs: object) -> bool:
        call_order.append("async_forward_entry_setups")
        return True

    async def _record_first_refresh() -> None:
        call_order.append("coordinator.async_run_first_refresh")

    mock_coordinator.async_setup = AsyncMock(side_effect=_record_setup)
    mock_coordinator.async_run_first_refresh = AsyncMock(
        side_effect=_record_first_refresh
    )
    mock_hass.config_entries.async_forward_entry_setups = AsyncMock(
        side_effect=_record_forward
    )

    entry = MagicMock()
    entry.runtime_data = None

    with (
        patch(
            "custom_components.hsem.check_huawei_solar_version",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "custom_components.hsem.async_init_hsem_logger",
            new=AsyncMock(),
        ),
        patch(
            "custom_components.hsem.HSEMDataUpdateCoordinator",
            return_value=mock_coordinator,
        ),
    ):
        result = await async_setup_entry(mock_hass, entry)

    assert result is True
    assert call_order == [
        "coordinator.async_setup",
        "async_forward_entry_setups",
        "coordinator.async_run_first_refresh",
    ]
    assert entry.runtime_data == HSEMRuntimeData(coordinator=mock_coordinator)
