"""Tests for ``_sanitize_passive_ev_fallback`` in the planner engine.

The passive candidate is the no-action baseline every other candidate is
scored against, so it must not contain flexible EV demand the planner chose —
otherwise the baseline is as expensive as the plan and the comparison is
meaningless. A live session the planner cannot control (an unmanaged charger
already drawing power) must survive, because pretending it away would make the
baseline cheaper than reality.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.hsem.models.ev_config import EVConfig
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.planner.candidate_generator import (
    CANDIDATE_MILP,
    CANDIDATE_PASSIVE,
    CandidatePlan,
)
from custom_components.hsem.planner.engine_core import _sanitize_passive_ev_fallback
from custom_components.hsem.utils.recommendations import Recommendations

_SLOT = timedelta(hours=1)
_NOW = datetime(2026, 6, 1, 12, 30, tzinfo=UTC)
_PAST_START = datetime(2026, 6, 1, 11, 0, tzinfo=UTC)
_CURRENT_START = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
_FUTURE_START = datetime(2026, 6, 1, 13, 0, tzinfo=UTC)


def _slot(start: datetime, *, recommendation: str | None = None) -> PlannedSlot:
    """Return a slot carrying planner-chosen EV load."""
    return PlannedSlot(
        start=start,
        end=start + _SLOT,
        avg_house_consumption_kwh=1.0,
        solcast_pv_estimate_kwh=0.25,
        estimated_net_consumption_kwh=4.0,
        ev_planned_load_kwh=3.0,
        ev_accounted_load_kwh=1.5,
        ev_total_planned_load_kwh=3.0,
        ev_charger_calculated_power=7400.0,
        ev_second_charger_calculated_power=3700.0,
        recommendation=recommendation,
    )


def _passive(*slots: PlannedSlot) -> CandidatePlan:
    """Return a passive candidate holding *slots*."""
    return CandidatePlan(name=CANDIDATE_PASSIVE, slots=list(slots))


class TestFlexibleEvDemandIsRemoved:
    """Planner-chosen EV load is stripped from the future of the baseline."""

    def test_future_slots_lose_all_ev_demand(self) -> None:
        """Every EV field is zeroed and net consumption recomputed."""
        slot = _slot(_FUTURE_START)
        candidate = _passive(slot)

        _sanitize_passive_ev_fallback([candidate], None, _NOW)

        assert slot.ev_planned_load_kwh == pytest.approx(0.0)
        assert slot.ev_accounted_load_kwh == pytest.approx(0.0)
        assert slot.ev_total_planned_load_kwh == pytest.approx(0.0)
        assert slot.ev_charger_calculated_power == pytest.approx(0.0)
        assert slot.ev_second_charger_calculated_power == pytest.approx(0.0)
        # House load minus PV, with no EV contribution.
        assert slot.estimated_net_consumption_kwh == pytest.approx(0.75)

    def test_the_current_slot_is_also_sanitised(self) -> None:
        """A slot still in progress is part of the future being compared."""
        slot = _slot(_CURRENT_START)

        _sanitize_passive_ev_fallback([_passive(slot)], None, _NOW)

        assert slot.ev_total_planned_load_kwh == pytest.approx(0.0)

    def test_finished_slots_are_left_untouched(self) -> None:
        """Past slots record what happened and are not rewritten."""
        slot = _slot(_PAST_START)

        _sanitize_passive_ev_fallback([_passive(slot)], None, _NOW)

        assert slot.ev_planned_load_kwh == pytest.approx(3.0)
        assert slot.estimated_net_consumption_kwh == pytest.approx(4.0)

    def test_ev_smart_charging_label_becomes_a_hold(self) -> None:
        """A baseline slot cannot claim to be EV charging once stripped."""
        slot = _slot(
            _FUTURE_START, recommendation=Recommendations.EVSmartCharging.value
        )

        _sanitize_passive_ev_fallback([_passive(slot)], None, _NOW)

        assert slot.recommendation == Recommendations.BatteriesWaitMode.value

    def test_other_labels_are_preserved(self) -> None:
        """Only the EV label is a lie once EV demand is removed."""
        slot = _slot(
            _FUTURE_START, recommendation=Recommendations.BatteriesChargeSolar.value
        )

        _sanitize_passive_ev_fallback([_passive(slot)], None, _NOW)

        assert slot.recommendation == Recommendations.BatteriesChargeSolar.value

    def test_other_candidates_are_not_touched(self) -> None:
        """Only the passive baseline is sanitised."""
        passive_slot = _slot(_FUTURE_START)
        milp_slot = _slot(_FUTURE_START)
        milp = CandidatePlan(name=CANDIDATE_MILP, slots=[milp_slot])

        _sanitize_passive_ev_fallback([milp, _passive(passive_slot)], None, _NOW)

        assert milp_slot.ev_total_planned_load_kwh == pytest.approx(3.0)
        assert passive_slot.ev_total_planned_load_kwh == pytest.approx(0.0)

    def test_without_a_passive_candidate_nothing_happens(self) -> None:
        """A candidate list with no baseline is left alone."""
        slot = _slot(_FUTURE_START)
        milp = CandidatePlan(name=CANDIDATE_MILP, slots=[slot])

        _sanitize_passive_ev_fallback([milp], None, _NOW)

        assert slot.ev_total_planned_load_kwh == pytest.approx(3.0)


def _fixed_session_ev(
    *, charge_kw: float = 7.4, base_load_includes_ev: bool = False
) -> EVConfig:
    """Return an EV whose live session the planner cannot control."""
    return EVConfig(
        session_charge_kw=charge_kw,
        fixed_session_only=True,
        base_load_includes_ev=base_load_includes_ev,
    )


class TestUnmanagedSessionIsPreserved:
    """A live session the planner cannot stop stays in the baseline."""

    def test_two_hours_of_session_load_is_added_back(self) -> None:
        """A 7.4 kW unmanaged session fills the next two hours."""
        first = _slot(_FUTURE_START)
        second = _slot(_FUTURE_START + _SLOT)
        third = _slot(_FUTURE_START + 2 * _SLOT)

        _sanitize_passive_ev_fallback(
            [_passive(first, second, third)], [_fixed_session_ev()], _NOW
        )

        assert first.ev_total_planned_load_kwh == pytest.approx(7.4)
        assert second.ev_total_planned_load_kwh == pytest.approx(7.4)
        # The session budget is two hours, so the third slot stays empty.
        assert third.ev_total_planned_load_kwh == pytest.approx(0.0)

    def test_the_partly_elapsed_slot_only_gets_its_remaining_time(self) -> None:
        """At 12:30 the current hour has half an hour of session left."""
        current = _slot(_CURRENT_START)
        following = _slot(_FUTURE_START)

        _sanitize_passive_ev_fallback(
            [_passive(current, following)], [_fixed_session_ev()], _NOW
        )

        assert current.ev_total_planned_load_kwh == pytest.approx(3.7)
        assert following.ev_total_planned_load_kwh == pytest.approx(7.4)

    def test_session_load_lands_in_planned_load_when_not_metered(self) -> None:
        """A house meter that excludes the charger needs the EV load added."""
        slot = _slot(_FUTURE_START)

        _sanitize_passive_ev_fallback(
            [_passive(slot)], [_fixed_session_ev(base_load_includes_ev=False)], _NOW
        )

        assert slot.ev_planned_load_kwh == pytest.approx(7.4)
        assert slot.ev_accounted_load_kwh == pytest.approx(0.0)
        # Net consumption includes the unmanaged EV draw.
        assert slot.estimated_net_consumption_kwh == pytest.approx(0.75 + 7.4)

    def test_session_load_is_accounted_when_already_metered(self) -> None:
        """An inclusive house meter already contains the session draw."""
        slot = _slot(_FUTURE_START)

        _sanitize_passive_ev_fallback(
            [_passive(slot)], [_fixed_session_ev(base_load_includes_ev=True)], _NOW
        )

        assert slot.ev_accounted_load_kwh == pytest.approx(7.4)
        assert slot.ev_planned_load_kwh == pytest.approx(0.0)
        # Already in the house figure, so net consumption is unchanged.
        assert slot.estimated_net_consumption_kwh == pytest.approx(0.75)

    def test_past_slots_never_receive_session_load(self) -> None:
        """The baseline's history is not rewritten."""
        past = _slot(_PAST_START)
        future = _slot(_FUTURE_START)

        _sanitize_passive_ev_fallback(
            [_passive(past, future)], [_fixed_session_ev()], _NOW
        )

        assert past.ev_total_planned_load_kwh == pytest.approx(3.0)
        assert future.ev_total_planned_load_kwh == pytest.approx(7.4)

    @pytest.mark.parametrize(
        "ev",
        [
            pytest.param(
                EVConfig(session_charge_kw=7.4, fixed_session_only=False),
                id="not_a_fixed_session",
            ),
            pytest.param(
                EVConfig(session_charge_kw=None, fixed_session_only=True),
                id="no_measured_session_power",
            ),
            pytest.param(
                EVConfig(session_charge_kw=0.0, fixed_session_only=True),
                id="zero_session_power",
            ),
        ],
    )
    def test_evs_without_an_unmanaged_session_add_nothing(self, ev: EVConfig) -> None:
        """Only a measured, uncontrollable session is preserved."""
        slot = _slot(_FUTURE_START)

        _sanitize_passive_ev_fallback([_passive(slot)], [ev], _NOW)

        assert slot.ev_total_planned_load_kwh == pytest.approx(0.0)

    def test_a_zero_length_slot_is_skipped(self) -> None:
        """A degenerate slot has no time to carry session energy."""
        degenerate = PlannedSlot(
            start=_FUTURE_START,
            end=_FUTURE_START,
            avg_house_consumption_kwh=1.0,
            solcast_pv_estimate_kwh=0.25,
        )
        usable = _slot(_FUTURE_START + _SLOT)

        _sanitize_passive_ev_fallback(
            [_passive(degenerate, usable)], [_fixed_session_ev()], _NOW
        )

        assert degenerate.ev_total_planned_load_kwh == pytest.approx(0.0)
        assert usable.ev_total_planned_load_kwh == pytest.approx(7.4)

    def test_both_evs_sessions_are_preserved(self) -> None:
        """Two unmanaged chargers both contribute to the baseline."""
        slot = _slot(_FUTURE_START)

        _sanitize_passive_ev_fallback(
            [_passive(slot)],
            [_fixed_session_ev(charge_kw=7.4), _fixed_session_ev(charge_kw=3.7)],
            _NOW,
        )

        assert slot.ev_total_planned_load_kwh == pytest.approx(11.1)
