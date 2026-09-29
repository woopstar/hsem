"""Tests for the live per-phase inputs of the main-fuse check (issue #1119, 6.3.x).

A current sensor on a phase field used to pass through the Watt normalisation
unchanged, so 16 A was read as 16 W and the phase-aware grid-charge limiter saw
almost the whole fuse as headroom.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from custom_components.hsem.custom_sensors.phase_inputs import (
    phase_reading_to_power_w,
    read_grid_phase_power_w,
)
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.phase_power import (
    compute_phase_charge_limits,
    phase_powers_valid,
)

_MODULE = "custom_components.hsem.custom_sensors.phase_inputs"
_PHASES = ("sensor.phase_a", "sensor.phase_b", "sensor.phase_c")


@pytest.mark.parametrize(
    ("value", "unit", "expected_w"),
    [
        (1_500.0, "W", 1_500.0),
        (-800.0, "W", -800.0),  # export stays signed for power readings
        (1.5, "kW", 1_500.0),
        (16.0, "A", 16.0 * 230.0),
        (-16.0, "A", 16.0 * 230.0),  # current has no direction: import
        (16_000.0, "mA", 16.0 * 230.0),
    ],
)
def test_power_and_current_readings_become_watts(
    value: float, unit: str, expected_w: float
) -> None:
    """Power converts to W; current becomes |I| × 230 V."""
    assert phase_reading_to_power_w(value, unit) == pytest.approx(expected_w)


@pytest.mark.parametrize(
    ("value", "unit"),
    [
        (16.0, None),
        (16.0, ""),
        (16.0, "%"),
        (16.0, "V"),
        (float("nan"), "W"),
        (None, "A"),
    ],
)
def test_unusable_readings_fail_closed(value: float | None, unit: str | None) -> None:
    """A missing value or a missing or unknown unit is unavailable."""
    assert phase_reading_to_power_w(value, unit) is None


def _sensor(units: dict[str, str | None]) -> Any:
    """Return a collecting component whose HA states carry *units*."""
    states = {
        entity_id: SimpleNamespace(attributes={"unit_of_measurement": unit})
        for entity_id, unit in units.items()
    }
    return SimpleNamespace(hass=SimpleNamespace(states=SimpleNamespace(get=states.get)))


def _cfg() -> SensorConfig:
    cfg = SensorConfig()
    cfg.huawei_solar_power_meter_phase_a_active_power = _PHASES[0]
    cfg.huawei_solar_power_meter_phase_b_active_power = _PHASES[1]
    cfg.huawei_solar_power_meter_phase_c_active_power = _PHASES[2]
    return cfg


def _reader(values: dict[str, float]) -> Any:
    return lambda entity_id, _kind, *, label: values.get(entity_id)


def test_current_sensors_are_read_as_amps() -> None:
    """Three 16 A phases read as 3680 W each, not 16 W."""
    sensor = _sensor(dict.fromkeys(_PHASES, "A"))

    readings = read_grid_phase_power_w(
        sensor, _cfg(), _reader(dict.fromkeys(_PHASES, 16.0))
    )

    assert readings == (
        pytest.approx(3_680.0),
        pytest.approx(3_680.0),
        pytest.approx(3_680.0),
    )


def test_mixed_units_and_an_unknown_unit() -> None:
    """Each phase keeps its own unit; one unusable phase fails that phase."""
    sensor = _sensor({_PHASES[0]: "W", _PHASES[1]: "kW", _PHASES[2]: None})
    values = {_PHASES[0]: 900.0, _PHASES[1]: 1.2, _PHASES[2]: 5.0}
    log = MagicMock()

    with patch(f"{_MODULE}._LOGGER", log):
        readings = read_grid_phase_power_w(sensor, _cfg(), _reader(values))

    assert readings[0] == pytest.approx(900.0)
    assert readings[1] == pytest.approx(1_200.0)
    assert readings[2] is None
    assert not phase_powers_valid(readings)
    log.warning.assert_called_once()
    assert log.warning.call_args.args[1:] == (
        _PHASES[2],
        "power_meter_phase_c_active_power",
        "none",
    )


def test_unusable_unit_warning_is_throttled() -> None:
    """The WARNING repeats at most once per hour per entity and unit."""
    sensor = _sensor(dict.fromkeys(_PHASES, "%"))
    log = MagicMock()

    with patch(f"{_MODULE}._LOGGER", log):
        for _ in range(3):
            read_grid_phase_power_w(
                sensor, _cfg(), _reader(dict.fromkeys(_PHASES, 50.0))
            )

    assert log.warning.call_count == 3  # once per phase, not per cycle


def test_unconfigured_or_unreadable_phase_is_none_without_warning() -> None:
    """No entity or no value is simply unavailable."""
    sensor = _sensor({})
    log = MagicMock()

    with patch(f"{_MODULE}._LOGGER", log):
        readings = read_grid_phase_power_w(sensor, SensorConfig(), _reader({}))

    assert readings == (None, None, None)
    log.warning.assert_not_called()


def test_fuse_fully_used_by_current_readings_blocks_grid_charge() -> None:
    """16 A on every phase of a 16 A fuse leaves no headroom (issue #1119).

    Read as 16 W per phase (the pre-fix behaviour), the limiter allowed a
    multi-kW charge on a fuse that was already at its rating.
    """
    sensor = _sensor(dict.fromkeys(_PHASES, "A"))
    readings = read_grid_phase_power_w(
        sensor, _cfg(), _reader(dict.fromkeys(_PHASES, 16.0))
    )
    assert phase_powers_valid(readings)

    limits = compute_phase_charge_limits(
        measured_phase_power_w=readings,
        fuse_amps=16.0,
        desired_charge_power_w=5_000.0,
        battery_actual_power_w=0.0,
        charge_efficiency_pct=97.0,
        discharge_efficiency_pct=97.0,
    )
    misread = compute_phase_charge_limits(
        measured_phase_power_w=(16.0, 16.0, 16.0),
        fuse_amps=16.0,
        desired_charge_power_w=5_000.0,
        battery_actual_power_w=0.0,
        charge_efficiency_pct=97.0,
        discharge_efficiency_pct=97.0,
    )

    assert limits.primary_charge_power_w == pytest.approx(0.0)
    assert misread.primary_charge_power_w == pytest.approx(5_000.0)


def test_collector_reads_phases_through_the_amp_aware_reader() -> None:
    """The collector must not route phase fields through the Watt normaliser.

    ``read_normalized_float`` passes an unconvertible unit through unchanged,
    which is exactly how 16 A became 16 W (issue #1119).
    """
    import inspect

    from custom_components.hsem.custom_sensors import state_collector

    source = inspect.getsource(state_collector)
    assert "read_grid_phase_power_w(sensor, cfg, _read)" in source
    for phase in "abc":
        assert f'label="power_meter_phase_{phase}_active_power"' not in source
