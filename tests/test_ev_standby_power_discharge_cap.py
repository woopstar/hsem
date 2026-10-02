"""Tests for the EV standby-power threshold on the discharge cap (issue #1251).

An idle EV charger's electronics draw a few watts.  The applier read any
positive live EV power as a charging session, so an EV without the discharge
permission held the Huawei ``maximum_discharging_power`` at 0 W in every slot,
including a ``batteries_discharge_mode`` slot with a planned discharge.

Covers:
- ``_ev_is_active_or_planned()`` — a live reading counts only above
  ``EV_STANDBY_POWER_W``; the charging flag and a planned command still count
  on their own
- ``async_apply_battery_settings`` — no 0 W cap for an idle charger, and the
  0 W cap for a real unpermitted session is unchanged
"""

from __future__ import annotations

import pytest

from custom_components.hsem.custom_sensors.applier_caps import (
    EV_STANDBY_POWER_W,
    _ev_is_active_or_planned,
)
from custom_components.hsem.models.live_state import EVLiveState
from tests.test_discharge_mode_cap_oscillation import (
    _apply,
    _cfg,
    _discharge_cap_writes,
    _live,
    _rec,
)

# The reporter's idle charger (``sensor.ev_charger_total_power``).
_IDLE_CHARGER_W = 4.0
_PLANNED_DISCHARGE_KWH = 0.5


def _ev(power_w: float | None, *, charging: bool = False) -> EVLiveState:
    """Return an EV live state reading *power_w*, without the discharge permission."""
    ev = EVLiveState()
    ev.is_charging = charging
    ev.power_w = power_w
    ev.force_max_discharge_power = False
    return ev


class TestEvIsActiveOrPlanned:
    """Standby draw is not a session; every other signal is unchanged."""

    @pytest.mark.parametrize(
        "power_w",
        [
            pytest.param(_IDLE_CHARGER_W, id="idle_charger"),
            pytest.param(EV_STANDBY_POWER_W, id="at_the_threshold"),
            pytest.param(0.0, id="zero"),
            pytest.param(None, id="no_reading"),
            pytest.param(float("nan"), id="non_finite"),
        ],
    )
    def test_standby_or_unusable_power_is_not_an_active_ev(
        self, power_w: float | None
    ) -> None:
        """Without the charging flag or a plan, standby draw is no session."""
        assert _ev_is_active_or_planned(ev=_ev(power_w), planned_power_w=0.0) is False

    def test_power_above_the_threshold_is_an_active_ev(self) -> None:
        """A draw above standby is a session even without the charging flag."""
        ev = _ev(EV_STANDBY_POWER_W + 1.0)
        assert _ev_is_active_or_planned(ev=ev, planned_power_w=0.0) is True

    def test_charging_flag_counts_whatever_the_power_reads(self) -> None:
        """The charger's own status is authoritative at any power."""
        ev = _ev(_IDLE_CHARGER_W, charging=True)
        assert _ev_is_active_or_planned(ev=ev, planned_power_w=0.0) is True

    def test_planned_command_counts_for_an_idle_charger(self) -> None:
        """Issue #797: a planned command is enforced before the charger starts."""
        ev = _ev(_IDLE_CHARGER_W)
        assert _ev_is_active_or_planned(ev=ev, planned_power_w=1380.0) is True


class TestIdleChargerDoesNotZeroTheCap:
    """A discharge slot keeps its cap while the charger only draws standby."""

    @pytest.mark.asyncio
    async def test_zero_cap_is_restored_to_rated_max(self) -> None:
        """The reporter's case: hardware at 0 W, idle charger, planned discharge."""
        live = _live(max_discharge_power_w=0)
        live.ev = _ev(_IDLE_CHARGER_W)
        rec = _rec(discharged_kwh=_PLANNED_DISCHARGE_KWH)
        mock_number = await _apply(_cfg(), live, rec)
        assert _discharge_cap_writes(mock_number) == [2500]

    @pytest.mark.asyncio
    async def test_rated_cap_is_not_written_back_to_zero(self) -> None:
        """A cap already at the rated maximum is left alone."""
        live = _live(max_discharge_power_w=2500)
        live.ev = _ev(_IDLE_CHARGER_W)
        rec = _rec(discharged_kwh=_PLANNED_DISCHARGE_KWH)
        mock_number = await _apply(_cfg(), live, rec)
        assert _discharge_cap_writes(mock_number) == []

    @pytest.mark.asyncio
    async def test_idle_second_charger_does_not_zero_the_cap(self) -> None:
        """The second EV uses the same threshold."""
        live = _live(max_discharge_power_w=0)
        live.ev_second = _ev(_IDLE_CHARGER_W)
        rec = _rec(discharged_kwh=_PLANNED_DISCHARGE_KWH)
        mock_number = await _apply(_cfg(), live, rec)
        assert _discharge_cap_writes(mock_number) == [2500]


class TestRealSessionStillZeroesTheCap:
    """Issue #797: an unpermitted EV that really charges blocks discharge."""

    @pytest.mark.asyncio
    async def test_power_above_standby_without_the_charging_flag(self) -> None:
        """A charger with no status entity is recognised by its draw."""
        live = _live(max_discharge_power_w=2500)
        live.ev = _ev(1380.0)
        rec = _rec(discharged_kwh=_PLANNED_DISCHARGE_KWH)
        mock_number = await _apply(_cfg(), live, rec)
        assert _discharge_cap_writes(mock_number) == [0]

    @pytest.mark.asyncio
    async def test_charging_flag_with_a_standby_reading(self) -> None:
        """A session that has not ramped up yet is already blocked."""
        live = _live(max_discharge_power_w=2500)
        live.ev = _ev(_IDLE_CHARGER_W, charging=True)
        rec = _rec(discharged_kwh=_PLANNED_DISCHARGE_KWH)
        mock_number = await _apply(_cfg(), live, rec)
        assert _discharge_cap_writes(mock_number) == [0]
