"""Tests for HSEM service handlers.

Verifies that each service handler is registered with the correct signature and
that the handler functions accept a single :class:`homeassistant.core.ServiceCall`
argument (the Home Assistant instance is available via ``call.hass``).
"""

from __future__ import annotations

import json
from collections.abc import Generator
from datetime import UTC
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.hsem import services as services_module
from custom_components.hsem.const import DOMAIN
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.services import (
    SCHEMA_CREATE_DASHBOARD,
    SERVICE_HANDLER_MAP,
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


@pytest.fixture(autouse=True)
def integration_version_patcher() -> Generator[AsyncMock]:
    """Keep service tests on the async manifest-version boundary."""
    version_lookup = AsyncMock(return_value="7.3.1")
    with patch.object(
        services_module,
        "async_get_hsem_integration_version",
        version_lookup,
    ):
        yield version_lookup


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

    with (
        patch.object(services_module, "_get_coordinator", return_value=None),
        pytest.raises(ServiceValidationError),
    ):
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

    with (
        patch.object(services_module, "_get_coordinator", return_value=None),
        pytest.raises(ServiceValidationError),
    ):
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

    with (
        patch.object(
            services_module, "_get_coordinator", return_value=mock_coordinator
        ),
        pytest.raises(HomeAssistantError),
    ):
        await services_module.async_handle_export_diagnostics(call)


@pytest.mark.asyncio
@pytest.mark.usefixtures("get_coordinator_patcher")
async def test_export_diagnostics_returns_dump(
    mock_hass: MagicMock,
    mock_coordinator: MagicMock,
    integration_version_patcher: AsyncMock,
) -> None:
    """export_diagnostics returns the diagnostics dictionary."""
    call = _make_service_call(mock_hass)

    with patch(
        "custom_components.hsem.services.build_diagnostics_dump",
        return_value={"integration_version": "1.0.0"},
    ) as mock_dump:
        result = await services_module.async_handle_export_diagnostics(call)

    mock_dump.assert_called_once()
    assert mock_dump.call_args.kwargs["integration_version"] == "7.3.1"
    integration_version_patcher.assert_awaited_once_with(mock_hass)
    assert result == {"integration_version": "1.0.0"}


@pytest.mark.asyncio
@pytest.mark.usefixtures("get_coordinator_patcher")
async def test_export_diagnostics_dump_is_json_serializable(
    mock_hass: MagicMock,
    mock_coordinator: MagicMock,
) -> None:
    """export_diagnostics must return a response that Home Assistant can serialize."""
    from datetime import datetime

    from custom_components.hsem.models.planner_output import PlannerOutput

    planner_input = PlannerInput(
        ev_planned_load_deadline=datetime(2026, 8, 3, 12, 0, tzinfo=UTC),
        ev_second_planned_load_deadline=datetime(2026, 8, 3, 18, 0, tzinfo=UTC),
    )
    planner_input.solar_corrector = object()  # type: ignore[attr-defined]
    mock_coordinator._last_planner_input = planner_input
    mock_coordinator._last_planner_output = PlannerOutput()

    result = await services_module.async_handle_export_diagnostics(
        _make_service_call(mock_hass)
    )

    # This is the same serialization path Home Assistant uses for service
    # responses. It must not raise on datetime fields or runtime objects.
    json.dumps(result, default=str)
    assert "planner_input" in result
    assert result["planner_input"]["solar_corrector"] is None
    assert (
        result["planner_input"]["ev_planned_load_deadline"]
        == "2026-08-03T12:00:00+00:00"
    )
    assert (
        result["planner_input"]["ev_second_planned_load_deadline"]
        == "2026-08-03T18:00:00+00:00"
    )


def test_create_dashboard_schema_accepts_empty_data() -> None:
    """create_dashboard schema accepts an empty dict."""
    assert SCHEMA_CREATE_DASHBOARD({}) == {}


def test_create_dashboard_schema_accepts_dashboard_path() -> None:
    """create_dashboard schema accepts an optional dashboard_path."""
    result = SCHEMA_CREATE_DASHBOARD({"dashboard_path": "/config/my_hsem.yaml"})
    assert result["dashboard_path"] == "/config/my_hsem.yaml"  # type: ignore[index]


@pytest.mark.asyncio
async def test_create_dashboard_calls_helper_and_returns_result(
    mock_hass: MagicMock,
) -> None:
    """create_dashboard delegates to the provisioning helper and returns its result."""
    call = _make_service_call(mock_hass)

    with patch.object(
        services_module,
        "async_ensure_hsem_dashboard",
        new=AsyncMock(
            return_value={
                "dashboard_path": "/config/hsem_dashboard.yaml",
                "dashboard_url": "/hsem-dashboard",
            }
        ),
    ) as mock_helper:
        result = await services_module.async_handle_create_dashboard(call)

    mock_helper.assert_awaited_once_with(mock_hass, dashboard_path=None)
    assert result == {
        "dashboard_path": "/config/hsem_dashboard.yaml",
        "dashboard_url": "/hsem-dashboard",
    }


@pytest.mark.asyncio
async def test_create_dashboard_passes_custom_path_to_helper(
    mock_hass: MagicMock,
) -> None:
    """create_dashboard forwards a custom dashboard_path to the helper."""
    call = _make_service_call(mock_hass, {"dashboard_path": "/config/custom.yaml"})

    with patch.object(
        services_module,
        "async_ensure_hsem_dashboard",
        new=AsyncMock(
            return_value={
                "dashboard_path": "/config/custom.yaml",
                "dashboard_url": "/hsem-dashboard",
            }
        ),
    ) as mock_helper:
        result = await services_module.async_handle_create_dashboard(call)

    mock_helper.assert_awaited_once_with(
        mock_hass, dashboard_path=Path("/config/custom.yaml")
    )
    assert result["dashboard_path"] == "/config/custom.yaml"
