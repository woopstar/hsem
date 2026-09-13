"""Regression tests for issue #989 — a charge label with nothing charged.

Bug
---
``soc_simulation.simulate_soc()`` cleared a charge recommendation whose
resolved charge was zero only when the battery was **completely full**
(``headroom <= 1e-9``).  A slot could reach zero charge for other reasons and
keep its ``batteries_charge_solar`` label with
``batteries_charged_kwh == 0.0``:

``discharge_scheduler.apply_optimization_strategy()`` allocates its per-day
solar budget over unassigned slots sorted by **ascending export price**, so
the cheapest-export slots are served first.  With the battery near full the
budget is exhausted before the loop reaches an expensive-export slot, which
then receives a residual allocation that rounds to ``0.000`` — while still
being labelled ``batteries_charge_solar``.

The applier maps every charge recommendation to a Huawei mode that absorbs
energy (``MaximizeSelfConsumption``), so the inverter physically charged the
battery from live PV even though the plan stored nothing — destroying the
headroom the MILP reserved and forfeiting the export it chose to sell.
Reported on 6.3.1 with the battery at 95 % SoC during a high export-price
slot: recommendation ``batteries_charge_solar``, planned charge
``0.000 kWh``, planned export ``0.178 kWh``, battery physically charging at
+359 W.

This is the same plan-vs-actuator contradiction as issue #983, in the charge
direction.

Fix
---
Clear the charge label whenever the resolved charge is zero, regardless of
headroom.  The slot becomes ``batteries_wait_mode``, which the applier keeps
in TOU wait with ``fed_to_grid`` excess routing when the slot carries a
material solved export (``held_planned_export``, issue #797) — holding SoC
while the planned PV sale still executes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.planner.discharge_scheduler import (
    apply_optimization_strategy,
)
from custom_components.hsem.planner.soc_simulation import simulate_soc
from custom_components.hsem.utils.prices import SlotPrice
from custom_components.hsem.utils.recommendations import Recommendations

_NOW = datetime(2026, 9, 13, 9, 0, tzinfo=UTC)

_CHARGE_SOLAR = Recommendations.BatteriesChargeSolar.value
_CHARGE_GRID = Recommendations.BatteriesChargeGrid.value
_WAIT = Recommendations.BatteriesWaitMode.value
_DISCHARGE = Recommendations.BatteriesDischargeMode.value

# LUNA2000 5 kWh, as reported.
_RATED_KWH = 5.0
_USABLE_KWH = 5.0
_MAX_CHARGE_PER_SLOT = 0.625  # 2500 W over a 15-minute slot


def _slot(
    *,
    index: int = 0,
    net_consumption: float = -0.228,
    charged: float = 0.0,
    export_price: float = 0.05,
    import_price: float = 0.20,
    recommendation: str | None = _CHARGE_SOLAR,
    grid_export: float = 0.0,
) -> PlannedSlot:
    """One 15-minute slot, defaulting to the reported PV-surplus shape."""
    start = _NOW + timedelta(minutes=15 * index)
    return PlannedSlot(
        start=start,
        end=start + timedelta(minutes=15),
        price=SlotPrice(import_price=import_price, export_price=export_price),
        estimated_net_consumption_kwh=net_consumption,
        recommendation=recommendation,
        batteries_charged_kwh=charged,
        grid_export_kwh=grid_export,
    )


def _simulate(
    slots: list[PlannedSlot],
    *,
    current_kwh: float,
    milp_prepopulated: bool = False,
) -> None:
    """Run the SoC simulation over ``slots`` with the reported battery.

    ``milp_prepopulated`` mirrors ``candidate_selector``, which passes it for
    the MILP candidate only. It makes the simulation trust the LP's solved
    energy flows verbatim instead of re-deriving them — so a slot's planned
    export survives. The charge-ceiling clamp that clears a zero charge label
    runs ahead of that branch and therefore applies either way.
    """
    simulate_soc(
        slots,
        _NOW,
        current_kwh,
        _USABLE_KWH,
        _USABLE_KWH,
        _MAX_CHARGE_PER_SLOT,
        _MAX_CHARGE_PER_SLOT,
        rated_kwh=_RATED_KWH,
        milp_prepopulated=milp_prepopulated,
    )


class TestZeroChargeClearsTheChargeLabel:
    """A charge label must never survive a zero resolved charge."""

    def test_zero_charge_with_headroom_available_is_relabelled(self):
        """The regression: 95 % SoC leaves headroom, so the old guard never fired."""
        slots = [_slot(charged=0.0)]
        # 95 % of 5 kWh -> 0.25 kWh of headroom remains, well above 1e-9.
        _simulate(slots, current_kwh=4.75)

        assert slots[0].recommendation == _WAIT
        assert slots[0].batteries_charged_kwh == pytest.approx(0.0)

    def test_zero_charge_with_no_headroom_still_relabelled(self):
        """The case the original guard already covered must keep working."""
        slots = [_slot(charged=0.0)]
        _simulate(slots, current_kwh=_USABLE_KWH)

        assert slots[0].recommendation == _WAIT
        assert slots[0].batteries_charged_kwh == pytest.approx(0.0)

    def test_charge_grid_with_zero_charge_is_relabelled(self):
        """Applies to every charge recommendation, not just the solar one."""
        slots = [_slot(charged=0.0, recommendation=_CHARGE_GRID, net_consumption=0.1)]
        _simulate(slots, current_kwh=4.75)

        assert slots[0].recommendation == _WAIT

    def test_material_charge_keeps_the_solar_label(self):
        """A genuine solar-charge slot is untouched."""
        slots = [_slot(charged=0.2)]
        _simulate(slots, current_kwh=1.0)

        assert slots[0].recommendation == _CHARGE_SOLAR
        assert slots[0].batteries_charged_kwh == pytest.approx(0.2)

    def test_charge_clamped_to_headroom_keeps_the_label(self):
        """Partial clamping is not zero — the label must survive."""
        slots = [_slot(charged=0.5)]
        # 0.1 kWh of headroom: the charge is clamped but stays material.
        _simulate(slots, current_kwh=_USABLE_KWH - 0.1)

        assert slots[0].recommendation == _CHARGE_SOLAR
        assert slots[0].batteries_charged_kwh == pytest.approx(0.1)

    def test_non_charge_recommendation_is_untouched(self):
        """Only charge labels are cleared."""
        slots = [_slot(charged=0.0, recommendation=_DISCHARGE, net_consumption=0.1)]
        _simulate(slots, current_kwh=4.75)

        assert slots[0].recommendation == _DISCHARGE


class TestReportedScenario:
    """End-to-end over the fill → simulate chain, issue #989."""

    def test_expensive_export_slot_is_not_labelled_solar_charge(self):
        """Battery near full, cheap-export slots soak the day budget first.

        ``apply_optimization_strategy`` sorts by ascending export price, so the
        high-export slot is reached last and gets a residual allocation that
        rounds to zero — the reported state. After the fix the simulation
        clears that label instead of leaving it to drive MSC.
        """
        # Enough cheap-export PV-surplus slots to exhaust a 5 kWh day budget.
        slots = [
            _slot(index=i, export_price=0.02, recommendation=None, net_consumption=-0.6)
            for i in range(10)
        ]
        # The reported slot: highest export price, genuine PV surplus.
        expensive = _slot(
            index=10,
            export_price=1.95,
            recommendation=None,
            net_consumption=-0.228,
            grid_export=0.178,
        )
        slots.append(expensive)

        apply_optimization_strategy(
            slots,
            _NOW,
            4.75,
            _USABLE_KWH,
            0.0,
            [1, 2, 3, 10, 11, 12],
            export_min_price=99.0,  # keep ForceExport out of this scenario
        )

        # Precondition: the fill did label it solar charge with ~zero energy.
        assert expensive.recommendation == _CHARGE_SOLAR
        assert expensive.batteries_charged_kwh == pytest.approx(0.0, abs=1e-3)

        # The reported winner was the MILP candidate.
        _simulate(slots, current_kwh=4.75, milp_prepopulated=True)

        assert expensive.recommendation != _CHARGE_SOLAR
        assert expensive.recommendation == _WAIT
        assert expensive.batteries_charged_kwh == pytest.approx(0.0)
        # The planned PV sale must survive the relabel.
        assert expensive.grid_export_kwh == pytest.approx(0.178)


class TestApplierExecutesTheHold:
    """The relabelled slot must hold SoC while the planned PV sale executes."""

    @pytest.mark.asyncio
    async def test_wait_slot_with_planned_export_uses_tou_wait_and_feeds_grid(self):
        """Acceptance criterion: hold battery SoC, allow PV export — not MSC.

        ``batteries_wait_mode`` with zero charge/discharge and a material
        solved export satisfies ``_held_planned_export_is_authoritative()``,
        so the applier keeps the slot in TOU wait with ``fed_to_grid`` excess
        routing instead of MaximizeSelfConsumption (issue #797).
        """
        from custom_components.hsem.const import DEFAULT_HSEM_BATTERIES_WAIT_MODE
        from custom_components.hsem.custom_sensors.applier import (
            async_apply_battery_settings,
        )
        from custom_components.hsem.models.hourly_recommendation import (
            HourlyRecommendation,
        )
        from custom_components.hsem.models.live_state import LiveState
        from custom_components.hsem.models.sensor_config import SensorConfig
        from custom_components.hsem.utils.degraded_mode import DegradedMode
        from custom_components.hsem.utils.inverter_verify import (
            ApplyResult,
            ApplyStatus,
        )
        from custom_components.hsem.utils.workingmodes import WorkingModes

        sensor = MagicMock()
        sensor.hass = MagicMock()

        cfg = SensorConfig()
        cfg.read_only = False
        cfg.batteries_wait_mode_behavior = "strict"
        cfg.huawei_solar_batteries_working_mode = "select.wm"
        cfg.huawei_solar_batteries_maximum_discharging_power = "number.maxdis"
        cfg.huawei_solar_batteries_excess_pv_energy_use_in_tou = "select.excess"
        cfg.huawei_solar_batteries_tou_charging_and_discharging_periods = "sensor.tou"
        cfg.huawei_solar_device_id_batteries = "bat1"

        live = LiveState()
        live._degraded_mode = DegradedMode.OK
        live.battery_current_capacity_kwh = 4.75
        live.huawei_batteries_rated_capacity_wh = 5000
        live.huawei_batteries_max_discharge_power_w = 2500
        live.huawei_batteries_working_mode = WorkingModes.MaximizeSelfConsumption.value
        live.huawei_batteries_excess_pv_use_in_tou = "charge"
        live.tou_periods.periods = list(DEFAULT_HSEM_BATTERIES_WAIT_MODE)

        rec = HourlyRecommendation(
            start=_NOW,
            end=_NOW + timedelta(minutes=15),
            recommendation=_WAIT,
            avg_house_consumption_kwh=0.1398,
            avg_house_consumption_1d_kwh=0.0,
            avg_house_consumption_3d_kwh=0.0,
            avg_house_consumption_7d_kwh=0.0,
            avg_house_consumption_14d_kwh=0.0,
            batteries_charged_kwh=0.0,
            batteries_discharged_kwh=0.0,
            estimated_battery_capacity_kwh=4.75,
            estimated_battery_soc_pct=95.0,
            estimated_cost_currency=0.0,
            estimated_net_consumption_kwh=-0.228,
            export_price=1.95,
            grid_export_kwh=0.178,
            grid_import_kwh=0.0,
            import_price=0.20,
            solcast_pv_estimate_kwh=0.368,
        )

        async def _ok(entity_id, desired, writer, reader, **kwargs):  # type: ignore[no-untyped-def]  # local shim mirrors async_write_and_verify
            await writer()
            return ApplyResult(
                entity_id=entity_id,
                desired=desired,
                actual=desired,
                status=ApplyStatus.OK,
                attempts=1,
            )

        with (
            patch(
                "custom_components.hsem.utils.logger.HSEM_LOGGER.debug",
                new_callable=MagicMock,
            ),
            patch(
                "custom_components.hsem.custom_sensors.applier.async_write_and_verify",
                side_effect=_ok,
            ),
            patch(
                "custom_components.hsem.custom_sensors.applier.async_set_select_option",
                new_callable=AsyncMock,
            ) as mock_select,
            patch(
                "custom_components.hsem.custom_sensors.applier.async_set_number_value",
                new_callable=AsyncMock,
            ),
        ):
            await async_apply_battery_settings(sensor, cfg, live, rec, 0.0)

        # Holds SoC: TOU wait, never MaximizeSelfConsumption.
        mock_select.assert_any_await(sensor, "select.wm", WorkingModes.TimeOfUse.value)
        modes = [
            call.args[2]
            for call in mock_select.await_args_list
            if call.args[1] == "select.wm"
        ]
        assert WorkingModes.MaximizeSelfConsumption.value not in modes
        # Allows the planned PV sale.
        mock_select.assert_any_await(sensor, "select.excess", "fed_to_grid")
