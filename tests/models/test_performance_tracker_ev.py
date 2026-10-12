"""Tests for the performance tracker's per-charger record and EV figures (issue #1271)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import homeassistant.util.dt as dt_util

from custom_components.hsem.models.performance_charger import ChargerSample
from custom_components.hsem.models.performance_record import (
    REASON_EV_UNSOLVED,
    REASON_INSUFFICIENT_COVERAGE,
    REASON_NO_EV_SESSION,
    REASON_POTENTIAL_BELOW_THRESHOLD,
    PerformanceSample,
    PerformanceScore,
)
from custom_components.hsem.models.performance_tracker import (
    REASON_NOT_EVALUATED,
    PerformanceTracker,
)

_TZ = ZoneInfo("Europe/Copenhagen")
_T0 = datetime(2026, 6, 10, 12, 0, tzinfo=_TZ)
_DEADLINE = datetime(2026, 6, 11, 7, 0, tzinfo=_TZ)


@pytest.fixture(autouse=True)
def _copenhagen() -> Iterator[None]:
    """Run every test with Home Assistant's zone set to Europe/Copenhagen."""
    previous = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(_TZ)
    try:
        yield
    finally:
        dt_util.set_default_time_zone(previous)


def _charger(power_w: float | None = 6000.0, **values: object) -> ChargerSample:
    defaults: dict[str, object] = {
        "connected": True,
        "managed": True,
        "deadline": _DEADLINE,
        "max_kw": 7.4,
    }
    return ChargerSample(power_w=power_w, **{**defaults, **values})  # type: ignore[arg-type]


def _sample(minutes: float, *chargers: ChargerSample) -> PerformanceSample:
    return PerformanceSample(
        at=_T0 + timedelta(minutes=minutes),
        grid_import_energy_kwh=100.0 + minutes / 10.0,
        grid_export_energy_kwh=20.0,
        soc_pct=50.0,
        import_price=1.0,
        export_price=0.7,
        chargers=chargers,
    )


def _feed(tracker: PerformanceTracker, *samples: PerformanceSample) -> None:
    for sample in samples:
        tracker.accumulate(sample, slot_minutes=15, max_gap_seconds=600.0)


def _score(
    day: str,
    *,
    oracle: float = 10.0,
    joint: float | None = None,
    plug: float | None = None,
    shiftable: float | None = None,
    reason: str | None = None,
) -> PerformanceScore:
    start = datetime.fromisoformat(day).replace(tzinfo=UTC)
    return PerformanceScore(
        start=start,
        end=start + timedelta(days=1),
        slot_count=96,
        reason=reason,
        coverage=1.0,
        control_coverage=1.0,
        realized_cost=oracle + 1.0,
        oracle_cost=oracle,
        baseline_cost=oracle + 5.0,
        potential=5.0,
        pv_as_realized_cost=oracle + 0.5,
        ev_shiftable_kwh=shiftable,
        ev_joint_oracle_cost=joint,
        ev_plug_and_charge_cost=plug,
        start_kwh=4.0,
        realized_end_kwh=4.0,
        baseline_end_kwh=4.0,
        day=day,
    )


class TestChargerRecord:
    def test_each_charger_is_recorded_on_its_own(self) -> None:
        tracker = PerformanceTracker()

        _feed(
            tracker,
            _sample(0, _charger(6000.0), _charger(3000.0, connected=False)),
            _sample(5, _charger(6000.0), _charger(3000.0, connected=False)),
        )

        primary, second = tracker.slots[_T0.astimezone(UTC)].chargers
        assert primary.kwh == pytest.approx(0.5)
        assert primary.connected is True
        assert primary.managed is True
        assert primary.deadline == _DEADLINE
        assert primary.max_kw == pytest.approx(7.4)
        assert second.kwh == pytest.approx(0.25)
        assert second.connected is False

    def test_energy_across_a_boundary_is_split_by_time(self) -> None:
        tracker = PerformanceTracker()

        _feed(tracker, _sample(11, _charger(6000.0)), _sample(21, _charger(6000.0)))

        first = tracker.slots[_T0.astimezone(UTC)].chargers[0]
        second = tracker.slots[(_T0 + timedelta(minutes=15)).astimezone(UTC)].chargers[
            0
        ]
        assert first.kwh == pytest.approx(0.4)
        assert second.kwh == pytest.approx(0.6)

    def test_a_car_that_leaves_mid_slot_was_not_connected_throughout(self) -> None:
        tracker = PerformanceTracker()

        _feed(
            tracker,
            _sample(0, _charger()),
            _sample(5, _charger()),
            _sample(10, _charger(0.0, connected=False)),
        )

        assert tracker.slots[_T0.astimezone(UTC)].chargers[0].connected is False

    def test_without_chargers_the_slot_has_none(self) -> None:
        tracker = PerformanceTracker()

        _feed(tracker, _sample(0), _sample(5))

        assert tracker.slots[_T0.astimezone(UTC)].chargers == []

    def test_a_request_takes_its_own_copy_of_the_chargers(self) -> None:
        """The executor reads them while the tracker keeps adding to them."""
        tracker = PerformanceTracker()
        _feed(tracker, _sample(0, _charger()), _sample(5, _charger()))
        request = tracker.request(_T0 + timedelta(hours=1), slot_minutes=15, zone=_TZ)

        _feed(tracker, _sample(10, _charger()))

        copied = request.slots[_T0.astimezone(UTC)].chargers[0]
        assert copied.kwh == pytest.approx(0.5)
        assert tracker.slots[_T0.astimezone(UTC)].chargers[0].kwh == pytest.approx(1.0)

    def test_the_chargers_survive_a_round_trip(self) -> None:
        tracker = PerformanceTracker()
        _feed(tracker, _sample(0, _charger()), _sample(5, _charger()))

        restored = PerformanceTracker.from_dict(
            json.loads(json.dumps(tracker.as_dict()))
        )

        assert restored.slots == tracker.slots
        assert restored._last_sample == tracker._last_sample
        assert restored.slots[_T0.astimezone(UTC)].chargers[0].deadline == _DEADLINE

    def test_a_history_file_from_before_the_chargers_is_still_read(self) -> None:
        tracker = PerformanceTracker()
        _feed(tracker, _sample(0, _charger()), _sample(5, _charger()))
        data = tracker.as_dict()
        del data["slots"][0]["chargers"]
        del data["last_sample"]["chargers"]

        restored = PerformanceTracker.from_dict(data)

        (slot,) = restored.slots.values()
        assert slot.chargers == []
        assert slot.grid_import_kwh == pytest.approx(0.5)
        assert restored._last_sample is not None
        assert restored._last_sample.chargers == ()


class TestEvFigures:
    def test_a_window_with_an_ev_score(self) -> None:
        """Plug-and-charge 14, as charged 10, best 8: 2 of 6 was missed."""
        tracker = PerformanceTracker()
        tracker.window = _score("2026-06-09", joint=8.0, plug=14.0, shiftable=20.0)

        attributes = tracker.as_sensor_attributes(_T0)

        assert attributes["ev_missed_opportunity"] == pytest.approx(2.0)
        assert attributes["ev_potential"] == pytest.approx(6.0)
        assert attributes["ev_charging_efficiency"] == pytest.approx(66.7)
        assert attributes["ev_shiftable_kwh"] == pytest.approx(20.0)
        assert attributes["ev_reason"] is None
        # The EV figure is on top of the missed opportunity, not inside it.
        assert attributes["missed_opportunity"] == pytest.approx(1.0)

    @pytest.mark.parametrize(
        ("score", "reason"),
        [
            (_score("2026-06-09"), REASON_NO_EV_SESSION),
            (_score("2026-06-09", shiftable=0.0), REASON_NO_EV_SESSION),
            (_score("2026-06-09", shiftable=12.0), REASON_EV_UNSOLVED),
            (_score("2026-06-09", shiftable=12.0, joint=8.0), REASON_EV_UNSOLVED),
            (
                _score("2026-06-09", shiftable=1.0, joint=10.0, plug=10.01),
                REASON_POTENTIAL_BELOW_THRESHOLD,
            ),
            (
                _score(
                    "2026-06-09",
                    shiftable=12.0,
                    joint=8.0,
                    plug=14.0,
                    reason=REASON_INSUFFICIENT_COVERAGE,
                ),
                REASON_INSUFFICIENT_COVERAGE,
            ),
        ],
    )
    def test_why_the_ev_efficiency_is_unknown(
        self, score: PerformanceScore, reason: str
    ) -> None:
        tracker = PerformanceTracker(window=score)

        attributes = tracker.as_sensor_attributes(_T0)

        assert attributes["ev_charging_efficiency"] is None
        assert attributes["ev_reason"] == reason

    def test_before_the_first_evaluation(self) -> None:
        attributes = PerformanceTracker().as_sensor_attributes(_T0)

        assert attributes["ev_reason"] == REASON_NOT_EVALUATED

    def test_an_efficiency_below_zero_is_not_clamped(self) -> None:
        """Charged at a cost of 16 where plugging in would have cost 14."""
        score = _score("2026-06-09", oracle=16.0, joint=8.0, plug=14.0, shiftable=20.0)

        assert score.ev_efficiency == pytest.approx(-1 / 3)

    def test_periods_are_ratios_of_sums_over_the_days_with_an_ev_score(self) -> None:
        tracker = PerformanceTracker()
        # 2 of 6 missed, then 0 of 2 missed; a day without a session between.
        tracker.days["2026-06-07"] = _score(
            "2026-06-07", joint=8.0, plug=14.0, shiftable=20.0
        )
        tracker.days["2026-06-08"] = _score("2026-06-08", shiftable=0.0)
        tracker.days["2026-06-09"] = _score(
            "2026-06-09", joint=10.0, plug=12.0, shiftable=6.0
        )

        period = tracker.period(date(2026, 6, 10), 7)

        assert period["ev_missed_opportunity"] == pytest.approx(2.0)
        assert period["ev_potential"] == pytest.approx(8.0)
        # 1 − 2 ÷ 8, not the mean of 66.7 % and 100 %.
        assert period["ev_charging_efficiency"] == pytest.approx(75.0)
        assert period["ev_shiftable_kwh"] == pytest.approx(26.0)
        assert period["ev_days"] == 2
        assert period["scored_days"] == 3

    def test_a_period_without_an_ev_day_has_no_ev_figures(self) -> None:
        tracker = PerformanceTracker()
        tracker.days["2026-06-09"] = _score("2026-06-09")

        period = tracker.period(date(2026, 6, 10), 7)

        assert period["ev_missed_opportunity"] is None
        assert period["ev_potential"] is None
        assert period["ev_charging_efficiency"] is None
        assert period["ev_shiftable_kwh"] is None
        assert period["ev_days"] == 0

    def test_the_ev_score_survives_a_round_trip(self) -> None:
        tracker = PerformanceTracker()
        tracker.days["2026-06-09"] = _score(
            "2026-06-09", joint=8.0, plug=14.0, shiftable=20.0
        )

        restored = PerformanceTracker.from_dict(
            json.loads(json.dumps(tracker.as_dict()))
        )

        assert restored.days == tracker.days
        assert restored.days["2026-06-09"].ev_missed == pytest.approx(2.0)

    def test_a_score_from_before_the_ev_figures_is_still_read(self) -> None:
        tracker = PerformanceTracker()
        tracker.days["2026-06-09"] = _score(
            "2026-06-09", joint=8.0, plug=14.0, shiftable=20.0
        )
        data = tracker.as_dict()
        for key in (
            "ev_shiftable_kwh",
            "ev_joint_oracle_cost",
            "ev_plug_and_charge_cost",
        ):
            del data["days"][0][key]

        day = PerformanceTracker.from_dict(data).days["2026-06-09"]

        assert day.regret == pytest.approx(1.0)
        assert day.ev_missed is None
        assert day.ev_reason == REASON_NO_EV_SESSION
