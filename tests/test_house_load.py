"""Tests for the derived house load (issue #1310).

Installations with only an inverter and a power meter have no house load
sensor; HSEM derives it as Σ inverter active power − power meter active power
and uses it when no working house sensor is configured.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import UnitOfPower

from custom_components.hsem import _entity_gating_snapshot
from custom_components.hsem.custom_sensors.house_consumption_power_sensor import (
    HSEMHouseConsumptionPowerSensor,
)
from custom_components.hsem.custom_sensors.house_load_power_sensor import (
    HSEMHouseLoadPowerSensor,
)
from custom_components.hsem.flows.power import (
    get_power_step_schema,
    validate_power_step_input,
)
from custom_components.hsem.flows.quick_setup import auto_detect_entities
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.house_load import (
    derived_house_load_w,
    effective_house_power_entity,
    entry_house_power_entity,
    house_load_inputs,
    resolve_house_power,
)
from custom_components.hsem.utils.misc import optional_entity_id
from custom_components.hsem.utils.schema import vol

_DERIVED = "sensor.hsem_house_load_power"
_INVERTER = "sensor.inverter_active_power"
_INVERTER_2 = "sensor.inverter_active_power_2"
_METER = "sensor.power_meter_active_power"


def _state(value: str, unit: str | None = UnitOfPower.WATT) -> SimpleNamespace:
    attributes = {} if unit is None else {"unit_of_measurement": unit}
    return SimpleNamespace(state=value, attributes=attributes)


def _hass(states: dict[str, SimpleNamespace]) -> MagicMock:
    hass = MagicMock()
    hass.states.get = states.get
    hass.states.async_all = lambda: [
        SimpleNamespace(entity_id=entity_id) for entity_id in states
    ]
    return hass


def _entry(**options: Any) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "entry"
    entry.options = options
    entry.data = {}
    return entry


# ---------------------------------------------------------------------------
# The formula
# ---------------------------------------------------------------------------


class TestDerivedHouseLoad:
    def test_export(self) -> None:
        assert derived_house_load_w([3000.0], 2000.0) == pytest.approx(1000.0)

    def test_battery_charging_from_the_grid(self) -> None:
        assert derived_house_load_w([-4500.0], -4600.0) == pytest.approx(100.0)

    def test_two_inverters_are_added_before_the_meter(self) -> None:
        assert derived_house_load_w([3000.0, 1500.0], 2000.0) == pytest.approx(2500.0)

    @pytest.mark.parametrize(
        ("inverters", "meter"),
        [
            ([None], 2000.0),
            ([3000.0], None),
            ([3000.0, None], 2000.0),
            ([float("nan")], 2000.0),
            ([3000.0], float("inf")),
            ([], 2000.0),
        ],
    )
    def test_a_missing_input_is_unknown_never_zero(
        self, inverters: list[float | None], meter: float | None
    ) -> None:
        assert derived_house_load_w(inverters, meter) is None


class TestInputs:
    def test_inverter_and_meter_are_required(self) -> None:
        assert house_load_inputs(_INVERTER, None, _METER) == ([_INVERTER], _METER)
        assert house_load_inputs(None, _INVERTER_2, _METER) is None
        assert house_load_inputs(_INVERTER, _INVERTER_2, None) is None

    def test_the_second_inverter_is_added(self) -> None:
        assert house_load_inputs(_INVERTER, _INVERTER_2, _METER) == (
            [_INVERTER, _INVERTER_2],
            _METER,
        )

    @pytest.mark.parametrize("value", [None, "", vol.UNDEFINED])
    def test_unset_entities_are_none(self, value: Any) -> None:
        assert optional_entity_id(value) is None

    def test_a_set_entity_is_kept(self) -> None:
        assert optional_entity_id(_METER) == _METER


# ---------------------------------------------------------------------------
# Which entity is the house power
# ---------------------------------------------------------------------------


class TestEffectiveHousePowerEntity:
    def test_an_existing_configured_sensor_always_wins(self) -> None:
        assert (
            effective_house_power_entity("sensor.house", True, lambda _e: True)
            == "sensor.house"
        )

    def test_a_missing_configured_sensor_falls_back_to_the_derived_one(
        self,
    ) -> None:
        assert (
            effective_house_power_entity(
                "sensor.power_house_load", True, lambda _e: False
            )
            == _DERIVED
        )

    def test_an_empty_configured_sensor_falls_back_to_the_derived_one(self) -> None:
        assert effective_house_power_entity(None, True, lambda _e: False) == _DERIVED

    def test_without_derived_inputs_the_configured_value_is_kept(self) -> None:
        assert (
            effective_house_power_entity("sensor.gone", False, lambda _e: False)
            == "sensor.gone"
        )
        assert effective_house_power_entity(None, False, lambda _e: False) is None

    def test_the_cycle_config_points_at_the_derived_sensor(self) -> None:
        cfg = SensorConfig(
            house_consumption_power="sensor.power_house_load",
            huawei_solar_inverter_active_power=_INVERTER,
            huawei_solar_power_meter_active_power=_METER,
        )

        resolve_house_power(_hass({_INVERTER: _state("1")}), cfg)

        assert cfg.house_consumption_power == _DERIVED

    def test_the_cycle_config_keeps_a_working_sensor(self) -> None:
        cfg = SensorConfig(
            house_consumption_power="sensor.house",
            huawei_solar_inverter_active_power=_INVERTER,
            huawei_solar_power_meter_active_power=_METER,
        )

        resolve_house_power(_hass({"sensor.house": _state("500")}), cfg)

        assert cfg.house_consumption_power == "sensor.house"

    def test_the_per_hour_sensors_resolve_from_the_entry(self) -> None:
        entry = _entry(
            hsem_house_consumption_power="sensor.power_house_load",
            hsem_huawei_solar_inverter_active_power=_INVERTER,
            hsem_huawei_solar_power_meter_active_power=_METER,
        )
        assert entry_house_power_entity(_hass({}), entry) == _DERIVED

        entry.options["hsem_huawei_solar_power_meter_active_power"] = vol.UNDEFINED
        assert entry_house_power_entity(_hass({}), entry) == "sensor.power_house_load"

    def test_a_per_hour_sensor_reads_the_derived_sensor(self) -> None:
        entry = _entry(
            hsem_house_consumption_power="sensor.power_house_load",
            hsem_ev_charger_power=vol.UNDEFINED,
            hsem_huawei_solar_inverter_active_power=_INVERTER,
            hsem_huawei_solar_power_meter_active_power=_METER,
        )
        sensor = HSEMHouseConsumptionPowerSensor(entry, 14, 15, MagicMock())
        assert sensor._hsem_house_consumption_power == "sensor.power_house_load"

        sensor.hass = _hass({_DERIVED: _state("750")})
        sensor._update_settings()

        assert sensor._hsem_house_consumption_power == _DERIVED


# ---------------------------------------------------------------------------
# The House Load Power sensor
# ---------------------------------------------------------------------------


def _sensor(
    states: dict[str, SimpleNamespace], inverters: list[str] | None = None
) -> HSEMHouseLoadPowerSensor:
    sensor = HSEMHouseLoadPowerSensor(_entry(), inverters or [_INVERTER], _METER)
    sensor.hass = _hass(states)
    return sensor


class TestHouseLoadPowerSensor:
    def test_metadata(self) -> None:
        sensor = _sensor({})
        assert sensor.entity_id == _DERIVED
        assert sensor.unique_id == "hsem_entry_house_load_power"
        assert sensor.device_class == SensorDeviceClass.POWER
        assert sensor.native_unit_of_measurement == UnitOfPower.WATT
        assert sensor.extra_state_attributes == {
            "inverter_active_power_entities": [_INVERTER],
            "power_meter_active_power_entity": _METER,
        }

    def test_export(self) -> None:
        sensor = _sensor({_INVERTER: _state("3000"), _METER: _state("2000")})
        sensor._recompute()
        assert sensor.available is True
        assert sensor.native_value == pytest.approx(1000.0)

    def test_grid_charging(self) -> None:
        sensor = _sensor({_INVERTER: _state("-4500"), _METER: _state("-4600")})
        sensor._recompute()
        assert sensor.native_value == pytest.approx(100.0)

    def test_two_inverters(self) -> None:
        sensor = _sensor(
            {
                _INVERTER: _state("3000"),
                _INVERTER_2: _state("1500"),
                _METER: _state("2000"),
            },
            inverters=[_INVERTER, _INVERTER_2],
        )
        sensor._recompute()
        assert sensor.native_value == pytest.approx(2500.0)

    def test_kilowatt_inputs_are_normalised(self) -> None:
        sensor = _sensor(
            {_INVERTER: _state("3", UnitOfPower.KILO_WATT), _METER: _state("2000")}
        )
        sensor._recompute()
        assert sensor.native_value == pytest.approx(1000.0)

    @pytest.mark.parametrize("meter", ["unavailable", "unknown", "abc"])
    def test_a_bad_input_makes_it_unavailable(self, meter: str) -> None:
        sensor = _sensor({_INVERTER: _state("3000"), _METER: _state(meter)})
        sensor._recompute()
        assert sensor.available is False
        assert sensor.native_value is None

    def test_a_missing_input_makes_it_unavailable(self) -> None:
        sensor = _sensor({_INVERTER: _state("3000")})
        sensor._recompute()
        assert sensor.available is False

    @pytest.mark.asyncio
    async def test_it_tracks_its_inputs_and_updates_on_change(self) -> None:
        states = {_INVERTER: _state("3000"), _METER: _state("2000")}
        sensor = _sensor(states)
        unsub = MagicMock()
        with (
            patch(
                "custom_components.hsem.custom_sensors.house_load_power_sensor."
                "async_track_state_change_event",
                return_value=unsub,
            ) as track,
            patch(
                "custom_components.hsem.entity.HSEMEntity.async_added_to_hass",
            ),
        ):
            await sensor.async_added_to_hass()

        assert track.call_args.args[1] == [_INVERTER, _METER]
        assert sensor.native_value == pytest.approx(1000.0)

        states[_METER] = _state("500")
        with patch.object(sensor, "async_write_ha_state") as write:
            sensor._async_input_changed(MagicMock())
        write.assert_called_once()
        assert sensor.native_value == pytest.approx(2500.0)

        with patch(
            "custom_components.hsem.entity.HSEMEntity.async_will_remove_from_hass"
        ):
            await sensor.async_will_remove_from_hass()
            await sensor.async_will_remove_from_hass()
        unsub.assert_called_once()


# ---------------------------------------------------------------------------
# Config flow and quick setup
# ---------------------------------------------------------------------------


class TestPowerStep:
    @pytest.mark.asyncio
    async def test_the_schema_offers_the_inputs(self) -> None:
        schema = await get_power_step_schema(None)
        keys = {str(key) for key in schema.schema}
        assert {
            "hsem_huawei_solar_inverter_active_power",
            "hsem_huawei_solar_inverter_2_active_power",
            "hsem_huawei_solar_power_meter_active_power",
        } <= keys

    @pytest.mark.asyncio
    async def test_the_house_sensor_may_be_empty_with_the_inputs(self) -> None:
        hass = _hass(
            {"sensor.solar": _state("1"), _INVERTER: _state("1"), _METER: _state("1")}
        )
        errors = await validate_power_step_input(
            hass,
            {
                "hsem_solar_production_power": "sensor.solar",
                "hsem_huawei_solar_inverter_active_power": _INVERTER,
                "hsem_huawei_solar_power_meter_active_power": _METER,
            },
        )
        assert errors == {}

    @pytest.mark.asyncio
    async def test_a_house_sensor_or_the_inputs_are_required(self) -> None:
        hass = _hass({"sensor.solar": _state("1"), _INVERTER: _state("1")})
        errors = await validate_power_step_input(
            hass,
            {
                "hsem_solar_production_power": "sensor.solar",
                "hsem_huawei_solar_inverter_active_power": _INVERTER,
            },
        )
        assert errors == {"hsem_house_consumption_power": "house_power_required"}

    @pytest.mark.asyncio
    async def test_the_second_inverter_needs_the_first(self) -> None:
        hass = _hass(
            {
                "sensor.solar": _state("1"),
                "sensor.house": _state("1"),
                _INVERTER_2: _state("1"),
            }
        )
        errors = await validate_power_step_input(
            hass,
            {
                "hsem_house_consumption_power": "sensor.house",
                "hsem_solar_production_power": "sensor.solar",
                "hsem_huawei_solar_inverter_2_active_power": _INVERTER_2,
            },
        )
        assert errors == {
            "hsem_huawei_solar_inverter_2_active_power": "house_load_needs_inverter_1"
        }

    @pytest.mark.asyncio
    async def test_a_missing_input_entity_is_reported(self) -> None:
        hass = _hass({"sensor.solar": _state("1"), _INVERTER: _state("1")})
        errors = await validate_power_step_input(
            hass,
            {
                "hsem_solar_production_power": "sensor.solar",
                "hsem_huawei_solar_inverter_active_power": _INVERTER,
                "hsem_huawei_solar_power_meter_active_power": _METER,
            },
        )
        assert errors["hsem_huawei_solar_power_meter_active_power"] == (
            "entity_not_found"
        )


class TestQuickSetup:
    _HUAWEI = {
        "sensor.inverter_active_power_control": _state("1"),
        _INVERTER: _state("1"),
        _METER: _state("1"),
    }

    @pytest.mark.asyncio
    async def test_an_emma_or_sdongle_load_sensor_comes_first(self) -> None:
        hass = _hass(
            {
                "sensor.my_house_consumption": _state("1"),
                _DERIVED: _state("1"),
                **self._HUAWEI,
                "sensor.emma_load_power": _state("1"),
            }
        )
        detected = await auto_detect_entities(hass)
        assert detected["house_power"] == "sensor.emma_load_power"
        assert detected["inverter_active_power"] == _INVERTER
        assert detected["power_meter_active_power"] == _METER

    @pytest.mark.asyncio
    async def test_then_the_derived_sensor(self) -> None:
        hass = _hass({"sensor.my_house_consumption": _state("1"), **self._HUAWEI})
        detected = await auto_detect_entities(hass)
        assert detected["house_power"] is None
        assert detected["inverter_active_power"] == _INVERTER

    @pytest.mark.asyncio
    async def test_then_a_user_sensor(self) -> None:
        hass = _hass(
            {
                "sensor.my_house_consumption": _state("1"),
                "sensor.inverter_active_power_control": _state("1"),
            }
        )
        detected = await auto_detect_entities(hass)
        assert detected["house_power"] == "sensor.my_house_consumption"
        assert detected["inverter_active_power"] is None


def test_changing_the_inputs_reloads_the_entry() -> None:
    entry = _entry()
    before = _entity_gating_snapshot(entry)
    entry.options["hsem_huawei_solar_inverter_active_power"] = _INVERTER
    entry.options["hsem_huawei_solar_power_meter_active_power"] = _METER
    after = _entity_gating_snapshot(entry)
    assert before != after
    entry.options["hsem_huawei_solar_power_meter_active_power"] = "sensor.other"
    assert _entity_gating_snapshot(entry) != after
