"""Guard-clause tests for small applier-cap and EV-planner helpers.

These all protect a hardware write or a planner injection from unusable data:
a log formatter that must tolerate a missing reading, the phase-headroom
reservation for an EV that has not ramped down yet, the export-authority check
on a held slot, and the EV charger-power writer that must skip slots it cannot
place.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.hsem.custom_sensors.applier_caps import (
    _ev_phase_headroom_reservation_w,
    _fmt_live_power_w,
    _held_planned_export_is_authoritative,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import EVLiveState
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.planner.engine_ev import _compute_ev_charger_power
from custom_components.hsem.planner.ev_planner_models import (
    EVChargingPlan,
    EVChargingSlot,
)
from custom_components.hsem.utils.recommendations import Recommendations

_SLOT = timedelta(hours=1)
_NOW = datetime(2026, 6, 1, 12, 30, tzinfo=UTC)
_CURRENT_START = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
_FUTURE_START = datetime(2026, 6, 1, 13, 0, tzinfo=UTC)


class TestFormatLivePower:
    """Log lines must survive a missing power reading."""

    def test_a_reading_is_formatted_in_watts(self) -> None:
        """A number is rendered as whole Watts."""
        assert _fmt_live_power_w(7400.6) == "7400 W"

    def test_a_missing_reading_reads_as_not_available(self) -> None:
        """``None`` must not become ``0 W`` in a log line."""
        assert _fmt_live_power_w(None) == "n/a"


def _ev(power_w: float | None, *, charging: bool = True) -> EVLiveState:
    """Return an EV live state drawing *power_w* while charging."""
    ev = EVLiveState()
    ev.is_charging = charging
    ev.power_w = power_w
    return ev


class TestPhaseHeadroomReservation:
    """A charger that has not ramped down still occupies phase headroom."""

    def test_live_draw_above_the_plan_is_reserved(self) -> None:
        """The reservation is the live draw minus what the plan asked for."""
        reservation = _ev_phase_headroom_reservation_w(
            ev=_ev(7400.0), planned_power_w=3700.0
        )

        assert reservation == 3700

    def test_a_planned_draw_at_or_above_the_live_draw_reserves_nothing(self) -> None:
        """Once the plan matches reality no extra headroom is needed."""
        assert (
            _ev_phase_headroom_reservation_w(ev=_ev(3700.0), planned_power_w=7400.0)
            == 0
        )

    @pytest.mark.parametrize(
        "planned",
        [
            pytest.param(None, id="no_plan"),
            pytest.param("not a number", id="unparseable_plan"),
            pytest.param(float("nan"), id="non_finite_plan"),
        ],
    )
    def test_an_unusable_planned_value_reserves_the_whole_live_draw(
        self, planned: object
    ) -> None:
        """Without a trustworthy plan the whole live draw is reserved."""
        assert (
            _ev_phase_headroom_reservation_w(ev=_ev(7400.0), planned_power_w=planned)
            == 7400
        )

    @pytest.mark.parametrize(
        "live",
        [
            pytest.param(None, id="no_reading"),
            pytest.param(0.0, id="zero_draw"),
            pytest.param(float("nan"), id="non_finite"),
        ],
    )
    def test_an_unreadable_live_draw_reserves_nothing(self, live: float | None) -> None:
        """No proven live draw means no reservation."""
        assert _ev_phase_headroom_reservation_w(ev=_ev(live), planned_power_w=0.0) == 0

    def test_a_charger_that_is_not_charging_reserves_nothing(self) -> None:
        """Only a live session can occupy phase headroom."""
        assert (
            _ev_phase_headroom_reservation_w(
                ev=_ev(7400.0, charging=False), planned_power_w=0.0
            )
            == 0
        )


def _held_slot(export_kwh: object) -> HourlyRecommendation:
    """Return a slot holding the battery while planning *export_kwh*."""
    zero = 0.0
    rec = HourlyRecommendation(
        start=_CURRENT_START,
        end=_CURRENT_START + _SLOT,
        recommendation=Recommendations.BatteriesWaitMode.value,
        avg_house_consumption_kwh=zero,
        avg_house_consumption_1d_kwh=zero,
        avg_house_consumption_3d_kwh=zero,
        avg_house_consumption_7d_kwh=zero,
        avg_house_consumption_14d_kwh=zero,
        batteries_charged_kwh=zero,
        batteries_discharged_kwh=zero,
        estimated_battery_capacity_kwh=zero,
        estimated_battery_soc_pct=zero,
        estimated_cost_currency=zero,
        estimated_net_consumption_kwh=zero,
        export_price=zero,
        grid_export_kwh=zero,
        grid_import_kwh=zero,
        import_price=zero,
        solcast_pv_estimate_kwh=zero,
    )
    rec.grid_export_kwh = export_kwh  # type: ignore[assignment]  # exercise bad data
    return rec


class TestHeldPlannedExportAuthority:
    """A held slot's planned export is only authoritative when usable."""

    def test_a_material_planned_export_is_authoritative(self) -> None:
        """A held slot with real planned export drives the export flow."""
        assert _held_planned_export_is_authoritative(_held_slot(2.0)) is True

    def test_a_negligible_planned_export_is_not_authoritative(self) -> None:
        """A rounding-level export is not a decision."""
        assert _held_planned_export_is_authoritative(_held_slot(1e-9)) is False

    @pytest.mark.parametrize(
        "export",
        [
            pytest.param("not a number", id="unparseable"),
            pytest.param(None, id="missing"),
            pytest.param(float("nan"), id="non_finite"),
        ],
    )
    def test_an_unusable_planned_export_is_not_authoritative(
        self, export: object
    ) -> None:
        """Bad data never authorises an export decision."""
        assert _held_planned_export_is_authoritative(_held_slot(export)) is False

    def test_a_moving_battery_is_never_a_held_slot(self) -> None:
        """A slot that charges the battery is not a hold."""
        slot = _held_slot(2.0)
        slot.batteries_charged_kwh = 1.0

        assert _held_planned_export_is_authoritative(slot) is False


def _plan(*slots: EVChargingSlot, charger_min_power_w: float = 0.0) -> EVChargingPlan:
    """Return an EV charging plan holding *slots*."""
    return EVChargingPlan(
        state="charging",
        charging_slots=list(slots),
        charger_min_power_w=charger_min_power_w,
    )


def _ev_slot(start: datetime, ac_load_kwh: float) -> EVChargingSlot:
    """Return one EV charging-plan slot."""
    return EVChargingSlot(start=start, end=start + _SLOT, ac_load_kwh=ac_load_kwh)


class TestComputeEvChargerPower:
    """The plan's AC energy becomes a charger power on matching slots only."""

    @staticmethod
    def _planner_slots() -> tuple[list[PlannedSlot], list[datetime]]:
        """Return the current and next planner slots plus their starts."""
        starts = [_CURRENT_START, _FUTURE_START]
        slots = [PlannedSlot(start=s, end=s + _SLOT) for s in starts]
        return slots, starts

    def test_a_future_slot_uses_the_full_slot_width(self) -> None:
        """3.7 kWh over a full hour is a 3700 W command."""
        slots, starts = self._planner_slots()

        _compute_ev_charger_power(
            slots, starts, _plan(_ev_slot(_FUTURE_START, 3.7)), 60, _NOW
        )

        assert slots[1].ev_charger_calculated_power == pytest.approx(3700.0)

    def test_the_current_slot_uses_its_remaining_time(self) -> None:
        """At 12:30 a half-hour remains, so 1.85 kWh is still 3700 W."""
        slots, starts = self._planner_slots()

        _compute_ev_charger_power(
            slots, starts, _plan(_ev_slot(_CURRENT_START, 1.85)), 60, _NOW
        )

        assert slots[0].ev_charger_calculated_power == pytest.approx(3700.0)

    def test_a_plan_slot_outside_the_horizon_is_skipped(self) -> None:
        """An EV slot with no matching planner slot is ignored."""
        slots, starts = self._planner_slots()

        _compute_ev_charger_power(
            slots, starts, _plan(_ev_slot(_FUTURE_START + 5 * _SLOT, 3.7)), 60, _NOW
        )

        assert all(
            slot.ev_charger_calculated_power == pytest.approx(0.0) for slot in slots
        )

    def test_a_slot_with_no_energy_is_skipped(self) -> None:
        """A zero-energy EV slot writes no command."""
        slots, starts = self._planner_slots()

        _compute_ev_charger_power(
            slots, starts, _plan(_ev_slot(_FUTURE_START, 0.0)), 60, _NOW
        )

        assert slots[1].ev_charger_calculated_power == pytest.approx(0.0)

    def test_a_command_below_the_charger_minimum_is_zeroed(self) -> None:
        """A charger that cannot start that low is not asked to throttle."""
        slots, starts = self._planner_slots()

        _compute_ev_charger_power(
            slots,
            starts,
            _plan(_ev_slot(_FUTURE_START, 0.5), charger_min_power_w=1380.0),
            60,
            _NOW,
        )

        assert slots[1].ev_charger_calculated_power == pytest.approx(0.0)

    def test_the_second_ev_writes_its_own_field(self) -> None:
        """The second EV never overwrites the primary charger's command."""
        slots, starts = self._planner_slots()

        _compute_ev_charger_power(
            slots, starts, _plan(_ev_slot(_FUTURE_START, 3.7)), 60, _NOW, second=True
        )

        assert slots[1].ev_second_charger_calculated_power == pytest.approx(3700.0)
        assert slots[1].ev_charger_calculated_power == pytest.approx(0.0)

    @pytest.mark.parametrize(
        "plan",
        [
            pytest.param(None, id="no_plan"),
            pytest.param(EVChargingPlan(state="waiting"), id="empty_plan"),
        ],
    )
    def test_without_a_plan_nothing_is_written(
        self, plan: EVChargingPlan | None
    ) -> None:
        """No plan leaves every command at its default."""
        slots, starts = self._planner_slots()

        _compute_ev_charger_power(slots, starts, plan, 60, _NOW)

        assert all(
            slot.ev_charger_calculated_power == pytest.approx(0.0) for slot in slots
        )
