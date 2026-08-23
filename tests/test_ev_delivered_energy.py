"""Tests for conservative stale-SoC delivered-energy credit."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.hsem.utils.ev_delivered_energy import (
    EVDeliveredEnergyEstimate,
    EVDeliveredEnergyTracker,
)


def _update(
    tracker: EVDeliveredEnergyTracker,
    now: datetime,
    *,
    soc: float | None = 50.0,
    power_w: float | None = 10_000.0,
    connected: bool = True,
    charging: bool = True,
    target: float | None = 80.0,
    capacity_kwh: float = 100.0,
    efficiency_pct: float = 100.0,
    allow_past_target: bool = False,
    max_gap_seconds: float = 3600.0,
) -> EVDeliveredEnergyEstimate:
    return tracker.update(
        now=now,
        connected=connected,
        charging=charging,
        power_w=power_w,
        reported_soc_pct=soc,
        target_soc_pct=target,
        battery_capacity_kwh=capacity_kwh,
        charger_efficiency_pct=efficiency_pct,
        max_power_w=12_000.0,
        allow_charge_past_target=allow_past_target,
        max_gap_seconds=max_gap_seconds,
    )


def test_integrates_measured_power_with_efficiency() -> None:
    tracker = EVDeliveredEnergyTracker()
    start = datetime(2026, 8, 23, 8, 0, tzinfo=UTC)
    _update(tracker, start, power_w=10_000.0, efficiency_pct=90.0)

    estimate = _update(
        tracker,
        start + timedelta(minutes=10),
        power_w=10_000.0,
        efficiency_pct=90.0,
    )

    assert estimate.credit_kwh == pytest.approx(1.5)
    assert estimate.effective_soc_pct == pytest.approx(51.5)


def test_soc_advance_preserves_only_unreported_residual_credit() -> None:
    tracker = EVDeliveredEnergyTracker()
    start = datetime(2026, 8, 23, 8, 0, tzinfo=UTC)
    _update(tracker, start)
    first = _update(tracker, start + timedelta(minutes=30))
    assert first.effective_soc_pct == pytest.approx(55.0)

    rebased = _update(tracker, start + timedelta(minutes=60), soc=53.0)

    assert rebased.credit_kwh == pytest.approx(7.0)
    assert rebased.effective_soc_pct == pytest.approx(60.0)


def test_reported_soc_catching_estimate_zeros_credit() -> None:
    tracker = EVDeliveredEnergyTracker()
    start = datetime(2026, 8, 23, 8, 0, tzinfo=UTC)
    _update(tracker, start)
    _update(tracker, start + timedelta(minutes=30))

    caught_up = _update(tracker, start + timedelta(minutes=60), soc=60.0)

    assert caught_up.credit_kwh == pytest.approx(0.0)
    assert caught_up.effective_soc_pct == pytest.approx(60.0)


def test_credit_is_capped_at_target_unless_past_target_is_allowed() -> None:
    start = datetime(2026, 8, 23, 8, 0, tzinfo=UTC)
    capped = EVDeliveredEnergyTracker()
    _update(capped, start, soc=79.0)
    capped_estimate = _update(capped, start + timedelta(minutes=30), soc=79.0)

    allowed = EVDeliveredEnergyTracker()
    _update(allowed, start, soc=79.0, allow_past_target=True)
    allowed_estimate = _update(
        allowed,
        start + timedelta(minutes=30),
        soc=79.0,
        allow_past_target=True,
    )

    assert capped_estimate.credit_kwh == pytest.approx(1.0)
    assert capped_estimate.effective_soc_pct == pytest.approx(80.0)
    assert allowed_estimate.credit_kwh == pytest.approx(5.0)
    assert allowed_estimate.effective_soc_pct == pytest.approx(84.0)


def test_excessive_gap_and_invalid_power_do_not_invent_energy() -> None:
    tracker = EVDeliveredEnergyTracker()
    start = datetime(2026, 8, 23, 8, 0, tzinfo=UTC)
    _update(tracker, start, max_gap_seconds=300.0)
    after_gap = _update(
        tracker,
        start + timedelta(minutes=10),
        max_gap_seconds=300.0,
    )
    invalid = _update(
        tracker,
        start + timedelta(minutes=11),
        power_w=float("nan"),
        max_gap_seconds=300.0,
    )
    after_invalid = _update(
        tracker,
        start + timedelta(minutes=12),
        max_gap_seconds=300.0,
    )

    assert after_gap.credit_kwh == pytest.approx(0.0)
    assert invalid.credit_kwh == pytest.approx(0.0)
    assert after_invalid.credit_kwh == pytest.approx(0.0)


def test_gap_and_invalid_power_preserve_credit_but_break_baseline() -> None:
    tracker = EVDeliveredEnergyTracker()
    start = datetime(2026, 8, 23, 8, 0, tzinfo=UTC)
    _update(tracker, start)
    credited = _update(tracker, start + timedelta(minutes=30))
    assert credited.credit_kwh == pytest.approx(5.0)

    after_gap = _update(
        tracker,
        start + timedelta(minutes=40),
        max_gap_seconds=300.0,
    )
    invalid = _update(
        tracker,
        start + timedelta(minutes=41),
        power_w=float("nan"),
        max_gap_seconds=300.0,
    )
    fresh_baseline = _update(
        tracker,
        start + timedelta(minutes=42),
        max_gap_seconds=300.0,
    )
    next_valid_interval = _update(
        tracker,
        start + timedelta(minutes=43),
        max_gap_seconds=300.0,
    )

    assert after_gap.credit_kwh == pytest.approx(5.0)
    assert invalid.credit_kwh == pytest.approx(5.0)
    assert fresh_baseline.credit_kwh == pytest.approx(5.0)
    assert next_valid_interval.credit_kwh == pytest.approx(5.0 + 1.0 / 6.0)


def test_disconnect_and_backwards_soc_reset_session_credit() -> None:
    tracker = EVDeliveredEnergyTracker()
    start = datetime(2026, 8, 23, 8, 0, tzinfo=UTC)
    _update(tracker, start)
    assert _update(
        tracker,
        start + timedelta(minutes=30),
    ).credit_kwh == pytest.approx(5.0)

    disconnected = _update(
        tracker,
        start + timedelta(minutes=31),
        connected=False,
        charging=False,
        power_w=0.0,
    )
    reconnected = _update(
        tracker,
        start + timedelta(minutes=32),
        soc=40.0,
    )
    backwards = _update(
        tracker,
        start + timedelta(minutes=33),
        soc=39.0,
    )

    assert disconnected.credit_kwh == pytest.approx(0.0)
    assert reconnected.effective_soc_pct == pytest.approx(40.0)
    assert backwards.credit_kwh == pytest.approx(0.0)
    assert backwards.effective_soc_pct == pytest.approx(39.0)


def test_naive_or_reversed_time_breaks_the_integration_baseline() -> None:
    tracker = EVDeliveredEnergyTracker()
    start = datetime(2026, 8, 23, 8, 0, tzinfo=UTC)
    _update(tracker, start)
    credited = _update(tracker, start + timedelta(minutes=30))
    naive = _update(tracker, datetime(2026, 8, 23, 8, 31))
    fresh_baseline = _update(tracker, start + timedelta(minutes=32))
    reversed_time = _update(tracker, start + timedelta(minutes=31))
    after_reversal = _update(tracker, start + timedelta(minutes=33))

    assert credited.credit_kwh == pytest.approx(5.0)
    assert naive.credit_kwh == pytest.approx(5.0)
    assert fresh_baseline.credit_kwh == pytest.approx(5.0)
    assert reversed_time.credit_kwh == pytest.approx(5.0)
    assert after_reversal.credit_kwh == pytest.approx(5.0)
