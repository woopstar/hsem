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

    def test_no_ev_charging_uses_live_value(self) -> None:
        """Without an active EV session the live read-back reflects the
        rated maximum (or a genuine user override) and is used unchanged."""
        live = self._live(ev_charging=False, max_discharge_w=321.0)
        assert _resolve_max_discharge_power_w(live) == pytest.approx(321.0)

    def test_ev_charging_uses_rated_capability(self) -> None:
        """During an EV session the applier caps the entity (e.g. 321 W).
        The planner must get the physical capability (5000 W for a
        10 kWh battery), not the capped value — otherwise the entire
        planning horizon is limited to the EV cap (issue #592)."""
        live = self._live(ev_charging=True, max_discharge_w=321.0)
        assert _resolve_max_discharge_power_w(live) == pytest.approx(5000.0)

    def test_ev_charging_without_rated_capacity_falls_back_to_live(self) -> None:
        """Missing rated capacity → keep the live value (degraded but safe)."""
        live = self._live(ev_charging=True, max_discharge_w=321.0, rated_wh=0)
        assert _resolve_max_discharge_power_w(live) == pytest.approx(321.0)

    def test_no_ev_charging_missing_live_value_returns_none(self) -> None:
        live = self._live(ev_charging=False, max_discharge_w=0.0)
        assert _resolve_max_discharge_power_w(live) is None
