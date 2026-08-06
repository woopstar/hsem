"""Tests for coordinator_builder helper functions.

Covers ``_resolve_max_discharge_power_w`` — the guard against the EV
discharge-cap feedback loop (issue #592, beta7).
"""

from __future__ import annotations

import pytest

from custom_components.hsem.coordinator_builder import _resolve_max_discharge_power_w
from custom_components.hsem.models.live_state import LiveState


class TestResolveMaxDischargePowerW:
    """The planner must see the battery's physical capability, not the
    applier's EV-capped write-back value."""

    @staticmethod
    def _live(
        *,
        ev_charging: bool,
        max_discharge_w: float,
        rated_wh: int = 10000,
    ) -> LiveState:
        live = LiveState()
        live.ev.is_charging = ev_charging
        live.huawei_batteries_max_discharge_power_w = max_discharge_w
        live.huawei_batteries_rated_capacity_wh = rated_wh
        return live

    def test_no_ev_charging_uses_rated_capability(self) -> None:
        """The planner always gets the physical capability derived from the
        rated capacity — the live entity is a commanded value, never a
        capability (issue #592)."""
        live = self._live(ev_charging=False, max_discharge_w=321.0)
        assert _resolve_max_discharge_power_w(live) == pytest.approx(5000.0)

    def test_ev_charging_uses_rated_capability(self) -> None:
        """During an EV session the applier caps the entity (e.g. 321 W).
        The planner must get the physical capability (5000 W for a
        10 kWh battery), not the capped value — otherwise the entire
        planning horizon is limited to the EV cap (issue #592)."""
        live = self._live(ev_charging=True, max_discharge_w=321.0)
        assert _resolve_max_discharge_power_w(live) == pytest.approx(5000.0)

    def test_ev_status_flicker_does_not_leak_cap(self) -> None:
        """A single-cycle EV boolean flicker must not let the still-capped
        entity poison an off-schedule planner run (beta8 regression)."""
        live = self._live(ev_charging=False, max_discharge_w=40.0)
        assert _resolve_max_discharge_power_w(live) == pytest.approx(5000.0)

    def test_missing_rated_capacity_falls_back_to_live(self) -> None:
        """Missing rated capacity → keep the live value (degraded but safe)."""
        live = self._live(ev_charging=True, max_discharge_w=321.0, rated_wh=0)
        assert _resolve_max_discharge_power_w(live) == pytest.approx(321.0)

    def test_missing_rated_capacity_and_live_value_returns_none(self) -> None:
        live = self._live(ev_charging=False, max_discharge_w=0.0, rated_wh=0)
        assert _resolve_max_discharge_power_w(live) is None
