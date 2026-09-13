"""Tests for pure helpers in :mod:`custom_components.hsem.coordinator_helpers`."""

from __future__ import annotations

import pytest

from custom_components.hsem.coordinator_helpers import ocpp_charge_target


class TestOcppChargeTarget:
    """Tests for ocpp_charge_target() — issue #886."""

    def test_single_phase_power_converts_to_kw_and_amps(self) -> None:
        """A single-phase command converts to the matching kW/amp pair."""
        target_kw, max_current_a, number_phases = ocpp_charge_target(
            3680.0, "single_phase"
        )
        assert target_kw == pytest.approx(3.68)
        assert max_current_a == 16

    def test_three_phase_balanced_splits_current_across_phases(self) -> None:
        """A three-phase balanced charger divides the power over 3 phases."""
        target_kw, max_current_a, number_phases = ocpp_charge_target(
            11040.0, "three_phase_balanced"
        )
        assert target_kw == pytest.approx(11.04)
        assert max_current_a == 16

    def test_zero_power_requests_zero_amps(self) -> None:
        """A zero target must never request a nonzero current."""
        target_kw, max_current_a, number_phases = ocpp_charge_target(
            0.0, "single_phase"
        )
        assert target_kw == pytest.approx(0.0)
        assert max_current_a == 0

    def test_negative_power_clamped_to_zero(self) -> None:
        """A negative/invalid power must never be published as negative."""
        target_kw, max_current_a, number_phases = ocpp_charge_target(
            -500.0, "single_phase"
        )
        assert target_kw == pytest.approx(0.0)
        assert max_current_a == 0

    def test_unknown_topology_falls_back_to_single_phase(self) -> None:
        """An unset/unknown topology must use the conservative single-phase rate."""
        target_kw, max_current_a, number_phases = ocpp_charge_target(3680.0, None)
        assert target_kw == pytest.approx(3.68)
        assert max_current_a == 16

    def test_low_power_never_exceeds_flat_sixteen_amp_default(self) -> None:
        """Regression: OCPP must not always request a flat 16 A (issue #886)."""
        target_kw, max_current_a, number_phases = ocpp_charge_target(
            1380.0, "single_phase"
        )
        assert target_kw == pytest.approx(1.38)
        assert max_current_a == 6


class TestOcppChargeTargetPhases:
    """number_phases in the OCPP charge target (issue #1001)."""

    def test_single_phase_reports_one_phase(self) -> None:
        _, _, number_phases = ocpp_charge_target(3680.0, "single_phase")
        assert number_phases == 1

    def test_three_phase_balanced_reports_three_phases(self) -> None:
        _, _, number_phases = ocpp_charge_target(11040.0, "three_phase_balanced")
        assert number_phases == 3

    def test_unknown_topology_reports_one_phase(self) -> None:
        """The conservative single-phase fallback also reports one phase."""
        _, _, number_phases = ocpp_charge_target(3680.0, None)
        assert number_phases == 1

    def test_switchable_reports_the_mode_of_the_command(self) -> None:
        """A switchable charger's phase count follows the mode boundary."""
        # 2300 W is a one-phase command (10 A x 230 V) for a 16 A charger.
        _, amps, phases = ocpp_charge_target(
            2300.0, "three_phase_switchable", rated_current_a=16
        )
        assert amps == 10
        assert phases == 1
        # 6900 W is a three-phase command (10 A x 230 V x 3).
        _, amps, phases = ocpp_charge_target(
            6900.0, "three_phase_switchable", rated_current_a=16
        )
        assert amps == 10
        assert phases == 3

    def test_switchable_zero_power_reports_one_phase(self) -> None:
        """A zero ceiling is modeless; the one-phase branch is inert."""
        _, amps, phases = ocpp_charge_target(
            0.0, "three_phase_switchable", rated_current_a=16
        )
        assert amps == 0
        assert phases == 1
