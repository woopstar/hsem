"""Tests for the reusable schedule and EV config-flow helper modules.

Covers:
- :mod:`custom_components.hsem.flows.schedule_helpers`
- :mod:`custom_components.hsem.flows.ev_helpers`

Acceptance criteria from issue #313:
- Schedule flows share one code path (via ``schedule_helpers``).
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
# schedule_helpers — build_batteries_schedule_step_schema
# ===========================================================================


class TestBuildBatteriesScheduleStepSchema:
    """Schema factory produces correct fields for each schedule number."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("n", [1, 2, 3])
    async def test_schema_contains_three_fields(self, n: int):
        """All expected keys must be present in the built schema."""
        from custom_components.hsem.flows.schedule_helpers import (
            build_batteries_schedule_step_schema,
        )

        schema = await build_batteries_schedule_step_schema(n, None)
        keys = {str(k) for k in schema.schema}
        prefix = f"hsem_batteries_enable_batteries_schedule_{n}"
        assert prefix in keys
        assert f"{prefix}_start" in keys
        assert f"{prefix}_end" in keys
        # min_price_difference field removed

    @pytest.mark.asyncio
    @pytest.mark.parametrize("n", [1, 2, 3])
    async def test_schema_keys_match_original_numbered_wrappers(self, n: int):
        """Schema keys produced by the helper must equal those of the original
        numbered modules — ensuring no config migration is needed."""
        # Import the numbered wrapper (which now delegates to the helper)
        import importlib

        from custom_components.hsem.flows.schedule_helpers import (
            build_batteries_schedule_step_schema,
        )

        mod = importlib.import_module(
            f"custom_components.hsem.flows.batteries_schedule_{n}"
        )
        getter = getattr(mod, f"get_batteries_schedule_{n}_step_schema")

        schema_helper = await build_batteries_schedule_step_schema(n, None)
        schema_wrapper = await getter(None)

        keys_helper = {str(k) for k in schema_helper.schema}
        keys_wrapper = {str(k) for k in schema_wrapper.schema}
        assert keys_helper == keys_wrapper

    @pytest.mark.asyncio
    async def test_schema_has_no_min_price_difference(self):
        """The min_price_difference field was removed from the schedule schema."""
        from custom_components.hsem.flows.schedule_helpers import (
            build_batteries_schedule_step_schema,
        )

        entry = _make_config_entry()
        schema = await build_batteries_schedule_step_schema(1, entry)
        keys = {str(k) for k in schema.schema}
        assert (
            "hsem_batteries_enable_batteries_schedule_1_min_price_difference"
            not in keys
        )

    @pytest.mark.asyncio
    async def test_rated_capacity_resolved_from_hass_state(self):
        """resolve_usable_capacity_kwh uses the live HA state when available."""
        from custom_components.hsem.flows.schedule_helpers import (
            resolve_usable_capacity_kwh,
        )

        hass = _make_hass({"sensor.batteries_rated_capacity": "15000"})
        entry = _make_config_entry()
        capacity = resolve_usable_capacity_kwh(hass, entry)
        assert capacity == pytest.approx(15.0)

    @pytest.mark.asyncio
    async def test_rated_capacity_falls_back_to_10kwh(self):
        """resolve_usable_capacity_kwh returns 10.0 when HA state is unavailable."""
        from custom_components.hsem.flows.schedule_helpers import (
            resolve_usable_capacity_kwh,
        )

        capacity = resolve_usable_capacity_kwh(None, None)
        assert capacity == pytest.approx(10.0)

    @pytest.mark.asyncio
    async def test_rated_capacity_falls_back_when_state_unparseable(self):
        """resolve_usable_capacity_kwh returns 10.0 when state is not a number."""
        from custom_components.hsem.flows.schedule_helpers import (
            resolve_usable_capacity_kwh,
        )

        hass = _make_hass({"sensor.batteries_rated_capacity": "unavailable"})
        entry = _make_config_entry()
        capacity = resolve_usable_capacity_kwh(hass, entry)
        assert capacity == pytest.approx(10.0)


# ===========================================================================
# schedule_helpers — validate_batteries_schedule_input
# ===========================================================================


class TestValidateBatteriesScheduleInput:
    """Validator is identical in behaviour for all three schedule numbers."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("n", [1, 2, 3])
    async def test_disabled_schedule_passes_with_invalid_times(self, n: int):
        """Disabled schedule must skip time validation entirely."""
        from custom_components.hsem.flows.schedule_helpers import (
            validate_batteries_schedule_input,
        )

        prefix = f"hsem_batteries_enable_batteries_schedule_{n}"
        errors = await validate_batteries_schedule_input(
            n,
            {
                prefix: False,
                f"{prefix}_start": "INVALID",
                f"{prefix}_end": "INVALID",
            },
        )
        assert errors == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("n", [1, 2, 3])
    async def test_zero_length_active_schedule_is_rejected(self, n: int):
        """start == end when enabled must produce a base error."""
        from custom_components.hsem.flows.schedule_helpers import (
            validate_batteries_schedule_input,
        )

        prefix = f"hsem_batteries_enable_batteries_schedule_{n}"
        errors = await validate_batteries_schedule_input(
            n,
            {
                prefix: True,
                f"{prefix}_start": "12:00:00",
                f"{prefix}_end": "12:00:00",
            },
        )
        assert errors.get("base") == "start_time_equals_end_time"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("n", [1, 2, 3])
    async def test_valid_cross_midnight_window_accepted(self, n: int):
        """A valid cross-midnight window must pass with no errors."""
        from custom_components.hsem.flows.schedule_helpers import (
            validate_batteries_schedule_input,
        )

        prefix = f"hsem_batteries_enable_batteries_schedule_{n}"
        errors = await validate_batteries_schedule_input(
            n,
            {
                prefix: True,
                f"{prefix}_start": "23:00:00",
                f"{prefix}_end": "02:00:00",
            },
        )
        assert errors == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("n", [1, 2, 3])
    async def test_invalid_time_format_rejected(self, n: int):
        """An unparseable time string must produce an ``invalid_time_format`` error."""
        from custom_components.hsem.flows.schedule_helpers import (
            validate_batteries_schedule_input,
        )

        prefix = f"hsem_batteries_enable_batteries_schedule_{n}"
        errors = await validate_batteries_schedule_input(
            n,
            {
                prefix: True,
                f"{prefix}_start": "not-a-time",
                f"{prefix}_end": "09:00:00",
            },
        )
        assert errors.get(f"{prefix}_start") == "invalid_time_format"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("n", [1, 2, 3])
    async def test_helper_output_matches_numbered_wrapper(self, n: int):
        """Helper and numbered wrapper must return identical errors for the same input."""
        import importlib

        from custom_components.hsem.flows.schedule_helpers import (
            validate_batteries_schedule_input,
        )

        mod = importlib.import_module(
            f"custom_components.hsem.flows.batteries_schedule_{n}"
        )
        wrapper_validate = getattr(mod, f"validate_batteries_schedule_{n}_input")

        prefix = f"hsem_batteries_enable_batteries_schedule_{n}"
        user_input = {
            prefix: True,
            f"{prefix}_start": "08:00:00",
            f"{prefix}_end": "08:00:00",
        }
        errors_helper = await validate_batteries_schedule_input(n, user_input)
        errors_wrapper = await wrapper_validate(user_input)
        assert errors_helper == errors_wrapper


# ===========================================================================
# Legacy private alias — _resolve_usable_capacity_kwh
# ===========================================================================


class TestLegacyPrivateAlias:
    """The legacy ``_resolve_usable_capacity_kwh`` re-export in batteries_schedule_1
    must still work so that any external code that imports it continues to function."""

    def test_alias_is_callable(self):
        from custom_components.hsem.flows.batteries_schedule_1 import (
            _resolve_usable_capacity_kwh,
        )

        assert callable(_resolve_usable_capacity_kwh)

    def test_alias_returns_fallback(self):
        from custom_components.hsem.flows.batteries_schedule_1 import (
            _resolve_usable_capacity_kwh,
        )

        assert _resolve_usable_capacity_kwh(None, None) == pytest.approx(10.0)


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
        assert "hsem_ev_soc_target" in keys
        assert "hsem_ev_connected" in keys
        assert "hsem_ev_allow_charge_past_target_soc" in keys

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
        assert "hsem_ev_second_soc_target" in keys
        assert "hsem_ev_second_connected" in keys
        assert "hsem_ev_second_allow_charge_past_target_soc" in keys

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
    @pytest.mark.parametrize("n", [1, 2, 3])
    async def test_schedule_schema_accepts_valid_input(self, n: int):
        from custom_components.hsem.flows.schedule_helpers import (
            build_batteries_schedule_step_schema,
        )

        prefix = f"hsem_batteries_enable_batteries_schedule_{n}"
        schema = await build_batteries_schedule_step_schema(n, None)
        valid_input = {
            prefix: True,
            f"{prefix}_start": "07:00:00",
            f"{prefix}_end": "09:00:00",
        }
        # Should not raise
        result = schema(valid_input)
        assert result[prefix] is True

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
        }
        result = schema(valid_input)
        assert result["hsem_ev_charger_max_discharge_power"] == 2000

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
        }
        result = schema(valid_input)
        assert result["hsem_ev_second_charger_max_discharge_power"] == 1500
