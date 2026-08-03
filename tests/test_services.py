"""Tests for HSEM service handlers.

Verifies that each service handler is registered with the correct signature and
that the handler functions accept a single :class:`homeassistant.core.ServiceCall`
argument (the Home Assistant instance is available via ``call.hass``).
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.hsem import services as services_module
from custom_components.hsem.const import DOMAIN
from custom_components.hsem.services import (
    SERVICE_CLEAR_OVERRIDE,
    SERVICE_CREATE_DASHBOARD,
    SERVICE_EXPORT_DIAGNOSTICS,
    SERVICE_FORCE_RECALCULATION,
    SERVICE_HANDLER_MAP,
    SERVICE_SET_TEMPORARY_OVERRIDE,
    async_register_services,
    async_unregister_services,
)


def _make_service_call(hass: HomeAssistant, data: dict | None = None) -> ServiceCall:
    """Return a minimal ServiceCall for use in handler tests."""
    return ServiceCall(
        hass=hass,
        domain=DOMAIN,
        service="test_service",
        data=data or {},
    )


@pytest.fixture
def mock_hass() -> MagicMock:
    """Return a mocked HomeAssistant with a mocked service registry."""
    hass = MagicMock(spec=HomeAssistant)
    hass.services = MagicMock()
    hass.services.has_service.return_value = False
    hass.services.async_call = AsyncMock()
    hass.states = MagicMock()
    hass.config_entries = MagicMock()
    hass.config_entries.async_entries.return_value = []
    return hass


@pytest.fixture
def mock_coordinator() -> MagicMock:
    """Return a mocked HSEM coordinator."""
    coordinator = MagicMock()
    coordinator.data = None
    coordinator._last_planner_input = MagicMock()
    coordinator._last_planner_output = MagicMock()
    coordinator._async_handle_update = AsyncMock()
    coordinator._override_expiry = None
    return coordinator


@pytest.fixture
def get_coordinator_patcher(mock_coordinator: MagicMock) -> Generator[None]:
    """Patch _get_coordinator so tests can target a known coordinator."""
    with patch.object(
        services_module,
        "_get_coordinator",
        return_value=mock_coordinator,
    ):
        yield


@pytest.mark.asyncio
async def test_register_and_unregister_services(mock_hass: MagicMock) -> None:
    """All services in SERVICE_HANDLER_MAP are registered and unregistered."""
    await async_register_services(mock_hass)

    registered_calls = mock_hass.services.async_register.call_args_list
    registered_names = {call.kwargs["service"] for call in registered_calls}
    assert registered_names == set(SERVICE_HANDLER_MAP)

    for call in registered_calls:
        assert call.kwargs["domain"] == DOMAIN
        assert isinstance(call.kwargs["schema"], vol.Schema)
        assert isinstance(call.kwargs["supports_response"], SupportsResponse)
        assert callable(call.kwargs["service_func"])

    mock_hass.services.has_service.return_value = True
    await async_unregister_services(mock_hass)
    removed_names = {
        call.args[1] for call in mock_hass.services.async_remove.call_args_list
    }
    assert removed_names == set(SERVICE_HANDLER_MAP)


@pytest.mark.asyncio
async def test_register_services_skips_existing(mock_hass: MagicMock) -> None:
    """Registration is a no-op when a service already exists."""
    mock_hass.services.has_service.return_value = True
    await async_register_services(mock_hass)
    mock_hass.services.async_register.assert_not_called()


@pytest.mark.asyncio
async def test_force_recalculation_no_coordinator_raises(
    mock_hass: MagicMock,
) -> None:
    """force_recalculation raises ServiceValidationError without a coordinator."""
    call = _make_service_call(mock_hass)

    with patch.object(services_module, "_get_coordinator", return_value=None):
        with pytest.raises(ServiceValidationError):
            await services_module.async_handle_force_recalculation(call)


@pytest.mark.asyncio
@pytest.mark.usefixtures("get_coordinator_patcher")
async def test_force_recalculation_triggers_update(
    mock_hass: MagicMock,
    mock_coordinator: MagicMock,
) -> None:
    """force_recalculation calls _async_handle_update on the coordinator."""
    call = _make_service_call(mock_hass)

    await services_module.async_handle_force_recalculation(call)

    mock_coordinator._async_handle_update.assert_awaited_once_with(None)


@pytest.mark.asyncio
async def test_set_temporary_override_missing_entity_raises(
    mock_hass: MagicMock,
) -> None:
    """set_temporary_override raises when the force-mode select entity is missing."""
    mock_hass.states.get.return_value = None
    call = _make_service_call(
        mock_hass, {"working_mode": "batteries_wait_mode", "duration_minutes": 30}
    )

    with pytest.raises(ServiceValidationError):
        await services_module.async_handle_set_temporary_override(call)


@pytest.mark.asyncio
@pytest.mark.usefixtures("get_coordinator_patcher")
async def test_set_temporary_override_calls_select_and_updates_coordinator(
    mock_hass: MagicMock,
    mock_coordinator: MagicMock,
) -> None:
    """set_temporary_override writes the select entity and updates the coordinator."""
    mock_hass.states.get.return_value = MagicMock()
    call = _make_service_call(
        mock_hass, {"working_mode": "batteries_wait_mode", "duration_minutes": 30}
    )

    await services_module.async_handle_set_temporary_override(call)

    mock_hass.services.async_call.assert_awaited_once_with(
        "select",
        "select_option",
        {
            "entity_id": "select.hsem_force_working_mode",
            "option": "batteries_wait_mode",
        },
        blocking=True,
    )
    mock_coordinator._async_handle_update.assert_awaited_once_with(None)
    assert mock_coordinator._override_expiry is not None


@pytest.mark.asyncio
@pytest.mark.usefixtures("get_coordinator_patcher")
async def test_set_temporary_override_without_duration(
    mock_hass: MagicMock,
    mock_coordinator: MagicMock,
) -> None:
    """set_temporary_override clears expiry when duration_minutes is omitted."""
    mock_hass.states.get.return_value = MagicMock()
    call = _make_service_call(mock_hass, {"working_mode": "batteries_wait_mode"})

    await services_module.async_handle_set_temporary_override(call)

    assert mock_coordinator._override_expiry is None


@pytest.mark.asyncio
async def test_clear_override_missing_entity_raises(
    mock_hass: MagicMock,
) -> None:
    """clear_override raises when the force-mode select entity is missing."""
    mock_hass.states.get.return_value = None
    call = _make_service_call(mock_hass)

    with pytest.raises(ServiceValidationError):
        await services_module.async_handle_clear_override(call)


@pytest.mark.asyncio
@pytest.mark.usefixtures("get_coordinator_patcher")
async def test_clear_override_resets_select_and_coordinator(
    mock_hass: MagicMock,
    mock_coordinator: MagicMock,
) -> None:
    """clear_override writes 'auto' to the select entity and clears expiry."""
    mock_hass.states.get.return_value = MagicMock()
    call = _make_service_call(mock_hass)

    await services_module.async_handle_clear_override(call)

    mock_hass.services.async_call.assert_awaited_once_with(
        "select",
        "select_option",
        {"entity_id": "select.hsem_force_working_mode", "option": "auto"},
        blocking=True,
    )
    assert mock_coordinator._override_expiry is None
    mock_coordinator._async_handle_update.assert_awaited_once_with(None)


@pytest.mark.asyncio
async def test_export_diagnostics_no_coordinator_raises(
    mock_hass: MagicMock,
) -> None:
    """export_diagnostics raises when no coordinator is loaded."""
    call = _make_service_call(mock_hass)

    with patch.object(services_module, "_get_coordinator", return_value=None):
        with pytest.raises(ServiceValidationError):
            await services_module.async_handle_export_diagnostics(call)


@pytest.mark.asyncio
async def test_export_diagnostics_no_planner_output_raises(
    mock_hass: MagicMock,
    mock_coordinator: MagicMock,
) -> None:
    """export_diagnostics raises when no planner cycle has completed."""
    mock_coordinator._last_planner_input = None
    mock_coordinator._last_planner_output = None
    call = _make_service_call(mock_hass)

    with patch.object(
        services_module, "_get_coordinator", return_value=mock_coordinator
    ):
        with pytest.raises(HomeAssistantError):
            await services_module.async_handle_export_diagnostics(call)


@pytest.mark.asyncio
@pytest.mark.usefixtures("get_coordinator_patcher")
async def test_export_diagnostics_returns_dump(
    mock_hass: MagicMock,
    mock_coordinator: MagicMock,
) -> None:
    """export_diagnostics returns the diagnostics dictionary."""
    call = _make_service_call(mock_hass)

    with patch(
        "custom_components.hsem.services.build_diagnostics_dump",
        return_value={"integration_version": "1.0.0"},
    ) as mock_dump:
        result = await services_module.async_handle_export_diagnostics(call)

    mock_dump.assert_called_once()
    assert result == {"integration_version": "1.0.0"}


@pytest.mark.asyncio
async def test_create_dashboard_missing_file_logs_error(
    mock_hass: MagicMock,
    tmp_path: Path,
) -> None:
    """create_dashboard logs an error when the bundled YAML is missing."""
    call = _make_service_call(mock_hass)

    with (
        patch.object(
            services_module,
            "__file__",
            str(tmp_path / "services.py"),
        ),
        patch.object(services_module, "_LOGGER") as mock_logger,
    ):
        await services_module.async_handle_create_dashboard(call)

    mock_logger.error.assert_called_once()


@pytest.mark.asyncio
async def test_create_dashboard_logs_path(
    mock_hass: MagicMock,
    tmp_path: Path,
) -> None:
    """create_dashboard logs the bundled YAML path when it exists."""
    dashboards_dir = tmp_path / "dashboards"
    dashboards_dir.mkdir()
    (dashboards_dir / "dashboard_en.yaml").write_text("title: Test")
    call = _make_service_call(mock_hass)

    with (
        patch.object(
            services_module,
            "__file__",
            str(tmp_path / "services.py"),
        ),
        patch.object(services_module, "_LOGGER") as mock_logger,
    ):
        await services_module.async_handle_create_dashboard(call)

    mock_logger.info.assert_called_once()
    assert str(dashboards_dir / "dashboard_en.yaml") in " ".join(
        str(arg) for arg in mock_logger.info.call_args[0]
    )
