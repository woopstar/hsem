"""Tests for the reusable EV config-flow helper module.

Covers:
- :mod:`custom_components.hsem.flows.ev_helpers`

Acceptance criteria from issue #313:
- EV flows share one code path (via ``ev_helpers``).
- Existing config migration still works (schema keys are unchanged).
- Schema field names produced by the helpers match the original hard-coded names.
- Validation behaviour is identical to the original numbered wrappers.
"""

from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_hass(entity_states: dict | None = None) -> MagicMock:
    """Build a minimal hass stub whose states.get() returns controlled values."""
    hass = MagicMock()
    entity_states = entity_states or {}

    def _states_get(entity_id):
        if entity_id in entity_states:
            state = MagicMock()
            state.state = entity_states[entity_id]
            return state
        return None

    hass.states.get.side_effect = _states_get
    return hass


def _make_config_entry(overrides: dict | None = None) -> MagicMock:
    """Build a config-entry stub backed by DEFAULT_CONFIG_VALUES."""
    from custom_components.hsem.const import DEFAULT_CONFIG_VALUES

    data = {**DEFAULT_CONFIG_VALUES, **(overrides or {})}
    entry = MagicMock()
    entry.options = {}
    entry.data = data
    return entry


# ===========================================================================
# ev_helpers — build_ev_charger_schema
# ===========================================================================


class TestBuildEvChargerSchema:
    """Schema factory produces correct fields for primary and secondary EV steps."""

    @pytest.mark.asyncio
    async def test_primary_ev_schema_contains_all_fields(self):
        """Primary EV schema must include the two extra primary-only boolean fields."""
        from custom_components.hsem.flows.ev_helpers import build_ev_charger_schema

        schema = await build_ev_charger_schema(
            None, prefix="hsem_ev", include_primary_fields=True
        )
        keys = {str(k) for k in schema.schema}
        # Extra primary fields
        assert "hsem_ev_second_enabled" in keys
        assert "hsem_house_power_includes_ev_charger_power" in keys
        # Standard fields
        assert "hsem_ev_charger_status" in keys
        assert "hsem_ev_charger_power" in keys
        assert "hsem_ev_charger_force_max_discharge_power" in keys
        assert "hsem_ev_charger_max_discharge_power" in keys
        assert "hsem_ev_soc" in keys
        assert "hsem_ev_connected" in keys
        assert "hsem_ev_allow_charge_past_target_soc" in keys
        assert "hsem_ev_past_target_confidence_factor" in keys

    @pytest.mark.asyncio
    async def test_secondary_ev_schema_omits_primary_only_fields(self):
        """Secondary EV schema must NOT include the primary-only fields."""
        from custom_components.hsem.flows.ev_helpers import build_ev_charger_schema

        schema = await build_ev_charger_schema(
            None, prefix="hsem_ev_second", include_primary_fields=False
        )
        keys = {str(k) for k in schema.schema}
        assert "hsem_ev_second_enabled" not in keys
        assert "hsem_house_power_includes_ev_charger_power" not in keys
        # Standard (second-prefixed) fields must be present
        assert "hsem_ev_second_charger_status" in keys
        assert "hsem_ev_second_charger_power" in keys
        assert "hsem_ev_second_charger_force_max_discharge_power" in keys
        assert "hsem_ev_second_charger_max_discharge_power" in keys
        assert "hsem_ev_second_soc" in keys
        assert "hsem_ev_second_connected" in keys
        assert "hsem_ev_second_allow_charge_past_target_soc" in keys
        assert "hsem_ev_second_past_target_confidence_factor" in keys

    @pytest.mark.asyncio
    async def test_primary_schema_keys_match_original_ev_step(self):
        """Keys from the helper must exactly match those from the original ev.py."""
        from custom_components.hsem.flows.ev import get_ev_step_schema
        from custom_components.hsem.flows.ev_helpers import build_ev_charger_schema

        schema_helper = await build_ev_charger_schema(
            None, prefix="hsem_ev", include_primary_fields=True
        )
        schema_wrapper = await get_ev_step_schema(None)

        keys_helper = {str(k) for k in schema_helper.schema}
        keys_wrapper = {str(k) for k in schema_wrapper.schema}
        assert keys_helper == keys_wrapper

    @pytest.mark.asyncio
    async def test_secondary_schema_keys_match_original_ev_second_step(self):
        """Keys from the helper must exactly match those from the original ev_second.py."""
        from custom_components.hsem.flows.ev_helpers import build_ev_charger_schema
        from custom_components.hsem.flows.ev_second import get_ev_second_step_schema

        schema_helper = await build_ev_charger_schema(
            None, prefix="hsem_ev_second", include_primary_fields=False
        )
        schema_wrapper = await get_ev_second_step_schema(None)

        keys_helper = {str(k) for k in schema_helper.schema}
        keys_wrapper = {str(k) for k in schema_wrapper.schema}
        assert keys_helper == keys_wrapper


# ===========================================================================
# ev_helpers — validate_ev_charger_input
# ===========================================================================


class TestValidateEvChargerInput:
    """Validator enforces required fields and delegates entity lookups to HA."""

    @pytest.mark.asyncio
    async def test_primary_ev_missing_required_fields_produces_errors(self):
        """Missing required boolean/numeric fields must each produce 'required'."""
        from custom_components.hsem.flows.ev_helpers import validate_ev_charger_input

        hass = _make_hass()
        errors = await validate_ev_charger_input(
            hass,
            user_input={},
            prefix="hsem_ev",
            extra_required_fields=["hsem_house_power_includes_ev_charger_power"],
        )
        assert errors.get("hsem_ev_charger_max_discharge_power") == "required"
        assert errors.get("hsem_ev_charger_force_max_discharge_power") == "required"
        assert errors.get("hsem_ev_allow_charge_past_target_soc") == "required"
        assert errors.get("hsem_ev_past_target_confidence_factor") == "required"
        assert errors.get("hsem_house_power_includes_ev_charger_power") == "required"

    @pytest.mark.asyncio
    async def test_secondary_ev_missing_required_fields_produces_errors(self):
        """Secondary EV required fields produce 'required' errors."""
        from custom_components.hsem.flows.ev_helpers import validate_ev_charger_input

        hass = _make_hass()
        errors = await validate_ev_charger_input(
            hass,
            user_input={},
            prefix="hsem_ev_second",
        )
        assert errors.get("hsem_ev_second_charger_max_discharge_power") == "required"
        assert (
            errors.get("hsem_ev_second_charger_force_max_discharge_power") == "required"
        )
        assert errors.get("hsem_ev_second_allow_charge_past_target_soc") == "required"
        assert errors.get("hsem_ev_second_past_target_confidence_factor") == "required"

    @pytest.mark.asyncio
    async def test_valid_primary_ev_input_passes(self):
        """All required fields present and no optional entities → no errors."""
        from custom_components.hsem.flows.ev_helpers import validate_ev_charger_input

        hass = _make_hass()
        errors = await validate_ev_charger_input(
            hass,
            user_input={
                "hsem_ev_charger_max_discharge_power": 2000,
                "hsem_ev_charger_force_max_discharge_power": False,
                "hsem_ev_allow_charge_past_target_soc": False,
                "hsem_ev_past_target_confidence_factor": 0.9,
                "hsem_ev_auto_full_negative_price": False,
                "hsem_house_power_includes_ev_charger_power": True,
            },
            prefix="hsem_ev",
            extra_required_fields=["hsem_house_power_includes_ev_charger_power"],
        )
        assert errors == {}

    @pytest.mark.asyncio
    async def test_nonexistent_optional_entity_is_flagged(self):
        """An optional entity that does not exist in HA must produce entity_not_found."""
        from custom_components.hsem.flows.ev_helpers import validate_ev_charger_input

        hass = _make_hass()  # no entities registered
        errors = await validate_ev_charger_input(
            hass,
            user_input={
                "hsem_ev_charger_max_discharge_power": 2000,
                "hsem_ev_charger_force_max_discharge_power": False,
                "hsem_ev_allow_charge_past_target_soc": False,
                "hsem_ev_past_target_confidence_factor": 0.9,
                "hsem_house_power_includes_ev_charger_power": True,
                "hsem_ev_charger_status": "sensor.ev_status_nonexistent",
            },
            prefix="hsem_ev",
            extra_required_fields=["hsem_house_power_includes_ev_charger_power"],
        )
        assert errors.get("hsem_ev_charger_status") == "entity_not_found"

    @pytest.mark.asyncio
    async def test_primary_ev_validation_matches_original_ev_step(self):
        """Helper and original ev.validate_ev_step_input must agree for the same input."""
        from custom_components.hsem.flows.ev import validate_ev_step_input
        from custom_components.hsem.flows.ev_helpers import validate_ev_charger_input

        hass = _make_hass()
        user_input = {
            "hsem_ev_charger_max_discharge_power": 500,
            "hsem_ev_charger_force_max_discharge_power": True,
            "hsem_ev_allow_charge_past_target_soc": True,
            "hsem_ev_past_target_confidence_factor": 0.9,
            "hsem_house_power_includes_ev_charger_power": False,
        }
        errors_helper = await validate_ev_charger_input(
            hass,
            user_input,
            prefix="hsem_ev",
            extra_required_fields=["hsem_house_power_includes_ev_charger_power"],
        )
        errors_wrapper = await validate_ev_step_input(hass, user_input)
        assert errors_helper == errors_wrapper

    @pytest.mark.asyncio
    async def test_secondary_ev_validation_matches_original_ev_second_step(self):
        """Helper and original ev_second.validate_ev_second_step_input must agree."""
        from custom_components.hsem.flows.ev_helpers import validate_ev_charger_input
        from custom_components.hsem.flows.ev_second import validate_ev_second_step_input

        hass = _make_hass()
        user_input = {
            "hsem_ev_second_charger_max_discharge_power": 1000,
            "hsem_ev_second_charger_force_max_discharge_power": False,
            "hsem_ev_second_allow_charge_past_target_soc": False,
            "hsem_ev_second_past_target_confidence_factor": 0.9,
        }
        errors_helper = await validate_ev_charger_input(
            hass, user_input, prefix="hsem_ev_second"
        )
        errors_wrapper = await validate_ev_second_step_input(hass, user_input)
        assert errors_helper == errors_wrapper


# ===========================================================================
# Round-trip: all four flow keys survive voluptuous validation
# ===========================================================================


class TestSchemaRoundTrip:
    """Valid user input must pass through the schema without voluptuous raising."""

    @pytest.mark.asyncio
    async def test_ev_primary_schema_accepts_valid_input(self):
        from custom_components.hsem.flows.ev_helpers import build_ev_charger_schema

        schema = await build_ev_charger_schema(
            None, prefix="hsem_ev", include_primary_fields=True
        )
        valid_input = {
            "hsem_ev_second_enabled": False,
            "hsem_house_power_includes_ev_charger_power": True,
            "hsem_ev_charger_force_max_discharge_power": False,
            "hsem_ev_charger_max_discharge_power": 2000,
            "hsem_ev_allow_charge_past_target_soc": False,
            "hsem_ev_past_target_confidence_factor": 0.9,
        }
        result = schema(valid_input)
        assert result["hsem_ev_charger_max_discharge_power"] == 2000  # pyright: ignore[reportIndexIssue]

    @pytest.mark.asyncio
    async def test_ev_secondary_schema_accepts_valid_input(self):
        from custom_components.hsem.flows.ev_helpers import build_ev_charger_schema

        schema = await build_ev_charger_schema(
            None, prefix="hsem_ev_second", include_primary_fields=False
        )
        valid_input = {
            "hsem_ev_second_charger_force_max_discharge_power": True,
            "hsem_ev_second_charger_max_discharge_power": 1500,
            "hsem_ev_second_allow_charge_past_target_soc": True,
            "hsem_ev_second_past_target_confidence_factor": 0.9,
        }
        result = schema(valid_input)
        assert result["hsem_ev_second_charger_max_discharge_power"] == 1500  # pyright: ignore[reportIndexIssue]
