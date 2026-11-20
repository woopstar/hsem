"""Tests for the EfficiencyLearner — battery efficiency per power step (#1431)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from custom_components.hsem.utils.efficiency_learner import (
    CHARGE,
    DISCHARGE,
    MAX_SAMPLES,
    MAX_STEP_W,
    STATUS_IMPLAUSIBLE,
    STATUS_LEARNING,
    STATUS_OK,
    EfficiencyLearner,
    EfficiencyStep,
)

_T0 = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
_MODULE = "custom_components.hsem.utils.efficiency_learner"

type Reading = tuple[float | None, float | None]


def _feed(
    learner: EfficiencyLearner,
    readings: Sequence[Reading],
    *,
    step_s: float = 300.0,
    capacity_kwh: float | None = 10.0,
    start: datetime = _T0,
) -> datetime:
    """Feed *readings* (power W, SoC %) one cycle apart; return the next time."""
    at = start
    for power_w, soc_pct in readings:
        learner.update(at, power_w, soc_pct, capacity_kwh)
        at += timedelta(seconds=step_s)
    return at


def _ramp(power_w: float, samples: int, *, start_soc: float = 20.0) -> list[Reading]:
    """Return readings that complete *samples* measurements at *power_w*.

    The SoC moves 1 % per cycle.  The first reading opens the window, the
    second anchors it, and every two readings after that close a measurement.
    """
    sign = 1.0 if power_w > 0 else -1.0
    return [(power_w, start_soc + sign * i) for i in range(2 * samples + 2)]


def _charged(samples: int = 12) -> EfficiencyLearner:
    """Return a learner holding *samples* charge measurements at 80 %.

    1500 W for 600 s is 0.25 kWh at the sensor; 2 % of 10 kWh is 0.2 kWh.
    """
    learner = EfficiencyLearner()
    _feed(learner, _ramp(1500.0, samples))
    return learner


class TestMeasurement:
    """A measurement runs between SoC changes and lands in its power step."""

    def test_charge_measurement_compares_stored_with_measured_energy(self) -> None:
        """One charge window: 0.2 kWh stored over 0.25 kWh measured is 80 %."""
        learner = _charged(samples=1)

        step = learner.charge[1500]
        assert step.samples == 1
        assert step.measured_kwh == pytest.approx(0.25)
        assert step.stored_kwh == pytest.approx(0.2)
        assert step.efficiency(CHARGE) == pytest.approx(0.8)
        assert learner.discharge == {}
        assert learner.dirty is True

    def test_discharge_measurement_compares_measured_with_stored_energy(self) -> None:
        """One discharge window: 0.16 kWh delivered from 0.2 kWh is 80 %."""
        learner = EfficiencyLearner()

        # 2000 W for 288 s is 0.16 kWh at the sensor.
        _feed(learner, _ramp(-2000.0, 1, start_soc=80.0), step_s=144.0)

        step = learner.discharge[2000]
        assert step.samples == 1
        assert step.measured_kwh == pytest.approx(0.16)
        assert step.stored_kwh == pytest.approx(0.2)
        assert step.efficiency(DISCHARGE) == pytest.approx(0.8)
        assert learner.charge == {}

    def test_nothing_is_measured_before_the_first_soc_change(self) -> None:
        """A flat SoC never anchors a window."""
        learner = EfficiencyLearner()

        _feed(learner, [(1500.0, 50.0)] * 6)

        assert learner.charge == {}
        assert learner.dirty is False

    def test_a_window_needs_two_percent(self) -> None:
        """One percent after the anchor is not enough to close."""
        learner = EfficiencyLearner()

        _feed(learner, [(1500.0, 20.0), (1500.0, 21.0), (1500.0, 22.0)])

        assert learner.charge == {}

    def test_measurements_chain_from_the_closing_soc(self) -> None:
        """The reading that closes a measurement anchors the next one."""
        learner = _charged(samples=3)

        assert learner.charge[1500].samples == 3
        assert learner.sample_count(CHARGE) == 3
        assert learner.sample_count(DISCHARGE) == 0

    def test_power_above_the_last_step_lands_in_the_last_step(self) -> None:
        """A 25 kW measurement is kept in the top step."""
        learner = EfficiencyLearner()

        # 25 kW for 36 s is 0.25 kWh.
        _feed(learner, _ramp(25_000.0, 1), step_s=18.0)

        assert list(learner.charge) == [MAX_STEP_W]


class TestRejection:
    """Unclean windows never become a sample."""

    @pytest.mark.parametrize(
        "interruption",
        [
            pytest.param((50.0, 22.5), id="idle"),
            pytest.param((-1500.0, 22.0), id="reversed"),
            pytest.param((None, 22.5), id="power-unavailable"),
            pytest.param((1500.0, None), id="soc-unavailable"),
        ],
    )
    def test_interruption_discards_the_window(self, interruption: Reading) -> None:
        """Without the interruption the last reading would close a sample."""
        learner = EfficiencyLearner()

        _feed(
            learner,
            [(1500.0, 20.0), (1500.0, 21.0), (1500.0, 22.0), interruption]
            + [(1500.0, 23.0)],
        )

        assert learner.charge == {}
        assert learner.discharge == {}

    @pytest.mark.parametrize("capacity_kwh", [None, 0.0, -5.0])
    def test_unknown_capacity_discards_the_window(
        self, capacity_kwh: float | None
    ) -> None:
        """A reading without a usable capacity resets the measurement."""
        learner = EfficiencyLearner()
        at = _feed(learner, [(1500.0, 20.0), (1500.0, 21.0), (1500.0, 22.0)])

        at = _feed(learner, [(1500.0, 22.5)], capacity_kwh=capacity_kwh, start=at)
        _feed(learner, [(1500.0, 23.0)], start=at)

        assert learner.charge == {}

    def test_a_long_gap_discards_the_window(self) -> None:
        """Two readings further apart than the gap limit start over."""
        learner = EfficiencyLearner()
        at = _feed(learner, [(1500.0, 20.0), (1500.0, 21.0), (1500.0, 22.0)])

        _feed(learner, [(1500.0, 23.0)], start=at + timedelta(seconds=601))

        assert learner.charge == {}

    def test_a_reading_that_is_not_newer_discards_the_window(self) -> None:
        """A repeated timestamp cannot be integrated."""
        learner = EfficiencyLearner()
        at = _feed(learner, [(1500.0, 20.0), (1500.0, 21.0), (1500.0, 22.0)])

        _feed(learner, [(1500.0, 23.0)], start=at - timedelta(seconds=300))

        assert learner.charge == {}

    def test_soc_moving_against_the_power_discards_the_window(self) -> None:
        """A falling SoC while charging is not a clean measurement."""
        learner = EfficiencyLearner()

        at = _feed(
            learner,
            [(1500.0, 20.0), (1500.0, 21.0), (1500.0, 20.5), (1500.0, 22.0)]
            + [(1500.0, 23.0)],
        )

        assert learner.charge == {}

        # The window restarted at 20.5 and anchored at 22: 24 closes it.
        _feed(learner, [(1500.0, 24.0)], start=at)
        assert learner.charge[1500].samples == 1

    def test_varying_power_is_rejected_and_the_next_window_counts(self) -> None:
        """A spread above one step is dropped; measuring continues after it."""
        learner = EfficiencyLearner()

        at = _feed(
            learner,
            [(1000.0, 20.0), (1000.0, 21.0), (1800.0, 22.0), (1000.0, 23.0)],
        )
        assert learner.charge == {}

        _feed(learner, [(1000.0, 24.0), (1000.0, 25.0)], start=at)
        assert learner.charge[1000].samples == 1

    def test_a_sensor_fault_is_rejected(self) -> None:
        """2 kWh stored from 0.25 kWh measured is not an efficiency."""
        learner = EfficiencyLearner()

        _feed(learner, _ramp(1500.0, 1), capacity_kwh=100.0)

        assert learner.charge == {}
        assert learner.dirty is False


class TestStepQualification:
    """A step is reported only with enough samples, energy and a sane value."""

    def test_too_few_samples_is_learning(self) -> None:
        """Nine samples are below the minimum of ten."""
        learner = _charged(samples=9)

        assert learner.charge[1500].status(CHARGE) == STATUS_LEARNING
        assert learner.charge_efficiency_pct is None
        assert learner.curve(CHARGE)[0]["efficiency_pct"] is None
        assert learner.curve(CHARGE)[0]["samples"] == 9

    def test_too_little_energy_is_learning(self) -> None:
        """Twelve samples of 0.1 kWh stay below the 2 kWh minimum."""
        learner = EfficiencyLearner()

        # 5 kWh battery: 0.1 kWh stored, 1500 W for 300 s is 0.125 kWh.
        _feed(learner, _ramp(1500.0, 12), step_s=150.0, capacity_kwh=5.0)

        step = learner.charge[1500]
        assert step.samples == 12
        assert step.stored_kwh == pytest.approx(1.2)
        assert step.status(CHARGE) == STATUS_LEARNING

    def test_an_empty_step_is_learning(self) -> None:
        """A step without energy has no efficiency."""
        step = EfficiencyStep()

        assert step.efficiency(CHARGE) is None
        assert step.status(CHARGE) == STATUS_LEARNING

    def test_usable_step_is_reported(self) -> None:
        """Twelve samples and 2.4 kWh at 80 % make the step usable."""
        learner = _charged()

        assert learner.charge[1500].status(CHARGE) == STATUS_OK
        assert learner.charge_efficiency_pct == pytest.approx(80.0)
        assert learner.curve(CHARGE) == [
            {
                "power_from_w": 1500,
                "power_to_w": 2000,
                "efficiency_pct": pytest.approx(80.0),
                "samples": 12,
                "energy_kwh": pytest.approx(2.4),
                "status": STATUS_OK,
            }
        ]

    @pytest.mark.parametrize(
        ("step_s", "expected"),
        [
            pytest.param(192.0, 1.25, id="above-100-percent"),
            pytest.param(600.0, 0.4, id="below-50-percent"),
        ],
    )
    def test_implausible_step_is_discarded(
        self, step_s: float, expected: float
    ) -> None:
        """A step outside 50-100 % is flagged and left out of the result."""
        learner = EfficiencyLearner()

        _feed(learner, _ramp(1500.0, 12), step_s=step_s)

        step = learner.charge[1500]
        assert step.efficiency(CHARGE) == pytest.approx(expected)
        assert step.status(CHARGE) == STATUS_IMPLAUSIBLE
        assert learner.charge_efficiency_pct is None
        assert learner.curve(CHARGE)[0]["efficiency_pct"] is None
        assert learner.curve(CHARGE)[0]["status"] == STATUS_IMPLAUSIBLE

    def test_a_full_step_fades_its_oldest_samples(self) -> None:
        """Beyond the cap the count stays and the sums are scaled down."""
        step = EfficiencyStep(samples=MAX_SAMPLES, measured_kwh=50.0, stored_kwh=40.0)

        step.add(0.25, 0.2)

        keep = (MAX_SAMPLES - 1) / MAX_SAMPLES
        assert step.samples == MAX_SAMPLES
        assert step.measured_kwh == pytest.approx(50.0 * keep + 0.25)
        assert step.stored_kwh == pytest.approx(40.0 * keep + 0.2)


class TestRoundTrip:
    """The round trip is the product of both measured directions."""

    def test_round_trip_needs_both_directions(self) -> None:
        """Charge alone gives no round trip."""
        learner = _charged()

        assert learner.discharge_efficiency_pct is None
        assert learner.round_trip_efficiency_pct is None

    def test_round_trip_is_charge_times_discharge(self) -> None:
        """80 % in and 80 % out is a 64 % round trip."""
        learner = _charged()
        _feed(
            learner,
            _ramp(-2000.0, 12, start_soc=80.0),
            step_s=144.0,
            start=_T0 + timedelta(days=1),
        )

        assert learner.discharge_efficiency_pct == pytest.approx(80.0)
        assert learner.round_trip_efficiency_pct == pytest.approx(64.0)

    def test_overall_efficiency_is_energy_weighted_over_usable_steps(self) -> None:
        """Only usable steps count, weighted by their energy."""
        learner = EfficiencyLearner()
        learner.charge[500] = EfficiencyStep(20, measured_kwh=10.0, stored_kwh=9.0)
        learner.charge[3000] = EfficiencyStep(20, measured_kwh=30.0, stored_kwh=24.0)
        learner.charge[1000] = EfficiencyStep(3, measured_kwh=1.0, stored_kwh=0.5)

        assert learner.charge_efficiency_pct == pytest.approx(33.0 / 40.0 * 100.0)
        assert [row["power_from_w"] for row in learner.curve(CHARGE)] == [
            500,
            1000,
            3000,
        ]


class TestPersistence:
    """The learned steps survive a restart."""

    @pytest.mark.asyncio
    async def test_save_and_load_round_trip(self, tmp_path: Path) -> None:
        """A second learner reads back what the first one wrote."""
        path = tmp_path / ".storage" / "efficiency.json"
        learner = _charged()
        learner.history_file = str(path)

        assert await learner.save_history() is True
        assert learner.dirty is False

        restored = EfficiencyLearner(history_file=str(path))
        await restored.load_history()

        assert restored.charge[1500].samples == 12
        assert restored.charge_efficiency_pct == pytest.approx(80.0)
        assert restored.discharge == {}
        assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1

    @pytest.mark.asyncio
    async def test_without_a_history_file_nothing_is_read_or_written(self) -> None:
        """Before the coordinator sets the path, persistence is a no-op."""
        learner = _charged()

        await learner.load_history()

        assert await learner.save_history() is False
        assert learner.charge[1500].samples == 12
        assert learner.dirty is True

    @pytest.mark.asyncio
    async def test_a_missing_file_keeps_the_learner_empty(self, tmp_path: Path) -> None:
        """First start: no file, no error."""
        learner = EfficiencyLearner(history_file=str(tmp_path / "missing.json"))

        await learner.load_history()

        assert learner.charge == {}

    @pytest.mark.asyncio
    async def test_a_failed_write_keeps_the_learner_dirty(self, tmp_path: Path) -> None:
        """The next cycle retries a write that failed."""
        learner = _charged()
        learner.history_file = str(tmp_path / "efficiency.json")

        with patch(f"{_MODULE}.write_json_history_file", return_value=False):
            assert await learner.save_history() is False

        assert learner.dirty is True

    def test_restore_drops_the_window_in_progress(self) -> None:
        """A restore does not continue a measurement from before it."""
        learner = EfficiencyLearner()
        at = _feed(learner, [(1500.0, 20.0), (1500.0, 21.0), (1500.0, 22.0)])

        learner.restore(learner.as_dict())
        _feed(learner, [(1500.0, 23.0)], start=at)

        assert learner.charge == {}

    @pytest.mark.parametrize(
        "entry",
        [
            pytest.param("not-a-dict", id="not-a-dict"),
            pytest.param({"samples": 3, "measured_kwh": 1.0}, id="missing-key"),
            pytest.param(
                {"samples": "x", "measured_kwh": 1.0, "stored_kwh": 1.0},
                id="bad-number",
            ),
            pytest.param(
                {"samples": None, "measured_kwh": 1.0, "stored_kwh": 1.0},
                id="null-number",
            ),
            pytest.param(
                {"samples": 0, "measured_kwh": 1.0, "stored_kwh": 1.0},
                id="no-samples",
            ),
            pytest.param(
                {"samples": 3, "measured_kwh": float("nan"), "stored_kwh": 1.0},
                id="nan-measured",
            ),
            pytest.param(
                {"samples": 3, "measured_kwh": 1.0, "stored_kwh": float("inf")},
                id="inf-stored",
            ),
            pytest.param(
                {"samples": 3, "measured_kwh": 0.0, "stored_kwh": 1.0},
                id="zero-measured",
            ),
            pytest.param(
                {"samples": 3, "measured_kwh": 1.0, "stored_kwh": -1.0},
                id="negative-stored",
            ),
        ],
    )
    def test_restore_skips_an_invalid_entry(self, entry: Any) -> None:
        """A damaged entry is dropped; the valid one next to it is kept."""
        valid = {"samples": 3, "measured_kwh": 1.0, "stored_kwh": 0.9}
        learner = _charged()

        learner.restore({CHARGE: {"500": entry, "1000": valid}, DISCHARGE: []})

        assert list(learner.charge) == [1000]
        assert learner.charge[1000].stored_kwh == pytest.approx(0.9)
        assert learner.discharge == {}
        assert learner.dirty is False

    @pytest.mark.parametrize("key", ["abc", "-500", "750", str(MAX_STEP_W + 500)])
    def test_restore_skips_an_invalid_step_key(self, key: str) -> None:
        """The key must be a step boundary inside the table."""
        valid = {"samples": 3, "measured_kwh": 1.0, "stored_kwh": 0.9}
        learner = EfficiencyLearner()

        learner.restore({DISCHARGE: {key: valid}})

        assert learner.discharge == {}
