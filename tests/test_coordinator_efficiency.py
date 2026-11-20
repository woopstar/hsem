"""Tests for the coordinator's battery efficiency tracking (issue #1431).

``coordinator_efficiency`` feeds each cycle's battery reading to the
efficiency learner and persists it.  The sensor tests read what that real
producer learned, not a hand-built learner.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from homeassistant.const import PERCENTAGE, EntityCategory

from custom_components.hsem.coordinator_data import CoordinatorData
from custom_components.hsem.coordinator_efficiency import (
    HISTORY_FILE,
    async_track_battery_efficiency,
    battery_capacity_kwh,
)
from custom_components.hsem.coordinator_state import CoordinatorSharedState
from custom_components.hsem.custom_sensors.battery_efficiency_sensor import (
    HSEMBatteryEfficiencySensor,
)
from custom_components.hsem.devices import HSEMDevice
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.capacity_learner import CapacityLearner
from custom_components.hsem.utils.efficiency_learner import (
    STATUS_OK,
    EfficiencyLearner,
)

_T0 = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
_MODULE = "custom_components.hsem.coordinator_efficiency"
_ENTRY_ID = "test_entry"


def _coordinator(tmp_path: Path) -> CoordinatorSharedState:
    """Return the slice of the coordinator the efficiency tracking uses."""
    return cast(
        CoordinatorSharedState,
        SimpleNamespace(
            hass=SimpleNamespace(config=SimpleNamespace(config_dir=str(tmp_path))),
            _efficiency_learner=EfficiencyLearner(),
            _capacity_learner=CapacityLearner(),
            last_update_success=True,
            data=CoordinatorData(cfg=SensorConfig()),
        ),
    )


def _live(power_w: float | None, soc_pct: float | None) -> LiveState:
    """Return a live state for a 10 kWh battery."""
    live = LiveState()
    live.huawei_batteries_charge_discharge_power_w = power_w
    live.huawei_batteries_soc_pct = soc_pct
    live.huawei_batteries_rated_capacity_wh = 10_000.0
    return live


async def _run_cycles(
    coordinator: CoordinatorSharedState,
    power_w: float,
    *,
    samples: int,
    step_s: float,
    start: datetime,
    cfg: SensorConfig | None = None,
) -> None:
    """Run the cycles that complete *samples* measurements at *power_w*."""
    sign = 1.0 if power_w > 0 else -1.0
    start_soc = 20.0 if power_w > 0 else 80.0
    for i in range(2 * samples + 2):
        await async_track_battery_efficiency(
            coordinator,
            start + timedelta(seconds=step_s * i),
            _live(power_w, start_soc + sign * i),
            cfg or SensorConfig(),
        )


async def _learn_both_directions(coordinator: CoordinatorSharedState) -> None:
    """Charge at 80 % and discharge at 90 % through the real cycle hook."""
    # Charge: 1500 W for 600 s = 0.25 kWh measured, 0.2 kWh stored.
    await _run_cycles(coordinator, 1500.0, samples=12, step_s=300.0, start=_T0)
    # Discharge: 2000 W for 324 s = 0.18 kWh measured, 0.2 kWh stored.
    await _run_cycles(
        coordinator,
        -2000.0,
        samples=12,
        step_s=162.0,
        start=_T0 + timedelta(days=1),
    )


def _sensor(coordinator: Any) -> HSEMBatteryEfficiencySensor:
    """Construct the sensor bound to *coordinator*."""
    entry = MagicMock()
    entry.entry_id = _ENTRY_ID
    entry.options = {}
    entry.data = {}
    return HSEMBatteryEfficiencySensor(entry, coordinator)


class TestBatteryCapacity:
    """The learned capacity wins; the rated capacity is the fallback."""

    def test_rated_capacity_until_the_capacity_is_learned(self, tmp_path: Path) -> None:
        """An empty capacity learner falls back to the rated capacity."""
        coordinator = _coordinator(tmp_path)

        assert battery_capacity_kwh(coordinator, _live(0.0, 50.0)) == pytest.approx(
            10.0
        )

    def test_learned_capacity_wins(self, tmp_path: Path) -> None:
        """Enough capacity samples replace the rated capacity."""
        coordinator = _coordinator(tmp_path)
        coordinator._capacity_learner.samples = [9.2] * 20

        assert battery_capacity_kwh(coordinator, _live(0.0, 50.0)) == pytest.approx(9.2)

    @pytest.mark.parametrize("rated_wh", [None, 0.0])
    def test_no_capacity_at_all(self, tmp_path: Path, rated_wh: float | None) -> None:
        """Without a learned or rated capacity there is nothing to measure with."""
        coordinator = cast(CoordinatorSharedState, SimpleNamespace())
        live = _live(0.0, 50.0)
        live.huawei_batteries_rated_capacity_wh = rated_wh

        assert battery_capacity_kwh(coordinator, live) is None


class TestTracking:
    """Each cycle feeds the learner and saves what it learned."""

    @pytest.mark.asyncio
    async def test_cycles_learn_and_persist(self, tmp_path: Path) -> None:
        """Clean charge cycles end up as a step on disk."""
        coordinator = _coordinator(tmp_path)

        await _run_cycles(coordinator, 1500.0, samples=12, step_s=300.0, start=_T0)

        learner = coordinator._efficiency_learner
        assert learner.charge[1500].samples == 12
        assert learner.charge_efficiency_pct == pytest.approx(80.0)
        assert learner.dirty is False
        assert (tmp_path / ".storage" / HISTORY_FILE).exists()

    @pytest.mark.asyncio
    async def test_a_restart_restores_the_steps(self, tmp_path: Path) -> None:
        """A fresh coordinator reads the previous one's file on its first cycle."""
        await _run_cycles(
            _coordinator(tmp_path), 1500.0, samples=12, step_s=300.0, start=_T0
        )
        restarted = _coordinator(tmp_path)

        await async_track_battery_efficiency(
            restarted, _T0 + timedelta(days=1), _live(0.0, 50.0), SensorConfig()
        )

        assert restarted._efficiency_learner.charge[1500].samples == 12
        assert restarted._efficiency_learner.charge_efficiency_pct == pytest.approx(
            80.0
        )

    @pytest.mark.asyncio
    async def test_a_long_update_interval_widens_the_gap_limit(
        self, tmp_path: Path
    ) -> None:
        """With a 10-minute interval, cycles 1100 s apart still measure."""
        coordinator = _coordinator(tmp_path)
        cfg = SensorConfig()
        cfg.update_interval = 10

        with patch.object(
            EfficiencyLearner, "update", autospec=True, return_value=None
        ) as update:
            await async_track_battery_efficiency(
                coordinator, _T0, _live(1500.0, 20.0), cfg
            )

        assert update.call_args.kwargs["max_gap_seconds"] == pytest.approx(1200.0)

    @pytest.mark.asyncio
    async def test_a_coordinator_without_a_learner_is_left_alone(self) -> None:
        """Test doubles without the learner do not fail the cycle."""
        coordinator = cast(CoordinatorSharedState, SimpleNamespace())

        await async_track_battery_efficiency(
            coordinator, _T0, _live(1500.0, 20.0), SensorConfig()
        )

    @pytest.mark.asyncio
    async def test_a_failure_is_logged_and_does_not_raise(self, tmp_path: Path) -> None:
        """The cycle that called the tracking survives an error in it."""
        coordinator = _coordinator(tmp_path)

        with (
            patch.object(EfficiencyLearner, "update", side_effect=RuntimeError("boom")),
            patch(f"{_MODULE}.async_log") as log,
        ):
            await async_track_battery_efficiency(
                coordinator, _T0, _live(1500.0, 20.0), SensorConfig()
            )

        assert log.call_args.args[0] == "error"


class TestBatteryEfficiencySensor:
    """The sensor publishes what the cycle hook learned."""

    @pytest.mark.asyncio
    async def test_reports_round_trip_curve_and_configured_value(
        self, tmp_path: Path
    ) -> None:
        """Learned through the real cycle hook, read through the sensor."""
        coordinator = _coordinator(tmp_path)
        await _learn_both_directions(coordinator)

        sensor = _sensor(coordinator)

        assert sensor.native_value == pytest.approx(72.0)
        attributes = sensor.extra_state_attributes
        assert attributes is not None
        assert attributes["measured_round_trip_efficiency_pct"] == pytest.approx(72.0)
        # SensorConfig defaults: 98 % x 98 %.
        assert attributes["configured_round_trip_efficiency_pct"] == pytest.approx(96.0)
        assert attributes["round_trip_difference_pct"] == pytest.approx(-24.0)
        assert attributes["measured_charge_efficiency_pct"] == pytest.approx(80.0)
        assert attributes["measured_discharge_efficiency_pct"] == pytest.approx(90.0)
        assert attributes["charge_samples"] == 12
        assert attributes["discharge_samples"] == 12
        assert attributes["charge_curve"] == [
            {
                "power_from_w": 1500,
                "power_to_w": 2000,
                "efficiency_pct": pytest.approx(80.0),
                "samples": 12,
                "energy_kwh": pytest.approx(2.4),
                "status": STATUS_OK,
            }
        ]
        assert attributes["discharge_curve"][0]["power_from_w"] == 2000
        assert attributes["discharge_curve"][0]["efficiency_pct"] == pytest.approx(90.0)
        assert attributes["power_step_w"] == 500
        assert attributes["min_samples_per_step"] == 10
        assert attributes["min_energy_per_step_kwh"] == pytest.approx(2.0)

    def test_unknown_until_both_directions_are_learned(self, tmp_path: Path) -> None:
        """A fresh learner has no round trip and empty curves."""
        sensor = _sensor(_coordinator(tmp_path))

        assert sensor.native_value is None
        attributes = sensor.extra_state_attributes
        assert attributes is not None
        assert attributes["measured_round_trip_efficiency_pct"] is None
        assert attributes["round_trip_difference_pct"] is None
        assert attributes["charge_curve"] == []
        assert attributes["discharge_curve"] == []
        assert attributes["charge_samples"] == 0

    def test_no_configured_value_before_the_first_cycle(self, tmp_path: Path) -> None:
        """Without a snapshot the configured round trip is unknown."""
        coordinator = _coordinator(tmp_path)
        coordinator.data = None  # type: ignore[assignment]  # before the first cycle

        attributes = _sensor(coordinator).extra_state_attributes

        assert attributes is not None
        assert attributes["configured_round_trip_efficiency_pct"] is None

    def test_unreadable_configured_efficiency_is_unknown(self, tmp_path: Path) -> None:
        """A configured value that is not a number gives no round trip."""
        coordinator = _coordinator(tmp_path)
        cfg = SensorConfig()
        cfg.batteries_charge_efficiency = None  # type: ignore[assignment]  # bad option
        coordinator.data = CoordinatorData(cfg=cfg)

        attributes = _sensor(coordinator).extra_state_attributes

        assert attributes is not None
        assert attributes["configured_round_trip_efficiency_pct"] is None

    def test_a_coordinator_without_a_learner_reports_nothing(self) -> None:
        """Without the learner there is no value and no attributes."""
        sensor = _sensor(SimpleNamespace(last_update_success=True, data=None))

        assert sensor.native_value is None
        assert sensor.extra_state_attributes is None

    def test_entity_contract(self, tmp_path: Path) -> None:
        """Diagnostic, push-driven, on the battery device, in percent."""
        sensor = _sensor(_coordinator(tmp_path))

        assert sensor.unique_id == f"hsem_{_ENTRY_ID}_battery_efficiency_sensor"
        assert sensor.entity_id == "sensor.hsem_battery_efficiency_sensor"
        assert sensor.should_poll is False
        assert sensor.entity_category is EntityCategory.DIAGNOSTIC
        assert sensor.native_unit_of_measurement == PERCENTAGE
        assert sensor.translation_key == "battery_efficiency"
        assert sensor._hsem_device is HSEMDevice.BATTERY_ENERGY
