"""Cycle-neutral terminal-SoC term and its end value ``V`` (issue #1138).

The MILP objective and ``score_plan`` value net stored energy at one end
value ``V``: ``c_obj[ec] −= V`` and ``c_obj[ed] += V``, undiscounted, so the
horizon total is ``−V × (E_end − E_0)``.  A cycle inside the horizon that
leaves the end energy unchanged adds exactly zero, whatever the slot prices.
The per-slot premiums this replaced (#638/#655, #694, #592) did not cancel
across a cycle (issue #1118).

``V`` is ``min(0.9 × (η_dis × peak − c), night / η_chg + c)``, taken from the
last known day of prices as a stand-in for the unknown day after the horizon.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.planner.cost_function import CostWeights, score_plan
from custom_components.hsem.planner.cost_helpers import (
    terminal_end_value,
    terminal_end_value_from_last_day,
    terminal_soc_value,
)
from custom_components.hsem.planner.milp._objective import _build_objective
from custom_components.hsem.planner.milp_optimizer import is_scipy_available, solve_milp
from custom_components.hsem.utils.prices import SlotPrice

_TZ = ZoneInfo("Europe/Copenhagen")
_DAY = datetime(2026, 9, 29, 0, 0, tzinfo=_TZ)
_ETA = 0.98
_CYCLE_COST = 0.093
_PEAK = 3.40
_USE_VALUE = 0.9 * (_ETA * _PEAK - _CYCLE_COST)  # 2.9151


def _slot(
    start: datetime,
    import_price: float,
    *,
    hours: float = 1.0,
    export_price: float = 0.0,
    load_kwh: float = 0.0,
    charged_kwh: float = 0.0,
    discharged_kwh: float = 0.0,
) -> PlannedSlot:
    """Build a no-PV slot with the given price, load and battery flows."""
    slot = PlannedSlot(
        start=start,
        end=start + timedelta(hours=hours),
        price=SlotPrice(import_price=import_price, export_price=export_price),
        batteries_charged_kwh=charged_kwh,
        batteries_discharged_kwh=discharged_kwh,
        estimated_battery_soc_pct=50.0,
    )
    slot.avg_house_consumption_kwh = load_kwh
    slot.solcast_pv_estimate_kwh = 0.0
    slot.ev_planned_load_kwh = 0.0
    slot.estimated_net_consumption_kwh = load_kwh
    return slot


def _day(prices: list[float], day: datetime = _DAY) -> list[PlannedSlot]:
    """Return one hourly slot per price, starting at *day*."""
    return [_slot(day + timedelta(hours=h), p) for h, p in enumerate(prices)]


# ---------------------------------------------------------------------------
# The shared term
# ---------------------------------------------------------------------------


class TestTerminalSocValue:
    """``terminal_soc_value`` is linear in net stored energy."""

    def test_charge_is_a_credit_and_discharge_a_penalty(self) -> None:
        assert terminal_soc_value(2.0, 0.0, 1.5) == pytest.approx(-3.0)
        assert terminal_soc_value(0.0, 2.0, 1.5) == pytest.approx(3.0)

    def test_a_cycle_that_keeps_the_end_energy_adds_zero(self) -> None:
        charge = terminal_soc_value(2.0, 0.0, 1.5)
        discharge = terminal_soc_value(0.0, 2.0, 1.5)
        assert charge + discharge == pytest.approx(0.0)

    @pytest.mark.parametrize("end_value", [None, 0.0, 1e-12])
    def test_disabled_without_an_end_value(self, end_value: float | None) -> None:
        assert terminal_soc_value(2.0, 5.0, end_value) == pytest.approx(0.0)


class TestMilpObjectiveIsCycleNeutral:
    """The objective's terminal coefficients are one undiscounted ``±V``."""

    _PRICES = (0.20, 2.06, 1.644, 3.40, -0.05)
    _V = 1.7706

    def _objective(self, end_value: float | None) -> tuple[np.ndarray, int]:
        slots = _day(list(self._PRICES))
        m = len(slots)
        c_obj = _build_objective(
            slots,
            list(range(m)),
            _DAY - timedelta(seconds=1),
            m,
            9 * m,
            0,  # ec
            m,  # ed
            2 * m,  # gi
            3 * m,  # ge
            4 * m,  # battery_export
            5 * m,  # cycle-cost aux
            6 * m,  # s_max
            7 * m,  # s_min
            8 * m,  # fuse penalty
            [],
            [],
            [],
            np.array(self._PRICES),
            np.array([p - 0.4 for p in self._PRICES]),
            100.0,
            _CYCLE_COST,
            _ETA,
            0.995,  # discounting applies to money, never to the terminal term
            end_value,
            False,
        )
        return c_obj, m

    def test_terminal_coefficients_are_uniform_and_undiscounted(self) -> None:
        with_term, m = self._objective(self._V)
        without, _ = self._objective(None)
        delta = with_term - without

        assert delta[:m] == pytest.approx([-self._V] * m)  # ec: credit
        assert delta[m : 2 * m] == pytest.approx([self._V] * m)  # ed: penalty
        assert delta[2 * m :] == pytest.approx([0.0] * (7 * m))

    @pytest.mark.parametrize(
        ("charges", "discharges"),
        [
            ({0: 2.0}, {3: 2.0}),  # charge cheap → discharge at the peak
            ({2: 1.5}, {1: 1.5}),  # discharge in the evening → recharge at night
            ({0: 1.0, 4: 2.0}, {1: 0.5, 3: 2.5}),  # several legs, same end
        ],
    )
    def test_a_cycle_with_unchanged_end_energy_adds_zero(
        self, charges: dict[int, float], discharges: dict[int, float]
    ) -> None:
        with_term, m = self._objective(self._V)
        without, _ = self._objective(None)
        x = np.zeros(9 * m)
        for t, kwh in charges.items():
            x[t] = kwh
        for t, kwh in discharges.items():
            x[m + t] = kwh

        assert float((with_term - without) @ x) == pytest.approx(0.0)

    def test_net_stored_energy_is_valued_at_v(self) -> None:
        with_term, m = self._objective(self._V)
        without, _ = self._objective(None)
        x = np.zeros(9 * m)
        x[0], x[1], x[m + 3] = 3.0, 1.0, 1.5  # E_end − E_0 = 2.5

        assert float((with_term - without) @ x) == pytest.approx(-2.5 * self._V)


class TestScorePlanIsCycleNeutral:
    """``score_plan`` adds the same zero for a cycle as the MILP."""

    @staticmethod
    def _score(flows: dict[int, tuple[float, float]]) -> float:
        slots = [
            _slot(
                _DAY + timedelta(hours=h),
                price,
                charged_kwh=flows.get(h, (0.0, 0.0))[0],
                discharged_kwh=flows.get(h, (0.0, 0.0))[1],
            )
            for h, price in enumerate((0.20, 2.06, 1.644, 3.40))
        ]
        return score_plan(
            slots,
            CostWeights(cycle_cost_per_kwh=0.0),
            now=_DAY - timedelta(seconds=1),
            initial_battery_kwh=2.0,
            replacement_price_per_kwh=1.7706,
        ).terminal_soc_value

    def test_charge_then_discharge_adds_zero(self) -> None:
        assert self._score({0: (2.0, 0.0), 3: (0.0, 2.0)}) == pytest.approx(0.0)

    def test_discharge_then_recharge_adds_zero(self) -> None:
        assert self._score({1: (0.0, 1.5), 2: (1.5, 0.0)}) == pytest.approx(0.0)

    def test_net_gain_is_credited_at_v(self) -> None:
        assert self._score({0: (3.0, 0.0), 3: (0.0, 1.0)}) == pytest.approx(
            -2.0 * 1.7706
        )


# ---------------------------------------------------------------------------
# End value V
# ---------------------------------------------------------------------------


class TestTerminalEndValue:
    """``V = max(0, min(use value, overnight recharge cost))``."""

    def _value(self, night: float | None, peak: float = _PEAK) -> float:
        return terminal_end_value(
            peak_import=peak,
            night_import=night,
            charge_eff=_ETA,
            discharge_eff=_ETA,
            cycle_cost_per_kwh=_CYCLE_COST,
        )

    def test_cheap_night_takes_the_recharge_side(self) -> None:
        """A kWh that is cheap to replace overnight is worth its replacement."""
        assert self._value(1.644) == pytest.approx(1.644 / _ETA + _CYCLE_COST)
        assert self._value(1.644) < _USE_VALUE

    def test_expensive_night_takes_the_use_side(self) -> None:
        """Replacing it overnight costs more than it saves: V is its use value."""
        assert 3.0 / _ETA + _CYCLE_COST > _USE_VALUE
        assert self._value(3.0) == pytest.approx(_USE_VALUE)

    def test_unknown_night_takes_the_use_side(self) -> None:
        assert self._value(None) == pytest.approx(_USE_VALUE)

    def test_never_negative(self) -> None:
        """A negative night price or a peak below the cycle cost gives V = 0."""
        assert self._value(-0.5) == pytest.approx(0.0)
        assert self._value(1.0, peak=0.05) == pytest.approx(0.0)


class TestTerminalEndValueFromLastDay:
    """``V`` comes from the last calendar day of prices in the slot list."""

    @staticmethod
    def _estimate(slots: list[PlannedSlot], top_n: int = 4) -> float | None:
        return terminal_end_value_from_last_day(
            slots,
            _TZ,
            top_n=top_n,
            charge_eff=_ETA,
            discharge_eff=_ETA,
            cycle_cost_per_kwh=_CYCLE_COST,
        )

    def test_uses_only_the_last_day(self) -> None:
        """Today's expensive night is ignored; tomorrow's cheap one sets V."""
        today = _day([3.0] * 6 + [2.0] * 18)
        tomorrow = _day(
            [1.0] * 6 + [2.0] * 11 + [_PEAK] * 4 + [2.0] * 3, _DAY + timedelta(days=1)
        )

        assert self._estimate(today + tomorrow) == pytest.approx(
            1.0 / _ETA + _CYCLE_COST
        )

    def test_peak_is_the_mean_of_the_top_n_slots(self) -> None:
        """An expensive night leaves the use side, set by the top-N mean."""
        prices = [3.5] * 6 + [2.0] * 11 + [4.0, 3.0, 2.8, 2.6] + [2.0] * 3
        expected = 0.9 * (_ETA * (4.0 + 3.5 + 3.5) / 3 - _CYCLE_COST)

        assert self._estimate(_day(prices), top_n=3) == pytest.approx(expected)

    def test_night_window_ends_at_six(self) -> None:
        """Only 00:00-06:00 counts as night; the 06:00 slot does not."""
        prices = [1.0] * 6 + [0.1] + [2.0] * 10 + [_PEAK] * 4 + [2.0] * 3

        assert self._estimate(_day(prices)) == pytest.approx(1.0 / _ETA + _CYCLE_COST)

    def test_partial_last_day_without_a_night_uses_the_use_side(self) -> None:
        """A last day that starts at noon has no night: only the use side."""
        slots = _day([2.0] * 5 + [_PEAK] * 4 + [2.0] * 3, _DAY + timedelta(hours=12))

        assert self._estimate(slots) == pytest.approx(_USE_VALUE)

    def test_non_finite_prices_are_skipped(self) -> None:
        slots = _day([1.0] * 6 + [2.0] * 11 + [_PEAK] * 4 + [2.0] * 3)
        slots.append(_slot(_DAY + timedelta(days=1), math.nan))

        assert self._estimate(slots) == pytest.approx(1.0 / _ETA + _CYCLE_COST)

    def test_none_without_prices(self) -> None:
        assert self._estimate([]) is None
        assert self._estimate([_slot(_DAY, math.nan)]) is None


# ---------------------------------------------------------------------------
# Both sides of the min drive the LP (engine path: estimate, then solve)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not is_scipy_available(), reason="scipy not available")
@pytest.mark.parametrize(
    ("night", "expected_discharge_kwh"),
    [
        # V = 1.0/0.98 + 0.093 ≈ 1.11 < 0.98·2.50 − 0.093 ≈ 2.36: use it now.
        (1.0, 3.0 / _ETA),
        # V = use side ≈ 2.92 > 2.36: keep it for the post-horizon peak.
        (3.0, 0.0),
    ],
)
def test_night_price_decides_whether_a_mid_priced_evening_is_served(
    night: float, expected_discharge_kwh: float
) -> None:
    """The same 2.50 evening is served from a full battery only before a cheap night.

    Today's prices stand in for tomorrow's.  After a cheap night the energy
    left at midnight is worth its cheap replacement, so covering the evening
    load is better.  After an expensive night it is worth its use at
    tomorrow's 3.40 peak, which a 2.50 evening does not beat.  A
    no-terminal-term plan would empty the battery in both cases.
    """
    prices = [night] * 6 + [2.50] * 11 + [_PEAK] * 4 + [2.50] * 3
    slots = [
        _slot(_DAY + timedelta(hours=h), p, export_price=0.5, load_kwh=1.0)
        for h, p in enumerate(prices)
    ]
    end_value = terminal_end_value_from_last_day(
        slots,
        _TZ,
        top_n=4,
        charge_eff=_ETA,
        discharge_eff=_ETA,
        cycle_cost_per_kwh=_CYCLE_COST,
    )
    now = _DAY + timedelta(hours=21) - timedelta(seconds=1)

    result = solve_milp(
        slots,
        now,
        current_kwh=5.0,
        usable_kwh=5.0,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=5.0,
        cycle_cost_per_kwh=_CYCLE_COST,
        charge_efficiency_pct=_ETA * 100,
        discharge_efficiency_pct=_ETA * 100,
        replacement_price_per_kwh=end_value,
    )
    assert result is not None
    out, _diag = result

    discharged = sum(s.batteries_discharged_kwh for s in out if s.start > now)
    # Slot energies are written out rounded to 3 dp.
    assert discharged == pytest.approx(expected_discharge_kwh, abs=5e-3)
