"""Tests for the centralized config validation layer (issue #306).

Covers:
- Entity ID format validation
- Async entity/device existence checks
- Month season validation
- Time window validation
- Power limit validation
- Energy limit validation
- Price / cost validation
- Consumption weight validation
- merge_errors composition helper
- Integration: flow-level validators delegate to the centralized module
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.utils.config_validator import (
    async_validate_entity_ids,
    merge_errors,
    validate_consumption_weights,
    validate_energy_limits,
    validate_entity_id_fields,
    validate_entity_id_format,
    validate_months,
    validate_power_limits,
    validate_price,
    validate_time_window,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hass_with_states(*entity_ids: str) -> MagicMock:
    """Return a minimal hass mock where the given entity IDs exist."""
    hass = MagicMock()

    def _get_state(eid):
        if eid in entity_ids:
            return MagicMock(state="ok")
        return None

    hass.states.get = _get_state
    return hass


# ---------------------------------------------------------------------------
# validate_entity_id_format
# ---------------------------------------------------------------------------


class TestValidateEntityIdFormat:
    """Pure format check for HA entity IDs."""

    def test_valid_sensor(self):
        assert validate_entity_id_format("sensor.my_sensor") is True

    def test_valid_number_with_digits(self):
        assert validate_entity_id_format("number.batteries_max_charge_100") is True

    def test_valid_select(self):
        assert validate_entity_id_format("select.batteries_working_mode") is True

    def test_missing_dot(self):
        assert validate_entity_id_format("sensor_no_dot") is False

    def test_uppercase_domain(self):
        assert validate_entity_id_format("Sensor.my_sensor") is False

    def test_uppercase_object_id(self):
        assert validate_entity_id_format("sensor.My_Sensor") is False

    def test_empty_string(self):
        assert validate_entity_id_format("") is False

    def test_none(self):
        assert validate_entity_id_format(None) is False  # type: ignore[arg-type]  # NOSONAR -- intentional None test

    def test_special_chars(self):
        assert validate_entity_id_format("sensor.my-sensor!") is False

    def test_spaces(self):
        assert validate_entity_id_format("sensor.my sensor") is False

    def test_extra_dot(self):
        # Two dots — not valid HA entity ID format
        assert validate_entity_id_format("sensor.sub.object") is False

    def test_whitespace_stripped_valid(self):
        # Leading/trailing space stripped before check
        assert validate_entity_id_format("  sensor.my_sensor  ") is True


# ---------------------------------------------------------------------------
# validate_entity_id_fields (sync, no HA)
# ---------------------------------------------------------------------------


class TestValidateEntityIdFields:
    """Sync format-only validation for multiple fields."""

    def test_valid_required(self):
        user_input = {
            "field_a": "sensor.ok",
            "field_b": "number.also_ok",
        }
        errors = validate_entity_id_fields(user_input, ["field_a", "field_b"])
        assert errors == {}

    def test_missing_required_field(self):
        errors = validate_entity_id_fields({}, ["field_a"])
        assert errors["field_a"] == "required"

    def test_malformed_required_field(self):
        errors = validate_entity_id_fields({"field_a": "BAD_ID"}, ["field_a"])
        assert errors["field_a"] == "invalid_entity_id"

    def test_optional_present_and_valid(self):
        user_input = {"field_opt": "sensor.fine"}
        errors = validate_entity_id_fields(user_input, [], ["field_opt"])
        assert errors == {}

    def test_optional_present_and_invalid(self):
        user_input = {"field_opt": "INVALID"}
        errors = validate_entity_id_fields(user_input, [], ["field_opt"])
        assert errors["field_opt"] == "invalid_entity_id"

    def test_optional_absent_no_error(self):
        errors = validate_entity_id_fields({}, [], ["field_opt"])
        assert errors == {}

    def test_required_and_optional_mixed(self):
        user_input = {
            "req": "sensor.good",
            "opt": "BAD",
        }
        errors = validate_entity_id_fields(user_input, ["req"], ["opt"])
        assert "req" not in errors
        assert errors["opt"] == "invalid_entity_id"


# ---------------------------------------------------------------------------
# async_validate_entity_ids
# ---------------------------------------------------------------------------


class TestAsyncValidateEntityIds:
    """HA entity-existence checks."""

    @pytest.mark.asyncio
    async def test_valid_entity_exists(self):
        hass = _hass_with_states("sensor.good")
        errors = await async_validate_entity_ids(
            hass, {"f": "sensor.good"}, required_fields=["f"]
        )
        assert errors == {}

    @pytest.mark.asyncio
    async def test_required_entity_not_found_in_ha(self):
        hass = _hass_with_states()
        errors = await async_validate_entity_ids(
            hass, {"f": "sensor.missing"}, required_fields=["f"]
        )
        assert errors["f"] == "entity_not_found"

    @pytest.mark.asyncio
    async def test_malformed_id_does_not_hit_ha(self):
        hass = _hass_with_states(
            "sensor.BAD"
        )  # even if HA had it, format rejects first
        errors = await async_validate_entity_ids(
            hass, {"f": "BAD_FORMAT"}, required_fields=["f"]
        )
        assert errors["f"] == "invalid_entity_id"

    @pytest.mark.asyncio
    async def test_missing_required_field(self):
        hass = _hass_with_states()
        errors = await async_validate_entity_ids(hass, {}, required_fields=["f"])
        assert errors["f"] == "required"

    @pytest.mark.asyncio
    async def test_optional_absent_no_error(self):
        hass = _hass_with_states()
        errors = await async_validate_entity_ids(
            hass, {}, required_fields=[], optional_fields=["f"]
        )
        assert errors == {}

    @pytest.mark.asyncio
    async def test_optional_present_and_valid(self):
        hass = _hass_with_states("sensor.opt")
        errors = await async_validate_entity_ids(
            hass, {"f": "sensor.opt"}, required_fields=[], optional_fields=["f"]
        )
        assert errors == {}

    @pytest.mark.asyncio
    async def test_optional_present_and_not_found(self):
        hass = _hass_with_states()
        errors = await async_validate_entity_ids(
            hass, {"f": "sensor.gone"}, required_fields=[], optional_fields=["f"]
        )
        assert errors["f"] == "entity_not_found"

    @pytest.mark.asyncio
    async def test_multiple_required_partial_failure(self):
        hass = _hass_with_states("sensor.a")
        errors = await async_validate_entity_ids(
            hass,
            {"a": "sensor.a", "b": "sensor.missing"},
            required_fields=["a", "b"],
        )
        assert "a" not in errors
        assert errors["b"] == "entity_not_found"


# ---------------------------------------------------------------------------
# async_validate_device_ids
# ---------------------------------------------------------------------------


_CV_MODULE = "custom_components.hsem.utils.config_validator"


class TestAsyncValidateDeviceIds:
    """HA device-existence checks (mocking the device registry)."""

    @pytest.mark.asyncio
    async def test_required_device_found(self):
        from custom_components.hsem.utils.config_validator import (
            async_validate_device_ids,
        )

        async def _found(hass, did):
            return did == "dev-abc"

        with patch(f"{_CV_MODULE}.async_device_exists", side_effect=_found):
            hass = MagicMock()
            errors = await async_validate_device_ids(
                hass, {"dev": "dev-abc"}, required_fields=["dev"]
            )
        assert errors == {}

    @pytest.mark.asyncio
    async def test_required_device_not_found(self):
        from custom_components.hsem.utils.config_validator import (
            async_validate_device_ids,
        )

        with patch(
            f"{_CV_MODULE}.async_device_exists", new=AsyncMock(return_value=False)
        ):
            hass = MagicMock()
            errors = await async_validate_device_ids(
                hass, {"dev": "dev-xyz"}, required_fields=["dev"]
            )
        assert errors["dev"] == "device_not_found"

    @pytest.mark.asyncio
    async def test_required_device_missing_field(self):
        from custom_components.hsem.utils.config_validator import (
            async_validate_device_ids,
        )

        with patch(
            f"{_CV_MODULE}.async_device_exists", new=AsyncMock(return_value=True)
        ):
            hass = MagicMock()
            errors = await async_validate_device_ids(hass, {}, required_fields=["dev"])
        assert errors["dev"] == "required"

    @pytest.mark.asyncio
    async def test_optional_device_absent_no_error(self):
        from custom_components.hsem.utils.config_validator import (
            async_validate_device_ids,
        )

        with patch(
            f"{_CV_MODULE}.async_device_exists", new=AsyncMock(return_value=False)
        ):
            hass = MagicMock()
            errors = await async_validate_device_ids(
                hass, {}, required_fields=[], optional_fields=["dev"]
            )
        assert errors == {}


# ---------------------------------------------------------------------------
# validate_months
# ---------------------------------------------------------------------------


class TestValidateMonths:
    """Month season assignment validation."""

    def test_valid_winter_months(self):
        user_input = {"hsem_months_winter": ["1", "2", "11", "12"]}
        errors = validate_months(user_input)
        assert errors == {}

    def test_missing_winter_field(self):
        errors = validate_months({})
        assert errors["hsem_months_winter"] == "required"

    def test_empty_winter_list(self):
        errors = validate_months({"hsem_months_winter": []})
        assert errors["hsem_months_winter"] == "required"

    def test_all_months_in_winter_leaves_no_summer(self):
        all_months = [str(i) for i in range(1, 13)]
        errors = validate_months({"hsem_months_winter": all_months})
        assert errors["hsem_months_winter"] == "months_summer_empty"

    def test_invalid_month_value(self):
        errors = validate_months({"hsem_months_winter": ["0", "13"]})
        assert errors["hsem_months_winter"] == "invalid_month_value"

    def test_non_numeric_month(self):
        errors = validate_months({"hsem_months_winter": ["Jan"]})
        assert errors["hsem_months_winter"] == "invalid_month_value"

    def test_single_valid_winter_month(self):
        errors = validate_months({"hsem_months_winter": ["12"]})
        assert errors == {}

    def test_custom_field_name(self):
        errors = validate_months({"custom_winter": ["3"]}, winter_field="custom_winter")
        assert errors == {}


# ---------------------------------------------------------------------------
# validate_time_window
# ---------------------------------------------------------------------------


class TestValidateTimeWindow:
    """Battery schedule time window validation."""

    def test_disabled_schedule_skips_validation(self):
        user_input = {
            "enabled": False,
            "start": "INVALID",
            "end": "ALSO_BAD",
        }
        errors = validate_time_window(user_input, "enabled", "start", "end")
        assert errors == {}

    def test_valid_same_day_window(self):
        user_input = {
            "enabled": True,
            "start": "07:00:00",
            "end": "09:00:00",
        }
        errors = validate_time_window(user_input, "enabled", "start", "end")
        assert errors == {}

    def test_valid_cross_midnight_window(self):
        user_input = {
            "enabled": True,
            "start": "23:00:00",
            "end": "02:00:00",
        }
        errors = validate_time_window(user_input, "enabled", "start", "end")
        assert errors == {}

    def test_zero_length_window_rejected(self):
        user_input = {
            "enabled": True,
            "start": "07:00:00",
            "end": "07:00:00",
        }
        errors = validate_time_window(user_input, "enabled", "start", "end")
        assert errors["base"] == "start_time_equals_end_time"

    def test_invalid_start_format(self):
        user_input = {
            "enabled": True,
            "start": "7:00",
            "end": "09:00:00",
        }
        errors = validate_time_window(user_input, "enabled", "start", "end")
        assert errors["start"] == "invalid_time_format"

    def test_invalid_end_format(self):
        user_input = {
            "enabled": True,
            "start": "07:00:00",
            "end": "NOT_A_TIME",
        }
        errors = validate_time_window(user_input, "enabled", "start", "end")
        assert errors["end"] == "invalid_time_format"

    def test_missing_start_treated_as_invalid_format(self):
        user_input = {
            "enabled": True,
            "end": "09:00:00",
        }
        errors = validate_time_window(user_input, "enabled", "start", "end")
        assert errors["start"] == "invalid_time_format"

    def test_midnight_00_not_equal_to_23_59_59(self):
        """Midnight start with one second before midnight end is a valid cross-midnight window."""
        user_input = {
            "enabled": True,
            "start": "00:00:00",
            "end": "23:59:59",
        }
        errors = validate_time_window(user_input, "enabled", "start", "end")
        assert errors == {}


# ---------------------------------------------------------------------------
# validate_power_limits
# ---------------------------------------------------------------------------


class TestValidatePowerLimits:
    """Power value range validation."""

    def test_valid_zero_power(self):
        errors = validate_power_limits({"f": 0}, "f", min_watts=0)
        assert errors == {}

    def test_valid_positive_power(self):
        errors = validate_power_limits({"f": 5000}, "f")
        assert errors == {}

    def test_below_minimum(self):
        errors = validate_power_limits({"f": -1}, "f", min_watts=0)
        assert errors["f"] == "power_out_of_range"

    def test_above_maximum(self):
        errors = validate_power_limits({"f": 200_000}, "f", max_watts=100_000)
        assert errors["f"] == "power_out_of_range"

    def test_not_a_number(self):
        errors = validate_power_limits({"f": "not_a_number"}, "f")
        assert errors["f"] == "invalid_power_value"

    def test_absent_field_no_error(self):
        errors = validate_power_limits({}, "f")
        assert errors == {}

    def test_boundary_inclusive_min(self):
        errors = validate_power_limits({"f": 50}, "f", min_watts=50, max_watts=5000)
        assert errors == {}

    def test_boundary_inclusive_max(self):
        errors = validate_power_limits({"f": 5000}, "f", min_watts=50, max_watts=5000)
        assert errors == {}


# ---------------------------------------------------------------------------
# validate_energy_limits
# ---------------------------------------------------------------------------


class TestValidateEnergyLimits:
    """Energy value range validation."""

    def test_valid_energy(self):
        errors = validate_energy_limits({"f": 10.5}, "f")
        assert errors == {}

    def test_zero_energy_valid_at_default_min(self):
        errors = validate_energy_limits({"f": 0}, "f", min_kwh=0)
        assert errors == {}

    def test_below_minimum(self):
        errors = validate_energy_limits({"f": -0.1}, "f", min_kwh=0)
        assert errors["f"] == "energy_out_of_range"

    def test_above_maximum(self):
        errors = validate_energy_limits({"f": 1001}, "f", max_kwh=1000)
        assert errors["f"] == "energy_out_of_range"

    def test_non_numeric(self):
        errors = validate_energy_limits({"f": "abc"}, "f")
        assert errors["f"] == "invalid_energy_value"

    def test_absent_field_no_error(self):
        errors = validate_energy_limits({}, "f")
        assert errors == {}


# ---------------------------------------------------------------------------
# validate_price
# ---------------------------------------------------------------------------


class TestValidatePrice:
    """Price value validation."""

    def test_valid_price(self):
        errors = validate_price({"p": 0.25}, "p")
        assert errors == {}

    def test_valid_negative_price_when_allowed(self):
        errors = validate_price({"p": -0.05}, "p", allow_negative=True)
        assert errors == {}

    def test_negative_price_rejected_when_not_allowed(self):
        errors = validate_price({"p": -0.01}, "p", allow_negative=False)
        assert errors["p"] == "price_out_of_range"

    def test_zero_allowed_when_non_negative(self):
        errors = validate_price({"p": 0.0}, "p", allow_negative=False)
        assert errors == {}

    def test_above_maximum(self):
        errors = validate_price({"p": 200}, "p", max_price=100)
        assert errors["p"] == "price_out_of_range"

    def test_non_numeric_price(self):
        errors = validate_price({"p": "free"}, "p")
        assert errors["p"] == "invalid_price_value"

    def test_absent_field_no_error(self):
        errors = validate_price({}, "p")
        assert errors == {}

    def test_boundary_at_min(self):
        errors = validate_price({"p": -2.0}, "p", min_price=-2.0, max_price=2.0)
        assert errors == {}

    def test_boundary_at_max(self):
        errors = validate_price({"p": 2.0}, "p", min_price=-2.0, max_price=2.0)
        assert errors == {}

    def test_just_below_min(self):
        errors = validate_price({"p": -2.01}, "p", min_price=-2.0, max_price=2.0)
        assert errors["p"] == "price_out_of_range"


# ---------------------------------------------------------------------------
# validate_consumption_weights
# ---------------------------------------------------------------------------


class TestValidateConsumptionWeights:
    """House consumption energy weight sum validation."""

    def test_valid_weights_sum_100(self):
        user_input = {
            "hsem_house_consumption_energy_weight_1d": 25,
            "hsem_house_consumption_energy_weight_3d": 30,
            "hsem_house_consumption_energy_weight_7d": 30,
            "hsem_house_consumption_energy_weight_14d": 15,
        }
        errors = validate_consumption_weights(user_input)
        assert errors == {}

    def test_weights_not_summing_to_100(self):
        user_input = {
            "hsem_house_consumption_energy_weight_1d": 20,
            "hsem_house_consumption_energy_weight_3d": 20,
            "hsem_house_consumption_energy_weight_7d": 20,
            "hsem_house_consumption_energy_weight_14d": 20,
        }
        errors = validate_consumption_weights(user_input)
        assert errors["base"] == "hsem_house_consumption_energy_weight_total"

    def test_all_zero_weights(self):
        user_input = {
            "hsem_house_consumption_energy_weight_1d": 0,
            "hsem_house_consumption_energy_weight_3d": 0,
            "hsem_house_consumption_energy_weight_7d": 0,
            "hsem_house_consumption_energy_weight_14d": 0,
        }
        errors = validate_consumption_weights(user_input)
        assert errors["base"] == "hsem_house_consumption_energy_weight_total"

    def test_missing_fields_treated_as_zero(self):
        # Missing fields default to 0 → sum < 100
        errors = validate_consumption_weights({})
        assert errors["base"] == "hsem_house_consumption_energy_weight_total"

    def test_non_numeric_weight_produces_error(self):
        user_input = {
            "hsem_house_consumption_energy_weight_1d": "abc",
            "hsem_house_consumption_energy_weight_3d": 25,
            "hsem_house_consumption_energy_weight_7d": 25,
            "hsem_house_consumption_energy_weight_14d": 25,
        }
        errors = validate_consumption_weights(user_input)
        assert errors["base"] == "hsem_house_consumption_energy_weight_total"


# ---------------------------------------------------------------------------
# merge_errors
# ---------------------------------------------------------------------------


class TestMergeErrors:
    """Error dict composition helper."""

    def test_empty_dicts(self):
        assert merge_errors({}, {}, {}) == {}

    def test_no_overlap(self):
        result = merge_errors({"a": "err1"}, {"b": "err2"})
        assert result == {"a": "err1", "b": "err2"}

    def test_first_error_wins_on_overlap(self):
        result = merge_errors({"a": "first"}, {"a": "second"})
        assert result["a"] == "first"

    def test_single_dict(self):
        result = merge_errors({"x": "err"})
        assert result == {"x": "err"}

    def test_three_dicts_overlap(self):
        result = merge_errors({"a": "1"}, {"a": "2", "b": "b1"}, {"b": "b2", "c": "c1"})
        assert result["a"] == "1"
        assert result["b"] == "b1"
        assert result["c"] == "c1"


# ---------------------------------------------------------------------------
# Integration: flow validators use centralized validators
# ---------------------------------------------------------------------------


class TestFlowValidatorsUseConfigValidator:
    """Smoke tests that flow-level validate_* functions correctly delegate."""

    @pytest.mark.asyncio
    async def test_validate_power_step_entity_not_found(self):
        from custom_components.hsem.flows.power import validate_power_step_input

        hass = _hass_with_states()
        errors = await validate_power_step_input(
            hass,
            {
                "hsem_house_consumption_power": "sensor.missing",
                "hsem_solar_production_power": "sensor.missing_too",
            },
        )
        assert errors["hsem_house_consumption_power"] == "entity_not_found"
        assert errors["hsem_solar_production_power"] == "entity_not_found"

    @pytest.mark.asyncio
    async def test_validate_power_step_valid(self):
        from custom_components.hsem.flows.power import validate_power_step_input

        hass = _hass_with_states("sensor.house", "sensor.solar")
        errors = await validate_power_step_input(
            hass,
            {
                "hsem_house_consumption_power": "sensor.house",
                "hsem_solar_production_power": "sensor.solar",
            },
        )
        assert errors == {}

    @pytest.mark.asyncio
    async def test_power_step_schema_includes_phase_field(self):
        """hsem_main_fuse_phases field round-trips through power step schema."""
        from custom_components.hsem.flows.power import get_power_step_schema

        schema = await get_power_step_schema(None)
        # Phase=1 should be valid
        result = schema(
            {
                "hsem_house_consumption_power": "sensor.house",
                "hsem_solar_production_power": "sensor.solar",
                "hsem_main_fuse_phases": 1,
            }
        )
        assert result["hsem_main_fuse_phases"] == 1  # pyright: ignore[reportIndexIssue]

        # Phase=3 should be valid
        result = schema(
            {
                "hsem_house_consumption_power": "sensor.house",
                "hsem_solar_production_power": "sensor.solar",
                "hsem_main_fuse_phases": 3,
            }
        )
        assert result["hsem_main_fuse_phases"] == 3  # pyright: ignore[reportIndexIssue]

        # Omitted phase should get default (3)
        result = schema(
            {
                "hsem_house_consumption_power": "sensor.house",
                "hsem_solar_production_power": "sensor.solar",
            }
        )
        assert result["hsem_main_fuse_phases"] == 3  # pyright: ignore[reportIndexIssue]

    @pytest.mark.asyncio
    async def test_validate_power_step_with_phase_field(self):
        """Power step validation should pass with the new phase field present."""
        from custom_components.hsem.flows.power import validate_power_step_input

        hass = _hass_with_states("sensor.house", "sensor.solar")
        errors = await validate_power_step_input(
            hass,
            {
                "hsem_house_consumption_power": "sensor.house",
                "hsem_solar_production_power": "sensor.solar",
                "hsem_main_fuse_amps": 25,
                "hsem_main_fuse_phases": 1,
            },
        )
        assert errors == {}

    @pytest.mark.asyncio
    async def test_validate_power_step_malformed_entity_id(self):
        from custom_components.hsem.flows.power import validate_power_step_input

        hass = _hass_with_states()
        errors = await validate_power_step_input(
            hass,
            {
                "hsem_house_consumption_power": "BADLY FORMATTED",
                "hsem_solar_production_power": "sensor.ok",
            },
        )
        assert errors["hsem_house_consumption_power"] == "invalid_entity_id"

    @pytest.mark.asyncio
    async def test_validate_months_empty_winter(self):
        from custom_components.hsem.flows.months import validate_months_input

        errors = await validate_months_input(None, {"hsem_months_winter": []})
        assert errors["hsem_months_winter"] == "required"

    @pytest.mark.asyncio
    async def test_validate_months_all_in_winter(self):
        from custom_components.hsem.flows.months import validate_months_input

        errors = await validate_months_input(
            None, {"hsem_months_winter": [str(i) for i in range(1, 13)]}
        )
        assert errors["hsem_months_winter"] == "months_summer_empty"

    @pytest.mark.asyncio
    async def test_validate_schedule_1_disabled_passes(self):
        from custom_components.hsem.flows.batteries_schedule_1 import (
            validate_batteries_schedule_1_input,
        )

        errors = await validate_batteries_schedule_1_input(
            {
                "hsem_batteries_enable_batteries_schedule_1": False,
                "hsem_batteries_enable_batteries_schedule_1_start": "INVALID",
                "hsem_batteries_enable_batteries_schedule_1_end": "INVALID",
            }
        )
        assert errors == {}

    @pytest.mark.asyncio
    async def test_validate_schedule_1_zero_length_rejected(self):
        from custom_components.hsem.flows.batteries_schedule_1 import (
            validate_batteries_schedule_1_input,
        )

        errors = await validate_batteries_schedule_1_input(
            {
                "hsem_batteries_enable_batteries_schedule_1": True,
                "hsem_batteries_enable_batteries_schedule_1_start": "07:00:00",
                "hsem_batteries_enable_batteries_schedule_1_end": "07:00:00",
            }
        )
        assert errors["base"] == "start_time_equals_end_time"

    @pytest.mark.asyncio
    async def test_validate_schedule_2_cross_midnight_valid(self):
        from custom_components.hsem.flows.batteries_schedule_2 import (
            validate_batteries_schedule_2_input,
        )

        errors = await validate_batteries_schedule_2_input(
            {
                "hsem_batteries_enable_batteries_schedule_2": True,
                "hsem_batteries_enable_batteries_schedule_2_start": "23:00:00",
                "hsem_batteries_enable_batteries_schedule_2_end": "02:00:00",
            }
        )
        assert errors == {}

    @pytest.mark.asyncio
    async def test_validate_schedule_3_invalid_time_format(self):
        from custom_components.hsem.flows.batteries_schedule_3 import (
            validate_batteries_schedule_3_input,
        )

        errors = await validate_batteries_schedule_3_input(
            {
                "hsem_batteries_enable_batteries_schedule_3": True,
                "hsem_batteries_enable_batteries_schedule_3_start": "9am",
                "hsem_batteries_enable_batteries_schedule_3_end": "11:00:00",
            }
        )
        assert (
            errors.get("hsem_batteries_enable_batteries_schedule_3_start")
            == "invalid_time_format"
        )

    @pytest.mark.asyncio
    async def test_validate_weighted_values_invalid_sum(self):
        from custom_components.hsem.flows.weighted_values import (
            validate_weighted_values_input,
        )

        errors = await validate_weighted_values_input(
            {
                "hsem_house_consumption_energy_weight_1d": 10,
                "hsem_house_consumption_energy_weight_3d": 10,
                "hsem_house_consumption_energy_weight_7d": 10,
                "hsem_house_consumption_energy_weight_14d": 10,
            }
        )
        assert errors["base"] == "hsem_house_consumption_energy_weight_total"

    @pytest.mark.asyncio
    async def test_validate_weighted_values_valid_100(self):
        from custom_components.hsem.flows.weighted_values import (
            validate_weighted_values_input,
        )

        errors = await validate_weighted_values_input(
            {
                "hsem_house_consumption_energy_weight_1d": 25,
                "hsem_house_consumption_energy_weight_3d": 30,
                "hsem_house_consumption_energy_weight_7d": 30,
                "hsem_house_consumption_energy_weight_14d": 15,
            }
        )
        assert errors == {}

    @pytest.mark.asyncio
    async def test_validate_prices_invalid_price(self):
        from custom_components.hsem.flows.prices import validate_prices_input

        hass = _hass_with_states("sensor.import", "sensor.export")
        # Price outside [-2, 2] range
        errors = await validate_prices_input(
            hass,
            {
                "hsem_import_electricity_price_sensor": "sensor.import",
                "hsem_export_electricity_price_sensor": "sensor.export",
                "hsem_export_electricity_min_price": 99.9,
                "hsem_electricity_price_update_interval": "15",
            },
        )
        assert errors.get("hsem_export_electricity_min_price") == "price_out_of_range"

    @pytest.mark.asyncio
    async def test_validate_prices_valid(self):
        from custom_components.hsem.flows.prices import validate_prices_input

        hass = _hass_with_states("sensor.import", "sensor.export")
        errors = await validate_prices_input(
            hass,
            {
                "hsem_import_electricity_price_sensor": "sensor.import",
                "hsem_export_electricity_price_sensor": "sensor.export",
                "hsem_export_electricity_min_price": -0.05,
                "hsem_electricity_price_update_interval": "15",
            },
        )
        assert errors == {}

    @pytest.mark.asyncio
    async def test_validate_solcast_entity_not_found(self):
        from custom_components.hsem.flows.solcast import validate_solcast_step_input

        hass = _hass_with_states()
        errors = await validate_solcast_step_input(
            hass,
            {
                "hsem_solcast_pv_forecast_forecast_today": "sensor.today",
                "hsem_solcast_pv_forecast_forecast_tomorrow": "sensor.tomorrow",
            },
        )
        assert errors["hsem_solcast_pv_forecast_forecast_today"] == "entity_not_found"

    @pytest.mark.asyncio
    async def test_validate_excess_export_buffer_too_high(self):
        from custom_components.hsem.flows.batteries_excess_export import (
            validate_batteries_excess_export_input,
        )

        errors = await validate_batteries_excess_export_input(
            {
                "hsem_batteries_enable_excess_export": True,
                "hsem_batteries_excess_export_discharge_buffer": 51,
            }
        )
        assert (
            errors.get("hsem_batteries_excess_export_discharge_buffer")
            == "price_out_of_range"
        )

    @pytest.mark.asyncio
    async def test_validate_excess_export_valid(self):
        from custom_components.hsem.flows.batteries_excess_export import (
            validate_batteries_excess_export_input,
        )

        errors = await validate_batteries_excess_export_input(
            {
                "hsem_batteries_enable_excess_export": True,
                "hsem_batteries_excess_export_discharge_buffer": 10,
            }
        )
        assert errors == {}
