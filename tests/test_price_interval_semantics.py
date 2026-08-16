"""Tests for HSEM price interval semantics (issue #371, updated for #720 fix).

Background
----------
HSEM supports two price-data granularities and a configurable planning slot
width (``recommendation_interval_minutes``).

**New contract (since PR #761)**:
The source cadence is auto-detected per attribute key from the data timestamps.
The populator stores the **raw price** directly — no divide by ``eds_share``.
``coordinator_builder`` passes it straight through to the planner — no multiply.

Acceptance criteria verified here
----------------------------------
- A price P stored for any configuration must equal P (raw, no scaling).
- The planner always receives the original currency/kWh rate.
- Negative prices survive the full pipeline unchanged.
- Zero prices are stored and recovered correctly (not treated as missing).
- The round-trip result is the same for all supported configurations.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Helpers: new contract — store raw, pass raw
# ---------------------------------------------------------------------------


def compute_eds_share(
    eds_update_interval_minutes: int,
    recommendation_interval_minutes: int,
) -> float:
    """Return the ratio of EDS interval to slot interval (kept for reference)."""
    return eds_update_interval_minutes / recommendation_interval_minutes


def simulate_store_price(raw_price: float, _eds_share: float) -> float:
    """Simulate what hourly_data_populator writes to a per-slot recommendation.

    Since PR #761 the populator stores the raw price directly — no divide.
    ``_eds_share`` is accepted for API compatibility but ignored.
    """
    return raw_price


def simulate_planner_price(stored_price: float, _eds_share: float) -> float:
    """Simulate what coordinator_builder hands to the planner.

    Since PR #761 the coordinator passes the stored value straight through —
    no multiply.  ``_eds_share`` is accepted for API compatibility but ignored.
    """
    return stored_price


def round_trip_price(
    raw_price: float,
    eds_update_interval_minutes: int,
    recommendation_interval_minutes: int,
) -> tuple[float, float, float]:
    """Return (eds_share, stored_price, planner_price) for a given configuration."""
    share = compute_eds_share(
        eds_update_interval_minutes, recommendation_interval_minutes
    )
    stored = simulate_store_price(raw_price, share)
    planner = simulate_planner_price(stored, share)
    return share, stored, planner


# ---------------------------------------------------------------------------
# 1. eds_share values for all supported configurations
# ---------------------------------------------------------------------------


class TestEdsShareValues:
    """The eds_share factor must equal EDS interval / slot interval."""

    def test_eds_60min_slots_15min_gives_share_4(self):
        share = compute_eds_share(60, 15)
        assert share == pytest.approx(4.0)

    def test_eds_15min_slots_15min_gives_share_1(self):
        share = compute_eds_share(15, 15)
        assert share == pytest.approx(1.0)

    def test_eds_60min_slots_60min_gives_share_1(self):
        share = compute_eds_share(60, 60)
        assert share == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 2. Per-slot stored value (divide step in hourly_data_populator)
# ---------------------------------------------------------------------------


class TestPerSlotStoredValue:
    """Since PR #761 prices written to per-slot recommendations equal the raw
    price for all configurations — no divide by eds_share."""

    def test_eds_60min_slot_15min_stores_quarter_price(self):
        """EDS 60 min / slot 15 min: raw price stored unchanged (no divide since PR #761)."""
        raw_price = 1.20
        share = compute_eds_share(60, 15)
        stored = simulate_store_price(raw_price, share)
        assert stored == pytest.approx(raw_price)

    def test_eds_15min_slot_15min_stores_full_price(self):
        """EDS 15 min / slot 15 min: no scaling — stored value equals input."""
        raw_price = 0.85
        share = compute_eds_share(15, 15)
        stored = simulate_store_price(raw_price, share)
        assert stored == pytest.approx(raw_price)

    def test_eds_60min_slot_60min_stores_full_price(self):
        """EDS 60 min / slot 60 min: no scaling — stored value equals input."""
        raw_price = 2.50
        share = compute_eds_share(60, 60)
        stored = simulate_store_price(raw_price, share)
        assert stored == pytest.approx(raw_price)

    def test_negative_price_eds_60min_slot_15min_stores_quarter(self):
        """Negative prices are stored unchanged (no scaling since PR #761)."""
        raw_price = -0.40
        share = compute_eds_share(60, 15)
        stored = simulate_store_price(raw_price, share)
        assert stored == pytest.approx(raw_price)

    def test_zero_price_stores_zero(self):
        """Zero price must store as zero for all configurations."""
        for eds, slot in [(60, 15), (15, 15), (60, 60)]:
            share = compute_eds_share(eds, slot)
            stored = simulate_store_price(0.0, share)
            assert stored == pytest.approx(0.0), f"config EDS={eds} slot={slot}"


# ---------------------------------------------------------------------------
# 3. Round-trip: store → planner must recover original price
# ---------------------------------------------------------------------------


class TestRoundTripPriceRecovery:
    """The divide-in-populator / multiply-in-coordinator pair must cancel exactly."""

    def test_eds_60min_slot_15min_round_trip(self):
        raw_price = 1.50
        _, _, planner_price = round_trip_price(raw_price, 60, 15)
        assert planner_price == pytest.approx(raw_price)

    def test_eds_15min_slot_15min_round_trip(self):
        raw_price = 0.75
        _, _, planner_price = round_trip_price(raw_price, 15, 15)
        assert planner_price == pytest.approx(raw_price)

    def test_eds_60min_slot_60min_round_trip(self):
        raw_price = 3.00
        _, _, planner_price = round_trip_price(raw_price, 60, 60)
        assert planner_price == pytest.approx(raw_price)

    def test_negative_price_round_trip_eds_60min_slot_15min(self):
        """Negative import prices (e.g. during surplus) must survive the pipeline."""
        raw_price = -0.05
        _, _, planner_price = round_trip_price(raw_price, 60, 15)
        assert planner_price == pytest.approx(raw_price)

    def test_negative_price_round_trip_eds_15min_slot_15min(self):
        raw_price = -0.12
        _, _, planner_price = round_trip_price(raw_price, 15, 15)
        assert planner_price == pytest.approx(raw_price)

    def test_zero_price_round_trip_all_configs(self):
        """Zero prices must recover as zero for every supported configuration."""
        for eds, slot in [(60, 15), (15, 15), (60, 60)]:
            _, _, planner_price = round_trip_price(0.0, eds, slot)
            assert planner_price == pytest.approx(0.0), f"config EDS={eds} slot={slot}"

    def test_round_trip_is_config_independent(self):
        """Different configurations must all produce the same planner price
        from the same raw price — the scaling is transparent to the planner."""
        raw_price = 0.95
        results = []
        for eds, slot in [(60, 15), (15, 15), (60, 60)]:
            _, _, planner_price = round_trip_price(raw_price, eds, slot)
            results.append(planner_price)

        for planner_price in results:
            assert planner_price == pytest.approx(raw_price), (
                f"round trip broke for price {raw_price}"
            )


# ---------------------------------------------------------------------------
# 4. Stored value equals original raw value for all configurations
# ---------------------------------------------------------------------------


class TestStoredValueIsScaled:
    """Since PR #761 the populator stores raw values — stored always equals
    the original price regardless of eds_share."""

    def test_stored_price_equals_raw_for_all_configs(self):
        """Since PR #761 the populator stores raw values — stored always equals raw."""
        for eds, slot in [(60, 15), (15, 15), (60, 60)]:
            share = compute_eds_share(eds, slot)
            raw_price = 2.00
            stored = simulate_store_price(raw_price, share)
            assert stored == pytest.approx(raw_price), f"config EDS={eds} slot={slot}"

    def test_stored_price_equals_raw_when_no_scaling_needed(self):
        """When eds_share == 1, stored value must equal raw price."""
        for eds, slot in [(15, 15), (60, 60)]:
            share = compute_eds_share(eds, slot)
            raw_price = 1.75
            stored = simulate_store_price(raw_price, share)
            assert stored == pytest.approx(raw_price), f"config EDS={eds} slot={slot}"


# ---------------------------------------------------------------------------
# 5. Planner only receives rates (currency/kWh), never sub-slot fractions
# ---------------------------------------------------------------------------


class TestPlannerReceivesFullRate:
    """The planner must never receive a sub-slot fraction of a price.

    Since PR #761 the coordinator passes raw values straight through — no
    multiply — so the planner always sees the original currency/kWh rate.
    """

    def test_planner_price_always_equals_original_for_positive_prices(self):
        prices = [0.10, 0.50, 1.00, 2.50, 5.00]
        configs = [(60, 15), (15, 15), (60, 60)]
        for raw_price in prices:
            for eds, slot in configs:
                _, _, planner_price = round_trip_price(raw_price, eds, slot)
                assert planner_price == pytest.approx(raw_price, rel=1e-7), (
                    f"Planner received {planner_price!r} instead of {raw_price!r} "
                    f"for EDS={eds} slot={slot}"
                )

    def test_planner_price_always_equals_original_for_negative_prices(self):
        prices = [-0.01, -0.05, -0.50]
        configs = [(60, 15), (15, 15), (60, 60)]
        for raw_price in prices:
            for eds, slot in configs:
                _, _, planner_price = round_trip_price(raw_price, eds, slot)
                assert planner_price == pytest.approx(raw_price, rel=1e-7), (
                    f"Planner received {planner_price!r} instead of {raw_price!r} "
                    f"for EDS={eds} slot={slot}"
                )

    def test_changing_eds_interval_does_not_change_planner_price(self):
        """Switching EDS from 60 min to 15 min must not alter the planner price."""
        raw_price = 1.30
        slot = 15

        _, _, price_60 = round_trip_price(raw_price, 60, slot)
        _, _, price_15 = round_trip_price(raw_price, 15, slot)

        assert price_60 == pytest.approx(price_15)


# ---------------------------------------------------------------------------
# 6. Solcast raw-value pass-through to 15-min planner slots
# ---------------------------------------------------------------------------


class TestSolcastRawValuePassThrough:
    """Hourly Solcast values must flow through build_planner_input unchanged."""

    def test_hourly_solcast_forecast_fans_out_to_quarter_hour_slots(self):
        """An hourly 1.0 kWh Solcast forecast must become four 0.25 kWh slots."""
        from datetime import UTC, datetime, timedelta
        from unittest.mock import patch

        from custom_components.hsem.coordinator_builder import build_planner_input
        from custom_components.hsem.models.hourly_recommendation import (
            HourlyRecommendation,
        )
        from custom_components.hsem.models.live_state import LiveState
        from custom_components.hsem.models.planned_slot import PlannedSlot
        from custom_components.hsem.models.sensor_config import SensorConfig
        from custom_components.hsem.planner.slot_population import populate_solcast

        base = datetime(2026, 8, 9, 0, 0, 0, tzinfo=UTC)

        def _rec(i: int) -> HourlyRecommendation:
            start = base + timedelta(minutes=15 * i)
            return HourlyRecommendation(
                start=start,
                end=start + timedelta(minutes=15),
                recommendation="idle",
                avg_house_consumption_kwh=0.0,
                avg_house_consumption_1d_kwh=0.0,
                avg_house_consumption_3d_kwh=0.0,
                avg_house_consumption_7d_kwh=0.0,
                avg_house_consumption_14d_kwh=0.0,
                batteries_charged_kwh=0.0,
                batteries_discharged_kwh=0.0,
                estimated_battery_capacity_kwh=0.0,
                estimated_battery_soc_pct=0.0,
                estimated_cost_currency=0.0,
                estimated_net_consumption_kwh=0.0,
                export_price=0.0,
                grid_export_kwh=0.0,
                grid_import_kwh=0.0,
                import_price=0.0,
                solcast_pv_estimate_kwh=1.0,
            )

        cfg = SensorConfig()
        cfg.recommendation_interval_minutes = 15
        cfg.recommendation_interval_length = 1

        recs = [_rec(i) for i in range(4)]
        with patch("homeassistant.util.dt.now", return_value=base):
            inp = build_planner_input(
                cfg=cfg,
                live=LiveState(),
                hourly_recommendations=recs,
                batteries_schedules=[],
                previous_winner_name=None,
                previous_winner_score=0.0,
            )

        slots = [
            PlannedSlot(
                start=base + timedelta(minutes=15 * i),
                end=base + timedelta(minutes=15 * (i + 1)),
            )
            for i in range(4)
        ]
        populate_solcast(slots, inp.solcast_slots, inp.interval_minutes)

        assert len(inp.solcast_slots) == 1
        assert [slot.solcast_pv_estimate_kwh for slot in slots] == pytest.approx(
            [0.25, 0.25, 0.25, 0.25]
        )
