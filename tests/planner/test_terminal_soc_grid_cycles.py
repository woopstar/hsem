"""Terminal-SoC economics of no-PV grid-charge cycles (issues #1118, #1138).

Issue #1118 showed that the old per-slot terminal premiums were not
cycle-neutral.  A discharge penalty ``max(0, R − p_imp[t])`` (#638) and a
charge credit ``max(0, R − p_imp[t] − p_exp[t] / η_chg)`` (#694) do not
cancel across an in-horizon cycle.  The LP therefore declined a profitable
evening-discharge / night-recharge cycle, and without the #694 cap it
grid-charged at mid prices to export at the peak at a real loss.

Since #1138 the term values net stored energy at one end value ``V``, so a
cycle that leaves the end energy unchanged adds zero and the LP decides it
on cash and cycle cost alone.  ``V`` is the recharge side of
``cost_helpers.terminal_end_value`` for this fixture's cheap night.

Real value of a cycle per kWh DC: ``η_dis·p_out − p_in / η_chg − 2·cycle_cost``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.planner.cost_helpers import terminal_end_value
from custom_components.hsem.planner.milp_optimizer import is_scipy_available, solve_milp
from custom_components.hsem.utils.prices import SlotPrice

pytestmark = pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available in this environment"
)

_TZ = ZoneInfo("Europe/Copenhagen")
_NOW = datetime(2026, 9, 27, 17, 0, tzinfo=_TZ)

_ETA_PCT = 98.0
_ETA = _ETA_PCT / 100.0
_CYCLE_COST = 0.093
_NIGHT_IMPORT = 1.644
_PEAK_IMPORT = 3.40
# min(0.9·(0.98·3.40 − 0.093), 1.644/0.98 + 0.093) = 1.7706: the recharge side.
_END_VALUE = terminal_end_value(
    peak_import=_PEAK_IMPORT,
    night_import=_NIGHT_IMPORT,
    charge_eff=_ETA,
    discharge_eff=_ETA,
    cycle_cost_per_kwh=_CYCLE_COST,
)


def _export_from_import(import_price: float) -> float:
    """Export price from the issue #1118 calibration ``imp = 1.25·(exp + 0.399)``."""
    return round(import_price / 1.25 - 0.399, 4)


def _slot(hour_offset: int, import_price: float, load_kwh: float) -> PlannedSlot:
    """Build a one-hour, no-PV slot starting *hour_offset* hours after ``_NOW``."""
    start = _NOW + timedelta(hours=hour_offset)
    slot = PlannedSlot(
        start=start,
        end=start + timedelta(hours=1),
        price=SlotPrice(
            import_price=import_price,
            export_price=_export_from_import(import_price),
        ),
    )
    slot.avg_house_consumption_kwh = load_kwh
    slot.solcast_pv_estimate_kwh = 0.0
    slot.ev_planned_load_kwh = 0.0
    slot.estimated_net_consumption_kwh = load_kwh
    return slot


def _solve(
    slots: list[PlannedSlot], *, current_kwh: float, usable_kwh: float
) -> list[PlannedSlot]:
    """Solve the MILP with the issue #1118 battery and the #1138 end value."""
    result = solve_milp(
        slots,
        _NOW,
        current_kwh=current_kwh,
        usable_kwh=usable_kwh,
        max_charge_per_slot=5.0,
        max_discharge_per_slot=5.0,
        cycle_cost_per_kwh=_CYCLE_COST,
        charge_efficiency_pct=_ETA_PCT,
        discharge_efficiency_pct=_ETA_PCT,
        replacement_price_per_kwh=_END_VALUE,
    )
    assert result is not None, "MILP must return a solution"
    out_slots, _diag = result
    return out_slots


def _evening_night_peak(evening_import: float) -> list[PlannedSlot]:
    """Evening load, empty night, then a peak that needs the full battery.

    The 5 kWh battery starts full and the peak's 4.9 kWh load consumes all
    of it (5 kWh DC × η_dis), so covering the evening load from the battery
    requires recharging the same energy from the grid at night.
    """
    return [
        _slot(0, evening_import, load_kwh=1.0),
        _slot(1, _NIGHT_IMPORT, load_kwh=0.0),
        _slot(2, _PEAK_IMPORT, load_kwh=5.0 * _ETA),
    ]


def test_milp_declines_grid_charge_to_peak_export_at_a_loss() -> None:
    """Grid energy bought at 2.40 must not be exported at the 3.40 peak.

    Charging at 2.40 to cover the peak's house load is worth
    ``0.98·3.40 − 2.40/0.98 − 0.186 ≈ +0.70`` per kWh DC, but exporting it
    at the peak's 2.321 export price is worth
    ``0.98·2.321 − 2.40/0.98 − 0.186 ≈ −0.36``.  Ending full is not worth
    it either: a kWh stored at 2.40 costs ``2.40/0.98 + 0.093 ≈ 2.54``, above
    ``V ≈ 1.77``.  An end value derived from the in-horizon peak (``V ≈ R``)
    would buy 5 kWh at 2.40 just to end full (#1138 prototype).
    """
    peak_load = 1.0
    out = _solve(
        [_slot(0, 2.40, load_kwh=0.0), _slot(1, _PEAK_IMPORT, load_kwh=peak_load)],
        current_kwh=0.0,
        usable_kwh=10.0,
    )

    assert out[0].batteries_charged_kwh == pytest.approx(peak_load / _ETA, abs=1e-3)
    assert out[1].batteries_discharged_kwh == pytest.approx(peak_load / _ETA, abs=1e-3)
    assert out[1].grid_export_kwh == pytest.approx(0.0, abs=1e-3)


def test_milp_declines_unprofitable_no_pv_evening_cycle() -> None:
    """An evening/night cycle with a negative real spread must be declined.

    Discharging at 1.85 and recharging at 1.644 is worth
    ``0.98·1.85 − 1.644/0.98 − 0.186 ≈ −0.05`` per kWh DC.  Without the
    #694 cap the old per-slot premiums added ``p_imp[evening] −
    p_imp[night] ≈ +0.21`` and the LP took the loss (#1118).
    """
    out = _solve(_evening_night_peak(1.85), current_kwh=5.0, usable_kwh=5.0)

    assert out[0].batteries_discharged_kwh == pytest.approx(0.0, abs=1e-3)
    assert out[1].batteries_charged_kwh == pytest.approx(0.0, abs=1e-3)
    assert out[2].batteries_discharged_kwh == pytest.approx(5.0, abs=1e-3)


def test_milp_takes_profitable_no_pv_evening_cycle() -> None:
    """An evening/night cycle with a positive real spread is taken (#1138).

    Discharging at 2.06 and recharging at 1.644 is worth
    ``0.98·2.06 − 1.644/0.98 − 0.186 ≈ +0.155`` per kWh DC, and the
    terminal SoC is unchanged, so the terminal term nets to zero.  The old
    per-slot premiums netted to
    ``(R − 2.06) − (R − 1.644 − 0.916/0.98) ≈ +0.52``, a penalty (#1118).
    """
    evening_load = 1.0
    out = _solve(_evening_night_peak(2.06), current_kwh=5.0, usable_kwh=5.0)

    assert out[0].batteries_discharged_kwh == pytest.approx(
        evening_load / _ETA, abs=1e-3
    )
    assert out[1].batteries_charged_kwh == pytest.approx(evening_load / _ETA, abs=1e-3)
