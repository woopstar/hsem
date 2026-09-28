"""Tests for the live per-phase main-fuse checks in amps (issue #1119).

A main fuse trips on per-phase current. Both live checks, the Huawei
grid-charge limiter and the switchable-EV one-phase hold, used to compare
signed active power against ``main_fuse_amps × 230 V``, and a current sensor
was passed through as Watts, so 16 A read as 16 W and the guard was silently
disabled. These tests pin the amps-based replacement:

- a current reading is compared with the fuse directly, as a magnitude;
- a power reading is converted at the live phase voltage (230 V fallback);
- an unusable phase reading blocks both checks;
- the config flow only offers power/current (and voltage) sensors.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from homeassistant.const import (
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfPower,
)
from homeassistant.core import State

from custom_components.hsem.coordinator_ev_command_stability import (
    CoordinatorEvCommandStabilityMixin,
    _EvCommandSpec,
)
from custom_components.hsem.custom_sensors.phase_charge_limiter import (
    build_phase_aware_charge_commands,
)
from custom_components.hsem.custom_sensors.phase_inputs import (
    PHASE_UNIT_WARNING_INTERVAL_S,
    read_grid_phase_inputs,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import EVLiveState, LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.phase_power import (
    EV_TOPOLOGY_THREE_PHASE_SWITCHABLE,
    PhaseReading,
    PhaseReadings,
    PhaseVoltages,
    compute_phase_charge_limits,
    phase_fuse_headroom_a,
    phase_import_current_a,
    phase_voltage_v,
)
from custom_components.hsem.utils.recommendations import Recommendations
from tests.phase_fixtures import amps, watts

_NO_VOLTAGE: PhaseVoltages = (None, None, None)
_INPUTS_MODULE = "custom_components.hsem.custom_sensors.phase_inputs"
_NOW = datetime(2026, 9, 28, 2, 0, tzinfo=UTC)


def _limits(
    readings: PhaseReadings,
    *,
    fuse_amps: float = 35.0,
    voltages: PhaseVoltages = _NO_VOLTAGE,
    desired_w: float = 20_000.0,
    battery_w: float = 0.0,
    efficiency_pct: float = 98.0,
) -> float:
    """Return the limiter's DC grid-charge command for a live snapshot."""
    return compute_phase_charge_limits(
        measured_phase=readings,
        phase_voltages_v=voltages,
        fuse_amps=fuse_amps,
        desired_charge_power_w=desired_w,
        battery_actual_power_w=battery_w,
        charge_efficiency_pct=efficiency_pct,
        discharge_efficiency_pct=efficiency_pct,
    ).primary_charge_power_w


# ---------------------------------------------------------------------------
# Amps-based headroom
# ---------------------------------------------------------------------------


class TestCurrentReadings:
    """A current reading is compared with the fuse directly."""

    def test_16a_on_a_35a_fuse_leaves_19a_headroom(self) -> None:
        """The issue's case: 16 A measured leaves about 19 A, not about 8 kW."""
        headroom = phase_fuse_headroom_a(amps(16.0, 16.0, 16.0), _NO_VOLTAGE, 35.0)

        assert headroom == pytest.approx((19.0, 19.0, 19.0))

    def test_the_limiter_caps_charging_at_the_amp_headroom(self) -> None:
        """16 A on every phase allows 3 × 19 A × 230 V of added AC load."""
        limits = compute_phase_charge_limits(
            measured_phase=amps(16.0, 16.0, 16.0),
            fuse_amps=35.0,
            desired_charge_power_w=20_000.0,
            battery_actual_power_w=0.0,
            charge_efficiency_pct=98.0,
            discharge_efficiency_pct=98.0,
        )

        # 13 110 W AC × 0.98 = 12 847.8 W DC, floored to the 100 W step.
        assert limits.primary_charge_power_w == pytest.approx(12_800.0)
        assert limits.predicted_phase_current_a is not None
        assert max(limits.predicted_phase_current_a) <= 35.0 + 1e-6

    def test_16a_is_not_read_as_16w(self) -> None:
        """Before #1119 the same number as Watts let the full request through."""
        assert _limits(watts(16.0, 16.0, 16.0)) == pytest.approx(20_000.0)
        assert _limits(amps(16.0, 16.0, 16.0)) < 20_000.0

    def test_own_battery_draw_is_removed_at_the_phase_voltage(self) -> None:
        """A running charge must not consume its own apparent amp headroom."""
        # 6900 W at 100 % efficiency is 2300 W, i.e. 10 A, per phase.
        command = _limits(
            amps(20.0, 20.0, 20.0), battery_w=6900.0, efficiency_pct=100.0
        )

        # 35 A − (20 A − 10 A) = 25 A per phase → 3 × 25 A × 230 V = 17 250 W.
        assert command == pytest.approx(17_200.0)


class TestUnsignedCurrent:
    """A current reading is a magnitude; only signed power earns export room."""

    def test_negative_current_is_treated_as_import(self) -> None:
        """An unsigned or reversed CT can never manufacture export headroom."""
        exporting = phase_fuse_headroom_a(amps(-10.0, -10.0, -10.0), _NO_VOLTAGE, 35.0)
        importing = phase_fuse_headroom_a(amps(10.0, 10.0, 10.0), _NO_VOLTAGE, 35.0)

        assert exporting == pytest.approx((25.0, 25.0, 25.0))
        assert exporting == pytest.approx(importing)

    def test_the_limiter_grants_no_extra_charge_for_negative_current(self) -> None:
        """The command for −10 A equals the command for +10 A."""
        assert _limits(amps(-10.0, -10.0, -10.0)) == pytest.approx(
            _limits(amps(10.0, 10.0, 10.0))
        )

    def test_signed_power_keeps_its_export_headroom(self) -> None:
        """A phase exporting 2300 W has 10 A more room than the fuse rating."""
        headroom = phase_fuse_headroom_a(
            watts(-2300.0, -2300.0, -2300.0), _NO_VOLTAGE, 35.0
        )

        assert headroom == pytest.approx((45.0, 45.0, 45.0))
        assert _limits(watts(-2300.0, -2300.0, -2300.0)) > _limits(
            amps(-10.0, -10.0, -10.0)
        )


class TestPhaseVoltage:
    """Power converts to current at the live phase voltage, else 230 V."""

    def test_8050w_at_220v_is_over_a_35a_fuse(self) -> None:
        """The issue's example: 8050 W (35 A × 230 V) is 36.6 A at 220 V."""
        current_a = phase_import_current_a(
            PhaseReading(8050.0, UnitOfPower.WATT), 220.0
        )
        headroom = phase_fuse_headroom_a(
            watts(8050.0, 8050.0, 8050.0), (220.0, 220.0, 220.0), 35.0
        )

        assert current_a == pytest.approx(36.59, abs=0.01)
        assert headroom is not None
        assert max(headroom) < 0.0

    def test_power_without_a_voltage_uses_230v(self) -> None:
        """Unconfigured voltage keeps the pre-#1119 W configs unchanged."""
        headroom = phase_fuse_headroom_a(
            watts(8050.0, 8050.0, 8050.0), _NO_VOLTAGE, 35.0
        )

        assert headroom == pytest.approx((0.0, 0.0, 0.0), abs=1e-9)

    def test_the_limiter_converts_headroom_back_at_the_live_voltage(self) -> None:
        """A 25 A headroom at 220 V is 3 × 25 A × 220 V of added AC load."""
        command = _limits(
            watts(2200.0, 2200.0, 2200.0),
            voltages=(220.0, 220.0, 220.0),
            efficiency_pct=100.0,
        )

        assert command == pytest.approx(16_500.0)
        assert command < _limits(watts(2200.0, 2200.0, 2200.0), efficiency_pct=100.0)

    @pytest.mark.parametrize("voltage", [None, float("nan"), 0.0, 50.0, 1000.0])
    def test_an_implausible_voltage_falls_back_to_230v(
        self, voltage: float | None
    ) -> None:
        """A missing or glitched voltage never distorts the conversion."""
        assert phase_voltage_v(voltage) == pytest.approx(230.0)

    @pytest.mark.parametrize("voltage", [120.0, 220.0, 240.0])
    def test_a_plausible_voltage_is_used(self, voltage: float) -> None:
        """Real phase voltages from 100 V to 240 V systems are honoured."""
        assert phase_voltage_v(voltage) == pytest.approx(voltage)


# ---------------------------------------------------------------------------
# Unusable readings fail closed
# ---------------------------------------------------------------------------


def _grid_charge_rec() -> HourlyRecommendation:
    """Return a one-hour grid-charge slot asking for 5 kWh."""
    return HourlyRecommendation(
        start=_NOW,
        end=_NOW + timedelta(hours=1),
        recommendation=Recommendations.BatteriesChargeGrid.value,
        avg_house_consumption_kwh=0.0,
        avg_house_consumption_1d_kwh=0.0,
        avg_house_consumption_3d_kwh=0.0,
        avg_house_consumption_7d_kwh=0.0,
        avg_house_consumption_14d_kwh=0.0,
        batteries_charged_kwh=5.0,
        batteries_discharged_kwh=0.0,
        estimated_battery_capacity_kwh=0.0,
        estimated_battery_soc_pct=50.0,
        estimated_cost_currency=0.0,
        estimated_net_consumption_kwh=0.0,
        export_price=0.0,
        grid_export_kwh=0.0,
        grid_import_kwh=0.0,
        import_price=1.0,
        solcast_pv_estimate_kwh=0.0,
    )


def _limiter_config() -> SensorConfig:
    """Return a config with the live grid-charge limiter enabled."""
    cfg = SensorConfig()
    cfg.phase_aware_charging_enabled = True
    cfg.main_fuse_amps = 35
    cfg.main_fuse_phases = 3
    return cfg


class TestUnusableReadingsBlock:
    """A phase HSEM cannot interpret stops the limiter, never guesses."""

    @pytest.mark.parametrize(
        "readings",
        [
            (*amps(16.0, 16.0, None)[:2], None),
            (*amps(16.0, 16.0, None)[:2], PhaseReading(16.0, "var")),
        ],
        ids=["unconvertible-unit-read-as-none", "unsupported-unit"],
    )
    def test_the_grid_charge_limiter_writes_zero(self, readings: Any) -> None:
        """The limiter pins the grid-charge cap to 0 W."""
        live = LiveState()
        live.grid_phase_readings = readings
        live.huawei_batteries_charge_discharge_power_w = 0.0

        commands = build_phase_aware_charge_commands(
            _limiter_config(), live, _grid_charge_rec()
        )

        assert commands.primary_grid_charge_power_w == pytest.approx(0.0)
        assert commands.limits is None

    def test_the_pure_core_also_fails_closed(self) -> None:
        """Called directly with an unusable phase, the core commands 0 W."""
        limits = compute_phase_charge_limits(
            measured_phase=amps(16.0, None, 16.0),
            fuse_amps=35.0,
            desired_charge_power_w=5000.0,
            battery_actual_power_w=0.0,
            charge_efficiency_pct=98.0,
            discharge_efficiency_pct=98.0,
        )

        assert limits.primary_charge_power_w == pytest.approx(0.0)
        assert limits.predicted_phase_current_a is None

    def test_a_current_reading_drives_the_limiter_end_to_end(self) -> None:
        """Amps readings reach the fuse comparison through the wrapper."""
        live = LiveState()
        live.grid_phase_readings = amps(33.0, 20.0, 20.0)
        live.huawei_batteries_charge_discharge_power_w = 0.0

        commands = build_phase_aware_charge_commands(
            _limiter_config(), live, _grid_charge_rec()
        )

        # 2 A left on phase A → 3 × 2 A × 230 V × 0.98 = 1352.4 W → 1300 W.
        assert commands.primary_grid_charge_power_w == pytest.approx(1300.0)


# ---------------------------------------------------------------------------
# Switchable-EV one-phase hold
# ---------------------------------------------------------------------------


def _ev_spec(
    readings: PhaseReadings,
    *,
    voltages: PhaseVoltages = _NO_VOLTAGE,
    ev_power_w: float = 1000.0,
    fuse_amps: float = 25.0,
    fuse_phases: int = 3,
) -> _EvCommandSpec:
    """Return a switchable-EV command spec with a live phase snapshot."""
    return _EvCommandSpec(
        key="ev",
        label="EV",
        is_second=False,
        deadband_a=3.0,
        stub_floor_minutes=2.0,
        topology=EV_TOPOLOGY_THREE_PHASE_SWITCHABLE,
        rated_current_a=16,
        min_current_a=6,
        managed=True,
        ev_live=EVLiveState(is_charging=True, power_w=ev_power_w),
        capacity_kwh=60.0,
        target_soc_pct=80.0,
        deadline=None,
        main_fuse_amps=fuse_amps,
        main_fuse_phases=fuse_phases,
        grid_phase_readings=readings,
        grid_phase_voltage_v=voltages,
    )


_hold_is_safe = CoordinatorEvCommandStabilityMixin._one_phase_hold_is_phase_safe


class TestOnePhaseHold:
    """The EV hold uses the same amps helper as the limiter."""

    def test_added_current_that_fits_every_phase_is_safe(self) -> None:
        """1840 W held over 1000 W live adds 3.65 A; 21 A + 3.65 A fits 25 A."""
        assert _hold_is_safe(_ev_spec(amps(21.0, 20.0, 20.0)), 1840.0) is True

    def test_added_current_that_overloads_any_phase_is_rejected(self) -> None:
        """21.5 A + 3.65 A exceeds 25 A on phase A."""
        assert _hold_is_safe(_ev_spec(amps(21.5, 20.0, 20.0)), 1840.0) is False

    def test_amps_are_not_read_as_watts(self) -> None:
        """A phase already over the fuse in amps rejects the hold."""
        assert _hold_is_safe(_ev_spec(amps(26.0, 5.0, 5.0)), 1840.0) is False
        assert _hold_is_safe(_ev_spec(watts(26.0, 5.0, 5.0)), 1840.0) is True

    def test_an_unusable_phase_rejects_the_hold(self) -> None:
        """No complete amp proof means no hold."""
        assert _hold_is_safe(_ev_spec(amps(10.0, None, 10.0)), 1840.0) is False

    def test_a_single_phase_supply_cannot_prove_the_hold(self) -> None:
        """Per-phase proof needs a three-phase fuse configuration."""
        spec = _ev_spec(amps(1.0, 1.0, 1.0), fuse_phases=1)

        assert _hold_is_safe(spec, 1840.0) is False

    def test_no_configured_fuse_needs_no_proof(self) -> None:
        """Without a fuse rating there is nothing to check against."""
        spec = _ev_spec((None, None, None), fuse_amps=0.0)

        assert _hold_is_safe(spec, 1840.0) is True

    def test_a_low_live_voltage_tightens_the_check(self) -> None:
        """4800 W + 840 W held is 24.5 A at 230 V but 26.9 A at 210 V."""
        readings = watts(4800.0, 1000.0, 1000.0)

        assert _hold_is_safe(_ev_spec(readings), 1840.0) is True
        assert (
            _hold_is_safe(_ev_spec(readings, voltages=(210.0, 210.0, 210.0)), 1840.0)
            is False
        )


# ---------------------------------------------------------------------------
# Reading the phase inputs from Home Assistant
# ---------------------------------------------------------------------------


def _phase_cfg() -> SensorConfig:
    """Return a config with all three phases and voltages wired up."""
    cfg = SensorConfig()
    cfg.huawei_solar_power_meter_phase_a_active_power = "sensor.phase_a"
    cfg.huawei_solar_power_meter_phase_b_active_power = "sensor.phase_b"
    cfg.huawei_solar_power_meter_phase_c_active_power = "sensor.phase_c"
    cfg.huawei_solar_power_meter_phase_a_voltage = "sensor.voltage_a"
    cfg.huawei_solar_power_meter_phase_b_voltage = "sensor.voltage_b"
    cfg.huawei_solar_power_meter_phase_c_voltage = "sensor.voltage_c"
    return cfg


def _read(
    values: Mapping[str, float | None],
    units: Mapping[str, str | None],
    *,
    sensor: SimpleNamespace | None = None,
    cfg: SensorConfig | None = None,
) -> tuple[PhaseReadings, PhaseVoltages]:
    """Run the phase-input reader against stubbed HA states."""

    def _state(entity_id: str) -> State | None:
        if entity_id not in units:
            return None
        unit = units[entity_id]
        return State(
            entity_id, "1", {} if unit is None else {"unit_of_measurement": unit}
        )

    def _reader(entity_id: str | None, conv_type: str, label: str = "") -> Any:
        return None if entity_id is None else values.get(entity_id)

    if sensor is None:
        sensor = SimpleNamespace(hass=MagicMock())
    sensor.hass.states.get.side_effect = _state
    return read_grid_phase_inputs(sensor, cfg or _phase_cfg(), _reader)


class TestReadGridPhaseInputs:
    """Each phase keeps its unit family; anything else becomes ``None``."""

    def test_power_and_current_sensors_can_be_mixed(self) -> None:
        """Every field is detected from its own unit and normalised."""
        readings, voltages = _read(
            {
                "sensor.phase_a": 16.0,
                "sensor.phase_b": 2.3,
                "sensor.phase_c": 500.0,
                "sensor.voltage_a": 229.0,
                "sensor.voltage_b": 0.231,
                "sensor.voltage_c": 232.0,
            },
            {
                "sensor.phase_a": UnitOfElectricCurrent.AMPERE,
                "sensor.phase_b": UnitOfPower.KILO_WATT,
                "sensor.phase_c": UnitOfElectricCurrent.MILLIAMPERE,
                "sensor.voltage_a": UnitOfElectricPotential.VOLT,
                "sensor.voltage_b": UnitOfElectricPotential.KILOVOLT,
                "sensor.voltage_c": UnitOfElectricPotential.VOLT,
            },
        )

        assert [reading.unit for reading in readings if reading] == [
            UnitOfElectricCurrent.AMPERE,
            UnitOfPower.WATT,
            UnitOfElectricCurrent.AMPERE,
        ]
        assert [reading.value for reading in readings if reading] == pytest.approx(
            [16.0, 2300.0, 0.5]
        )
        assert voltages == pytest.approx((229.0, 231.0, 232.0))

    @pytest.mark.parametrize("unit", ["var", "VA", None])
    def test_an_uninterpretable_unit_becomes_unavailable(
        self, unit: str | None
    ) -> None:
        """Reactive/apparent power or no unit at all fails closed, loudly."""
        with patch(f"{_INPUTS_MODULE}._LOGGER") as logger:
            readings, _ = _read(
                {"sensor.phase_a": 16.0, "sensor.phase_b": 1.0, "sensor.phase_c": 1.0},
                {
                    "sensor.phase_a": unit,
                    "sensor.phase_b": UnitOfPower.WATT,
                    "sensor.phase_c": UnitOfPower.WATT,
                },
            )

        assert readings[0] is None
        assert readings[1] == PhaseReading(1.0, UnitOfPower.WATT)
        logger.warning.assert_called_once()
        args = logger.warning.call_args.args
        assert args[1] == "sensor.phase_a"
        assert args[3] == (unit or "none")

    def test_the_unit_warning_is_rate_limited(self) -> None:
        """One WARNING per entity and unit per interval, not one per cycle."""
        sensor = SimpleNamespace(hass=MagicMock())
        values = {"sensor.phase_a": 16.0}
        with (
            patch(f"{_INPUTS_MODULE}._LOGGER") as logger,
            patch(f"{_INPUTS_MODULE}.monotonic") as clock,
        ):
            clock.return_value = 1000.0
            _read(values, {"sensor.phase_a": "var"}, sensor=sensor)
            clock.return_value = 1000.0 + PHASE_UNIT_WARNING_INTERVAL_S - 1.0
            _read(values, {"sensor.phase_a": "var"}, sensor=sensor)
            assert logger.warning.call_count == 1

            _read(values, {"sensor.phase_a": "VA"}, sensor=sensor)
            assert logger.warning.call_count == 2

            clock.return_value = 1000.0 + PHASE_UNIT_WARNING_INTERVAL_S
            _read(values, {"sensor.phase_a": "var"}, sensor=sensor)
            assert logger.warning.call_count == 3

    def test_an_unavailable_reading_is_none_without_a_unit_warning(self) -> None:
        """A missing value is ordinary unavailability, not a unit problem."""
        with patch(f"{_INPUTS_MODULE}._LOGGER") as logger:
            readings, voltages = _read(
                {}, {"sensor.phase_a": UnitOfElectricCurrent.AMPERE}
            )

        assert readings == (None, None, None)
        assert voltages == (None, None, None)
        logger.warning.assert_not_called()

    def test_unconfigured_voltages_stay_none(self) -> None:
        """Voltage is optional; the helpers fall back to 230 V."""
        cfg = _phase_cfg()
        cfg.huawei_solar_power_meter_phase_a_voltage = None
        cfg.huawei_solar_power_meter_phase_b_voltage = None
        cfg.huawei_solar_power_meter_phase_c_voltage = None

        _, voltages = _read({"sensor.voltage_a": 229.0}, {}, cfg=cfg)

        assert voltages == (None, None, None)


# ---------------------------------------------------------------------------
# Config flow
# ---------------------------------------------------------------------------


class TestPowerStepSelectors:
    """The phase fields only offer sensors HSEM can compare with the fuse."""

    @pytest.mark.asyncio
    async def test_phase_and_voltage_selectors_are_restricted(self) -> None:
        """Power/current for the phase readings; voltage for the voltages."""
        from custom_components.hsem.flows.power import get_power_step_schema

        schema = await get_power_step_schema(None)
        configs = {str(key): value.config for key, value in schema.schema.items()}

        for phase in "abc":
            reading = configs[
                f"hsem_huawei_solar_power_meter_phase_{phase}_active_power"
            ]
            voltage = configs[f"hsem_huawei_solar_power_meter_phase_{phase}_voltage"]
            assert reading["domain"] == ["sensor"]
            assert set(reading["device_class"]) == {"power", "current"}
            assert voltage["device_class"] == ["voltage"]

    @pytest.mark.asyncio
    async def test_voltage_fields_round_trip_and_are_optional(self) -> None:
        """Voltages are optional and stored under their own keys."""
        from custom_components.hsem.flows.power import get_power_step_schema

        schema = await get_power_step_schema(None)
        base = {
            "hsem_house_consumption_power": "sensor.house",
            "hsem_solar_production_power": "sensor.solar",
        }

        with_voltage = cast(
            dict[str, Any],
            schema(
                {**base, "hsem_huawei_solar_power_meter_phase_a_voltage": "sensor.v_a"}
            ),
        )
        without_voltage = cast(dict[str, Any], schema(base))

        assert with_voltage["hsem_huawei_solar_power_meter_phase_a_voltage"] == (
            "sensor.v_a"
        )
        assert "hsem_huawei_solar_power_meter_phase_a_voltage" not in without_voltage

    @pytest.mark.asyncio
    async def test_a_missing_voltage_entity_is_reported(self) -> None:
        """A configured voltage entity must exist, like the phase readings."""
        from custom_components.hsem.flows.power import validate_power_step_input

        hass = MagicMock()
        hass.states.get.side_effect = lambda entity_id: (
            None if entity_id == "sensor.missing" else State(entity_id, "1")
        )

        errors = await validate_power_step_input(
            hass,
            {
                "hsem_house_consumption_power": "sensor.house",
                "hsem_solar_production_power": "sensor.solar",
                "hsem_huawei_solar_power_meter_phase_b_voltage": "sensor.missing",
            },
        )

        assert errors == {
            "hsem_huawei_solar_power_meter_phase_b_voltage": "entity_not_found"
        }
