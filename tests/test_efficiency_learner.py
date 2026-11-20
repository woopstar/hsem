"""Tests for the EfficiencyLearner — battery efficiency per power step (#1431)."""

from __future__ import annotations

import json
import math
import random
from collections.abc import Callable, Sequence
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
    STORAGE_VERSION,
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
    second anchors it, and every five readings after that close a measurement.
    """
    sign = 1.0 if power_w > 0 else -1.0
    return [(power_w, start_soc + sign * i) for i in range(5 * samples + 2)]


def _charged(samples: int = 12) -> EfficiencyLearner:
    """Return a learner holding *samples* charge measurements at 80 %.

    1500 W for 1500 s is 0.625 kWh at the sensor; 5 % of 10 kWh is 0.5 kWh.
    """
    learner = EfficiencyLearner()
    _feed(learner, _ramp(1500.0, samples, start_soc=10.0))
    return learner


def _discharge(learner: EfficiencyLearner, samples: int = 12) -> None:
    """Add *samples* discharge measurements at 80 % to *learner*.

    2000 W for 720 s is 0.4 kWh at the sensor; 5 % of 10 kWh is 0.5 kWh.
    """
    _feed(
        learner,
        _ramp(-2000.0, samples, start_soc=90.0),
        step_s=144.0,
        start=_T0 + timedelta(days=1),
    )


_TRUE_EFFICIENCY = 0.97
_CYCLE_S = 300.0


def _simulate(
    power_w: Callable[[random.Random], float],
    direction: str,
    *,
    true_capacity_kwh: float = 10.0,
    learner: EfficiencyLearner | None = None,
    days: int = 40,
    seed: int = 1,
) -> EfficiencyLearner:
    """Run a battery with a whole-percent SoC sensor through *days* runs.

    The battery really holds *true_capacity_kwh* and is 97 % efficient in
    each direction.  The learner is told 10 kWh and sees the SoC rounded
    down to a whole percent every 300 s, like the real sensor.
    """
    rng = random.Random(seed)
    learner = learner or EfficiencyLearner()
    sign = 1.0 if direction == CHARGE else -1.0
    at = _T0
    for _ in range(days):
        soc = (10.0 if direction == CHARGE else 90.0) + rng.random()
        previous_w = power_w(rng)
        learner.update(at, sign * previous_w, math.floor(soc), 10.0)
        while 6.0 < soc < 96.0:
            now_w = power_w(rng)
            energy_kwh = (previous_w + now_w) / 2.0 * _CYCLE_S / 3.6e6
            if direction == CHARGE:
                soc += energy_kwh * _TRUE_EFFICIENCY / true_capacity_kwh * 100.0
            else:
                soc -= energy_kwh / _TRUE_EFFICIENCY / true_capacity_kwh * 100.0
            at += timedelta(seconds=_CYCLE_S)
            learner.update(at, sign * now_w, math.floor(soc), 10.0)
            previous_w = now_w
        at += timedelta(hours=6)
    return learner


def _steady(power_w: float) -> Callable[[random.Random], float]:
    """Return a constant battery power."""
    return lambda _rng: power_w


def _jumping(mid_w: float, spread_w: float) -> Callable[[random.Random], float]:
    """Return a power that jumps anywhere in the band every cycle."""
    return lambda rng: mid_w + rng.uniform(-spread_w, spread_w)


def _wandering(mid_w: float, spread_w: float) -> Callable[[random.Random], float]:
    """Return a power that drifts inside the band, like a house load."""
    state = {"power_w": mid_w}

    def power(rng: random.Random) -> float:
        drifted = state["power_w"] + rng.uniform(-spread_w / 2.0, spread_w / 2.0)
        state["power_w"] = min(mid_w + spread_w, max(mid_w - spread_w, drifted))
        return state["power_w"]

    return power


class TestMeasurement:
    """A measurement runs between SoC changes and lands in its power steps."""

    def test_charge_measurement_compares_stored_with_measured_energy(self) -> None:
        """One charge window: 0.5 kWh stored over 0.625 kWh measured is 80 %."""
        learner = _charged(samples=1)

        step = learner.charge[1500]
        assert step.samples == 1
        assert step.measured_kwh == pytest.approx(0.625)
        assert step.stored_kwh == pytest.approx(0.5)
        assert step.efficiency(CHARGE) == pytest.approx(0.8)
        assert learner.charge_total.measured_kwh == pytest.approx(0.625)
        assert learner.charge_total.stored_kwh == pytest.approx(0.5)
        assert learner.discharge == {}
        assert learner.dirty is True

    def test_discharge_measurement_compares_measured_with_stored_energy(self) -> None:
        """One discharge window: 0.4 kWh delivered from 0.5 kWh is 80 %."""
        learner = EfficiencyLearner()

        _discharge(learner, samples=1)

        step = learner.discharge[2000]
        assert step.samples == 1
        assert step.measured_kwh == pytest.approx(0.4)
        assert step.stored_kwh == pytest.approx(0.5)
        assert step.efficiency(DISCHARGE) == pytest.approx(0.8)
        assert learner.charge == {}

    def test_nothing_is_measured_before_the_first_soc_change(self) -> None:
        """A flat SoC never anchors a window."""
        learner = EfficiencyLearner()

        _feed(learner, [(1500.0, 50.0)] * 6)

        assert learner.charge == {}
        assert learner.dirty is False

    def test_a_window_needs_five_percent(self) -> None:
        """Four percent after the anchor is not enough to close."""
        learner = EfficiencyLearner()

        _feed(learner, [(1500.0, 20.0 + i) for i in range(6)])

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

        # 25 kW for 90 s is 0.625 kWh.
        _feed(learner, _ramp(25_000.0, 1), step_s=18.0)

        assert list(learner.charge) == [MAX_STEP_W]

    def test_varying_power_is_shared_between_its_steps(self) -> None:
        """Each step gets the stored energy that matches its measured share."""
        learner = EfficiencyLearner()

        # Anchored at 21 %.  Three cycles at 1200 W, then two at 1800 W, with
        # one cycle averaging 1500 W in between.
        _feed(
            learner,
            [(1200.0, 20.0), (1200.0, 21.0), (1200.0, 22.0), (1200.0, 23.0)]
            + [(1800.0, 24.0), (1800.0, 25.0), (1800.0, 26.0)],
        )

        low, high = learner.charge[1000], learner.charge[1500]
        assert low.measured_kwh == pytest.approx(0.2)
        assert high.measured_kwh == pytest.approx(0.425)
        assert low.stored_kwh == pytest.approx(0.5 * 0.2 / 0.625)
        assert high.stored_kwh == pytest.approx(0.5 * 0.425 / 0.625)
        assert low.efficiency(CHARGE) == pytest.approx(0.8)
        assert high.efficiency(CHARGE) == pytest.approx(0.8)
        assert learner.sample_count(CHARGE) == 1
        assert learner.charge_total.stored_kwh == pytest.approx(0.5)


class TestRejection:
    """Unclean windows never become a sample."""

    @pytest.mark.parametrize(
        "interruption",
        [
            pytest.param((50.0, 25.5), id="idle"),
            pytest.param((-1500.0, 25.0), id="reversed"),
            pytest.param((None, 25.5), id="power-unavailable"),
            pytest.param((1500.0, None), id="soc-unavailable"),
        ],
    )
    def test_interruption_discards_the_window(self, interruption: Reading) -> None:
        """Without the interruption the last reading would close a sample."""
        learner = EfficiencyLearner()

        _feed(
            learner,
            [(1500.0, 20.0 + i) for i in range(6)] + [interruption, (1500.0, 26.0)],
        )

        assert learner.charge == {}
        assert learner.discharge == {}

    @pytest.mark.parametrize("capacity_kwh", [None, 0.0, -5.0])
    def test_unknown_capacity_discards_the_window(
        self, capacity_kwh: float | None
    ) -> None:
        """A reading without a usable capacity resets the measurement."""
        learner = EfficiencyLearner()
        at = _feed(learner, [(1500.0, 20.0 + i) for i in range(6)])

        at = _feed(learner, [(1500.0, 25.5)], capacity_kwh=capacity_kwh, start=at)
        _feed(learner, [(1500.0, 26.0)], start=at)

        assert learner.charge == {}

    def test_a_long_gap_discards_the_window(self) -> None:
        """Two readings further apart than the gap limit start over."""
        learner = EfficiencyLearner()
        at = _feed(learner, [(1500.0, 20.0 + i) for i in range(6)])

        _feed(learner, [(1500.0, 26.0)], start=at + timedelta(seconds=601))

        assert learner.charge == {}

    def test_a_reading_that_is_not_newer_discards_the_window(self) -> None:
        """A repeated timestamp cannot be integrated."""
        learner = EfficiencyLearner()
        at = _feed(learner, [(1500.0, 20.0 + i) for i in range(6)])

        _feed(learner, [(1500.0, 26.0)], start=at - timedelta(seconds=300))

        assert learner.charge == {}

    def test_soc_moving_against_the_power_discards_the_window(self) -> None:
        """A falling SoC while charging is not a clean measurement."""
        learner = EfficiencyLearner()

        at = _feed(
            learner,
            [(1500.0, 20.0), (1500.0, 21.0), (1500.0, 20.5)]
            + [(1500.0, 22.0 + i) for i in range(5)],
        )

        assert learner.charge == {}

        # The window restarted at 20.5 and anchored at 22: 27 closes it.
        _feed(learner, [(1500.0, 27.0)], start=at)
        assert learner.charge[1500].samples == 1

    def test_a_sensor_fault_is_rejected(self) -> None:
        """5 kWh stored from 0.625 kWh measured is not an efficiency."""
        learner = EfficiencyLearner()

        _feed(learner, _ramp(1500.0, 1), capacity_kwh=100.0)

        assert learner.charge == {}
        assert learner.sample_count(CHARGE) == 0
        assert learner.dirty is False

    def test_a_reading_that_looks_too_good_is_kept(self) -> None:
        """A single result above 100 % is SoC resolution, not a fault.

        Cutting it would lean the sums low, because the result that reads
        too low by the same amount is always kept.
        """
        learner = EfficiencyLearner()

        # 0.5 kWh stored from 0.4 kWh measured reads 125 %.
        _feed(learner, _ramp(1500.0, 1), step_s=192.0)

        assert learner.charge_total.efficiency(CHARGE) == pytest.approx(1.25)
        assert learner.sample_count(CHARGE) == 1


class TestStepQualification:
    """A step is reported only with enough samples, energy and a sane value."""

    def test_too_few_samples_is_learning(self) -> None:
        """Nine samples are below the minimum of ten."""
        learner = _charged(samples=9)

        assert learner.charge[1500].status(CHARGE) == STATUS_LEARNING
        assert learner.curve(CHARGE)[0]["apparent_efficiency_pct"] is None
        assert learner.curve(CHARGE)[0]["samples"] == 9

    def test_too_little_energy_is_learning(self) -> None:
        """Twelve samples of 0.1 kWh stay below the 2 kWh minimum."""
        learner = EfficiencyLearner()

        # 2 kWh battery: 0.1 kWh stored, 1500 W for 300 s is 0.125 kWh.
        _feed(learner, _ramp(1500.0, 12), step_s=60.0, capacity_kwh=2.0)

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
        """Twelve samples and 6 kWh at 80 % make the step usable."""
        learner = _charged()

        assert learner.charge[1500].status(CHARGE) == STATUS_OK
        assert learner.curve(CHARGE) == [
            {
                "power_from_w": 1500,
                "power_to_w": 2000,
                "apparent_efficiency_pct": pytest.approx(80.0),
                "samples": 12,
                "energy_kwh": pytest.approx(6.0),
                "status": STATUS_OK,
            }
        ]

    def test_a_step_above_100_percent_is_reported(self) -> None:
        """A battery below its rated capacity reads above 100 % when charging."""
        learner = EfficiencyLearner()
        learner.charge[1500] = EfficiencyStep(12, measured_kwh=5.0, stored_kwh=5.4)

        assert learner.charge[1500].status(CHARGE) == STATUS_OK
        assert learner.curve(CHARGE)[0]["apparent_efficiency_pct"] == pytest.approx(
            108.0
        )

    @pytest.mark.parametrize(
        ("stored_kwh", "expected"),
        [
            pytest.param(12.5, 2.5, id="above-200-percent"),
            pytest.param(2.0, 0.4, id="below-50-percent"),
        ],
    )
    def test_implausible_step_is_discarded(
        self, stored_kwh: float, expected: float
    ) -> None:
        """A step outside 50-200 % is flagged and its value withheld."""
        learner = EfficiencyLearner()
        learner.charge[1500] = EfficiencyStep(
            12, measured_kwh=5.0, stored_kwh=stored_kwh
        )

        step = learner.charge[1500]
        assert step.efficiency(CHARGE) == pytest.approx(expected)
        assert step.status(CHARGE) == STATUS_IMPLAUSIBLE
        assert learner.curve(CHARGE)[0]["apparent_efficiency_pct"] is None
        assert learner.curve(CHARGE)[0]["status"] == STATUS_IMPLAUSIBLE

    def test_a_full_step_fades_its_oldest_samples(self) -> None:
        """Beyond the cap the count stays and the sums are scaled down."""
        step = EfficiencyStep(samples=MAX_SAMPLES, measured_kwh=50.0, stored_kwh=40.0)

        step.add(0.25, 0.2)

        keep = (MAX_SAMPLES - 1) / MAX_SAMPLES
        assert step.samples == MAX_SAMPLES
        assert step.measured_kwh == pytest.approx(50.0 * keep + 0.25)
        assert step.stored_kwh == pytest.approx(40.0 * keep + 0.2)


class TestDirection:
    """A direction as a whole needs its own minimum of samples and energy."""

    def test_too_few_samples_is_learning(self) -> None:
        """Nineteen measurements are below the minimum of twenty."""
        learner = _charged(samples=19)

        assert learner.status(CHARGE) == STATUS_LEARNING
        assert learner.charge_efficiency_pct is None

    def test_enough_samples_report_the_direction(self) -> None:
        """Twenty measurements and 10 kWh at 80 %."""
        learner = _charged(samples=20)

        assert learner.status(CHARGE) == STATUS_OK
        assert learner.charge_efficiency_pct == pytest.approx(80.0)
        assert learner.status(DISCHARGE) == STATUS_LEARNING

    def test_the_direction_counts_every_measurement(self) -> None:
        """Steps that are still learning or implausible are not left out."""
        learner = EfficiencyLearner()
        learner.charge[500] = EfficiencyStep(3, measured_kwh=1.0, stored_kwh=0.9)
        learner.charge[3000] = EfficiencyStep(20, measured_kwh=10.0, stored_kwh=25.0)
        learner.charge_total = EfficiencyStep(23, measured_kwh=40.0, stored_kwh=38.0)

        assert learner.charge_efficiency_pct == pytest.approx(95.0)
        assert [row["status"] for row in learner.curve(CHARGE)] == [
            STATUS_LEARNING,
            STATUS_IMPLAUSIBLE,
        ]

    def test_an_implausible_direction_is_withheld(self) -> None:
        """A direction outside 50-200 % has no value."""
        learner = EfficiencyLearner()
        learner.discharge_total = EfficiencyStep(30, measured_kwh=4.0, stored_kwh=20.0)

        assert learner.status(DISCHARGE) == STATUS_IMPLAUSIBLE
        assert learner.discharge_efficiency_pct is None


class TestRoundTrip:
    """The round trip is the product of both measured directions."""

    def test_round_trip_needs_both_directions(self) -> None:
        """Charge alone gives no round trip."""
        learner = _charged(samples=24)

        assert learner.discharge_efficiency_pct is None
        assert learner.round_trip_efficiency_pct is None
        assert learner.round_trip_status == STATUS_LEARNING

    def test_round_trip_is_charge_times_discharge(self) -> None:
        """80 % in and 80 % out is a 64 % round trip."""
        learner = _charged(samples=24)
        _discharge(learner, samples=24)

        assert learner.discharge_efficiency_pct == pytest.approx(80.0)
        assert learner.round_trip_efficiency_pct == pytest.approx(64.0)
        assert learner.round_trip_status == STATUS_OK

    @pytest.mark.parametrize(
        ("charge_stored_kwh", "expected"),
        [
            pytest.param(44.0, 1.045, id="above-100-percent"),
            pytest.param(20.0, 0.475, id="below-50-percent"),
        ],
    )
    def test_an_implausible_round_trip_is_discarded(
        self, charge_stored_kwh: float, expected: float
    ) -> None:
        """A round trip outside 50-100 % is flagged and has no value."""
        learner = EfficiencyLearner()
        learner.charge_total = EfficiencyStep(
            30, measured_kwh=40.0, stored_kwh=charge_stored_kwh
        )
        learner.discharge_total = EfficiencyStep(30, measured_kwh=38.0, stored_kwh=40.0)

        assert learner._round_trip() == pytest.approx(expected)
        assert learner.round_trip_status == STATUS_IMPLAUSIBLE
        assert learner.round_trip_efficiency_pct is None


class TestWholePercentSoc:
    """A SoC sensor in whole percent must not lean the result (PR #1441 review).

    Each run simulates a battery that is 97 % efficient in each direction
    and reports its SoC rounded down to a whole percent.
    """

    @pytest.mark.parametrize("power_w", [600.0, 1300.0, 1800.0, 2500.0, 3600.0])
    def test_steady_charge_reads_true_at_every_power(self, power_w: float) -> None:
        """The first version read 92.3 % at 1300 W and 93.5 % at 1800 W."""
        learner = _simulate(_steady(power_w), CHARGE)

        assert learner.charge_efficiency_pct == pytest.approx(97.0, abs=0.6)

    @pytest.mark.parametrize("power_w", [600.0, 1300.0, 2500.0])
    def test_steady_discharge_reads_true_at_every_power(self, power_w: float) -> None:
        """Discharge is measured the other way round and must agree."""
        learner = _simulate(_steady(power_w), DISCHARGE)

        assert learner.discharge_efficiency_pct == pytest.approx(97.0, abs=0.6)

    @pytest.mark.parametrize(
        "power_w",
        [
            pytest.param(_wandering(800.0, 250.0), id="wandering"),
            pytest.param(_jumping(800.0, 250.0), id="jumping"),
        ],
    )
    def test_a_load_following_discharge_reads_true(
        self, power_w: Callable[[random.Random], float]
    ) -> None:
        """Varying power is measured, not rejected."""
        learner = _simulate(power_w, DISCHARGE)

        assert learner.discharge_efficiency_pct == pytest.approx(97.0, abs=0.6)

    def test_widely_varying_power_does_not_lean_single_steps(self) -> None:
        """Every usable step stays near the truth when power jumps each cycle.

        Filing a whole measurement under its mean power read 109 % in the
        lowest step and 106 % in the highest in this case.
        """
        learner = _simulate(_jumping(1500.0, 700.0), CHARGE)

        assert learner.charge_efficiency_pct == pytest.approx(97.0, abs=0.6)
        usable = [
            row["apparent_efficiency_pct"]
            for row in learner.curve(CHARGE)
            if row["status"] == STATUS_OK
        ]
        assert len(usable) == 4
        assert usable == pytest.approx([97.0] * 4, abs=2.0)

    def test_a_capacity_error_cancels_in_the_round_trip(self) -> None:
        """A battery at 90 % of its rated capacity, measured against the rating.

        Charge reads high and discharge reads low by the same factor.  The
        round trip is the true 97 % x 97 %.
        """
        learner = _simulate(_wandering(1300.0, 600.0), CHARGE, true_capacity_kwh=9.0)
        _simulate(
            _wandering(700.0, 500.0),
            DISCHARGE,
            true_capacity_kwh=9.0,
            learner=learner,
            seed=2,
        )

        assert learner.charge_efficiency_pct == pytest.approx(97.0 / 0.9, abs=0.8)
        assert learner.discharge_efficiency_pct == pytest.approx(97.0 * 0.9, abs=0.8)
        assert learner.status(CHARGE) == STATUS_OK
        assert learner.round_trip_status == STATUS_OK
        assert learner.round_trip_efficiency_pct == pytest.approx(94.09, abs=0.8)


_VALID = {"samples": 3, "measured_kwh": 1.0, "stored_kwh": 0.9}


class TestPersistence:
    """The learned steps survive a restart."""

    @pytest.mark.asyncio
    async def test_save_and_load_round_trip(self, tmp_path: Path) -> None:
        """A second learner reads back what the first one wrote."""
        path = tmp_path / ".storage" / "efficiency.json"
        learner = _charged(samples=24)
        learner.history_file = str(path)

        assert await learner.save_history() is True
        assert learner.dirty is False

        restored = EfficiencyLearner(history_file=str(path))
        await restored.load_history()

        assert restored.charge[1500].samples == 24
        assert restored.sample_count(CHARGE) == 24
        assert restored.charge_efficiency_pct == pytest.approx(80.0)
        assert restored.discharge == {}
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert stored["version"] == STORAGE_VERSION

    @pytest.mark.parametrize("version", [1, STORAGE_VERSION + 1, None, "2"])
    def test_another_schema_version_is_not_read(self, version: Any) -> None:
        """Version 1 held biased sums; an unknown version cannot be trusted."""
        data = _charged().as_dict()
        data["version"] = version
        if version is None:
            del data["version"]
        learner = _charged()

        learner.restore(data)

        assert learner.charge == {}
        assert learner.sample_count(CHARGE) == 0
        assert learner.dirty is False

    @pytest.mark.parametrize(
        "direction_data",
        [
            pytest.param({"steps": {"1500": _VALID}}, id="no-total"),
            pytest.param({"total": "x", "steps": {"1500": _VALID}}, id="bad-total"),
            pytest.param([], id="not-a-dict"),
        ],
    )
    def test_a_direction_without_valid_totals_starts_empty(
        self, direction_data: Any
    ) -> None:
        """Steps without the sums they belong to are not restored."""
        learner = _charged()

        learner.restore({"version": STORAGE_VERSION, CHARGE: direction_data})

        assert learner.charge == {}
        assert learner.sample_count(CHARGE) == 0

    def test_totals_without_steps_are_restored(self) -> None:
        """A direction whose step table is damaged keeps its totals."""
        learner = EfficiencyLearner()

        learner.restore(
            {"version": STORAGE_VERSION, DISCHARGE: {"total": _VALID, "steps": 7}}
        )

        assert learner.sample_count(DISCHARGE) == 3
        assert learner.discharge == {}

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
        at = _feed(learner, [(1500.0, 20.0 + i) for i in range(6)])

        learner.restore(learner.as_dict())
        _feed(learner, [(1500.0, 26.0)], start=at)

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
        learner = _charged()

        learner.restore(
            {
                "version": STORAGE_VERSION,
                CHARGE: {"total": _VALID, "steps": {"500": entry, "1000": _VALID}},
                DISCHARGE: [],
            }
        )

        assert list(learner.charge) == [1000]
        assert learner.charge[1000].stored_kwh == pytest.approx(0.9)
        assert learner.discharge == {}
        assert learner.dirty is False

    @pytest.mark.parametrize("key", ["abc", "-500", "750", str(MAX_STEP_W + 500)])
    def test_restore_skips_an_invalid_step_key(self, key: str) -> None:
        """The key must be a step boundary inside the table."""
        learner = EfficiencyLearner()

        learner.restore(
            {
                "version": STORAGE_VERSION,
                DISCHARGE: {"total": _VALID, "steps": {key: _VALID}},
            }
        )

        assert learner.discharge == {}
