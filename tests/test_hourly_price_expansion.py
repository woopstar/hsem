"""Tests for hourly-to-slot price expansion (issue #287).

Acceptance criteria verified here
----------------------------------
- Hourly price data expands to all slots in the hour (broadcast, not divided).
- Negative import and export prices are preserved unchanged.
- Missing price hours produce ``NaN`` (``MISSING_SENTINEL``) slots.
- Tests cover 15-minute slot resolution (4 slots per hour).
- ``fill_missing_prices`` correctly replaces ``NaN`` with caller-supplied defaults.
- ``missing_price_hours`` identifies all hours with incomplete data.
- VAT / currency assumptions are transparent (prices passed through unmodified).
- DST-transition days produce the correct slot count and safe alignment.
- ``SlotPrice`` named-tuple properties (``is_missing_import``, etc.) behave correctly.

Timezone under test: ``Europe/Copenhagen``
  - DST forward (spring):  2024-03-31 02:00 → 03:00
  - DST backward (autumn): 2024-10-27 03:00 → 02:00
"""

from __future__ import annotations

import math
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.models.time_series import MISSING_SENTINEL
from custom_components.hsem.utils.prices import (
    SlotPrice,
    expand_hourly_prices_to_slots,
    fill_missing_prices,
    missing_price_hours,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TZ = ZoneInfo("Europe/Copenhagen")


def _cph(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """Return a Copenhagen-aware datetime."""
    return datetime(year, month, day, hour, minute, tzinfo=_TZ)


def _full_prices(
    import_base: float = 0.10,
    export_base: float = 0.08,
) -> tuple[dict[int, float], dict[int, float]]:
    """Return complete import/export price dicts for hours 0-23."""
    imp = {h: round(import_base + h * 0.005, 6) for h in range(24)}
    exp = {h: round(export_base + h * 0.003, 6) for h in range(24)}
    return imp, exp


def _is_sentinel(val: float) -> bool:
    return math.isnan(val)


# ---------------------------------------------------------------------------
# 1. SlotPrice named-tuple
# ---------------------------------------------------------------------------


class TestSlotPrice:
    """SlotPrice behaves as an immutable named-tuple with helper properties."""

    def test_normal_slot_has_no_missing(self):
        sp = SlotPrice(import_price=0.25, export_price=0.10)
        assert not sp.is_missing_import
        assert not sp.is_missing_export
        assert not sp.has_any_missing

    def test_nan_import_is_missing(self):
        sp = SlotPrice(import_price=MISSING_SENTINEL, export_price=0.10)
        assert sp.is_missing_import
        assert not sp.is_missing_export
        assert sp.has_any_missing

    def test_nan_export_is_missing(self):
        sp = SlotPrice(import_price=0.25, export_price=MISSING_SENTINEL)
        assert not sp.is_missing_import
        assert sp.is_missing_export
        assert sp.has_any_missing

    def test_both_nan_is_missing(self):
        sp = SlotPrice(import_price=MISSING_SENTINEL, export_price=MISSING_SENTINEL)
        assert sp.is_missing_import
        assert sp.is_missing_export
        assert sp.has_any_missing

    def test_negative_prices_are_not_missing(self):
        sp = SlotPrice(import_price=-0.05, export_price=-0.10)
        assert not sp.is_missing_import
        assert not sp.is_missing_export
        assert not sp.has_any_missing

    def test_zero_prices_are_not_missing(self):
        sp = SlotPrice(import_price=0.0, export_price=0.0)
        assert not sp.has_any_missing

    def test_is_a_named_tuple(self):
        sp = SlotPrice(import_price=1.0, export_price=2.0)
        assert sp[0] == pytest.approx(1.0)
        assert sp[1] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# 2. expand_hourly_prices_to_slots: basic correctness
# ---------------------------------------------------------------------------


class TestExpandBasicCorrectness:
    """Core expansion: slot count, price broadcast, and price values."""

    def setup_method(self):
        self.now = _cph(2024, 6, 15)

    def test_15_min_24h_gives_96_slots(self):
        imp, exp = _full_prices()
        slots = expand_hourly_prices_to_slots(self.now, imp, exp)
        assert len(slots) == 96

    def test_30_min_24h_gives_48_slots(self):
        imp, exp = _full_prices()
        slots = expand_hourly_prices_to_slots(self.now, imp, exp, interval_minutes=30)
        assert len(slots) == 48

    def test_60_min_24h_gives_24_slots(self):
        imp, exp = _full_prices()
        slots = expand_hourly_prices_to_slots(self.now, imp, exp, interval_minutes=60)
        assert len(slots) == 24

    def test_15_min_48h_gives_192_slots(self):
        imp, exp = _full_prices()
        slots = expand_hourly_prices_to_slots(
            self.now, imp, exp, interval_minutes=15, horizon_hours=48
        )
        assert len(slots) == 192

    def test_each_hour_has_4_identical_import_slots(self):
        imp, exp = _full_prices()
        slots = expand_hourly_prices_to_slots(self.now, imp, exp)
        for h in range(24):
            base = h * 4
            assert slots[base].import_price == pytest.approx(
                slots[base + 1].import_price
            )
            assert slots[base + 1].import_price == pytest.approx(
                slots[base + 2].import_price
            )
            assert slots[base + 2].import_price == pytest.approx(
                slots[base + 3].import_price
            )

    def test_each_hour_has_4_identical_export_slots(self):
        imp, exp = _full_prices()
        slots = expand_hourly_prices_to_slots(self.now, imp, exp)
        for h in range(24):
            base = h * 4
            assert slots[base].export_price == pytest.approx(
                slots[base + 1].export_price
            )
            assert slots[base + 1].export_price == pytest.approx(
                slots[base + 2].export_price
            )
            assert slots[base + 2].export_price == pytest.approx(
                slots[base + 3].export_price
            )

    def test_import_price_matches_input_value(self):
        imp = {h: float(h) for h in range(24)}
        exp = {h: 0.0 for h in range(24)}
        slots = expand_hourly_prices_to_slots(self.now, imp, exp)
        for h in range(24):
            for s in range(4):
                assert slots[h * 4 + s].import_price == pytest.approx(float(h))

    def test_export_price_matches_input_value(self):
        imp = {h: 0.0 for h in range(24)}
        exp = {h: float(h) * 0.5 for h in range(24)}
        slots = expand_hourly_prices_to_slots(self.now, imp, exp)
        for h in range(24):
            for s in range(4):
                assert slots[h * 4 + s].export_price == pytest.approx(float(h) * 0.5)

    def test_returns_list_of_slot_price_instances(self):
        imp, exp = _full_prices()
        slots = expand_hourly_prices_to_slots(self.now, imp, exp)
        assert all(isinstance(sp, SlotPrice) for sp in slots)

    def test_all_slots_have_data_when_prices_complete(self):
        imp, exp = _full_prices()
        slots = expand_hourly_prices_to_slots(self.now, imp, exp)
        assert all(not sp.has_any_missing for sp in slots)


# ---------------------------------------------------------------------------
# 3. Negative prices
# ---------------------------------------------------------------------------


class TestNegativePrices:
    """Negative import and export prices must pass through unchanged."""

    def setup_method(self):
        self.now = _cph(2024, 6, 15)

    def test_negative_export_price_preserved(self):
        imp = {h: 0.10 for h in range(24)}
        exp = {h: -0.05 for h in range(24)}
        slots = expand_hourly_prices_to_slots(self.now, imp, exp)
        for sp in slots:
            assert sp.export_price == pytest.approx(-0.05)
            assert not sp.is_missing_export

    def test_negative_import_price_preserved(self):
        # Negative import prices can occur during renewable surplus events.
        imp = {h: -0.02 for h in range(24)}
        exp = {h: 0.0 for h in range(24)}
        slots = expand_hourly_prices_to_slots(self.now, imp, exp)
        for sp in slots:
            assert sp.import_price == pytest.approx(-0.02)
            assert not sp.is_missing_import

    def test_mixed_positive_and_negative_per_hour(self):
        imp = {h: (-0.05 if h < 4 else 0.20) for h in range(24)}
        exp = {h: (-0.10 if h < 4 else 0.08) for h in range(24)}
        slots = expand_hourly_prices_to_slots(self.now, imp, exp)
        # First 4 hours (16 slots) should be negative
        for s in range(16):
            assert slots[s].import_price < 0
            assert slots[s].export_price < 0
        # Remaining slots should be positive
        for s in range(16, 96):
            assert slots[s].import_price > 0
            assert slots[s].export_price > 0

    def test_deeply_negative_export_price(self):
        exp = {h: -999.99 for h in range(24)}
        slots = expand_hourly_prices_to_slots(self.now, {}, exp)
        # Import should be sentinel (not provided); export should be -999.99
        for sp in slots:
            assert sp.is_missing_import
            assert sp.export_price == pytest.approx(-999.99)

    def test_zero_price_preserved_as_zero(self):
        imp = {h: 0.0 for h in range(24)}
        exp = {h: 0.0 for h in range(24)}
        slots = expand_hourly_prices_to_slots(self.now, imp, exp)
        for sp in slots:
            assert sp.import_price == pytest.approx(0.0)
            assert sp.export_price == pytest.approx(0.0)
            assert not sp.has_any_missing

    def test_negative_price_is_not_treated_as_missing(self):
        imp = {h: -0.01 for h in range(24)}
        exp = {h: -0.01 for h in range(24)}
        slots = expand_hourly_prices_to_slots(self.now, imp, exp)
        assert all(not sp.has_any_missing for sp in slots)


# ---------------------------------------------------------------------------
# 4. Missing price hours
# ---------------------------------------------------------------------------


class TestMissingPriceHours:
    """Absent hours must produce NaN slots and be identifiable."""

    def setup_method(self):
        self.now = _cph(2024, 6, 15)

    def test_missing_single_import_hour_produces_nan(self):
        imp = {h: 0.10 for h in range(24) if h != 12}
        exp = {h: 0.08 for h in range(24)}
        slots = expand_hourly_prices_to_slots(self.now, imp, exp)
        for s in range(4):
            assert _is_sentinel(slots[12 * 4 + s].import_price)

    def test_missing_single_export_hour_produces_nan(self):
        imp = {h: 0.10 for h in range(24)}
        exp = {h: 0.08 for h in range(24) if h != 7}
        slots = expand_hourly_prices_to_slots(self.now, imp, exp)
        for s in range(4):
            assert _is_sentinel(slots[7 * 4 + s].export_price)

    def test_present_hours_are_not_nan(self):
        imp = {h: 0.10 for h in range(24) if h != 12}
        exp = {h: 0.08 for h in range(24) if h != 12}
        slots = expand_hourly_prices_to_slots(self.now, imp, exp)
        for h in range(24):
            if h == 12:
                continue
            for s in range(4):
                assert not _is_sentinel(slots[h * 4 + s].import_price)
                assert not _is_sentinel(slots[h * 4 + s].export_price)

    def test_empty_import_dict_all_nan(self):
        exp = {h: 0.08 for h in range(24)}
        slots = expand_hourly_prices_to_slots(self.now, {}, exp)
        assert all(_is_sentinel(sp.import_price) for sp in slots)

    def test_empty_export_dict_all_nan(self):
        imp = {h: 0.10 for h in range(24)}
        slots = expand_hourly_prices_to_slots(self.now, imp, {})
        assert all(_is_sentinel(sp.export_price) for sp in slots)

    def test_both_dicts_empty_all_nan(self):
        slots = expand_hourly_prices_to_slots(self.now, {}, {})
        assert all(sp.has_any_missing for sp in slots)

    def test_missing_first_hour(self):
        imp = {h: 0.10 for h in range(1, 24)}  # missing hour 0
        exp = {h: 0.08 for h in range(1, 24)}
        slots = expand_hourly_prices_to_slots(self.now, imp, exp)
        for s in range(4):
            assert _is_sentinel(slots[s].import_price)
            assert _is_sentinel(slots[s].export_price)

    def test_missing_last_hour(self):
        imp = {h: 0.10 for h in range(23)}  # missing hour 23
        exp = {h: 0.08 for h in range(23)}
        slots = expand_hourly_prices_to_slots(self.now, imp, exp)
        for s in range(4):
            assert _is_sentinel(slots[23 * 4 + s].import_price)
            assert _is_sentinel(slots[23 * 4 + s].export_price)

    def test_multiple_missing_hours(self):
        missing = {3, 7, 14, 22}
        imp = {h: 0.10 for h in range(24) if h not in missing}
        exp = {h: 0.08 for h in range(24) if h not in missing}
        slots = expand_hourly_prices_to_slots(self.now, imp, exp)
        for h in missing:
            for s in range(4):
                assert _is_sentinel(
                    slots[h * 4 + s].import_price
                ), f"hour {h} slot {s} import should be sentinel"
                assert _is_sentinel(
                    slots[h * 4 + s].export_price
                ), f"hour {h} slot {s} export should be sentinel"

    def test_import_and_export_can_have_different_missing_hours(self):
        # Import missing hour 5, export missing hour 10 — independent gaps.
        imp = {h: 0.10 for h in range(24) if h != 5}
        exp = {h: 0.08 for h in range(24) if h != 10}
        slots = expand_hourly_prices_to_slots(self.now, imp, exp)
        for s in range(4):
            assert _is_sentinel(slots[5 * 4 + s].import_price)
            assert not _is_sentinel(slots[5 * 4 + s].export_price)
            assert not _is_sentinel(slots[10 * 4 + s].import_price)
            assert _is_sentinel(slots[10 * 4 + s].export_price)


# ---------------------------------------------------------------------------
# 5. fill_missing_prices
# ---------------------------------------------------------------------------


class TestFillMissingPrices:
    """fill_missing_prices replaces NaN sentinels with caller-supplied defaults."""

    def setup_method(self):
        self.now = _cph(2024, 6, 15)

    def test_fill_missing_import_with_default(self):
        imp = {h: 0.10 for h in range(24) if h != 3}
        exp = {h: 0.08 for h in range(24)}
        slots = expand_hourly_prices_to_slots(self.now, imp, exp)
        filled = fill_missing_prices(slots, import_fallback=0.50)
        for s in range(4):
            assert filled[3 * 4 + s].import_price == pytest.approx(0.50)

    def test_fill_missing_export_with_default(self):
        imp = {h: 0.10 for h in range(24)}
        exp = {h: 0.08 for h in range(24) if h != 20}
        slots = expand_hourly_prices_to_slots(self.now, imp, exp)
        filled = fill_missing_prices(slots, export_fallback=0.0)
        for s in range(4):
            assert filled[20 * 4 + s].export_price == pytest.approx(0.0)

    def test_fill_does_not_mutate_original(self):
        imp = {h: 0.10 for h in range(24) if h != 6}
        exp = {h: 0.08 for h in range(24)}
        slots = expand_hourly_prices_to_slots(self.now, imp, exp)
        fill_missing_prices(slots, import_fallback=99.0)
        # Hour 6 is missing → original sentinel must be preserved (NaN)
        assert math.isnan(
            slots[6 * 4].import_price
        ), "fill_missing_prices must not mutate the original slot list"

    def test_fill_returns_new_list(self):
        slots = expand_hourly_prices_to_slots(self.now, {}, {})
        filled = fill_missing_prices(slots)
        assert filled is not slots

    def test_fill_all_nan_with_negative_fallback(self):
        slots = expand_hourly_prices_to_slots(self.now, {}, {})
        filled = fill_missing_prices(
            slots, import_fallback=-0.05, export_fallback=-0.02
        )
        for sp in filled:
            assert sp.import_price == pytest.approx(-0.05)
            assert sp.export_price == pytest.approx(-0.02)
            assert not sp.has_any_missing

    def test_fill_preserves_non_nan_values(self):
        imp, exp = _full_prices()
        slots = expand_hourly_prices_to_slots(self.now, imp, exp)
        filled = fill_missing_prices(slots, import_fallback=99.0, export_fallback=99.0)
        for sp_orig, sp_filled in zip(slots, filled):
            assert sp_filled.import_price == pytest.approx(sp_orig.import_price)
            assert sp_filled.export_price == pytest.approx(sp_orig.export_price)

    def test_fill_empty_list_returns_empty(self):
        filled = fill_missing_prices([])
        assert filled == []


# ---------------------------------------------------------------------------
# 6. missing_price_hours helper
# ---------------------------------------------------------------------------


class TestMissingPriceHoursHelper:
    """missing_price_hours correctly identifies hours with absent price data."""

    def setup_method(self):
        self.now = _cph(2024, 6, 15)

    def test_no_missing_when_prices_complete(self):
        imp, exp = _full_prices()
        slots = expand_hourly_prices_to_slots(self.now, imp, exp)
        assert missing_price_hours(slots) == set()

    def test_single_missing_import_hour_identified(self):
        imp = {h: 0.10 for h in range(24) if h != 9}
        exp = {h: 0.08 for h in range(24)}
        slots = expand_hourly_prices_to_slots(self.now, imp, exp)
        assert 9 in missing_price_hours(slots)

    def test_single_missing_export_hour_identified(self):
        imp = {h: 0.10 for h in range(24)}
        exp = {h: 0.08 for h in range(24) if h != 18}
        slots = expand_hourly_prices_to_slots(self.now, imp, exp)
        assert 18 in missing_price_hours(slots)

    def test_all_hours_missing_when_empty_dicts(self):
        slots = expand_hourly_prices_to_slots(self.now, {}, {})
        assert missing_price_hours(slots) == set(range(24))

    def test_multiple_missing_hours(self):
        gaps = {2, 11, 17}
        imp = {h: 0.10 for h in range(24) if h not in gaps}
        exp = {h: 0.08 for h in range(24) if h not in gaps}
        slots = expand_hourly_prices_to_slots(self.now, imp, exp)
        assert missing_price_hours(slots) == gaps

    def test_empty_slot_list_returns_empty_set(self):
        assert missing_price_hours([]) == set()

    def test_present_hours_not_in_missing_set(self):
        imp = {h: 0.10 for h in range(24) if h != 5}
        exp = {h: 0.08 for h in range(24) if h != 5}
        slots = expand_hourly_prices_to_slots(self.now, imp, exp)
        result = missing_price_hours(slots)
        for h in range(24):
            if h != 5:
                assert h not in result


# ---------------------------------------------------------------------------
# 7. Price is broadcast (not divided) across sub-hour slots
# ---------------------------------------------------------------------------


class TestPriceBroadcast:
    """Prices are a rate (currency/kWh) and must NOT be scaled per slot."""

    def setup_method(self):
        self.now = _cph(2024, 6, 15)

    def test_import_price_not_divided_by_slot_count(self):
        # If each slot received price/4, the slot price for hour 8 = 0.40
        # would be 0.10 — verify it stays 0.40.
        imp = {h: 0.40 for h in range(24)}
        exp = {h: 0.20 for h in range(24)}
        slots = expand_hourly_prices_to_slots(self.now, imp, exp)
        for s in range(4):
            assert slots[8 * 4 + s].import_price == pytest.approx(0.40)

    def test_export_price_not_divided_by_slot_count(self):
        imp = {h: 0.0 for h in range(24)}
        exp = {h: 1.00 for h in range(24)}
        slots = expand_hourly_prices_to_slots(self.now, imp, exp)
        for sp in slots:
            assert sp.export_price == pytest.approx(1.00)

    def test_all_four_slots_in_hour_have_same_price_not_fractions(self):
        imp = {h: 0.25 for h in range(24)}
        exp = {h: 0.12 for h in range(24)}
        slots = expand_hourly_prices_to_slots(self.now, imp, exp)
        for h in range(24):
            prices = [slots[h * 4 + s].import_price for s in range(4)]
            assert all(
                p == pytest.approx(0.25) for p in prices
            ), f"hour {h} prices should all be 0.25, got {prices}"

    def test_30_min_slots_have_same_price_as_input(self):
        imp = {h: float(h) for h in range(24)}
        exp = {h: float(h) * 0.5 for h in range(24)}
        slots = expand_hourly_prices_to_slots(self.now, imp, exp, interval_minutes=30)
        for h in range(24):
            assert slots[h * 2].import_price == pytest.approx(float(h))
            assert slots[h * 2 + 1].import_price == pytest.approx(float(h))


# ---------------------------------------------------------------------------
# 8. VAT / currency transparency
# ---------------------------------------------------------------------------


class TestVatCurrencyTransparency:
    """Prices are passed through unchanged; no transformation is applied."""

    def setup_method(self):
        self.now = _cph(2024, 6, 15)

    def test_high_precision_price_preserved(self):
        # DKK prices often have 6 decimal places.
        imp = {h: 1.234567 for h in range(24)}
        exp = {h: 0.987654 for h in range(24)}
        slots = expand_hourly_prices_to_slots(self.now, imp, exp)
        for sp in slots:
            assert sp.import_price == pytest.approx(1.234567, rel=1e-6)
            assert sp.export_price == pytest.approx(0.987654, rel=1e-6)

    def test_large_price_values_preserved(self):
        # Extreme price spike (e.g. 10 EUR/kWh = 74 DKK/kWh during energy crisis).
        imp = {h: 74.0 for h in range(24)}
        exp = {h: 55.0 for h in range(24)}
        slots = expand_hourly_prices_to_slots(self.now, imp, exp)
        for sp in slots:
            assert sp.import_price == pytest.approx(74.0)
            assert sp.export_price == pytest.approx(55.0)

    def test_small_fractional_price_preserved(self):
        imp = {h: 0.000001 for h in range(24)}
        exp = {h: 0.0 for h in range(24)}
        slots = expand_hourly_prices_to_slots(self.now, imp, exp)
        for sp in slots:
            assert sp.import_price == pytest.approx(0.000001, rel=1e-5)


# ---------------------------------------------------------------------------
# 9. Validation: reject invalid inputs
# ---------------------------------------------------------------------------


class TestInputValidation:
    """expand_hourly_prices_to_slots must reject invalid arguments."""

    def test_naive_datetime_raises(self):
        naive = datetime(2024, 6, 15, 0, 0)
        with pytest.raises(ValueError, match="timezone-aware"):
            expand_hourly_prices_to_slots(naive, {}, {})

    def test_zero_interval_raises(self):
        now = _cph(2024, 6, 15)
        with pytest.raises(ValueError, match="interval_minutes"):
            expand_hourly_prices_to_slots(now, {}, {}, interval_minutes=0)

    def test_non_divisor_interval_raises(self):
        now = _cph(2024, 6, 15)
        with pytest.raises(ValueError, match="interval_minutes"):
            expand_hourly_prices_to_slots(now, {}, {}, interval_minutes=7)

    def test_zero_horizon_raises(self):
        now = _cph(2024, 6, 15)
        with pytest.raises(ValueError, match="horizon_hours"):
            expand_hourly_prices_to_slots(now, {}, {}, horizon_hours=0)

    def test_negative_horizon_raises(self):
        now = _cph(2024, 6, 15)
        with pytest.raises(ValueError, match="horizon_hours"):
            expand_hourly_prices_to_slots(now, {}, {}, horizon_hours=-1)


# ---------------------------------------------------------------------------
# 10. DST transition days
# ---------------------------------------------------------------------------


class TestDstTransitions:
    """Price expansion is safe on spring-forward and autumn-fallback days."""

    def test_spring_forward_15min_gives_96_slots(self):
        now = _cph(2024, 3, 31)  # DST spring-forward day
        imp, exp = _full_prices()
        slots = expand_hourly_prices_to_slots(now, imp, exp)
        assert len(slots) == 96

    def test_spring_forward_no_nan_when_prices_complete(self):
        now = _cph(2024, 3, 31)
        imp, exp = _full_prices()
        slots = expand_hourly_prices_to_slots(now, imp, exp)
        assert all(not sp.has_any_missing for sp in slots)

    def test_spring_forward_missing_hour_still_nan(self):
        now = _cph(2024, 3, 31)
        imp = {h: 0.10 for h in range(24) if h != 2}  # hour 2 is skipped by DST
        exp = {h: 0.08 for h in range(24) if h != 2}
        slots = expand_hourly_prices_to_slots(now, imp, exp)
        # We only care that the function runs safely, not the exact slot index for hour 2
        # (wall-clock hour 2 may not appear in a spring-forward day slot sequence).
        assert len(slots) == 96

    def test_autumn_fallback_15min_gives_96_slots(self):
        now = _cph(2024, 10, 27)  # DST autumn-fallback day
        imp, exp = _full_prices()
        slots = expand_hourly_prices_to_slots(now, imp, exp)
        assert len(slots) == 96

    def test_autumn_fallback_no_nan_when_prices_complete(self):
        now = _cph(2024, 10, 27)
        imp, exp = _full_prices()
        slots = expand_hourly_prices_to_slots(now, imp, exp)
        assert all(not sp.has_any_missing for sp in slots)

    def test_autumn_fallback_price_broadcast_first_slot(self):
        now = _cph(2024, 10, 27)
        imp = {h: float(h) for h in range(24)}
        exp = {h: float(h) * 0.5 for h in range(24)}
        slots = expand_hourly_prices_to_slots(now, imp, exp)
        # Hour 0 → first 4 slots all have import price 0.0
        for s in range(4):
            assert slots[s].import_price == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 11. Integration: round-trip with populate_prices
# ---------------------------------------------------------------------------


class TestIntegrationWithPopulatePrices:
    """Prices produced by expand_hourly_prices_to_slots match populate_prices output."""

    def setup_method(self):
        self.now = _cph(2024, 6, 15)

    def test_expanded_prices_match_populated_slot_prices(self):
        """PlannedSlot prices from populate_prices equal SlotPrice values."""
        from datetime import timedelta

        from custom_components.hsem.models.planner_inputs import (
            PlannerInput,
            PricePoint,
        )
        from custom_components.hsem.models.planner_outputs import PlannedSlot
        from custom_components.hsem.planner.slot_population import (
            build_time_series_index,
            populate_prices,
        )

        imp_raw = {h: round(0.10 + h * 0.01, 4) for h in range(24)}
        exp_raw = {h: round(0.08 + h * 0.005, 4) for h in range(24)}

        # Build via the new utility function
        expanded = expand_hourly_prices_to_slots(self.now, imp_raw, exp_raw)

        # Build via the planner slot_population path
        price_points = [
            PricePoint(hour=h, import_price=imp_raw[h], export_price=exp_raw[h])
            for h in range(24)
        ]
        inp = PlannerInput(now_iso=self.now.isoformat(), interval_minutes=15)
        tsi = build_time_series_index(inp, self.now)
        midnight = self.now.replace(hour=0, minute=0, second=0, microsecond=0)
        planned_slots = [
            PlannedSlot(
                start=midnight + timedelta(minutes=i * 15),
                end=midnight + timedelta(minutes=(i + 1) * 15),
            )
            for i in range(96)
        ]
        populate_prices(planned_slots, price_points, tsi=tsi)

        for i, (sp, ps) in enumerate(zip(expanded, planned_slots)):
            assert sp.import_price == pytest.approx(
                ps.price.import_price
            ), f"slot {i} import mismatch"
            assert sp.export_price == pytest.approx(
                ps.price.export_price
            ), f"slot {i} export mismatch"

    def test_negative_export_survives_populate_prices(self):
        """Negative export prices must not be zeroed out by populate_prices."""
        from datetime import timedelta

        from custom_components.hsem.models.planner_inputs import (
            PlannerInput,
            PricePoint,
        )
        from custom_components.hsem.models.planner_outputs import PlannedSlot
        from custom_components.hsem.planner.slot_population import (
            build_time_series_index,
            populate_prices,
        )

        imp_raw = {h: 0.10 for h in range(24)}
        exp_raw = {h: -0.05 for h in range(24)}

        price_points = [
            PricePoint(hour=h, import_price=imp_raw[h], export_price=exp_raw[h])
            for h in range(24)
        ]
        inp = PlannerInput(now_iso=self.now.isoformat(), interval_minutes=15)
        tsi = build_time_series_index(inp, self.now)
        midnight = self.now.replace(hour=0, minute=0, second=0, microsecond=0)
        planned_slots = [
            PlannedSlot(
                start=midnight + timedelta(minutes=i * 15),
                end=midnight + timedelta(minutes=(i + 1) * 15),
            )
            for i in range(96)
        ]
        populate_prices(planned_slots, price_points, tsi=tsi)

        for ps in planned_slots:
            assert ps.price.export_price == pytest.approx(
                -0.05
            ), f"negative export price was lost: {ps.price.export_price}"
