"""Terminal-SoC economics of no-PV grid-charge cycles (issue #1118).

The MILP's terminal-SoC term is per-slot (docs/planner-spec.md §5): a
discharge penalty ``max(0, R − p_imp[t])`` (#638) and a charge credit
``max(0, R − p_imp[t] − p_exp[t] / η_chg)`` (#694).  Issue #1118 asked
whether the #694 export cap wrongly blocks grid-charge arbitrage on slots
without PV surplus, and whether dropping it there would help.

A replay on a 2026-09-27-like price shape showed that the cap does block a
profitable evening-discharge / night-recharge cycle, but that dropping it
is net harmful: the #638 charge credit ``R − p_imp`` is not cancelled when
the energy is discharged at a slot priced at or above ``R`` (penalty 0), so
without the #694 cap the LP grid-charges at mid prices and exports at the
peak at a real loss, and also takes evening cycles whose real spread is
negative.

These tests pin the economically correct declines — they hold on the
current objective and on any future cycle-neutral one — and document the
remaining gap with a strict ``xfail`` that flips once the terminal term is
made cycle-neutral.

Real value of a cycle per kWh DC: ``η_dis·p_out − p_in / η_chg − 2·cycle_cost``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.models.planned_slot import PlannedSlot
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
_REPLACEMENT_PRICE = 3.3998  # logged repl_price in issue #1118
_NIGHT_IMPORT = 1.644
_PEAK_IMPORT = 3.40


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
    """Solve the MILP with the issue #1118 battery and terminal-SoC settings."""
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
        replacement_price_per_kwh=_REPLACEMENT_PRICE,
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
    ``0.98·2.321 − 2.40/0.98 − 0.186 ≈ −0.36``.  Without the #694 cap the
    #638 charge credit ``R − 2.40 ≈ 1.00`` makes the export look like
    +0.64, so the LP would fill the battery and sell it at a loss.
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
    #694 cap the LP sees an extra ``p_imp[evening] − p_imp[night] ≈ +0.21``
    from the non-cancelling #638 per-slot premiums and takes the loss.
    """
    out = _solve(_evening_night_peak(1.85), current_kwh=5.0, usable_kwh=5.0)

    assert out[0].batteries_discharged_kwh == pytest.approx(0.0, abs=1e-3)
    assert out[1].batteries_charged_kwh == pytest.approx(0.0, abs=1e-3)
    assert out[2].batteries_discharged_kwh == pytest.approx(5.0, abs=1e-3)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Issue #1118: the per-slot terminal-SoC premiums (#638/#694) are not "
        "cycle-neutral, so the LP sees this +0.155/kWh cycle as −0.36/kWh. "
        "Remove this marker once the terminal term values net Σ(ec − ed) "
        "at a single price."
    ),
)
def test_milp_takes_profitable_no_pv_evening_cycle() -> None:
    """An evening/night cycle with a positive real spread should be taken.

    Discharging at 2.06 and recharging at 1.644 is worth
    ``0.98·2.06 − 1.644/0.98 − 0.186 ≈ +0.155`` per kWh DC, and the
    terminal SoC is unchanged, so the terminal term should net to zero.
    The per-slot premiums instead net to
    ``(R − 2.06) − (R − 1.644 − 0.916/0.98) ≈ +0.52``, a penalty.
    """
    evening_load = 1.0
    out = _solve(_evening_night_peak(2.06), current_kwh=5.0, usable_kwh=5.0)

    assert out[0].batteries_discharged_kwh == pytest.approx(
        evening_load / _ETA, abs=1e-3
    )
    assert out[1].batteries_charged_kwh == pytest.approx(evening_load / _ETA, abs=1e-3)
