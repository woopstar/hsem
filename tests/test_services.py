"""Tests for the HSEM service handlers (issue #319).

Acceptance criteria
-------------------
1. ``force_recalculation`` triggers a coordinator update cycle.
2. ``set_temporary_override`` sets the force-mode select and triggers an update.
3. ``clear_override`` resets the force-mode select to ``"auto"`` and triggers
   an update.
4. ``export_diagnostics`` returns a structured diagnostics dump.
5. All services validate input (voluptuous schemas).
6. Services raise ``HomeAssistantError`` when the coordinator is unavailable.
7. The force-mode select entity existence check traps missing entities.
8. The schema for ``set_temporary_override`` rejects invalid working modes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.exceptions import HomeAssistantError

from custom_components.hsem.const import DOMAIN
from custom_components.hsem.coordinator import HSEMDataUpdateCoordinator
from custom_components.hsem.services import (
    SCHEMA_CLEAR_OVERRIDE,
    SCHEMA_EXPORT_DIAGNOSTICS,
    SCHEMA_FORCE_RECALCULATION,
    SCHEMA_SET_TEMPORARY_OVERRIDE,
    SERVICE_CLEAR_OVERRIDE,
    SERVICE_EXPORT_DIAGNOSTICS,
    SERVICE_FORCE_RECALCULATION,
    SERVICE_SET_TEMPORARY_OVERRIDE,
    SUPPORTED_OVERRIDE_MODES,
    async_handle_clear_override,
    async_handle_export_diagnostics,
    async_handle_force_recalculation,
    async_handle_set_temporary_override,
    async_register_services,
    async_unregister_services,
)
from custom_components.hsem.utils.sensornames import (
    get_force_working_mode_selector_entity_id,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_hass(coordinator: MagicMock | None = None) -> MagicMock:
    """Return a minimal mocked ``hass`` with an HSEM coordinator in domain data."""
    hass = MagicMock()
    hass.data = {DOMAIN: {}}
    if coordinator is not None:
        hass.data[DOMAIN]["test_entry"] = {"coordinator": coordinator}
    # Add a simple state machine that returns None by default.
    state_mock = MagicMock()
    state_mock.state = "auto"
    hass.states.get.return_value = state_mock
    hass.services.async_call = AsyncMock()
    hass.services.has_service.side_effect = lambda domain, _: domain == DOMAIN
    hass.services.async_remove = MagicMock()
    hass.config_entries.async_entries.return_value = []
    return hass


def _make_coordinator(hass: MagicMock | None = None) -> MagicMock:
    """Return a mocked HSEMDataUpdateCoordinator."""
    coordinator = MagicMock(spec=HSEMDataUpdateCoordinator)
    coordinator._async_handle_update = AsyncMock()  # noqa: SLF001
    coordinator._last_planner_input = MagicMock()
    coordinator._last_planner_output = MagicMock()
    coordinator.data = MagicMock()
    coordinator.data.apply_summary = MagicMock()
    if hass is not None:
        coordinator.hass = hass
    return coordinator


# ---------------------------------------------------------------------------
# Schema validation tests
# ---------------------------------------------------------------------------


class TestSchemaForceRecalculation:
    """Voluptuous schema for force_recalculation accepts only empty data."""

    def test_empty_data_accepted(self):
        """Schema must accept an empty dict."""
        assert SCHEMA_FORCE_RECALCULATION({}) == {}

    def test_extra_fields_rejected(self):
        """Schema must reject unexpected keys."""
        with pytest.raises(vol.Invalid):
            SCHEMA_FORCE_RECALCULATION({"unknown": "value"})


class TestSchemaSetTemporaryOverride:
    """Voluptuous schema for set_temporary_override input validation."""

    def test_valid_mode_accepted(self):
        """Schema must accept a valid working mode."""
        for mode in SUPPORTED_OVERRIDE_MODES:
            result = SCHEMA_SET_TEMPORARY_OVERRIDE({"working_mode": mode})
            assert result == {"working_mode": mode}

    def test_invalid_mode_rejected(self):
        """Schema must reject an invalid working mode."""
        with pytest.raises(vol.Invalid):
            SCHEMA_SET_TEMPORARY_OVERRIDE({"working_mode": "invalid_mode"})

    def test_missing_working_mode_rejected(self):
        """Schema must reject when working_mode is missing."""
        with pytest.raises(vol.Invalid):
            SCHEMA_SET_TEMPORARY_OVERRIDE({})

    def test_empty_string_rejected(self):
        """Schema must reject an empty string working mode."""
        with pytest.raises(vol.Invalid):
            SCHEMA_SET_TEMPORARY_OVERRIDE({"working_mode": ""})


class TestSchemaClearOverride:
    """Voluptuous schema for clear_override accepts only empty data."""

    def test_empty_data_accepted(self):
        """Schema must accept an empty dict."""
        assert SCHEMA_CLEAR_OVERRIDE({}) == {}

    def test_extra_fields_rejected(self):
        """Schema must reject unexpected keys."""
        with pytest.raises(vol.Invalid):
            SCHEMA_CLEAR_OVERRIDE({"working_mode": "auto"})


class TestSchemaExportDiagnostics:
    """Voluptuous schema for export_diagnostics accepts only empty data."""

    def test_empty_data_accepted(self):
        """Schema must accept an empty dict."""
        assert SCHEMA_EXPORT_DIAGNOSTICS({}) == {}

    def test_extra_fields_rejected(self):
        """Schema must reject unexpected keys."""
        with pytest.raises(vol.Invalid):
            SCHEMA_EXPORT_DIAGNOSTICS({"format": "json"})


# ---------------------------------------------------------------------------
# Service registration / unregistration
# ---------------------------------------------------------------------------


class TestServiceRegistration:
    """Service registration and unregistration logic."""

    @pytest.mark.asyncio
    async def test_register_services(self):
        """Must register all four HSEM services."""
        hass = _make_hass()
        # Clear the side_effect so return_value takes effect.
        hass.services.has_service.side_effect = None
        hass.services.has_service.return_value = False

        await async_register_services(hass)

        expected_calls = {
            SERVICE_FORCE_RECALCULATION,
            SERVICE_SET_TEMPORARY_OVERRIDE,
            SERVICE_CLEAR_OVERRIDE,
            SERVICE_EXPORT_DIAGNOSTICS,
        }
        actual_calls = {
            call.kwargs["service"]
            for call in hass.services.async_register.call_args_list
        }
        assert actual_calls == expected_calls

    @pytest.mark.asyncio
    async def test_register_services_skips_existing(self):
        """Must not re-register services that already exist."""
        hass = _make_hass()
        hass.services.has_service.return_value = True

        await async_register_services(hass)

        hass.services.async_register.assert_not_called()

    @pytest.mark.asyncio
    async def test_unregister_services(self):
        """Must remove all four HSEM services."""
        hass = _make_hass()
        hass.services.has_service.return_value = True

        await async_unregister_services(hass)

        assert hass.services.async_remove.call_count == 4

    @pytest.mark.asyncio
    async def test_unregister_services_skips_missing(self):
        """Must not try to remove services that don't exist."""
        hass = _make_hass()
        # Override side_effect so has_service returns False for all services.
        hass.services.has_service.side_effect = None
        hass.services.has_service.return_value = False

        await async_unregister_services(hass)

        hass.services.async_remove.assert_not_called()


# ---------------------------------------------------------------------------
# Service handler tests
# ---------------------------------------------------------------------------


class TestForceRecalculation:
    """Tests for async_handle_force_recalculation."""

    @pytest.mark.asyncio
    async def test_triggers_update_cycle(self):
        """Must call _async_handle_update on the coordinator."""
        coordinator = _make_coordinator()
        hass = _make_hass(coordinator)

        await async_handle_force_recalculation(hass, MagicMock())

        coordinator._async_handle_update.assert_awaited_once_with(None)

    @pytest.mark.asyncio
    async def test_raises_when_no_coordinator(self):
        """Must raise HomeAssistantError when no coordinator is found."""
        hass = _make_hass()  # No coordinator added

        with pytest.raises(HomeAssistantError, match="HSEM coordinator not found"):
            await async_handle_force_recalculation(hass, MagicMock())


class TestSetTemporaryOverride:
    """Tests for async_handle_set_temporary_override."""

    @pytest.mark.asyncio
    async def test_sets_override_and_triggers_update(self):
        """Must set the select entity and trigger an update cycle."""
        coordinator = _make_coordinator()
        hass = _make_hass(coordinator)

        call = MagicMock()
        call.data = {"working_mode": "batteries_charge_grid"}

        await async_handle_set_temporary_override(hass, call)

        selector_entity_id = get_force_working_mode_selector_entity_id()
        hass.services.async_call.assert_awaited_with(
            "select",
            "select_option",
            {"entity_id": selector_entity_id, "option": "batteries_charge_grid"},
            blocking=True,
        )
        coordinator._async_handle_update.assert_awaited_once_with(None)

    @pytest.mark.asyncio
    async def test_raises_when_entity_not_found(self):
        """Must raise HomeAssistantError when select entity is missing."""
        coordinator = _make_coordinator()
        hass = _make_hass(coordinator)
        # Simulate missing entity by returning None from states.get.
        hass.states.get.return_value = None

        call = MagicMock()
        call.data = {"working_mode": "batteries_charge_grid"}

        with pytest.raises(HomeAssistantError, match="force working mode entity"):
            await async_handle_set_temporary_override(hass, call)

        # The service call should NOT have been made.
        hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_raises_when_no_coordinator_still_calls_select(self):
        """Must still set the select entity even when coordinator is absent."""
        hass = _make_hass()  # No coordinator

        call = MagicMock()
        call.data = {"working_mode": "force_export"}

        await async_handle_set_temporary_override(hass, call)

        # The select call should have been made regardless.
        hass.services.async_call.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_all_modes_accepted(self):
        """Must accept every supported override mode."""
        coordinator = _make_coordinator()
        hass = _make_hass(coordinator)

        for mode in SUPPORTED_OVERRIDE_MODES:
            call = MagicMock()
            call.data = {"working_mode": mode}
            await async_handle_set_temporary_override(hass, call)
            assert True  # No exception raised

    @pytest.mark.asyncio
    async def test_handles_select_service_error(self):
        """Must propagate errors from the select service call."""
        coordinator = _make_coordinator()
        hass = _make_hass(coordinator)
        hass.services.async_call.side_effect = HomeAssistantError("select failed")

        call = MagicMock()
        call.data = {"working_mode": "batteries_charge_grid"}

        with pytest.raises(HomeAssistantError, match="select failed"):
            await async_handle_set_temporary_override(hass, call)

    # ------------------------------------------------------------------
    # Duration / expiry tests (issue #317)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_duration_minutes_sets_override_expiry_on_coordinator(self):
        """Must store override_expiry on the coordinator when duration_minutes is provided."""
        coordinator = _make_coordinator()
        # Coordinator needs _override_expiry attribute.
        coordinator._override_expiry = None  # noqa: SLF001
        hass = _make_hass(coordinator)

        call = MagicMock()
        call.data = {
            "working_mode": "force_batteries_discharge",
            "duration_minutes": 30,
        }

        await async_handle_set_temporary_override(hass, call)

        # The expiry should be set to some future datetime.
        assert coordinator._override_expiry is not None  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_duration_minutes_none_clears_expiry(self):
        """Must clear override_expiry when duration_minutes is not provided."""
        coordinator = _make_coordinator()
        coordinator._override_expiry = "some_old_value"  # noqa: SLF001
        hass = _make_hass(coordinator)

        call = MagicMock()
        call.data = {"working_mode": "batteries_charge_grid"}

        await async_handle_set_temporary_override(hass, call)

        # The expiry should be None when no duration is given.
        assert coordinator._override_expiry is None  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_duration_minutes_triggers_update_after_setting_expiry(self):
        """Must trigger a coordinator update after setting the override with duration."""
        coordinator = _make_coordinator()
        coordinator._override_expiry = None  # noqa: SLF001
        hass = _make_hass(coordinator)

        call = MagicMock()
        call.data = {"working_mode": "batteries_wait_mode", "duration_minutes": 60}

        await async_handle_set_temporary_override(hass, call)

        coordinator._async_handle_update.assert_awaited_once_with(None)

    @pytest.mark.asyncio
    async def test_clear_override_clears_expiry(self):
        """Must clear override_expiry when clear_override is called."""
        coordinator = _make_coordinator()
        coordinator._override_expiry = "some_expiry_value"  # noqa: SLF001
        hass = _make_hass(coordinator)

        call = MagicMock()
        call.data = {}

        await async_handle_clear_override(hass, call)

        assert coordinator._override_expiry is None  # noqa: SLF001


class TestSchemaSetTemporaryOverrideDuration:
    """Voluptuous schema for set_temporary_override with duration_minutes."""

    def test_duration_minutes_accepted_valid_range(self):
        """Schema must accept a valid duration_minutes within 1-1440."""
        for minutes in [1, 30, 60, 120, 1440]:
            result = SCHEMA_SET_TEMPORARY_OVERRIDE(
                {"working_mode": "batteries_charge_grid", "duration_minutes": minutes}
            )
            assert result["working_mode"] == "batteries_charge_grid"
            assert result["duration_minutes"] == minutes

    def test_duration_minutes_below_min_rejected(self):
        """Schema must reject duration_minutes less than 1."""
        with pytest.raises(vol.Invalid):
            SCHEMA_SET_TEMPORARY_OVERRIDE(
                {"working_mode": "batteries_charge_grid", "duration_minutes": 0}
            )

    def test_duration_minutes_above_max_rejected(self):
        """Schema must reject duration_minutes greater than 1440."""
        with pytest.raises(vol.Invalid):
            SCHEMA_SET_TEMPORARY_OVERRIDE(
                {"working_mode": "batteries_charge_grid", "duration_minutes": 1441}
            )

    def test_duration_minutes_negative_rejected(self):
        """Schema must reject negative duration_minutes."""
        with pytest.raises(vol.Invalid):
            SCHEMA_SET_TEMPORARY_OVERRIDE(
                {"working_mode": "batteries_charge_grid", "duration_minutes": -5}
            )

    def test_duration_minutes_optional(self):
        """Schema must accept working_mode without duration_minutes."""
        result = SCHEMA_SET_TEMPORARY_OVERRIDE(
            {"working_mode": "batteries_discharge_mode"}
        )
        assert "duration_minutes" not in result
        assert result["working_mode"] == "batteries_discharge_mode"

    def test_duration_minutes_string_parsed_as_int(self):
        """Schema must coerce a numeric string to int."""
        result = SCHEMA_SET_TEMPORARY_OVERRIDE(
            {"working_mode": "batteries_charge_grid", "duration_minutes": "45"}
        )
        assert result["duration_minutes"] == 45
        assert isinstance(result["duration_minutes"], int)


class TestClearOverride:
    """Tests for async_handle_clear_override."""

    @pytest.mark.asyncio
    async def test_resets_to_auto_and_triggers_update(self):
        """Must set the select to 'auto' and trigger an update cycle."""
        coordinator = _make_coordinator()
        hass = _make_hass(coordinator)

        await async_handle_clear_override(hass, MagicMock())

        selector_entity_id = get_force_working_mode_selector_entity_id()
        hass.services.async_call.assert_awaited_with(
            "select",
            "select_option",
            {"entity_id": selector_entity_id, "option": "auto"},
            blocking=True,
        )
        coordinator._async_handle_update.assert_awaited_once_with(None)

    @pytest.mark.asyncio
    async def test_raises_when_entity_not_found(self):
        """Must raise HomeAssistantError when select entity is missing."""
        hass = _make_hass()
        hass.states.get.return_value = None

        with pytest.raises(HomeAssistantError, match="force working mode entity"):
            await async_handle_clear_override(hass, MagicMock())

        hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_noop_when_already_auto(self):
        """Must set 'auto' even when already 'auto' (idempotent)."""
        coordinator = _make_coordinator()
        hass = _make_hass(coordinator)

        await async_handle_clear_override(hass, MagicMock())

        # Should attempt to set 'auto' regardless.
        hass.services.async_call.assert_awaited_once()


class TestExportDiagnostics:
    """Tests for async_handle_export_diagnostics."""

    @pytest.mark.asyncio
    async def test_returns_diagnostics_dump(self):
        """Must return a structured diagnostics dictionary."""
        coordinator = _make_coordinator()
        hass = _make_hass(coordinator)

        with patch(
            "custom_components.hsem.services.build_diagnostics_dump",
            return_value={"key": "value"},
        ):
            result = await async_handle_export_diagnostics(hass, MagicMock())

        assert isinstance(result, dict)
        assert result["key"] == "value"

    @pytest.mark.asyncio
    async def test_raises_when_no_coordinator(self):
        """Must raise HomeAssistantError when no coordinator is found."""
        hass = _make_hass()  # No coordinator

        with pytest.raises(HomeAssistantError, match="HSEM coordinator not found"):
            await async_handle_export_diagnostics(hass, MagicMock())

    @pytest.mark.asyncio
    async def test_raises_when_no_planner_cycle(self):
        """Must raise HomeAssistantError when no planner cycle has completed."""
        coordinator = _make_coordinator()
        coordinator._last_planner_input = None  # Simulate no cycle
        hass = _make_hass(coordinator)

        with pytest.raises(HomeAssistantError, match="no planner cycle has completed"):
            await async_handle_export_diagnostics(hass, MagicMock())

    @pytest.mark.asyncio
    async def test_includes_integration_version(self):
        """Must include the integration version in the dump."""
        coordinator = _make_coordinator()
        hass = _make_hass(coordinator)

        with patch(
            "custom_components.hsem.services.build_diagnostics_dump",
            return_value={
                "integration_version": "5.1.0",
                "timestamp": "2025-01-01T00:00:00",
            },
        ):
            result = await async_handle_export_diagnostics(hass, MagicMock())

        assert "integration_version" in result
        assert result["integration_version"] == "5.1.0"


class TestSupportedOverrideModes:
    """Verifies SUPPORTED_OVERRIDE_MODES matches the available recommendations."""

    def test_includes_all_expected_modes(self):
        """Must include all modes from the recommendations enum that are not 'auto'."""
        assert "batteries_charge_grid" in SUPPORTED_OVERRIDE_MODES
        assert "batteries_charge_solar" in SUPPORTED_OVERRIDE_MODES
        assert "batteries_discharge_mode" in SUPPORTED_OVERRIDE_MODES
        assert "batteries_wait_mode" in SUPPORTED_OVERRIDE_MODES
        assert "ev_smart_charging" in SUPPORTED_OVERRIDE_MODES
        assert "force_batteries_discharge" in SUPPORTED_OVERRIDE_MODES
        assert "force_export" in SUPPORTED_OVERRIDE_MODES

    def test_does_not_include_override_sentinel(self):
        """Must not include 'auto' or 'missing_input_entities' in the list."""
        assert "auto" not in SUPPORTED_OVERRIDE_MODES
        assert "missing_input_entities" not in SUPPORTED_OVERRIDE_MODES
        assert "time_passed" not in SUPPORTED_OVERRIDE_MODES


# ---------------------------------------------------------------------------
# Service name constants
# ---------------------------------------------------------------------------


class TestServiceNameConstants:
    """Verifies service name constants match the yaml definitions."""

    def test_service_names_match_yaml(self):
        """Service name constants must match the services.yaml definitions."""
        assert SERVICE_FORCE_RECALCULATION == "force_recalculation"
        assert SERVICE_SET_TEMPORARY_OVERRIDE == "set_temporary_override"
        assert SERVICE_CLEAR_OVERRIDE == "clear_override"
        assert SERVICE_EXPORT_DIAGNOSTICS == "export_diagnostics"
