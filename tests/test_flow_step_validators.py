"""Tests for per-step config flow validators and helpers in ``flows/``.

Complements ``test_flow_step_routing.py`` (which patches the validators to
exercise routing) by running the real validators and helpers directly:
required-field checks, range and type errors, entity/device lookups,
quick-setup auto-detection, and the v1 → v2 data migration.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.flows.battery_economics import (
    validate_battery_economics_input,
)
from custom_components.hsem.flows.energy_and_ml import validate_energy_and_ml_input
from custom_components.hsem.flows.ev_planned_load import (
    validate_ev_planned_load_input,
)
from custom_components.hsem.flows.ev_second_planned_load import (
    validate_ev_second_planned_load_input,
)
from custom_components.hsem.flows.huawei_solar import validate_huawei_solar_input
from custom_components.hsem.flows.init import validate_init_step_input
from custom_components.hsem.flows.migrations import _migrate_v1_to_v2
from custom_components.hsem.flows.ocpp import validate_ocpp_step_input
from custom_components.hsem.flows.quick_setup import auto_detect_entities

_CONFIG_VALIDATOR_MODULE = "custom_components.hsem.utils.config_validator"

_VALID_INIT_INPUT: dict[str, Any] = {
    "device_name": "HSEM",
    "hsem_read_only": False,
    "hsem_verbose_logging": False,
    "hsem_extended_attributes": False,
    "hsem_update_interval": 5,
    "hsem_recommendation_interval_minutes": "60",
    "hsem_recommendation_interval_length": "24",
}

_VALID_BATTERY_ECONOMICS_INPUT: dict[str, Any] = {
    "hsem_batteries_purchase_price": 50_000,
    "hsem_batteries_expected_cycles": 6000,
    "hsem_batteries_cycle_cost": 0.1,
    "hsem_batteries_capacity_loss_pct": 30,
    "hsem_batteries_charge_efficiency": 95,
    "hsem_batteries_discharge_efficiency": 95,
    "hsem_planner_hysteresis_enabled": True,
    "hsem_planner_hysteresis_absolute": 0.1,
    "hsem_planner_hysteresis_percentage": 5,
    "hsem_planner_window_hysteresis_minutes": 15,
}

_VALID_OCPP_INPUT: dict[str, Any] = {
    "hsem_ocpp_enabled": True,
    "hsem_ocpp_port": 9000,
    "hsem_ocpp_start_window_s": 60,
    "hsem_ocpp_stop_window_s": 180,
}


class TestInitValidator:
    """The init step requires every general setting."""

    @pytest.mark.asyncio
    async def test_complete_input_is_valid(self) -> None:
        """All required fields present → no errors."""
        assert await validate_init_step_input(dict(_VALID_INIT_INPUT)) == {}

    @pytest.mark.asyncio
    async def test_each_missing_field_is_required(self) -> None:
        """An empty submission flags every required field."""
        errors = await validate_init_step_input({})

        assert errors == dict.fromkeys(_VALID_INIT_INPUT, "required")


class TestBatteryEconomicsValidator:
    """Battery economics requires all fields and a non-negative price."""

    @pytest.mark.asyncio
    async def test_complete_input_is_valid(self) -> None:
        """A complete, in-range submission passes."""
        errors = await validate_battery_economics_input(
            dict(_VALID_BATTERY_ECONOMICS_INPUT)
        )

        assert errors == {}

    @pytest.mark.asyncio
    async def test_missing_fields_are_required(self) -> None:
        """Every omitted field is reported as required."""
        errors = await validate_battery_economics_input({})

        assert errors == dict.fromkeys(_VALID_BATTERY_ECONOMICS_INPUT, "required")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("price", "expected"),
        [
            (-1, "price_out_of_range"),
            (100_001, "price_out_of_range"),
            ("not a number", "invalid_price_value"),
        ],
    )
    async def test_purchase_price_bounds_and_type(
        self, price: object, expected: str
    ) -> None:
        """Negative, too-large, or non-numeric purchase prices are rejected."""
        user_input = {
            **_VALID_BATTERY_ECONOMICS_INPUT,
            "hsem_batteries_purchase_price": price,
        }

        errors = await validate_battery_economics_input(user_input)

        assert errors == {"hsem_batteries_purchase_price": expected}


class TestHuaweiSolarValidator:
    """The Huawei step checks entities and devices exist in Home Assistant."""

    _REQUIRED_ENTITIES = (
        "hsem_huawei_solar_batteries_working_mode",
        "hsem_huawei_solar_batteries_state_of_capacity",
        "hsem_huawei_solar_inverter_active_power_control",
        "hsem_huawei_solar_batteries_maximum_charging_power",
        "hsem_huawei_solar_batteries_grid_charge_cutoff_soc",
        "hsem_huawei_solar_batteries_charging_cutoff_capacity",
        "hsem_huawei_solar_batteries_tou_charging_and_discharging_periods",
        "hsem_huawei_solar_batteries_rated_capacity",
        "hsem_huawei_solar_batteries_excess_pv_energy_use_in_tou",
        "hsem_huawei_solar_batteries_end_of_discharge_soc",
    )

    def _valid_input(self) -> dict[str, Any]:
        """Return a submission with every required entity and device set."""
        user_input: dict[str, Any] = {
            field: f"sensor.{field.removeprefix('hsem_')}"
            for field in self._REQUIRED_ENTITIES
        }
        user_input["hsem_huawei_solar_device_id_inverter_1"] = "inverter_1"
        return user_input

    @pytest.mark.asyncio
    async def test_existing_entities_and_devices_are_valid(self) -> None:
        """Everything resolves → no errors."""
        with (
            patch(
                f"{_CONFIG_VALIDATOR_MODULE}.async_entity_exists",
                AsyncMock(return_value=True),
            ),
            patch(
                f"{_CONFIG_VALIDATOR_MODULE}.async_device_exists",
                AsyncMock(return_value=True),
            ),
        ):
            errors = await validate_huawei_solar_input(MagicMock(), self._valid_input())

        assert errors == {}

    @pytest.mark.asyncio
    async def test_unknown_entities_and_devices_are_reported(self) -> None:
        """Entity and device errors from both lookups are merged."""
        user_input = self._valid_input()
        user_input["hsem_huawei_solar_batteries_maximum_discharging_power"] = (
            "number.missing_discharge_power"
        )
        user_input["hsem_huawei_solar_device_id_batteries_2"] = "missing_battery"

        async def entity_exists(_hass: Any, entity_id: str) -> bool:
            return entity_id != "number.missing_discharge_power"

        async def device_exists(_hass: Any, device_id: str) -> bool:
            return device_id != "missing_battery"

        with (
            patch(f"{_CONFIG_VALIDATOR_MODULE}.async_entity_exists", entity_exists),
            patch(f"{_CONFIG_VALIDATOR_MODULE}.async_device_exists", device_exists),
        ):
            errors = await validate_huawei_solar_input(MagicMock(), user_input)

        assert errors == {
            "hsem_huawei_solar_batteries_maximum_discharging_power": (
                "entity_not_found"
            ),
            "hsem_huawei_solar_device_id_batteries_2": "device_not_found",
        }

    @pytest.mark.asyncio
    async def test_missing_primary_inverter_is_required(self) -> None:
        """The first inverter device is mandatory."""
        user_input = self._valid_input()
        del user_input["hsem_huawei_solar_device_id_inverter_1"]

        with (
            patch(
                f"{_CONFIG_VALIDATOR_MODULE}.async_entity_exists",
                AsyncMock(return_value=True),
            ),
            patch(
                f"{_CONFIG_VALIDATOR_MODULE}.async_device_exists",
                AsyncMock(return_value=True),
            ),
        ):
            errors = await validate_huawei_solar_input(MagicMock(), user_input)

        assert errors == {"hsem_huawei_solar_device_id_inverter_1": "required"}


class TestPlannedLoadValidators:
    """Both planned-load steps validate only their own prefixed fields."""

    @pytest.mark.asyncio
    async def test_primary_planned_load_rejects_oversized_battery(self) -> None:
        """An enabled primary planned load checks the EV battery capacity."""
        errors = await validate_ev_planned_load_input(
            MagicMock(),
            {
                "hsem_ev_planned_load_enabled": True,
                "hsem_ev_planned_load_battery_capacity_kwh": 500,
            },
        )

        assert errors == {
            "hsem_ev_planned_load_battery_capacity_kwh": "energy_out_of_range"
        }

    @pytest.mark.asyncio
    async def test_second_planned_load_uses_its_own_prefix(self) -> None:
        """The second EV step ignores the primary EV's fields."""
        errors = await validate_ev_second_planned_load_input(
            MagicMock(),
            {
                "hsem_ev_second_planned_load_enabled": True,
                "hsem_ev_second_planned_load_battery_capacity_kwh": 500,
                "hsem_ev_planned_load_battery_capacity_kwh": 500,
            },
        )

        assert errors == {
            "hsem_ev_second_planned_load_battery_capacity_kwh": "energy_out_of_range"
        }

    @pytest.mark.asyncio
    async def test_disabled_second_planned_load_is_not_validated(self) -> None:
        """A disabled planned load accepts any leftover values."""
        errors = await validate_ev_second_planned_load_input(
            MagicMock(),
            {
                "hsem_ev_second_planned_load_enabled": False,
                "hsem_ev_second_planned_load_battery_capacity_kwh": 500,
            },
        )

        assert errors == {}


class TestEnergyAndMlValidator:
    """The energy and ML step has no cross-field validation."""

    @pytest.mark.asyncio
    async def test_any_input_is_accepted(self) -> None:
        """All fields are optional, so nothing is flagged."""
        assert await validate_energy_and_ml_input(MagicMock(), {}) == {}


class TestOcppValidator:
    """OCPP ports must be integers in the unprivileged range and distinct."""

    @pytest.mark.asyncio
    async def test_missing_fields_are_required(self) -> None:
        """Omitted core fields are reported; the second port only if enabled."""
        errors = await validate_ocpp_step_input(
            MagicMock(), {"hsem_ocpp_second_enabled": True}
        )

        assert errors == {
            "hsem_ocpp_enabled": "required",
            "hsem_ocpp_port": "required",
            "hsem_ocpp_start_window_s": "required",
            "hsem_ocpp_stop_window_s": "required",
            "hsem_ocpp_second_port": "required",
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("field", "value", "expected"),
        [
            ("hsem_ocpp_port", 80, "power_out_of_range"),
            ("hsem_ocpp_port", 70_000, "power_out_of_range"),
            ("hsem_ocpp_port", "not a port", "invalid_power_value"),
            ("hsem_ocpp_second_port", 1023, "power_out_of_range"),
            ("hsem_ocpp_second_port", "not a port", "invalid_power_value"),
        ],
    )
    async def test_port_range_and_type(
        self, field: str, value: object, expected: str
    ) -> None:
        """Privileged, oversized, or non-numeric ports are rejected."""
        user_input = {**_VALID_OCPP_INPUT, field: value}

        errors = await validate_ocpp_step_input(MagicMock(), user_input)

        assert errors == {field: expected}

    @pytest.mark.asyncio
    async def test_conflict_is_not_reported_against_an_invalid_primary_port(
        self,
    ) -> None:
        """Only the invalid primary port is flagged, not a bogus conflict."""
        user_input = {
            **_VALID_OCPP_INPUT,
            "hsem_ocpp_port": "not a port",
            "hsem_ocpp_second_port": 9000,
        }

        errors = await validate_ocpp_step_input(MagicMock(), user_input)

        assert errors == {"hsem_ocpp_port": "invalid_power_value"}


def _hass_with_entities(*entity_ids: str) -> MagicMock:
    """Return a mock ``hass`` whose state machine holds *entity_ids*."""
    hass = MagicMock()
    states = []
    for entity_id in entity_ids:
        state = MagicMock()
        state.entity_id = entity_id
        states.append(state)
    hass.states.async_all.return_value = states
    return hass


class TestQuickSetupAutoDetection:
    """Auto-detection maps well-known entity id patterns to HSEM roles."""

    @pytest.mark.asyncio
    async def test_detects_huawei_solcast_price_and_power_entities(self) -> None:
        """The first matching entity per role is picked, case-insensitively."""
        hass = _hass_with_entities(
            "sensor.unrelated_temperature",
            "sensor.Battery_State_Of_Capacity",
            "select.batteries_working_mode",
            "number.batteries_maximum_charging_power",
            "sensor.solcast_pv_forecast_forecast_today",
            "sensor.nordpool_kwh_dk1",
            "sensor.house_consumption_power",
            "sensor.pv_power",
            "sensor.second_battery_soc",
        )

        detected = await auto_detect_entities(hass)

        assert detected["battery_soc"] == "sensor.Battery_State_Of_Capacity"
        assert detected["working_mode"] == "select.batteries_working_mode"
        assert detected["max_charge_power"] == (
            "number.batteries_maximum_charging_power"
        )
        assert detected["solcast_today"] == "sensor.solcast_pv_forecast_forecast_today"
        assert detected["import_price"] == "sensor.nordpool_kwh_dk1"
        # Export price defaults to the same spot price entity.
        assert detected["export_price"] == "sensor.nordpool_kwh_dk1"
        assert detected["house_power"] == "sensor.house_consumption_power"
        assert detected["solar_power"] == "sensor.pv_power"
        assert detected["solcast_tomorrow"] is None
        assert detected["tou_periods"] is None

    @pytest.mark.asyncio
    async def test_nothing_detected_without_matching_entities(self) -> None:
        """An empty state machine detects nothing."""
        detected = await auto_detect_entities(_hass_with_entities())

        assert detected
        assert all(value is None for value in detected.values())


class TestMigrateV1ToV2:
    """The v1 → v2 data migration renames keys and converts month strings."""

    def test_renames_legacy_keys_and_converts_month_strings(self) -> None:
        """Energi Data Service keys are renamed; string months become ints."""
        migrated = _migrate_v1_to_v2(
            {
                "hsem_energi_data_service_import": "sensor.import",
                "hsem_batteries_conversion_loss": 10,
                "hsem_months_winter": ["1", "2", "12"],
                "hsem_months_summer": [3, 4],
            }
        )

        assert migrated["hsem_import_electricity_price_sensor"] == "sensor.import"
        assert "hsem_energi_data_service_import" not in migrated
        assert "hsem_batteries_conversion_loss" not in migrated
        assert migrated["hsem_months_winter"] == [1, 2, 12]
        assert migrated["hsem_months_summer"] == [3, 4]

    def test_existing_new_key_wins_over_legacy_key(self) -> None:
        """A partially migrated entry keeps its v2 value."""
        migrated = _migrate_v1_to_v2(
            {
                "hsem_energi_data_service_import": "sensor.legacy",
                "hsem_import_electricity_price_sensor": "sensor.current",
            }
        )

        assert migrated["hsem_import_electricity_price_sensor"] == "sensor.current"
        assert migrated["hsem_energi_data_service_import"] == "sensor.legacy"
