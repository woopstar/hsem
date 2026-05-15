"""Tests for explicit surfacing of missing tomorrow price and PV data (issue #370).

Covers the acceptance criteria from GitHub issue #370:

- Missing tomorrow price data is visible in diagnostics.
- Missing tomorrow PV data is visible in diagnostics.
- Planner does not treat missing future data as real zero silently.
- Degraded mode is NOT triggered for price/PV missing data (non-critical).
- Tests cover partial tomorrow price data, partial tomorrow PV data, and
  complete tomorrow data.

All tests are pure-Python; no Home Assistant runtime is required.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

from custom_components.hsem.models.planner_inputs import (
    HourlyConsumptionAverage,
    PlannerInput,
    PricePoint,
    SolcastSlot,
)
from custom_components.hsem.models.planner_outputs import DataQuality
from custom_components.hsem.models.time_series import TimeSeriesIndex
from custom_components.hsem.planner import run_planner
from custom_components.hsem.utils.degraded_mode import (
    DegradedMode,
    classify_degraded_mode,
)

_TZ = ZoneInfo("Europe/Copenhagen")

# ---------------------------------------------------------------------------
# Shared input-building helpers
# ---------------------------------------------------------------------------

_IMPORT_PRICES_TODAY = [0.20 + 0.01 * h for h in range(24)]
_EXPORT_PRICES_TODAY = [max(p - 0.05, 0.01) for p in _IMPORT_PRICES_TODAY]
_IMPORT_PRICES_TOMORROW = [0.18 + 0.01 * h for h in range(24)]
_EXPORT_PRICES_TOMORROW = [max(p - 0.05, 0.01) for p in _IMPORT_PRICES_TOMORROW]
_PV_TODAY = [
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.1,
    0.4,
    1.2,
    2.5,
    3.8,
    5.0,
    5.5,
    5.2,
    4.8,
    3.8,
    2.5,
    1.5,
    0.6,
    0.1,
    0.0,
    0.0,
    0.0,
    0.0,
]
_PV_TOMORROW = [
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.2,
    0.6,
    1.5,
    2.8,
    4.0,
    5.2,
    5.6,
    5.3,
    4.9,
    3.9,
    2.6,
    1.6,
    0.7,
    0.2,
    0.0,
    0.0,
    0.0,
    0.0,
]
_CONSUMPTION = [0.4 + 0.01 * h for h in range(24)]


def _today_price_points() -> list[PricePoint]:
    """Return 24 price points for today (hours 0-23)."""
    return [
        PricePoint(
            hour=h,
            import_price=_IMPORT_PRICES_TODAY[h],
            export_price=_EXPORT_PRICES_TODAY[h],
        )
        for h in range(24)
    ]


def _tomorrow_price_points() -> list[PricePoint]:
    """Return 24 price points for tomorrow (hours 24-47 mapped back to 0-23).

    The planner input uses hour 0-23; both today and tomorrow are keyed by
    wall-clock hour.  When the horizon is 48 hours the TSI assigns day_offset=0
    to today's slots and day_offset=1 to tomorrow's slots.  Both share the
    same hour keys (0-23) — the planner de-duplication in the coordinator
    already handles this by building two separate passes.

    For test purposes, we provide a single list of 48 PricePoint objects
    (hours 0-47) but the planner PricePoint model only accepts hour 0-23.
    We therefore supply 24 price points covering each hour — tomorrow's
    data is the *same keys* as today but distinct values.

    In real usage the coordinator builds two separate 24-hour lists and
    feeds them to the engine.  For testing, we replicate that pattern by
    providing 24 entries: the TSI uses ``day_offset`` to distinguish today
    and tomorrow, but price alignment is keyed by wall-clock hour only.
    So for a 48-hour horizon, the same 24-hour price list covers BOTH days.
    """
    return [
        PricePoint(
            hour=h,
            import_price=_IMPORT_PRICES_TOMORROW[h],
            export_price=_EXPORT_PRICES_TOMORROW[h],
        )
        for h in range(24)
    ]


def _pv_slots() -> list[SolcastSlot]:
    """Return 24 PV slots for today."""
    return [SolcastSlot(hour=h, pv_estimate=_PV_TODAY[h]) for h in range(24)]


def _pv_slots_tomorrow() -> list[SolcastSlot]:
    """Return 24 PV slots for tomorrow."""
    return [SolcastSlot(hour=h, pv_estimate=_PV_TOMORROW[h]) for h in range(24)]


def _consumption_averages() -> list[HourlyConsumptionAverage]:
    """Return 24 hourly consumption averages."""
    return [
        HourlyConsumptionAverage(
            hour=h,
            avg_1d=_CONSUMPTION[h],
            avg_3d=_CONSUMPTION[h],
            avg_7d=_CONSUMPTION[h],
            avg_14d=_CONSUMPTION[h],
        )
        for h in range(24)
    ]


def _make_48h_input(
    *,
    price_points: list[PricePoint] | None = None,
    solcast_slots: list[SolcastSlot] | None = None,
) -> PlannerInput:
    """Return a 48-hour PlannerInput covering today and tomorrow.

    Args:
        price_points: Override price data.  Defaults to 24 full-day prices
            (will cover both days since alignment is by wall-clock hour).
        solcast_slots: Override PV forecast.  Defaults to 24 full-day PV slots.

    Returns:
        Fully populated :class:`PlannerInput` with a 48-hour horizon.
    """
    return PlannerInput(
        now_iso="2024-06-15T00:00:00+02:00",
        interval_minutes=60,
        interval_length_hours=48,
        battery_soc_pct=50.0,
        battery_rated_capacity_kwh=10.0,
        battery_end_of_discharge_soc_pct=10.0,
        battery_max_soc_pct=100.0,
        battery_max_charge_power_w=5000.0,
        battery_conversion_loss_pct=10.0,
        battery_purchase_price=10_000.0,
        battery_expected_cycles=6000,
        weight_1d=25,
        weight_3d=30,
        weight_7d=30,
        weight_14d=15,
        consumption_averages=_consumption_averages(),
        price_points=(
            price_points if price_points is not None else _today_price_points()
        ),
        solcast_slots=solcast_slots if solcast_slots is not None else _pv_slots(),
        battery_schedules=[],
        excess_export_enabled=False,
        months_winter=[1, 2, 3, 4, 10, 11, 12],
        house_power_includes_ev=True,
        is_read_only=True,
    )


# ===========================================================================
# DataQuality dataclass unit tests
# ===========================================================================


class TestDataQuality:
    """Unit tests for the :class:`DataQuality` dataclass."""

    def test_default_instance_is_complete(self) -> None:
        """An empty DataQuality instance must report complete (no missing hours)."""
        dq = DataQuality()
        assert dq.is_complete is True

    def test_tomorrow_price_missing_marks_incomplete(self) -> None:
        """Setting tomorrow price missing hours must mark the quality as incomplete."""
        dq = DataQuality(tomorrow_price_missing_hours=[0, 1, 2])
        assert dq.is_complete is False
        assert dq.tomorrow_price_complete is False
        assert dq.tomorrow_pv_complete is True

    def test_tomorrow_pv_missing_marks_incomplete(self) -> None:
        """Setting tomorrow PV missing hours must mark the quality as incomplete."""
        dq = DataQuality(tomorrow_pv_missing_hours=[12, 13, 14])
        assert dq.is_complete is False
        assert dq.tomorrow_pv_complete is False
        assert dq.tomorrow_price_complete is True

    def test_as_dict_contains_expected_keys(self) -> None:
        """as_dict() must contain the required diagnostic keys."""
        dq = DataQuality(
            tomorrow_price_missing_hours=[0, 1],
            tomorrow_pv_missing_hours=[12],
            today_price_missing_hours=[],
            today_pv_missing_hours=[],
            horizon_has_tomorrow=True,
        )
        result = dq.as_dict()
        assert "is_complete" in result
        assert "horizon_has_tomorrow" in result
        assert "tomorrow_price_missing_hours" in result
        assert "tomorrow_pv_missing_hours" in result
        assert "today_price_missing_hours" in result
        assert "today_pv_missing_hours" in result

    def test_as_dict_is_sorted(self) -> None:
        """as_dict() must return sorted lists of missing hours."""
        dq = DataQuality(tomorrow_price_missing_hours=[22, 1, 5])
        result = dq.as_dict()
        assert result["tomorrow_price_missing_hours"] == [1, 5, 22]


# ===========================================================================
# TimeSeriesIndex tomorrow helpers
# ===========================================================================


class TestTimeSeriesIndexTomorrowHelpers:
    """Unit tests for the new tomorrow-specific TSI helper methods."""

    def test_has_tomorrow_slots_returns_false_for_24h_horizon(self) -> None:
        """A 24-hour horizon starting at midnight has no day_offset=1 slots."""
        from datetime import datetime

        now = datetime(2024, 6, 15, 0, 0, tzinfo=_TZ)
        tsi = TimeSeriesIndex.from_now(now, interval_minutes=60, horizon_hours=24)
        assert tsi.has_tomorrow_slots() is False

    def test_has_tomorrow_slots_returns_true_for_48h_horizon(self) -> None:
        """A 48-hour horizon starting at midnight has day_offset=1 slots."""
        from datetime import datetime

        now = datetime(2024, 6, 15, 0, 0, tzinfo=_TZ)
        tsi = TimeSeriesIndex.from_now(now, interval_minutes=60, horizon_hours=48)
        assert tsi.has_tomorrow_slots() is True

    def test_missing_tomorrow_price_hours_empty_when_fully_populated(self) -> None:
        """No missing tomorrow hours when all 24 price hours are provided."""
        from datetime import datetime

        now = datetime(2024, 6, 15, 0, 0, tzinfo=_TZ)
        tsi = TimeSeriesIndex.from_now(now, interval_minutes=60, horizon_hours=48)
        prices = {h: 0.20 for h in range(24)}
        tsi.align_hourly_prices(prices, prices)
        assert tsi.missing_tomorrow_price_hours() == set()

    def test_missing_tomorrow_price_hours_detects_partial_gap(self) -> None:
        """Partial tomorrow price data must be detected by the TSI."""
        from datetime import datetime

        now = datetime(2024, 6, 15, 0, 0, tzinfo=_TZ)
        tsi = TimeSeriesIndex.from_now(now, interval_minutes=60, horizon_hours=48)
        # Only provide prices for hours 0-11 (morning half)
        prices = {h: 0.20 for h in range(12)}
        tsi.align_hourly_prices(prices, prices)
        missing = tsi.missing_tomorrow_price_hours()
        assert 12 in missing
        assert 23 in missing
        assert 0 not in missing  # today's hour 0 is covered
        assert 11 not in missing  # today's hour 11 is covered

    def test_missing_tomorrow_pv_hours_detects_partial_gap(self) -> None:
        """Partial tomorrow PV data must be detected by the TSI."""
        from datetime import datetime

        now = datetime(2024, 6, 15, 0, 0, tzinfo=_TZ)
        tsi = TimeSeriesIndex.from_now(now, interval_minutes=60, horizon_hours=48)
        # Only provide PV for night hours (no production expected anyway)
        pv = {h: 0.0 for h in range(6)}  # only 00:00-05:00
        tsi.align_hourly_pv(pv)
        missing = tsi.missing_tomorrow_pv_hours()
        # Hours 6-23 should be flagged as missing in tomorrow
        assert 6 in missing
        assert 12 in missing
        assert 23 in missing

    def test_per_series_tracking_is_independent(self) -> None:
        """Price and PV missing sets must be tracked independently."""
        from datetime import datetime

        now = datetime(2024, 6, 15, 0, 0, tzinfo=_TZ)
        tsi = TimeSeriesIndex.from_now(now, interval_minutes=60, horizon_hours=48)
        # Full PV, partial prices
        full_pv = {h: 0.5 for h in range(24)}
        partial_prices = {h: 0.20 for h in range(12)}  # only 0-11

        tsi.align_hourly_prices(partial_prices, partial_prices)
        tsi.align_hourly_pv(full_pv)

        assert len(tsi.missing_tomorrow_price_hours()) > 0
        assert len(tsi.missing_tomorrow_pv_hours()) == 0


# ===========================================================================
# Planner engine: complete tomorrow data
# ===========================================================================


class TestCompleteTomorrowData:
    """When tomorrow data is fully populated, data_quality must report clean."""

    def test_complete_data_no_missing_inputs(self) -> None:
        """A 24-hour horizon (no tomorrow) must produce no missing tomorrow entries."""
        inp = PlannerInput(
            now_iso="2024-06-15T00:00:00+02:00",
            interval_minutes=60,
            interval_length_hours=24,
            battery_rated_capacity_kwh=10.0,
            battery_soc_pct=50.0,
            battery_end_of_discharge_soc_pct=10.0,
            battery_max_charge_power_w=5000.0,
            battery_conversion_loss_pct=10.0,
            consumption_averages=_consumption_averages(),
            price_points=_today_price_points(),
            solcast_slots=_pv_slots(),
            is_read_only=True,
        )
        result = run_planner(inp)
        # 24h horizon: no tomorrow slots, so tomorrow missing lists must be empty
        assert result.data_quality.tomorrow_price_missing_hours == []
        assert result.data_quality.tomorrow_pv_missing_hours == []
        assert result.data_quality.horizon_has_tomorrow is False

    def test_48h_complete_data_is_clean(self) -> None:
        """A 48-hour horizon with full price and PV data must report clean quality."""
        inp = _make_48h_input(
            price_points=_today_price_points(),  # same 24h data covers both days
            solcast_slots=_pv_slots(),
        )
        result = run_planner(inp)
        # 24h price/PV data covers both days by wall-clock hour alignment
        assert result.data_quality.tomorrow_price_missing_hours == []
        assert result.data_quality.tomorrow_pv_missing_hours == []
        assert result.data_quality.horizon_has_tomorrow is True
        assert result.data_quality.is_complete is True

    def test_complete_data_no_tomorrow_missing_inputs_entries(self) -> None:
        """Complete data must not add tomorrow_*_missing_hours to missing_inputs."""
        inp = _make_48h_input(
            price_points=_today_price_points(),
            solcast_slots=_pv_slots(),
        )
        result = run_planner(inp)
        tomorrow_entries = [
            m for m in result.missing_inputs if m.startswith("tomorrow_")
        ]
        assert tomorrow_entries == [], (
            f"Expected no tomorrow missing_inputs entries but got: {tomorrow_entries}"
        )


# ===========================================================================
# Planner engine: partial tomorrow price data
# ===========================================================================


class TestPartialTomorrowPriceData:
    """Partial tomorrow price data must be detected and surfaced explicitly."""

    def _make_partial_price_input(self, missing_hours: set[int]) -> PlannerInput:
        """Build a 48h input where the given hours have no price data."""
        prices = [
            PricePoint(hour=h, import_price=0.20, export_price=0.05)
            for h in range(24)
            if h not in missing_hours
        ]
        return _make_48h_input(price_points=prices, solcast_slots=_pv_slots())

    def test_missing_single_tomorrow_price_hour_surfaced(self) -> None:
        """A single missing price hour must appear in tomorrow_price_missing_hours."""
        inp = self._make_partial_price_input(missing_hours={12})
        result = run_planner(inp)
        # Hour 12 must be flagged as missing in tomorrow's price data
        assert 12 in result.data_quality.tomorrow_price_missing_hours

    def test_missing_tomorrow_prices_in_missing_inputs(self) -> None:
        """Missing tomorrow prices must produce a 'tomorrow_price_missing_hours:...' entry."""
        inp = self._make_partial_price_input(missing_hours={0, 1, 2})
        result = run_planner(inp)
        tomorrow_entries = [
            m
            for m in result.missing_inputs
            if m.startswith("tomorrow_price_missing_hours")
        ]
        assert len(tomorrow_entries) == 1, (
            f"Expected one tomorrow_price_missing_hours entry; got {result.missing_inputs}"
        )
        # The entry must include the missing hours
        entry = tomorrow_entries[0]
        assert "00" in entry
        assert "01" in entry
        assert "02" in entry

    def test_missing_tomorrow_prices_produce_warning(self) -> None:
        """Missing tomorrow prices must emit a human-readable warning."""
        inp = self._make_partial_price_input(missing_hours={6, 7, 8})
        result = run_planner(inp)
        price_warnings = [w for w in result.warnings if "tomorrow price" in w.lower()]
        assert len(price_warnings) >= 1, (
            f"Expected a warning about tomorrow price data. Got: {result.warnings}"
        )

    def test_missing_tomorrow_prices_data_quality_incomplete(self) -> None:
        """DataQuality.is_complete must be False when tomorrow prices are missing."""
        inp = self._make_partial_price_input(missing_hours={10, 11, 12})
        result = run_planner(inp)
        assert result.data_quality.is_complete is False
        assert result.data_quality.tomorrow_price_complete is False
        assert len(result.data_quality.tomorrow_price_missing_hours) > 0

    def test_partial_tomorrow_prices_does_not_crash_planner(self) -> None:
        """The planner must not raise even when half of tomorrow's prices are missing."""
        inp = self._make_partial_price_input(missing_hours=set(range(12, 24)))
        result = run_planner(inp)
        # The planner must complete and produce slots
        assert len(result.slots) == 48, (
            f"Expected 48 slots for a 48h horizon; got {len(result.slots)}"
        )

    def test_missing_tomorrow_price_is_non_critical_for_degraded_mode(self) -> None:
        """Missing tomorrow prices must produce Degraded, not Error, in degraded mode."""
        inp = self._make_partial_price_input(missing_hours={5, 6, 7})
        result = run_planner(inp)

        # Simulate how LiveState would classify these missing_inputs entries
        tomorrow_price_entries = [
            m
            for m in result.missing_inputs
            if m.startswith("tomorrow_price_missing_hours")
        ]
        # The label must NOT contain any critical keywords
        for entry in tomorrow_price_entries:
            mode = classify_degraded_mode(True, [entry])
            assert mode is DegradedMode.Degraded, (
                f"Missing tomorrow price should produce Degraded, not {mode}: {entry!r}"
            )


# ===========================================================================
# Planner engine: partial tomorrow PV data
# ===========================================================================


class TestPartialTomorrowPvData:
    """Partial tomorrow PV data must be detected and surfaced explicitly."""

    def _make_partial_pv_input(self, missing_hours: set[int]) -> PlannerInput:
        """Build a 48h input where the given hours have no PV forecast data."""
        pv = [
            SolcastSlot(hour=h, pv_estimate=_PV_TODAY[h])
            for h in range(24)
            if h not in missing_hours
        ]
        return _make_48h_input(price_points=_today_price_points(), solcast_slots=pv)

    def test_missing_single_tomorrow_pv_hour_surfaced(self) -> None:
        """A single missing PV hour must appear in tomorrow_pv_missing_hours."""
        inp = self._make_partial_pv_input(missing_hours={12})
        result = run_planner(inp)
        assert 12 in result.data_quality.tomorrow_pv_missing_hours

    def test_missing_tomorrow_pv_in_missing_inputs(self) -> None:
        """Missing tomorrow PV must produce a 'tomorrow_pv_missing_hours:...' entry."""
        inp = self._make_partial_pv_input(missing_hours={10, 11, 12, 13})
        result = run_planner(inp)
        tomorrow_entries = [
            m
            for m in result.missing_inputs
            if m.startswith("tomorrow_pv_missing_hours")
        ]
        assert len(tomorrow_entries) == 1, (
            f"Expected one tomorrow_pv_missing_hours entry; got {result.missing_inputs}"
        )
        entry = tomorrow_entries[0]
        assert "10" in entry
        assert "11" in entry
        assert "12" in entry
        assert "13" in entry

    def test_missing_tomorrow_pv_produce_warning(self) -> None:
        """Missing tomorrow PV must emit a human-readable warning."""
        inp = self._make_partial_pv_input(missing_hours={9, 10, 11, 12})
        result = run_planner(inp)
        pv_warnings = [w for w in result.warnings if "tomorrow pv" in w.lower()]
        assert len(pv_warnings) >= 1, (
            f"Expected a warning about tomorrow PV data. Got: {result.warnings}"
        )

    def test_missing_tomorrow_pv_data_quality_incomplete(self) -> None:
        """DataQuality.is_complete must be False when tomorrow PV is missing."""
        inp = self._make_partial_pv_input(missing_hours={9, 10, 11})
        result = run_planner(inp)
        assert result.data_quality.is_complete is False
        assert result.data_quality.tomorrow_pv_complete is False
        assert len(result.data_quality.tomorrow_pv_missing_hours) > 0

    def test_partial_tomorrow_pv_does_not_crash_planner(self) -> None:
        """The planner must not raise even when all of tomorrow's PV is missing."""
        inp = self._make_partial_pv_input(missing_hours=set(range(24)))
        result = run_planner(inp)
        assert len(result.slots) == 48

    def test_missing_tomorrow_pv_is_non_critical_for_degraded_mode(self) -> None:
        """Missing tomorrow PV must produce Degraded, not Error, in degraded mode."""
        inp = self._make_partial_pv_input(missing_hours={10, 11, 12})
        result = run_planner(inp)

        tomorrow_pv_entries = [
            m
            for m in result.missing_inputs
            if m.startswith("tomorrow_pv_missing_hours")
        ]
        for entry in tomorrow_pv_entries:
            mode = classify_degraded_mode(True, [entry])
            assert mode is DegradedMode.Degraded, (
                f"Missing tomorrow PV should produce Degraded, not {mode}: {entry!r}"
            )


# ===========================================================================
# Planner engine: both price and PV missing tomorrow
# ===========================================================================


class TestBothMissingTomorrow:
    """When both price and PV data are missing for tomorrow, both must be surfaced."""

    def test_both_missing_surfaces_both_diagnostics(self) -> None:
        """Both tomorrow_price and tomorrow_pv missing_inputs must appear."""
        # Provide only 12 hours of price and PV data
        prices = [
            PricePoint(hour=h, import_price=0.20, export_price=0.05) for h in range(12)
        ]
        pv = [SolcastSlot(hour=h, pv_estimate=0.5) for h in range(12)]
        inp = _make_48h_input(price_points=prices, solcast_slots=pv)
        result = run_planner(inp)

        assert not result.data_quality.tomorrow_price_complete
        assert not result.data_quality.tomorrow_pv_complete
        assert result.data_quality.is_complete is False

        price_entries = [
            m for m in result.missing_inputs if m.startswith("tomorrow_price")
        ]
        pv_entries = [m for m in result.missing_inputs if m.startswith("tomorrow_pv")]
        assert len(price_entries) == 1
        assert len(pv_entries) == 1

    def test_both_missing_produces_two_warnings(self) -> None:
        """Two separate warnings must be emitted — one per missing series."""
        prices = [
            PricePoint(hour=h, import_price=0.20, export_price=0.05) for h in range(12)
        ]
        pv = [SolcastSlot(hour=h, pv_estimate=0.5) for h in range(12)]
        inp = _make_48h_input(price_points=prices, solcast_slots=pv)
        result = run_planner(inp)

        price_warnings = [w for w in result.warnings if "tomorrow price" in w.lower()]
        pv_warnings = [w for w in result.warnings if "tomorrow pv" in w.lower()]
        assert len(price_warnings) >= 1
        assert len(pv_warnings) >= 1


# ===========================================================================
# Planner engine: zero vs missing distinction
# ===========================================================================


class TestZeroVsMissingDistinction:
    """Explicitly-provided zero PV values must NOT be flagged as missing.

    A Solcast forecast can legitimately be 0.0 kWh at night.  The planner
    must distinguish between "data not provided" and "data provided as 0".
    """

    def test_explicit_zero_pv_not_flagged_as_missing(self) -> None:
        """PV slots explicitly set to 0.0 kWh must not appear in missing lists."""
        # Provide all 24 slots including zeros (night hours)
        pv = [SolcastSlot(hour=h, pv_estimate=0.0) for h in range(24)]
        inp = _make_48h_input(
            price_points=_today_price_points(),
            solcast_slots=pv,
        )
        result = run_planner(inp)
        assert result.data_quality.tomorrow_pv_missing_hours == [], (
            "Explicit zero PV should NOT be flagged as missing data."
        )

    def test_explicit_zero_prices_not_flagged_as_missing(self) -> None:
        """Price slots explicitly set to 0.0 must not appear in missing price lists."""
        prices = [
            PricePoint(hour=h, import_price=0.0, export_price=0.0) for h in range(24)
        ]
        inp = _make_48h_input(
            price_points=prices,
            solcast_slots=_pv_slots(),
        )
        result = run_planner(inp)
        assert result.data_quality.tomorrow_price_missing_hours == [], (
            "Explicit zero prices should NOT be flagged as missing data."
        )

    def test_absent_pv_slot_flagged_as_missing(self) -> None:
        """An absent PV slot (not provided) must be flagged, even if 0 is expected."""
        # Only provide daytime hours (6-18) — night hours are absent, not zero
        pv = [SolcastSlot(hour=h, pv_estimate=_PV_TODAY[h]) for h in range(6, 19)]
        inp = _make_48h_input(
            price_points=_today_price_points(),
            solcast_slots=pv,
        )
        result = run_planner(inp)
        # Night hours 0-5 and 19-23 must be flagged as missing in tomorrow
        missing = result.data_quality.tomorrow_pv_missing_hours
        assert any(h < 6 for h in missing), (
            f"Expected early morning hours to be missing. Got: {missing}"
        )
        assert any(h >= 19 for h in missing), (
            f"Expected late night hours to be missing. Got: {missing}"
        )


# ===========================================================================
# data_quality.as_dict() round-trip
# ===========================================================================


class TestDataQualityAsDict:
    """Verify that as_dict() returns a JSON-safe structure for HA attributes."""

    def test_as_dict_on_clean_result(self) -> None:
        """A clean 24-hour run must produce a JSON-safe as_dict()."""
        inp = PlannerInput(
            now_iso="2024-06-15T00:00:00+02:00",
            interval_minutes=60,
            interval_length_hours=24,
            battery_rated_capacity_kwh=10.0,
            battery_soc_pct=50.0,
            battery_end_of_discharge_soc_pct=10.0,
            battery_max_charge_power_w=5000.0,
            battery_conversion_loss_pct=10.0,
            consumption_averages=_consumption_averages(),
            price_points=_today_price_points(),
            solcast_slots=_pv_slots(),
            is_read_only=True,
        )
        result = run_planner(inp)
        d = result.data_quality.as_dict()
        assert isinstance(d["is_complete"], bool)
        assert isinstance(d["horizon_has_tomorrow"], bool)
        assert isinstance(d["tomorrow_price_missing_hours"], list)
        assert isinstance(d["tomorrow_pv_missing_hours"], list)
        assert isinstance(d["today_price_missing_hours"], list)
        assert isinstance(d["today_pv_missing_hours"], list)

    def test_as_dict_on_partial_result_has_sorted_lists(self) -> None:
        """Missing hours in as_dict() must be sorted ascending."""
        prices = [
            PricePoint(hour=h, import_price=0.20, export_price=0.05)
            for h in [0, 5, 12, 23]  # sparse, out-of-order would confuse dict
        ]
        inp = _make_48h_input(price_points=prices, solcast_slots=_pv_slots())
        result = run_planner(inp)
        d = result.data_quality.as_dict()
        assert d["tomorrow_price_missing_hours"] == sorted(
            d["tomorrow_price_missing_hours"]
        )
