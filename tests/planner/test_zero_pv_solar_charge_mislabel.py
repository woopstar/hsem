"""Regression tests for issue #720 — zero-PV slots must not be mislabeled
as BatteriesChargeSolar.

Bug
---
``apply_optimization_strategy`` used ``NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH``
(0.1 kWh) to decide whether an unassigned summer slot should charge from
solar.  A slot with a small positive house load (e.g. 0.08 kWh) and zero
PV would pass the ``<= 0.1`` check and get ``BatteriesChargeSolar`` even
though there was no PV surplus at all.  The result was a grid-charging
slot masquerading as solar charging, which:

- Confused the ``hourly_recommendations`` output
- Caused the applier to write ``MaximizeSelfConsumption`` instead of
  ``TimeOfUse`` + charge TOU
- Made the plan look more fragmented than it actually was

Fix
---
Both the per-day solar-charging loop and the summer seasonal fill now
require ``estimated_net_consumption_kwh < 0.0`` (an actual PV surplus)
before assigning ``BatteriesChargeSolar``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.planner.discharge_scheduler import (
    apply_optimization_strategy,
)
from custom_components.hsem.utils.prices import SlotPrice
from custom_components.hsem.utils.recommendations import Recommendations

_UTC = UTC
_CHARGE_SOLAR = Recommendations.BatteriesChargeSolar.value
_DISCHARGE = Recommendations.BatteriesDischargeMode.value


def _slot(
    hour: int,
    minute: int = 0,
    net_consumption: float = 0.0,
    import_price: float = 0.10,
    export_price: float = 0.05,
    recommendation: str | None = None,
) -> PlannedSlot:
    """Construct a minimal PlannedSlot for testing."""
    start = datetime(2026, 8, 10, hour, minute, tzinfo=_UTC)
    end = (
        start.replace(minute=minute + 15)
        if minute < 45
        else start.replace(hour=hour + 1, minute=0)
    )
    return PlannedSlot(
        start=start,
        end=end,
        price=SlotPrice(import_price=import_price, export_price=export_price),
        estimated_net_consumption_kwh=net_consumption,
        recommendation=recommendation,
    )


class TestZeroPvSolarChargeMislabel:
    """Slots with zero PV must never be labeled BatteriesChargeSolar."""

    def test_small_house_load_no_pv_not_charge_solar(self) -> None:
        """tonnr's exact scenario: house=0.08 kWh, PV=0.0 kWh →
        must NOT be BatteriesChargeSolar."""
        now = datetime(2026, 8, 10, 22, 0, tzinfo=_UTC)
        slot = _slot(
            hour=22,
            minute=30,
            net_consumption=0.08,  # 0.08 house - 0.0 PV = +0.08
            import_price=0.074,
            export_price=0.005,
        )
        apply_optimization_strategy(
            slots=[slot],
            now=now,
            current_capacity=8.7,
            usable_capacity=9.5,
            required_capacity=0.0,
            months_winter=[1, 2, 3, 4, 10, 11, 12],
        )
        assert slot.recommendation == _DISCHARGE, (
            "Slot with 0.08 kWh house load and zero PV must not be "
            "mislabeled as BatteriesChargeSolar"
        )

    def test_zero_net_consumption_not_charge_solar(self) -> None:
        """A slot at exactly zero net consumption has no PV surplus."""
        now = datetime(2026, 8, 10, 12, 0, tzinfo=_UTC)
        slot = _slot(hour=12, net_consumption=0.0)
        apply_optimization_strategy(
            slots=[slot],
            now=now,
            current_capacity=5.0,
            usable_capacity=9.0,
            required_capacity=0.0,
            months_winter=[1, 2, 3, 4, 10, 11, 12],
        )
        assert slot.recommendation == _DISCHARGE

    def test_actual_pv_surplus_is_charge_solar(self) -> None:
        """A slot with negative net consumption (real PV surplus) must
        still get BatteriesChargeSolar."""
        now = datetime(2026, 8, 10, 12, 0, tzinfo=_UTC)
        slot = _slot(hour=12, net_consumption=-0.5)
        apply_optimization_strategy(
            slots=[slot],
            now=now,
            current_capacity=5.0,
            usable_capacity=9.0,
            required_capacity=0.0,
            months_winter=[1, 2, 3, 4, 10, 11, 12],
        )
        assert slot.recommendation == _CHARGE_SOLAR

    def test_solar_charge_loop_excludes_zero_pv_slots(self) -> None:
        """The per-day solar-charging loop must skip zero-PV slots."""
        now = datetime(2026, 8, 10, 0, 0, tzinfo=_UTC)
        slots = [
            _slot(hour=22, minute=30, net_consumption=0.08),
            _slot(hour=22, minute=45, net_consumption=0.08),
            _slot(hour=12, minute=0, net_consumption=-0.5),
        ]
        apply_optimization_strategy(
            slots=slots,
            now=now,
            current_capacity=8.7,
            usable_capacity=9.5,
            required_capacity=0.0,
            months_winter=[1, 2, 3, 4, 10, 11, 12],
        )
        # The two zero-PV evening slots must not be solar-charged
        assert slots[0].recommendation == _DISCHARGE
        assert slots[1].recommendation == _DISCHARGE
        # The midday PV-surplus slot must be solar-charged
        assert slots[2].recommendation == _CHARGE_SOLAR
        assert slots[2].batteries_charged_kwh == 0.5
