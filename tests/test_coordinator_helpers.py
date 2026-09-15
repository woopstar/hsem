"""Tests for pure helpers in :mod:`custom_components.hsem.coordinator_helpers`."""

from __future__ import annotations

import pytest

from custom_components.hsem.coordinator_helpers import (
    ev_is_managed,
    ev_management_enabled,
    ocpp_charge_target,
    ocpp_management_flags,
)
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig


class TestOcppChargeTarget:
    """Tests for ocpp_charge_target() — issue #886."""

    def test_single_phase_power_converts_to_kw_and_amps(self) -> None:
        """A single-phase command converts to the matching kW/amp pair."""
        target_kw, max_current_a = ocpp_charge_target(3680.0, "single_phase")
        assert target_kw == pytest.approx(3.68)
        assert max_current_a == 16

    def test_three_phase_balanced_splits_current_across_phases(self) -> None:
        """A three-phase balanced charger divides the power over 3 phases."""
        target_kw, max_current_a = ocpp_charge_target(11040.0, "three_phase_balanced")
        assert target_kw == pytest.approx(11.04)
        assert max_current_a == 16

    def test_zero_power_requests_zero_amps(self) -> None:
        """A zero target must never request a nonzero current."""
        target_kw, max_current_a = ocpp_charge_target(0.0, "single_phase")
        assert target_kw == pytest.approx(0.0)
        assert max_current_a == 0

    def test_negative_power_clamped_to_zero(self) -> None:
        """A negative/invalid power must never be published as negative."""
        target_kw, max_current_a = ocpp_charge_target(-500.0, "single_phase")
        assert target_kw == pytest.approx(0.0)
        assert max_current_a == 0

    def test_unknown_topology_falls_back_to_single_phase(self) -> None:
        """An unset/unknown topology must use the conservative single-phase rate."""
        target_kw, max_current_a = ocpp_charge_target(3680.0, None)
        assert target_kw == pytest.approx(3.68)
        assert max_current_a == 16

    def test_low_power_never_exceeds_flat_sixteen_amp_default(self) -> None:
        """Regression: OCPP must not always request a flat 16 A (issue #886)."""
        target_kw, max_current_a = ocpp_charge_target(1380.0, "single_phase")
        assert target_kw == pytest.approx(1.38)
        assert max_current_a == 6


# ---------------------------------------------------------------------------
# EV management: configuration vs connectivity (issue #1018)
# ---------------------------------------------------------------------------


def _ev_state(
    *, enabled: bool, smart: bool, connected: bool, is_second: bool
) -> tuple[SensorConfig, LiveState]:
    cfg, live = SensorConfig(), LiveState()
    prefix = "ev_second_planned_load" if is_second else "ev_planned_load"
    setattr(cfg, f"{prefix}_enabled", enabled)
    setattr(live, f"{prefix}_smart_charging_enabled", smart)
    setattr(live, f"{prefix}_connected", connected)
    return cfg, live


@pytest.mark.parametrize("is_second", [False, True])
@pytest.mark.parametrize(
    ("enabled", "smart", "connected", "management_enabled", "managed"),
    [
        (True, True, True, True, True),
        # A "connected" blip changes only *managed*, never the configuration.
        (True, True, False, True, False),
        (True, False, True, False, False),
        (False, True, True, False, False),
        (False, False, False, False, False),
    ],
)
def test_management_enabled_excludes_the_connected_reading(
    is_second: bool,
    enabled: bool,
    smart: bool,
    connected: bool,
    management_enabled: bool,
    managed: bool,
) -> None:
    cfg, live = _ev_state(
        enabled=enabled, smart=smart, connected=connected, is_second=is_second
    )
    assert ev_management_enabled(cfg, live, is_second=is_second) is management_enabled
    assert ev_is_managed(cfg, live, is_second=is_second) is managed


def test_each_ev_reads_only_its_own_fields() -> None:
    """The primary's configuration never leaks into the second EV's answer."""
    cfg, live = _ev_state(enabled=True, smart=True, connected=True, is_second=False)
    assert ev_management_enabled(cfg, live, is_second=False) is True
    assert ev_management_enabled(cfg, live, is_second=True) is False


def test_ocpp_management_flags_carries_both_answers() -> None:
    """The OCPP dispatch receives the connectivity-free answer alongside *managed*."""
    cfg, live = _ev_state(enabled=True, smart=True, connected=False, is_second=True)
    assert ocpp_management_flags(cfg, live, is_second=True) == {
        "managed": False,
        "management_enabled": True,
    }
