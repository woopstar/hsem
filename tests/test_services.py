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
    SCHEMA_OCPP_DEBUG_SET_AVAILABILITY,
    SCHEMA_OCPP_DEBUG_SET_CONFIGURATION,
    SCHEMA_OCPP_DEBUG_START_CHARGING,
    SCHEMA_OCPP_DEBUG_STOP_CHARGING,
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


def _make_ocpp_server(cpid: str | None = "CP1") -> MagicMock:
    """Return a mocked OCPPServer with one connected charger by default."""
    server = MagicMock()
    server.active_chargers = [cpid] if cpid else []
    server.send_remote_start = AsyncMock(return_value=True)
    server.send_set_charging_profile = AsyncMock(return_value=True)
    server.send_remote_stop = AsyncMock(return_value=True)
    return server


def test_ocpp_debug_start_charging_schema_defaults() -> None:
    """charger defaults to 'primary', max_current_a defaults to 16."""
    result = SCHEMA_OCPP_DEBUG_START_CHARGING({})
    assert result == {"charger": "primary", "max_current_a": 16}


def test_ocpp_debug_start_charging_schema_rejects_unknown_charger() -> None:
    """An unsupported 'charger' value is rejected."""
    with pytest.raises(vol.Invalid):
        SCHEMA_OCPP_DEBUG_START_CHARGING({"charger": "third"})


def test_ocpp_debug_stop_charging_schema_defaults() -> None:
    """charger defaults to 'primary' for the stop schema too."""
    assert SCHEMA_OCPP_DEBUG_STOP_CHARGING({}) == {"charger": "primary"}


@pytest.mark.asyncio
async def test_ocpp_debug_start_charging_no_coordinator_raises(
    mock_hass: MagicMock,
) -> None:
    """ocpp_debug_start_charging raises without a coordinator."""
    call = _make_service_call(mock_hass, {"charger": "primary", "max_current_a": 16})
    with (
        patch.object(services_module, "_get_coordinator", return_value=None),
        pytest.raises(ServiceValidationError),
    ):
        await services_module.async_handle_ocpp_debug_start_charging(call)


@pytest.mark.asyncio
@pytest.mark.usefixtures("get_coordinator_patcher")
async def test_ocpp_debug_start_charging_server_not_running_raises(
    mock_hass: MagicMock,
    mock_coordinator: MagicMock,
) -> None:
    """Raises when the selected EV's OCPP server isn't running."""
    mock_coordinator._ocpp_server = None
    call = _make_service_call(mock_hass, {"charger": "primary", "max_current_a": 16})
    with pytest.raises(ServiceValidationError):
        await services_module.async_handle_ocpp_debug_start_charging(call)


@pytest.mark.asyncio
@pytest.mark.usefixtures("get_coordinator_patcher")
async def test_ocpp_debug_start_charging_no_charger_connected_raises(
    mock_hass: MagicMock,
    mock_coordinator: MagicMock,
) -> None:
    """Raises when the OCPP server is running but nothing is connected."""
    mock_coordinator._ocpp_server = _make_ocpp_server(cpid=None)
    call = _make_service_call(mock_hass, {"charger": "primary", "max_current_a": 16})
    with pytest.raises(ServiceValidationError):
        await services_module.async_handle_ocpp_debug_start_charging(call)


@pytest.mark.asyncio
@pytest.mark.usefixtures("get_coordinator_patcher")
async def test_ocpp_debug_start_charging_sends_start_and_profile(
    mock_hass: MagicMock,
    mock_coordinator: MagicMock,
) -> None:
    """Success path sends RemoteStartTransaction then SetChargingProfile."""
    server = _make_ocpp_server(cpid="CP1")
    mock_coordinator._ocpp_server = server
    call = _make_service_call(mock_hass, {"charger": "primary", "max_current_a": 10})

    await services_module.async_handle_ocpp_debug_start_charging(call)

    server.send_remote_start.assert_awaited_once_with("CP1")
    server.send_set_charging_profile.assert_awaited_once_with("CP1", 2300, 10)


@pytest.mark.asyncio
@pytest.mark.usefixtures("get_coordinator_patcher")
async def test_ocpp_debug_start_charging_targets_second_charger(
    mock_hass: MagicMock,
    mock_coordinator: MagicMock,
) -> None:
    """charger='second' targets the second EV's OCPP server."""
    mock_coordinator._ocpp_server = None
    second_server = _make_ocpp_server(cpid="CP2")
    mock_coordinator._ocpp_second_server = second_server
    call = _make_service_call(mock_hass, {"charger": "second", "max_current_a": 16})

    await services_module.async_handle_ocpp_debug_start_charging(call)

    second_server.send_remote_start.assert_awaited_once_with("CP2")


@pytest.mark.asyncio
@pytest.mark.usefixtures("get_coordinator_patcher")
async def test_ocpp_debug_start_charging_raises_when_send_fails(
    mock_hass: MagicMock,
    mock_coordinator: MagicMock,
) -> None:
    """A failed send raises HomeAssistantError rather than silently succeeding."""
    server = _make_ocpp_server(cpid="CP1")
    server.send_remote_start = AsyncMock(return_value=False)
    mock_coordinator._ocpp_server = server
    call = _make_service_call(mock_hass, {"charger": "primary", "max_current_a": 16})

    with pytest.raises(HomeAssistantError):
        await services_module.async_handle_ocpp_debug_start_charging(call)


@pytest.mark.asyncio
async def test_ocpp_debug_stop_charging_no_coordinator_raises(
    mock_hass: MagicMock,
) -> None:
    """ocpp_debug_stop_charging raises without a coordinator."""
    call = _make_service_call(mock_hass, {"charger": "primary"})
    with (
        patch.object(services_module, "_get_coordinator", return_value=None),
        pytest.raises(ServiceValidationError),
    ):
        await services_module.async_handle_ocpp_debug_stop_charging(call)


@pytest.mark.asyncio
@pytest.mark.usefixtures("get_coordinator_patcher")
async def test_ocpp_debug_stop_charging_no_charger_connected_raises(
    mock_hass: MagicMock,
    mock_coordinator: MagicMock,
) -> None:
    """Raises when nothing is connected to stop."""
    mock_coordinator._ocpp_server = _make_ocpp_server(cpid=None)
    call = _make_service_call(mock_hass, {"charger": "primary"})
    with pytest.raises(ServiceValidationError):
        await services_module.async_handle_ocpp_debug_stop_charging(call)


@pytest.mark.asyncio
@pytest.mark.usefixtures("get_coordinator_patcher")
async def test_ocpp_debug_stop_charging_sends_remote_stop(
    mock_hass: MagicMock,
    mock_coordinator: MagicMock,
) -> None:
    """Success path sends RemoteStopTransaction to the connected charger."""
    server = _make_ocpp_server(cpid="CP1")
    mock_coordinator._ocpp_server = server
    call = _make_service_call(mock_hass, {"charger": "primary"})

    await services_module.async_handle_ocpp_debug_stop_charging(call)

    server.send_remote_stop.assert_awaited_once_with("CP1")


@pytest.mark.asyncio
@pytest.mark.usefixtures("get_coordinator_patcher")
async def test_ocpp_debug_stop_charging_raises_when_send_fails(
    mock_hass: MagicMock,
    mock_coordinator: MagicMock,
) -> None:
    """A failed stop send raises HomeAssistantError."""
    server = _make_ocpp_server(cpid="CP1")
    server.send_remote_stop = AsyncMock(return_value=False)
    mock_coordinator._ocpp_server = server
    call = _make_service_call(mock_hass, {"charger": "primary"})

    with pytest.raises(HomeAssistantError):
        await services_module.async_handle_ocpp_debug_stop_charging(call)


@pytest.mark.asyncio
@pytest.mark.usefixtures("get_coordinator_patcher")
async def test_ocpp_debug_diagnostics_sends_both_queries(
    mock_hass: MagicMock,
    mock_coordinator: MagicMock,
) -> None:
    """ocpp_debug_diagnostics queries config and the computed schedule."""
    server = _make_ocpp_server(cpid="CP1")
    server.send_get_configuration = AsyncMock(return_value=True)
    server.send_get_composite_schedule = AsyncMock(return_value=True)
    mock_coordinator._ocpp_server = server
    call = _make_service_call(mock_hass, {"charger": "primary"})

    await services_module.async_handle_ocpp_debug_diagnostics(call)

    server.send_get_configuration.assert_awaited_once_with("CP1")
    server.send_get_composite_schedule.assert_awaited_once_with("CP1")


@pytest.mark.asyncio
@pytest.mark.usefixtures("get_coordinator_patcher")
async def test_ocpp_debug_diagnostics_no_charger_connected_raises(
    mock_hass: MagicMock,
    mock_coordinator: MagicMock,
) -> None:
    """Raises when nothing is connected to interrogate."""
    mock_coordinator._ocpp_server = _make_ocpp_server(cpid=None)
    call = _make_service_call(mock_hass, {"charger": "primary"})
    with pytest.raises(ServiceValidationError):
        await services_module.async_handle_ocpp_debug_diagnostics(call)


@pytest.mark.asyncio
@pytest.mark.usefixtures("get_coordinator_patcher")
async def test_ocpp_debug_diagnostics_raises_when_send_fails(
    mock_hass: MagicMock,
    mock_coordinator: MagicMock,
) -> None:
    """A failed diagnostic send raises rather than reporting success."""
    server = _make_ocpp_server(cpid="CP1")
    server.send_get_configuration = AsyncMock(return_value=False)
    server.send_get_composite_schedule = AsyncMock(return_value=True)
    mock_coordinator._ocpp_server = server
    call = _make_service_call(mock_hass, {"charger": "primary"})

    with pytest.raises(HomeAssistantError):
        await services_module.async_handle_ocpp_debug_diagnostics(call)


@pytest.mark.asyncio
@pytest.mark.usefixtures("get_coordinator_patcher")
async def test_ocpp_debug_set_availability_sends_command(
    mock_hass: MagicMock,
    mock_coordinator: MagicMock,
) -> None:
    """ocpp_debug_set_availability forwards operative/connector through."""
    server = _make_ocpp_server(cpid="CP1")
    server.send_change_availability = AsyncMock(return_value=True)
    mock_coordinator._ocpp_server = server
    call = _make_service_call(
        mock_hass, {"charger": "primary", "operative": False, "connector_id": 0}
    )

    await services_module.async_handle_ocpp_debug_set_availability(call)

    server.send_change_availability.assert_awaited_once_with(
        "CP1", operative=False, connector_id=0
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("get_coordinator_patcher")
async def test_ocpp_debug_set_configuration_sends_key_value(
    mock_hass: MagicMock,
    mock_coordinator: MagicMock,
) -> None:
    """ocpp_debug_set_configuration forwards the key/value verbatim."""
    server = _make_ocpp_server(cpid="CP1")
    server.send_change_configuration = AsyncMock(return_value=True)
    mock_coordinator._ocpp_server = server
    call = _make_service_call(
        mock_hass,
        {"charger": "primary", "key": "AuthorizeRemoteTxRequests", "value": "false"},
    )

    await services_module.async_handle_ocpp_debug_set_configuration(call)

    server.send_change_configuration.assert_awaited_once_with(
        "CP1", "AuthorizeRemoteTxRequests", "false"
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("get_coordinator_patcher")
async def test_ocpp_debug_set_configuration_raises_when_send_fails(
    mock_hass: MagicMock,
    mock_coordinator: MagicMock,
) -> None:
    """A rejected/failed ChangeConfiguration send surfaces as an error."""
    server = _make_ocpp_server(cpid="CP1")
    server.send_change_configuration = AsyncMock(return_value=False)
    mock_coordinator._ocpp_server = server
    call = _make_service_call(
        mock_hass, {"charger": "primary", "key": "K", "value": "V"}
    )

    with pytest.raises(HomeAssistantError):
        await services_module.async_handle_ocpp_debug_set_configuration(call)


def test_ocpp_debug_set_configuration_schema_requires_key_and_value() -> None:
    """key/value are mandatory — there is no sensible default for either."""
    with pytest.raises(vol.Invalid):
        SCHEMA_OCPP_DEBUG_SET_CONFIGURATION({"key": "OnlyKey"})
    result = SCHEMA_OCPP_DEBUG_SET_CONFIGURATION({"key": "K", "value": "V"})
    assert result["charger"] == "primary"  # type: ignore[index]


def test_ocpp_debug_set_availability_schema_defaults() -> None:
    """Availability defaults to Operative on connector 1."""
    result = SCHEMA_OCPP_DEBUG_SET_AVAILABILITY({})
    assert result == {"charger": "primary", "operative": True, "connector_id": 1}


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
