"""Tests for the EV timing score of recorded slots (issue #1271).

``score_slots`` finds the charging sessions in the per-charger records and
adds the two EV hindsight runs to a scored window.  These tests pin what the
figures say for a day charged at good and bad times, when they are unknown,
and that they sit on top of the stage-1 and stage-2 figures without changing
them.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import homeassistant.util.dt as dt_util

from custom_components.hsem.models.performance_charger import ChargerRecord
from custom_components.hsem.models.performance_record import (
    REASON_EV_UNSOLVED,
    REASON_NO_EV_SESSION,
    REASON_POTENTIAL_BELOW_THRESHOLD,
    PerformanceSlot,
)
from custom_components.hsem.planner import hindsight_ev
from custom_components.hsem.planner.hindsight_scoring import SiteLimits
from custom_components.hsem.planner.milp_optimizer import is_scipy_available
from custom_components.hsem.utils.performance_scoring import score_slots

pytestmark = pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available in this environment"
)

_TZ = ZoneInfo("Europe/Copenhagen")
_START = datetime(2026, 6, 9, 10, 0, tzinfo=UTC)
_KEYS = [_START + timedelta(hours=index) for index in range(24)]
_SITE = SiteLimits(
    rated_kwh=10.0,
    min_soc_pct=5.0,
    max_soc_pct=100.0,
    max_charge_kw=5.0,
    max_discharge_kw=5.0,
    charge_efficiency=0.97,
    discharge_efficiency=0.97,
    battery_export_enabled=False,
)
#: Six ordinary hours, a dear evening, two ordinary hours, a cheap night.
_PRICES = [1.2] * 6 + [2.5] * 4 + [1.2] * 2 + [0.5] * 6 + [1.2] * 6
_PLUGGED = range(6, 20)
_EVENING = {6: 7.0, 7: 7.0, 8: 6.0}
_NIGHT = {12: 7.0, 13: 7.0, 14: 6.0}


@pytest.fixture(autouse=True)
def _copenhagen() -> Iterator[None]:
    """Run every test with Home Assistant's zone set to Europe/Copenhagen."""
    previous = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(_TZ)
    try:
        yield
    finally:
        dt_util.set_default_time_zone(previous)


def _day(
    *ev_per_charger: dict[int, float], managed: bool = True, records: bool = True
) -> dict[datetime, PerformanceSlot]:
    """Return 24 hourly slots with an idle battery and the EVs charged as given."""
    slots: dict[datetime, PerformanceSlot] = {}
    for index, key in enumerate(_KEYS):
        ev = sum(ev_at.get(index, 0.0) for ev_at in ev_per_charger)
        slots[key] = PerformanceSlot(
            start=key,
            minutes=60,
            grid_import_kwh=0.6 + ev,
            grid_export_kwh=0.0,
            soc_start_pct=40.0,
            soc_end_pct=40.0,
            import_price=_PRICES[index],
            export_price=_PRICES[index] - 0.3,
            coverage_seconds=3600.0,
            control_seconds=3600.0,
            pv_kwh=0.0,
            ev_kwh=ev,
            chargers=[
                ChargerRecord(
                    kwh=ev_at.get(index, 0.0),
                    connected=index in _PLUGGED,
                    managed=managed,
                    max_kw=7.0,
                )
                for ev_at in ev_per_charger
            ]
            if records
            else [],
        )
    return slots


class TestWhatTheSensorSays:
    def test_charged_through_the_dear_evening(self) -> None:
        """Exactly what plugging in and charging would have done: 0 %."""
        score = score_slots(_KEYS, _day(_EVENING), 60, _SITE)

        assert score.ev_reason is None
        assert score.ev_shiftable_kwh == pytest.approx(20.0)
        assert score.ev_efficiency == pytest.approx(0.0, abs=1e-6)
        assert score.ev_missed == pytest.approx(score.ev_potential)
        # 20 kWh at 2.5 where 0.5 was available a few hours later.
        assert score.ev_missed is not None and score.ev_missed > 30.0

    def test_charged_in_the_cheap_night(self) -> None:
        score = score_slots(_KEYS, _day(_NIGHT), 60, _SITE)

        assert score.ev_efficiency == pytest.approx(1.0, abs=1e-6)
        assert score.ev_missed == pytest.approx(0.0, abs=1e-6)
        assert score.ev_potential is not None and score.ev_potential > 30.0

    def test_the_ev_figure_is_on_top_of_the_others(self) -> None:
        """The missed opportunity and its split treat the EV as fixed load."""
        with_records = score_slots(_KEYS, _day(_EVENING), 60, _SITE)
        without = score_slots(_KEYS, _day(_EVENING, records=False), 60, _SITE)

        assert with_records.regret == pytest.approx(without.regret)
        assert with_records.battery_loss == pytest.approx(without.battery_loss)
        assert with_records.solar_loss == pytest.approx(without.solar_loss)
        assert with_records.capture == pytest.approx(without.capture)

    def test_all_the_parts_add_up(self) -> None:
        """Realized − joint oracle = battery loss + solar loss + EV missed."""
        score = score_slots(_KEYS, _day(_EVENING), 60, _SITE)

        assert score.realized_cost is not None
        assert score.ev_joint_oracle_cost is not None
        assert score.battery_loss is not None and score.solar_loss is not None
        assert score.ev_missed is not None
        assert score.realized_cost - score.ev_joint_oracle_cost == pytest.approx(
            score.battery_loss + score.solar_loss + score.ev_missed
        )

    def test_two_chargers_are_scored_together(self) -> None:
        score = score_slots(_KEYS, _day({6: 7.0, 7: 3.0}, {8: 5.0, 9: 5.0}), 60, _SITE)

        assert score.ev_shiftable_kwh == pytest.approx(20.0)
        assert score.ev_efficiency == pytest.approx(0.0, abs=0.05)


class TestWhenItIsUnknown:
    def test_slots_recorded_without_chargers(self) -> None:
        """No EV configured, or slots from before the chargers were recorded."""
        score = score_slots(_KEYS, _day(_EVENING, records=False), 60, _SITE)

        assert score.is_scored
        assert score.ev_shiftable_kwh is None
        assert score.ev_efficiency is None
        assert score.ev_reason == REASON_NO_EV_SESSION

    def test_a_day_on_which_the_car_did_not_charge(self) -> None:
        score = score_slots(_KEYS, _day({}), 60, _SITE)

        assert score.ev_shiftable_kwh == pytest.approx(0.0)
        assert score.ev_joint_oracle_cost is None
        assert score.ev_reason == REASON_NO_EV_SESSION

    def test_charging_the_user_decided_is_not_scored(self) -> None:
        """Smart charging off or "force charge now": not HSEM's timing."""
        score = score_slots(_KEYS, _day(_EVENING, managed=False), 60, _SITE)

        assert score.ev_shiftable_kwh == pytest.approx(0.0)
        assert score.ev_reason == REASON_NO_EV_SESSION

    def test_the_ev_solves_fail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Stages 1 and 2 are published; only the EV figure is unknown."""
        monkeypatch.setattr(hindsight_ev, "_solve", lambda *_args, **_kwargs: None)

        score = score_slots(_KEYS, _day(_EVENING), 60, _SITE)

        assert score.is_scored
        assert score.regret is not None and score.battery_loss is not None
        assert score.ev_shiftable_kwh == pytest.approx(20.0)
        assert score.ev_efficiency is None
        assert score.ev_reason == REASON_EV_UNSOLVED

    def test_flat_prices_leave_nothing_to_time(self) -> None:
        slots = _day(_EVENING)
        for slot in slots.values():
            slot.import_price = 1.0
            slot.export_price = 0.7

        score = score_slots(_KEYS, slots, 60, _SITE)

        assert score.ev_potential == pytest.approx(0.0, abs=1e-6)
        assert score.ev_efficiency is None
        assert score.ev_reason == REASON_POTENTIAL_BELOW_THRESHOLD

    def test_an_unscored_window_has_no_ev_figure_either(self) -> None:
        slots = _day(_EVENING)
        del slots[_KEYS[3]]

        score = score_slots(_KEYS, slots, 60, _SITE)

        assert score.ev_efficiency is None
        assert score.ev_reason == score.reason
