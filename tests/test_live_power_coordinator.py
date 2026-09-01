"""Tests for the coordinator-level live-power window and replan budget (issue #797).

Builds the missing coordinator-side half of Ambilights/hsem-ambilights#29,
extended with #31's budget-v2 (one correction plus one proven-opposite-
direction reversal per slot), adapted to this repo's split coordinator and
dedicated fast timer (see ``coordinator_live_power.py`` for why a separate
timer is required: the rolling window needs samples fresher than its
``LIVE_POWER_MAX_SAMPLE_AGE_SECONDS`` threshold, which the existing
minutes-scale coordinator interval cannot provide).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.hsem.coordinator import HSEMDataUpdateCoordinator
from custom_components.hsem.coordinator_live_power import (
    LIVE_POWER_MISMATCH_DEBOUNCE_SECONDS,
    LIVE_POWER_REPLAN_MAX_CORRECTIONS_PER_SLOT,
)
from custom_components.hsem.models.live_state import EVLiveState, LiveState
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.live_power import LivePowerEstimate, LivePowerWindow

_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def _make_coordinator() -> HSEMDataUpdateCoordinator:
    """Return a coordinator instance with __init__ bypassed (see test_coordinator.py)."""
    coord = object.__new__(HSEMDataUpdateCoordinator)
    cfg = SensorConfig()
    cfg.recommendation_interval_minutes = 15
    coord._cfg = cfg
    coord._live = LiveState()
    coord._live_power_window = LivePowerWindow(
        window_seconds=60, minimum_samples=3, maximum_sample_age_seconds=20
    )
    coord._live_power_source_signature = None
    coord._last_plan_live_power_estimate = None
    coord._live_power_mismatch_since = None
    coord._live_power_mismatch_slot_start = None
    coord._live_power_replan_pending_slot = None
    coord._live_power_replanned_slot_start = None
    coord._live_power_replan_count = 0
    coord._live_power_first_replan_direction = None
    coord._last_plan_slot_start = _NOW
    return coord


def _estimate(house_w: float | None, solar_w: float | None) -> LivePowerEstimate:
    return LivePowerEstimate(
        house_power_w=house_w,
        solar_power_w=solar_w,
        house_sample_count=3 if house_w is not None else 0,
        solar_sample_count=3 if solar_w is not None else 0,
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestCanonicalLivePowerNumber:
    def test_rejects_bool(self) -> None:
        assert HSEMDataUpdateCoordinator._canonical_live_power_number(True) is None

    def test_rejects_negative_by_default(self) -> None:
        assert HSEMDataUpdateCoordinator._canonical_live_power_number(-5.0) is None

    def test_allows_negative_when_requested(self) -> None:
        assert HSEMDataUpdateCoordinator._canonical_live_power_number(
            -5.0, allow_negative=True
        ) == pytest.approx(-5.0)

    def test_rejects_non_finite(self) -> None:
        assert (
            HSEMDataUpdateCoordinator._canonical_live_power_number(float("nan")) is None
        )

    def test_accepts_zero(self) -> None:
        assert HSEMDataUpdateCoordinator._canonical_live_power_number(0.0) == 0.0


class TestLivePowerChannelChangedMaterially:
    def test_both_none_is_unchanged(self) -> None:
        assert not HSEMDataUpdateCoordinator._live_power_channel_changed_materially(
            None, None, slot_hours=0.25
        )

    def test_availability_flip_is_material(self) -> None:
        assert HSEMDataUpdateCoordinator._live_power_channel_changed_materially(
            500.0, None, slot_hours=0.25
        )

    def test_small_delta_below_floor_is_immaterial(self) -> None:
        # 10 W over 15 min = 0.0025 kWh, well under the 0.05 kWh floor.
        assert not HSEMDataUpdateCoordinator._live_power_channel_changed_materially(
            510.0, 500.0, slot_hours=0.25
        )

    def test_large_absolute_delta_is_material(self) -> None:
        # 1000 W over 15 min = 0.25 kWh >> the 0.05 kWh floor.
        assert HSEMDataUpdateCoordinator._live_power_channel_changed_materially(
            1500.0, 500.0, slot_hours=0.25
        )

    def test_relative_delta_scales_with_accepted_magnitude(self) -> None:
        # accepted=5000W: 10% relative floor = 500W -> 501W delta over 1h = 0.501kWh
        assert HSEMDataUpdateCoordinator._live_power_channel_changed_materially(
            5501.0, 5000.0, slot_hours=1.0
        )
        assert not HSEMDataUpdateCoordinator._live_power_channel_changed_materially(
            5100.0, 5000.0, slot_hours=1.0
        )


class TestLivePowerSiteBalanceDirection:
    def test_no_accepted_estimate_is_unprovable(self) -> None:
        estimate = _estimate(1000.0, 0.0)
        assert (
            HSEMDataUpdateCoordinator._live_power_site_balance_direction(
                estimate, None, house_ambiguous=False
            )
            is None
        )

    def test_house_increase_is_positive_direction(self) -> None:
        current = _estimate(1500.0, 0.0)
        accepted = _estimate(500.0, 0.0)
        assert (
            HSEMDataUpdateCoordinator._live_power_site_balance_direction(
                current, accepted, house_ambiguous=False
            )
            == 1
        )

    def test_solar_increase_is_negative_direction(self) -> None:
        # More PV -> less net demand -> negative direction.
        current = _estimate(500.0, 2000.0)
        accepted = _estimate(500.0, 500.0)
        assert (
            HSEMDataUpdateCoordinator._live_power_site_balance_direction(
                current, accepted, house_ambiguous=False
            )
            == -1
        )

    def test_ambiguous_house_excludes_house_channel(self) -> None:
        # House channel swings wildly but is ambiguous (EV charging on an
        # inclusive meter); only the solar channel may prove a direction.
        # Solar dropped 1500 -> 500 (less PV, more net demand) => positive.
        current = _estimate(9000.0, 500.0)
        accepted = _estimate(500.0, 1500.0)
        assert (
            HSEMDataUpdateCoordinator._live_power_site_balance_direction(
                current, accepted, house_ambiguous=True
            )
            == 1
        )

    def test_no_comparable_channel_is_unprovable(self) -> None:
        current = _estimate(None, None)
        accepted = _estimate(500.0, 500.0)
        assert (
            HSEMDataUpdateCoordinator._live_power_site_balance_direction(
                current, accepted, house_ambiguous=False
            )
            is None
        )


# ---------------------------------------------------------------------------
# EV ambiguity
# ---------------------------------------------------------------------------


class TestLivePowerEvAmbiguous:
    def test_not_ambiguous_when_meter_excludes_ev(self) -> None:
        cfg = SensorConfig()
        cfg.house_power_includes_ev_charger_power = False
        live = LiveState()
        live.ev = EVLiveState(is_charging=True)
        assert not HSEMDataUpdateCoordinator._live_power_ev_ambiguous(cfg, live)

    def test_ambiguous_when_meter_includes_ev_and_charging(self) -> None:
        cfg = SensorConfig()
        cfg.house_power_includes_ev_charger_power = True
        live = LiveState()
        live.ev = EVLiveState(is_charging=True)
        assert HSEMDataUpdateCoordinator._live_power_ev_ambiguous(cfg, live)

    def test_ambiguous_from_positive_power_even_when_not_flagged_charging(self) -> None:
        cfg = SensorConfig()
        cfg.house_power_includes_ev_charger_power = True
        live = LiveState()
        live.ev = EVLiveState(is_charging=False, power_w=500.0)
        assert HSEMDataUpdateCoordinator._live_power_ev_ambiguous(cfg, live)

    def test_not_ambiguous_when_no_ev_activity(self) -> None:
        cfg = SensorConfig()
        cfg.house_power_includes_ev_charger_power = True
        live = LiveState()
        assert not HSEMDataUpdateCoordinator._live_power_ev_ambiguous(cfg, live)


# ---------------------------------------------------------------------------
# Replan budget (issue #797 — one correction + one proven reversal)
# ---------------------------------------------------------------------------


class TestLivePowerReplanBudget:
    def test_first_correction_always_allowed(self) -> None:
        coord = _make_coordinator()
        estimate = _estimate(1500.0, 0.0)
        assert coord._live_power_replan_budget_allows(_NOW, estimate)

    def test_second_correction_in_same_slot_blocked_without_reversal_proof(
        self,
    ) -> None:
        coord = _make_coordinator()
        coord._live_power_replanned_slot_start = _NOW
        coord._live_power_replan_count = 1
        coord._live_power_first_replan_direction = 1
        coord._last_plan_live_power_estimate = _estimate(1500.0, 0.0)
        # Same direction as the first correction: not a reversal.
        same_direction = _estimate(2500.0, 0.0)
        assert not coord._live_power_replan_budget_allows(_NOW, same_direction)

    def test_second_correction_allowed_when_direction_reverses(self) -> None:
        coord = _make_coordinator()
        coord._live_power_replanned_slot_start = _NOW
        coord._live_power_replan_count = 1
        coord._live_power_first_replan_direction = 1
        coord._last_plan_live_power_estimate = _estimate(1500.0, 0.0)
        # Net demand now goes back down -> opposite of the first correction.
        reversed_direction = _estimate(500.0, 0.0)
        assert coord._live_power_replan_budget_allows(_NOW, reversed_direction)

    def test_budget_exhausted_after_two_corrections(self) -> None:
        coord = _make_coordinator()
        coord._live_power_replanned_slot_start = _NOW
        coord._live_power_replan_count = LIVE_POWER_REPLAN_MAX_CORRECTIONS_PER_SLOT
        coord._live_power_first_replan_direction = 1
        estimate = _estimate(1500.0, 0.0)
        assert not coord._live_power_replan_budget_allows(_NOW, estimate)

    def test_new_slot_resets_budget(self) -> None:
        coord = _make_coordinator()
        coord._live_power_replanned_slot_start = _NOW
        coord._live_power_replan_count = LIVE_POWER_REPLAN_MAX_CORRECTIONS_PER_SLOT
        next_slot = _NOW + timedelta(minutes=15)
        estimate = _estimate(1500.0, 0.0)
        assert coord._live_power_replan_budget_allows(next_slot, estimate)


# ---------------------------------------------------------------------------
# Mismatch tracking -> actionable replan slot -> acceptance (full flow)
# ---------------------------------------------------------------------------


class TestLivePowerMismatchToAcceptanceFlow:
    def test_sustained_mismatch_requests_replan_after_debounce(self) -> None:
        coord = _make_coordinator()
        coord._last_plan_live_power_estimate = _estimate(500.0, 0.0)
        estimate = _estimate(2000.0, 0.0)  # well above materiality floor

        # First tick: starts the debounce window, does not yet request.
        t0 = _NOW + timedelta(minutes=1)
        assert not coord._track_live_power_mismatch(t0, estimate)

        # Still within the debounce window.
        t1 = t0 + timedelta(seconds=LIVE_POWER_MISMATCH_DEBOUNCE_SECONDS - 5)
        assert not coord._track_live_power_mismatch(t1, estimate)

        # Debounce elapsed: now requests a replan.
        t2 = t0 + timedelta(seconds=LIVE_POWER_MISMATCH_DEBOUNCE_SECONDS + 1)
        assert coord._track_live_power_mismatch(t2, estimate)
        assert coord._live_power_replan_pending_slot == _NOW

    def test_immaterial_change_never_requests_replan(self) -> None:
        coord = _make_coordinator()
        coord._last_plan_live_power_estimate = _estimate(500.0, 0.0)
        estimate = _estimate(505.0, 0.0)  # trivial delta
        t = _NOW + timedelta(seconds=LIVE_POWER_MISMATCH_DEBOUNCE_SECONDS + 5)
        assert not coord._track_live_power_mismatch(t, estimate)

    def test_actionable_slot_revalidates_pending_request(self) -> None:
        coord = _make_coordinator()
        coord._last_plan_live_power_estimate = _estimate(500.0, 0.0)
        coord._live_power_replan_pending_slot = _NOW
        estimate = _estimate(2000.0, 0.0)
        # Plenty of time remains in the slot (checked at slot start).
        assert coord._actionable_live_power_replan_slot(_NOW, estimate) == _NOW

    def test_actionable_slot_rejects_when_no_longer_material(self) -> None:
        coord = _make_coordinator()
        coord._last_plan_live_power_estimate = _estimate(500.0, 0.0)
        coord._live_power_replan_pending_slot = _NOW
        # Estimate has reverted back to what was already accepted.
        estimate = _estimate(500.0, 0.0)
        assert coord._actionable_live_power_replan_slot(_NOW, estimate) is None
        # Revalidation failure clears the stale pending request.
        assert coord._live_power_replan_pending_slot is None

    def test_accept_after_consumed_request_starts_budget(self) -> None:
        coord = _make_coordinator()
        coord._last_plan_live_power_estimate = _estimate(500.0, 0.0)
        estimate = _estimate(2000.0, 0.0)

        coord._accept_live_power_plan_estimate(
            estimate, plan_now=_NOW, requested_slot=_NOW
        )

        assert coord._last_plan_live_power_estimate == estimate
        assert coord._live_power_replanned_slot_start == _NOW
        assert coord._live_power_replan_count == 1
        assert coord._live_power_first_replan_direction == 1
        # Consuming a request always clears in-progress mismatch tracking.
        assert coord._live_power_replan_pending_slot is None

    def test_accept_without_request_does_not_touch_budget(self) -> None:
        """A slot-boundary/normal replan must not consume the live-power budget."""
        coord = _make_coordinator()
        coord._last_plan_live_power_estimate = _estimate(500.0, 0.0)
        estimate = _estimate(500.0, 0.0)  # matches accepted -> no fresh mismatch

        coord._accept_live_power_plan_estimate(
            estimate, plan_now=_NOW, requested_slot=None
        )

        assert coord._live_power_replanned_slot_start is None
        assert coord._live_power_replan_count == 0


# ---------------------------------------------------------------------------
# _should_replan must never fire blind on a pending live-power request
# (issue #866) — it only fires when this cycle's already-revalidated
# ``live_power_replan_request_slot`` is a concrete slot, never merely
# because a request is pending.
# ---------------------------------------------------------------------------


class TestShouldReplanLivePowerFailsClosed:
    def _prime(self, coord: HSEMDataUpdateCoordinator, live: LiveState) -> None:
        """Put the coordinator in a settled post-plan state at ``_NOW``."""
        coord._last_planner_output = PlannerOutput()
        coord._last_plan_slot_start = _NOW
        coord._persist_plan_state(live)

    def test_no_request_slot_this_cycle_does_not_force_replan(self) -> None:
        """A pending request alone must not bypass revalidation.

        Even though a live-power replan request is pending, if this cycle's
        already-revalidated ``live_power_replan_request_slot`` is ``None``
        (no fresh estimate proved it actionable), no blind replan may fire.
        """
        coord = _make_coordinator()
        live = LiveState()
        self._prime(coord, live)
        coord._live_power_replan_pending_slot = _NOW

        assert (
            coord._should_replan(live, _NOW, live_power_replan_request_slot=None)
            is False
        )

    def test_actionable_request_slot_forces_replan(self) -> None:
        """A concrete, already-revalidated request slot does trigger a replan."""
        coord = _make_coordinator()
        live = LiveState()
        self._prime(coord, live)
        coord._live_power_replan_pending_slot = _NOW

        assert (
            coord._should_replan(live, _NOW, live_power_replan_request_slot=_NOW)
            is True
        )
