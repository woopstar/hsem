"""Tests for the shared time-series slot model (issue #286).

Acceptance criteria verified here
----------------------------------
- All planner inputs map to the same slot index.
- Missing slots are explicit (``MISSING_SENTINEL``).
- DST transitions are handled correctly: no gap/overlap in slot boundaries,
  correct slot count, all slots timezone-aware.
- ``TimeSeriesIndex.from_now`` rejects naive datetimes, bad intervals, and
  non-positive horizons.
- Slot resolution is configurable (15 min default, 30 min, 60 min).
- Alignment methods (prices, PV, load, import/export, battery SoC) all
  produce lists parallel to ``tsi.slots``.
- Sub-hourly slots receive correctly scaled energy values.
- ``slot_index_for`` correctly locates a datetime in the grid.

Timezone under test: ``Europe/Copenhagen``
  - DST forward (spring):  2024-03-31 02:00 → 03:00 (UTC+1 → UTC+2)
  - DST backward (autumn): 2024-10-27 03:00 → 02:00 (UTC+2 → UTC+1)
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.models.time_series import (
    DEFAULT_SLOT_MINUTES,
    MISSING_SENTINEL,
    SlotKey,
    SlotMeta,
    TimeSeriesIndex,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TZ = ZoneInfo("Europe/Copenhagen")
_TZ_UTC = ZoneInfo("UTC")


def _cph(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """Return a Copenhagen-aware datetime (first occurrence on DST days)."""
    return datetime(year, month, day, hour, minute, tzinfo=_TZ)


def _utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """Return a UTC-aware datetime."""
    return datetime(year, month, day, hour, minute, tzinfo=_TZ_UTC)


def _is_sentinel(val: float) -> bool:
    """Return True if val is the MISSING_SENTINEL (NaN)."""
    return math.isnan(val)


# ---------------------------------------------------------------------------
# 1. Module-level constants
# ---------------------------------------------------------------------------


class TestConstants:
    """Public constants must be accessible and have correct defaults."""

    def test_default_slot_minutes_is_15(self):
        assert DEFAULT_SLOT_MINUTES == 15

    def test_missing_sentinel_is_nan(self):
        assert math.isnan(MISSING_SENTINEL)

    def test_slot_key_is_named_tuple(self):
        key = SlotKey(day_offset=0, slot_in_day=3)
        assert key.day_offset == 0
        assert key.slot_in_day == 3
        assert key == SlotKey(0, 3)


# ---------------------------------------------------------------------------
# 2. from_now: validation
# ---------------------------------------------------------------------------


class TestFromNowValidation:
    """from_now must reject invalid arguments."""

    def test_naive_datetime_raises(self):
        naive = datetime(2024, 6, 15, 12, 0)
        with pytest.raises(ValueError, match="timezone-aware"):
            TimeSeriesIndex.from_now(naive)

    def test_zero_interval_raises(self):
        now = _cph(2024, 6, 15, 0)
        with pytest.raises(ValueError, match="interval_minutes"):
            TimeSeriesIndex.from_now(now, interval_minutes=0)

    def test_negative_interval_raises(self):
        now = _cph(2024, 6, 15, 0)
        with pytest.raises(ValueError, match="interval_minutes"):
            TimeSeriesIndex.from_now(now, interval_minutes=-15)

    def test_non_divisor_interval_raises(self):
        now = _cph(2024, 6, 15, 0)
        with pytest.raises(ValueError, match="interval_minutes"):
            TimeSeriesIndex.from_now(now, interval_minutes=7)

    def test_zero_horizon_raises(self):
        now = _cph(2024, 6, 15, 0)
        with pytest.raises(ValueError, match="horizon_hours"):
            TimeSeriesIndex.from_now(now, horizon_hours=0)

    def test_negative_horizon_raises(self):
        now = _cph(2024, 6, 15, 0)
        with pytest.raises(ValueError, match="horizon_hours"):
            TimeSeriesIndex.from_now(now, horizon_hours=-1)


# ---------------------------------------------------------------------------
# 3. from_now: slot count for various resolutions
# ---------------------------------------------------------------------------


class TestFromNowSlotCount:
    """Correct number of slots for each supported resolution."""

    def test_15_min_24h_gives_96_slots(self):
        now = _cph(2024, 6, 15, 14, 30)
        tsi = TimeSeriesIndex.from_now(now, interval_minutes=15, horizon_hours=24)
        assert len(tsi) == 96

    def test_30_min_24h_gives_48_slots(self):
        now = _cph(2024, 6, 15, 0)
        tsi = TimeSeriesIndex.from_now(now, interval_minutes=30, horizon_hours=24)
        assert len(tsi) == 48

    def test_60_min_24h_gives_24_slots(self):
        now = _cph(2024, 6, 15, 0)
        tsi = TimeSeriesIndex.from_now(now, interval_minutes=60, horizon_hours=24)
        assert len(tsi) == 24

    def test_15_min_48h_gives_192_slots(self):
        now = _cph(2024, 6, 15, 0)
        tsi = TimeSeriesIndex.from_now(now, interval_minutes=15, horizon_hours=48)
        assert len(tsi) == 192

    def test_default_resolution_is_15_minutes(self):
        """Omitting interval_minutes should default to 15-minute slots."""
        now = _cph(2024, 6, 15, 0)
        tsi = TimeSeriesIndex.from_now(now, horizon_hours=24)
        assert tsi.interval_minutes == 15
        assert len(tsi) == 96


# ---------------------------------------------------------------------------
# 4. SlotMeta: structure and correctness
# ---------------------------------------------------------------------------


class TestSlotMeta:
    """Each SlotMeta must have correct key, start/end, hour, minute, fraction."""

    def setup_method(self):
        now = _cph(2024, 6, 15, 0)
        self.tsi = TimeSeriesIndex.from_now(now, interval_minutes=15, horizon_hours=24)

    def test_first_slot_starts_at_midnight(self):
        first = self.tsi.slots[0]
        assert first.hour == 0
        assert first.minute == 0

    def test_first_slot_key_is_zero_zero(self):
        assert self.tsi.slots[0].key == SlotKey(0, 0)

    def test_slot_4_starts_at_01_00(self):
        # slots[4] is the 5th slot, which is 4 * 15 = 60 min = 01:00
        meta = self.tsi.slots[4]
        assert meta.hour == 1
        assert meta.minute == 0

    def test_slot_fraction_is_quarter(self):
        for meta in self.tsi.slots:
            assert meta.slot_fraction == pytest.approx(0.25)

    def test_all_slots_are_timezone_aware(self):
        for meta in self.tsi.slots:
            assert meta.start.tzinfo is not None, f"start naive: {meta.start}"
            assert meta.end.tzinfo is not None, f"end naive: {meta.end}"

    def test_slots_are_contiguous(self):
        for a, b in zip(self.tsi.slots, self.tsi.slots[1:]):
            assert a.end == b.start, (
                f"Gap between {a.end.isoformat()} and {b.start.isoformat()}"
            )

    def test_total_span_is_24_hours(self):
        first = self.tsi.slots[0]
        last = self.tsi.slots[-1]
        assert last.end - first.start == timedelta(hours=24)

    def test_60_min_slot_fraction_is_one(self):
        now = _cph(2024, 6, 15, 0)
        tsi = TimeSeriesIndex.from_now(now, interval_minutes=60, horizon_hours=24)
        for meta in tsi.slots:
            assert meta.slot_fraction == pytest.approx(1.0)

    def test_slot_meta_is_frozen(self):
        meta = self.tsi.slots[0]
        with pytest.raises((AttributeError, TypeError)):
            meta.hour = 99  # type: ignore[misc]  # test fixture override


# ---------------------------------------------------------------------------
# 5. DST transitions
# ---------------------------------------------------------------------------


class TestDstTransitions:
    """Slot generation must be correct on DST spring-forward and autumn-fallback days."""

    # ---- Spring forward: 2024-03-31, 02:00 → 03:00, UTC+1 → UTC+2 -----

    def test_spring_forward_96_slots_15min(self):
        now = _cph(2024, 3, 31, 0)
        tsi = TimeSeriesIndex.from_now(now, interval_minutes=15, horizon_hours=24)
        assert len(tsi) == 96

    def test_spring_forward_24_slots_60min(self):
        now = _cph(2024, 3, 31, 0)
        tsi = TimeSeriesIndex.from_now(now, interval_minutes=60, horizon_hours=24)
        assert len(tsi) == 24

    def test_spring_forward_total_span_24h(self):
        now = _cph(2024, 3, 31, 0)
        tsi = TimeSeriesIndex.from_now(now, interval_minutes=15, horizon_hours=24)
        assert tsi.slots[-1].end - tsi.slots[0].start == timedelta(hours=24)

    def test_spring_forward_all_slots_aware(self):
        now = _cph(2024, 3, 31, 0)
        tsi = TimeSeriesIndex.from_now(now, interval_minutes=15, horizon_hours=24)
        for meta in tsi:
            assert meta.start.tzinfo is not None
            assert meta.end.tzinfo is not None

    def test_spring_forward_slots_contiguous(self):
        now = _cph(2024, 3, 31, 0)
        tsi = TimeSeriesIndex.from_now(now, interval_minutes=15, horizon_hours=24)
        for a, b in zip(tsi.slots, tsi.slots[1:]):
            assert a.end == b.start

    # ---- Autumn fallback: 2024-10-27, 03:00 → 02:00, UTC+2 → UTC+1 ---

    def test_autumn_fallback_96_slots_15min(self):
        now = _cph(2024, 10, 27, 0)
        tsi = TimeSeriesIndex.from_now(now, interval_minutes=15, horizon_hours=24)
        assert len(tsi) == 96

    def test_autumn_fallback_24_slots_60min(self):
        now = _cph(2024, 10, 27, 0)
        tsi = TimeSeriesIndex.from_now(now, interval_minutes=60, horizon_hours=24)
        assert len(tsi) == 24

    def test_autumn_fallback_total_span_24h(self):
        now = _cph(2024, 10, 27, 0)
        tsi = TimeSeriesIndex.from_now(now, interval_minutes=15, horizon_hours=24)
        assert tsi.slots[-1].end - tsi.slots[0].start == timedelta(hours=24)

    def test_autumn_fallback_all_slots_aware(self):
        now = _cph(2024, 10, 27, 0)
        tsi = TimeSeriesIndex.from_now(now, interval_minutes=15, horizon_hours=24)
        for meta in tsi:
            assert meta.start.tzinfo is not None
            assert meta.end.tzinfo is not None

    def test_autumn_fallback_slots_contiguous(self):
        now = _cph(2024, 10, 27, 0)
        tsi = TimeSeriesIndex.from_now(now, interval_minutes=15, horizon_hours=24)
        for a, b in zip(tsi.slots, tsi.slots[1:]):
            assert a.end == b.start


# ---------------------------------------------------------------------------
# 6. align_hourly_prices
# ---------------------------------------------------------------------------


class TestAlignHourlyPrices:
    """Prices are broadcast to every sub-hour slot; missing hours → sentinel."""

    def setup_method(self):
        now = _cph(2024, 6, 15, 0)
        self.tsi = TimeSeriesIndex.from_now(now, interval_minutes=15, horizon_hours=24)

    def _full_price_dicts(self) -> tuple[dict[int, float], dict[int, float]]:
        """Return import/export price dicts for all 24 hours."""
        imp = {h: round(0.10 + h * 0.01, 4) for h in range(24)}
        exp = {h: round(max(imp[h] - 0.02, 0.0), 4) for h in range(24)}
        return imp, exp

    def test_lengths_match_slots(self):
        imp, exp = self._full_price_dicts()
        ai, ae = self.tsi.align_hourly_prices(imp, exp)
        assert len(ai) == len(self.tsi)
        assert len(ae) == len(self.tsi)

    def test_each_hour_has_4_identical_slots(self):
        imp, exp = self._full_price_dicts()
        ai, _ = self.tsi.align_hourly_prices(imp, exp)
        for h in range(24):
            base = h * 4
            assert ai[base] == ai[base + 1] == ai[base + 2] == ai[base + 3]

    def test_price_value_matches_input(self):
        imp = {h: float(h) for h in range(24)}
        exp = {h: 0.0 for h in range(24)}
        ai, _ = self.tsi.align_hourly_prices(imp, exp)
        for h in range(24):
            for s in range(4):
                assert ai[h * 4 + s] == pytest.approx(float(h))

    def test_missing_hour_is_sentinel(self):
        imp = {h: 0.10 for h in range(24) if h != 12}
        exp = {h: 0.08 for h in range(24) if h != 12}
        ai, ae = self.tsi.align_hourly_prices(imp, exp)
        for s in range(4):
            assert _is_sentinel(ai[12 * 4 + s])
            assert _is_sentinel(ae[12 * 4 + s])

    def test_missing_hour_added_to_missing_slots(self):
        imp = {h: 0.10 for h in range(24) if h != 5}
        exp = {h: 0.08 for h in range(24)}
        self.tsi.align_hourly_prices(imp, exp)
        missing_hours = self.tsi.missing_hours()
        assert 5 in missing_hours

    def test_no_missing_hours_when_complete(self):
        imp, exp = self._full_price_dicts()
        self.tsi.align_hourly_prices(imp, exp)
        assert not self.tsi.has_missing()


# ---------------------------------------------------------------------------
# 7. align_hourly_pv
# ---------------------------------------------------------------------------


class TestAlignHourlyPv:
    """PV energy is scaled proportionally to sub-hour slot fraction."""

    def setup_method(self):
        now = _cph(2024, 6, 15, 0)
        self.tsi = TimeSeriesIndex.from_now(now, interval_minutes=15, horizon_hours=24)

    def test_length_matches_slots(self):
        pv = {h: 2.0 for h in range(24)}
        aligned = self.tsi.align_hourly_pv(pv)
        assert len(aligned) == len(self.tsi)

    def test_slot_value_is_quarter_of_hourly(self):
        pv = {h: 4.0 for h in range(24)}
        aligned = self.tsi.align_hourly_pv(pv)
        for val in aligned:
            assert val == pytest.approx(1.0)  # 4.0 * 0.25

    def test_four_slots_sum_to_hourly(self):
        pv = {h: float(h) for h in range(24)}
        aligned = self.tsi.align_hourly_pv(pv)
        for h in range(24):
            total = sum(aligned[h * 4 : h * 4 + 4])
            assert total == pytest.approx(float(h), abs=1e-9)

    def test_missing_hour_is_sentinel(self):
        pv = {h: 1.0 for h in range(24) if h != 10}
        aligned = self.tsi.align_hourly_pv(pv)
        for s in range(4):
            assert _is_sentinel(aligned[10 * 4 + s])

    def test_night_hours_zero_produce_zero_slots(self):
        night_hours = list(range(0, 6)) + list(range(22, 24))
        pv = {h: (0.0 if h in night_hours else 2.0) for h in range(24)}
        aligned = self.tsi.align_hourly_pv(pv)
        for h in night_hours:
            for s in range(4):
                assert aligned[h * 4 + s] == pytest.approx(0.0)

    def test_60min_slot_fraction_is_one(self):
        now = _cph(2024, 6, 15, 0)
        tsi = TimeSeriesIndex.from_now(now, interval_minutes=60, horizon_hours=24)
        pv = {h: 3.6 for h in range(24)}
        aligned = tsi.align_hourly_pv(pv)
        for val in aligned:
            assert val == pytest.approx(3.6)


# ---------------------------------------------------------------------------
# 8. align_hourly_load
# ---------------------------------------------------------------------------


class TestAlignHourlyLoad:
    """Load energy is scaled proportionally; mirrors PV alignment logic."""

    def setup_method(self):
        now = _cph(2024, 6, 15, 0)
        self.tsi = TimeSeriesIndex.from_now(now, interval_minutes=15, horizon_hours=24)

    def test_length_matches_slots(self):
        load = {h: 0.5 for h in range(24)}
        aligned = self.tsi.align_hourly_load(load)
        assert len(aligned) == len(self.tsi)

    def test_slot_value_is_quarter_of_hourly(self):
        load = {h: 0.8 for h in range(24)}
        aligned = self.tsi.align_hourly_load(load)
        for val in aligned:
            assert val == pytest.approx(0.2)  # 0.8 * 0.25

    def test_missing_hour_is_sentinel(self):
        load = {h: 0.5 for h in range(24) if h != 3}
        aligned = self.tsi.align_hourly_load(load)
        for s in range(4):
            assert _is_sentinel(aligned[3 * 4 + s])

    def test_missing_hour_tracked_in_missing_slots(self):
        load = {h: 0.5 for h in range(24) if h != 7}
        self.tsi.align_hourly_load(load)
        assert 7 in self.tsi.missing_hours()


# ---------------------------------------------------------------------------
# 9. align_net_import_export
# ---------------------------------------------------------------------------


class TestAlignNetImportExport:
    """Grid import/export energy values are scaled to slot fractions."""

    def setup_method(self):
        now = _cph(2024, 6, 15, 0)
        self.tsi = TimeSeriesIndex.from_now(now, interval_minutes=15, horizon_hours=24)

    def test_lengths_match_slots(self):
        imp = {h: 1.0 for h in range(24)}
        exp = {h: 0.0 for h in range(24)}
        ai, ae = self.tsi.align_net_import_export(imp, exp)
        assert len(ai) == len(self.tsi)
        assert len(ae) == len(self.tsi)

    def test_values_scaled_to_slot_fraction(self):
        imp = {h: 2.0 for h in range(24)}
        exp = {h: 1.0 for h in range(24)}
        ai, ae = self.tsi.align_net_import_export(imp, exp)
        for i_val, e_val in zip(ai, ae):
            assert i_val == pytest.approx(0.5)  # 2.0 * 0.25
            assert e_val == pytest.approx(0.25)  # 1.0 * 0.25

    def test_missing_export_is_sentinel(self):
        imp = {h: 0.5 for h in range(24)}
        exp = {h: 0.0 for h in range(24) if h != 20}
        _, ae = self.tsi.align_net_import_export(imp, exp)
        for s in range(4):
            assert _is_sentinel(ae[20 * 4 + s])


# ---------------------------------------------------------------------------
# 10. align_battery_soc
# ---------------------------------------------------------------------------


class TestAlignBatterySoc:
    """Battery SoC is a state (not scaled); missing hours → sentinel."""

    def setup_method(self):
        now = _cph(2024, 6, 15, 0)
        self.tsi = TimeSeriesIndex.from_now(now, interval_minutes=15, horizon_hours=24)

    def test_length_matches_slots(self):
        soc = {h: 80.0 for h in range(24)}
        aligned = self.tsi.align_battery_soc(soc)
        assert len(aligned) == len(self.tsi)

    def test_value_is_not_scaled(self):
        soc = {h: 75.0 for h in range(24)}
        aligned = self.tsi.align_battery_soc(soc)
        for val in aligned:
            assert val == pytest.approx(75.0)

    def test_all_4_sub_slots_same_value(self):
        soc = {h: float(h * 4) for h in range(24)}
        aligned = self.tsi.align_battery_soc(soc)
        for h in range(24):
            base = h * 4
            for s in range(1, 4):
                assert aligned[base] == aligned[base + s]

    def test_missing_hour_is_sentinel(self):
        soc = {h: 50.0 for h in range(24) if h != 8}
        aligned = self.tsi.align_battery_soc(soc)
        for s in range(4):
            assert _is_sentinel(aligned[8 * 4 + s])


# ---------------------------------------------------------------------------
# 11. All series aligned together: index consistency
# ---------------------------------------------------------------------------


class TestMultiSeriesAlignment:
    """Aligning all series at once produces parallel, consistent lists."""

    def test_all_series_same_length(self):
        now = _cph(2024, 6, 15, 0)
        tsi = TimeSeriesIndex.from_now(now, interval_minutes=15, horizon_hours=24)

        imp_p = {h: 0.10 for h in range(24)}
        exp_p = {h: 0.08 for h in range(24)}
        pv = {h: 2.0 for h in range(24)}
        load = {h: 0.5 for h in range(24)}
        grid_imp = {h: 0.2 for h in range(24)}
        grid_exp = {h: 0.0 for h in range(24)}
        soc = {h: 60.0 for h in range(24)}

        ai, ae = tsi.align_hourly_prices(imp_p, exp_p)
        apv = tsi.align_hourly_pv(pv)
        aload = tsi.align_hourly_load(load)
        a_grid_imp, a_grid_exp = tsi.align_net_import_export(grid_imp, grid_exp)
        asoc = tsi.align_battery_soc(soc)

        n = len(tsi)
        assert len(ai) == n
        assert len(ae) == n
        assert len(apv) == n
        assert len(aload) == n
        assert len(a_grid_imp) == n
        assert len(a_grid_exp) == n
        assert len(asoc) == n

    def test_missing_aggregation_across_series(self):
        """Missing slot set accumulates across multiple alignment calls."""
        now = _cph(2024, 6, 15, 0)
        tsi = TimeSeriesIndex.from_now(now, interval_minutes=15, horizon_hours=24)

        # hour 5 missing from prices
        imp_p = {h: 0.10 for h in range(24) if h != 5}
        exp_p = {h: 0.08 for h in range(24)}
        # hour 10 missing from PV
        pv = {h: 2.0 for h in range(24) if h != 10}

        tsi.align_hourly_prices(imp_p, exp_p)
        tsi.align_hourly_pv(pv)

        missing = tsi.missing_hours()
        assert 5 in missing
        assert 10 in missing
        assert tsi.has_missing()


# ---------------------------------------------------------------------------
# 12. slot_index_for
# ---------------------------------------------------------------------------


class TestSlotIndexFor:
    """slot_index_for must locate datetimes correctly and handle edge cases."""

    def setup_method(self):
        now = _cph(2024, 6, 15, 0)
        self.tsi = TimeSeriesIndex.from_now(now, interval_minutes=15, horizon_hours=24)

    def test_midnight_is_slot_zero(self):
        dt = _cph(2024, 6, 15, 0, 0)
        assert self.tsi.slot_index_for(dt) == 0

    def test_00_15_is_slot_one(self):
        dt = _cph(2024, 6, 15, 0, 15)
        assert self.tsi.slot_index_for(dt) == 1

    def test_01_00_is_slot_four(self):
        dt = _cph(2024, 6, 15, 1, 0)
        assert self.tsi.slot_index_for(dt) == 4

    def test_23_45_is_last_slot(self):
        dt = _cph(2024, 6, 15, 23, 45)
        assert self.tsi.slot_index_for(dt) == 95

    def test_outside_horizon_returns_none(self):
        dt = _cph(2024, 6, 16, 0, 0)  # midnight next day (= end of last slot)
        assert self.tsi.slot_index_for(dt) is None

    def test_naive_datetime_raises(self):
        naive = datetime(2024, 6, 15, 1, 0)
        with pytest.raises(ValueError, match="timezone-aware"):
            self.tsi.slot_index_for(naive)

    def test_utc_datetime_finds_correct_slot(self):
        """A UTC-aware datetime in the same instant as a Copenhagen slot is found."""
        # 2024-06-15 01:00 CEST = 2024-06-14 23:00 UTC
        dt_utc = _utc(2024, 6, 14, 23, 0)
        idx = self.tsi.slot_index_for(dt_utc)
        assert idx == 4  # slot at 01:00 CEST


# ---------------------------------------------------------------------------
# 13. Dunder methods and repr
# ---------------------------------------------------------------------------


class TestDunderMethods:
    """__len__, __iter__, and __repr__ must behave correctly."""

    def test_len_returns_slot_count(self):
        now = _cph(2024, 6, 15, 0)
        tsi = TimeSeriesIndex.from_now(now, interval_minutes=15, horizon_hours=24)
        assert len(tsi) == 96

    def test_iter_yields_slot_meta(self):
        now = _cph(2024, 6, 15, 0)
        tsi = TimeSeriesIndex.from_now(now, interval_minutes=15, horizon_hours=24)
        items = list(tsi)
        assert len(items) == 96
        assert all(isinstance(m, SlotMeta) for m in items)

    def test_repr_contains_interval_and_slot_count(self):
        now = _cph(2024, 6, 15, 0)
        tsi = TimeSeriesIndex.from_now(now, interval_minutes=15, horizon_hours=24)
        r = repr(tsi)
        assert "96" in r
        assert "15" in r

    def test_missing_slots_empty_initially(self):
        now = _cph(2024, 6, 15, 0)
        tsi = TimeSeriesIndex.from_now(now, interval_minutes=15, horizon_hours=24)
        assert not tsi.has_missing()
        assert tsi.missing_slots == set()
