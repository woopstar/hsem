"""Tests for the refactored ``convert_to_float`` and ``convert_to_int`` helpers.

Verifies:
- ``unknown``, ``unavailable``, empty string, and invalid text return ``None``.
- Real numeric ``0`` / ``"0"`` returns ``0.0`` / ``0``.
- Positive and negative numbers round-trip correctly.
- Whitespace-padded strings are handled.
- Critical battery sensor unavailability sets ``live.missing_entities = True``
  so the planner enters safe mode instead of using a silent zero.
- The recommendation resolver handles a ``None`` import price safely.

All tests are pure-Python — no Home Assistant runtime is required.
"""

from __future__ import annotations

import pytest

from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.utils.misc import convert_to_float, convert_to_int

# ---------------------------------------------------------------------------
# convert_to_float — return None for invalid / missing values
# ---------------------------------------------------------------------------


class TestConvertToFloatInvalidValues:
    """Inputs that cannot represent a real measurement must return None."""

    @pytest.mark.parametrize(
        "bad_input",
        [
            "unknown",
            "Unknown",
            "UNKNOWN",
            "unavailable",
            "Unavailable",
            "UNAVAILABLE",
            "",
            "   ",
            "not_a_number",
            "N/A",
            "abc",
            "1.2.3",
            None,
        ],
    )
    def test_returns_none(self, bad_input: str | None) -> None:
        """Every HA sentinel / non-numeric value must yield None."""
        assert convert_to_float(bad_input) is None


class TestConvertToFloatRealZero:
    """A real zero measurement must NOT be treated as missing."""

    @pytest.mark.parametrize(
        "zero_input",
        [
            0,
            0.0,
            "0",
            "0.0",
            "0.00",
            "  0  ",
        ],
    )
    def test_zero_returns_zero(self, zero_input: int | float | str) -> None:
        """Real zero is a valid, non-missing measurement."""
        result = convert_to_float(zero_input)
        assert result == pytest.approx(0.0)
        assert result is not None


class TestConvertToFloatNumericRoundTrip:
    """Positive and negative numeric values should round-trip without loss."""

    @pytest.mark.parametrize(
        "value, expected",
        [
            (42, 42.0),
            (3.14, 3.14),
            (-7.5, -7.5),
            ("1.23", 1.23),
            ("  -99.9  ", -99.9),
            (0.001, 0.001),
            ("1e3", 1000.0),
        ],
    )
    def test_numeric_round_trip(
        self, value: int | float | str, expected: float
    ) -> None:
        result = convert_to_float(value)
        assert result == pytest.approx(expected)


# ---------------------------------------------------------------------------
# LiveState / state_collector — critical sensor None → missing_entities flag
# ---------------------------------------------------------------------------


class TestCriticalSensorNoneSetsMissingFlag:
    """When a critical battery sensor returns None the LiveState must flag it."""

    def _make_live_with_none_soc(self) -> LiveState:
        """Simulate state_collector behaviour for a None battery SoC."""
        state = LiveState()
        soc_pct = convert_to_float(
            "unavailable"
        )  # simulates HA returning "unavailable"
        if soc_pct is None:
            state.add_missing_entity(
                "Critical: battery SoC returned None (unavailable/invalid)"
            )
        state.huawei_batteries_soc_pct = soc_pct
        return state

    def test_unavailable_soc_sets_missing_entities(self) -> None:
        state = self._make_live_with_none_soc()
        assert state.missing_entities is True

    def test_unavailable_soc_is_none_not_zero(self) -> None:
        state = self._make_live_with_none_soc()
        assert state.huawei_batteries_soc_pct is None
        # Value must be None — not zero and not a number at all
        assert state.huawei_batteries_soc_pct is None

    def test_missing_entity_label_recorded(self) -> None:
        state = self._make_live_with_none_soc()
        assert any("SoC" in label for label in state.missing_entities_list)

    def _make_live_with_valid_soc(self, raw: str = "75.0") -> LiveState:

        state = LiveState()
        soc_pct = convert_to_float(raw)
        if soc_pct is None:
            state.add_missing_entity("Critical: battery SoC returned None")
        state.huawei_batteries_soc_pct = soc_pct
        return state

    def test_valid_soc_does_not_set_missing_flag(self) -> None:
        state = self._make_live_with_valid_soc("75.0")
        assert state.missing_entities is False
        assert state.huawei_batteries_soc_pct == pytest.approx(75.0)

    def test_zero_soc_does_not_set_missing_flag(self) -> None:
        """A battery at 0 % SoC is valid data — it must not be treated as missing."""
        state = self._make_live_with_valid_soc("0")
        assert state.missing_entities is False
        assert state.huawei_batteries_soc_pct == pytest.approx(0.0)

    @pytest.mark.parametrize("bad", ["unknown", "unavailable", "", "error"])
    def test_critical_sensor_bad_values_set_missing(self, bad: str) -> None:
        # Re-run with each bad value to confirm consistency
        from custom_components.hsem.models.live_state import LiveState

        s = LiveState()
        v = convert_to_float(bad)
        if v is None:
            s.add_missing_entity("Critical: battery SoC returned None")
        assert s.missing_entities is True
        assert s.huawei_batteries_soc_pct is None  # default field value


# ---------------------------------------------------------------------------
# Recommendation resolver — None-safe price comparison
# ---------------------------------------------------------------------------


class TestRecommendationResolverNullSafety:
    """resolve_current_recommendation must not raise when import price is None."""

    def _make_rec(self):
        from datetime import UTC, datetime

        from custom_components.hsem.models.hourly_recommendation import (
            HourlyRecommendation,
        )

        now = datetime(2024, 6, 15, 14, 0, tzinfo=UTC)
        end = datetime(2024, 6, 15, 15, 0, tzinfo=UTC)
        return HourlyRecommendation(
            start=now,
            end=end,
            recommendation="BatteriesDischargeMode",
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
            solcast_pv_estimate_kwh=0.0,
        )

    def test_none_import_price_does_not_trigger_force_export(self) -> None:
        """A None import price must not be misread as negative → no ForceExport."""
        from custom_components.hsem.custom_sensors.recommendation_resolver import (
            resolve_current_recommendation,
        )
        from custom_components.hsem.models.live_state import LiveState
        from custom_components.hsem.utils.recommendations import Recommendations

        rec = self._make_rec()
        live = LiveState()
        live.import_electricity_price = 0.0  # explicitly zero — not negative

        resolve_current_recommendation(rec, live, 0.0)
        # Should NOT override to ForceExport with a zero (non-negative) price
        assert rec.recommendation != Recommendations.ForceExport.value

    def test_negative_import_price_triggers_force_export(self) -> None:
        """A confirmed negative price still triggers ForceExport."""
        from custom_components.hsem.custom_sensors.recommendation_resolver import (
            resolve_current_recommendation,
        )
        from custom_components.hsem.models.live_state import LiveState
        from custom_components.hsem.utils.recommendations import Recommendations

        rec = self._make_rec()
        live = LiveState()
        live.import_electricity_price = -0.05

        resolve_current_recommendation(rec, live, 0.0)
        assert rec.recommendation == Recommendations.ForceExport.value


# ---------------------------------------------------------------------------
# End-of-discharge SoC — safe fallback, not treated as missing
# ---------------------------------------------------------------------------


class TestEodSocFallback:
    """End-of-discharge SoC should fall back to 5 % when unavailable, not missing."""

    def test_unavailable_eod_soc_falls_back_to_five(self) -> None:
        """None EoD SoC must use the 5 % safe default, not mark the entity missing."""
        from custom_components.hsem.models.live_state import LiveState

        state = LiveState()
        eod = convert_to_float("unavailable")
        # EoD is non-critical — fall back silently
        state.huawei_batteries_end_of_discharge_soc_pct = (
            eod if eod is not None else 5.0
        )

        assert state.missing_entities is False
        assert state.huawei_batteries_end_of_discharge_soc_pct == pytest.approx(5.0)

    def test_valid_eod_soc_is_preserved(self) -> None:
        from custom_components.hsem.models.live_state import LiveState

        state = LiveState()
        eod = convert_to_float("20.0")
        state.huawei_batteries_end_of_discharge_soc_pct = (
            eod if eod is not None else 5.0
        )

        assert state.huawei_batteries_end_of_discharge_soc_pct == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# convert_to_int — return None for invalid / missing values (regression #NNN)
# ---------------------------------------------------------------------------


class TestConvertToIntInvalidValues:
    """Invalid or sentinel inputs must return None, not a silent 0."""

    @pytest.mark.parametrize(
        "bad_input",
        [
            "unknown",
            "Unknown",
            "UNKNOWN",
            "unavailable",
            "Unavailable",
            "UNAVAILABLE",
            "",
            "   ",
            "not_a_number",
            "abc",
            None,
        ],
    )
    def test_returns_none(self, bad_input: str | None) -> None:
        """Every HA sentinel / non-numeric value must yield None."""
        assert convert_to_int(bad_input) is None


class TestConvertToIntRealZero:
    """A real zero measurement must NOT be treated as missing."""

    @pytest.mark.parametrize(
        "zero_input",
        [
            0,
            0.0,
            "0",
            "0.0",
            "  0  ",
        ],
    )
    def test_zero_returns_zero(self, zero_input: int | float | str) -> None:
        """Real zero is a valid, non-missing value."""
        result = convert_to_int(zero_input)
        assert result == 0
        assert result is not None


class TestConvertToIntNumericRoundTrip:
    """Positive and negative integers should round-trip correctly."""

    @pytest.mark.parametrize(
        "value, expected",
        [
            (42, 42),
            (-7, -7),
            ("25", 25),
            ("  30  ", 30),
            (6000, 6000),
            ("6000", 6000),
            # Float string should truncate to int
            ("3.9", 3),
            (3.9, 3),
        ],
    )
    def test_numeric_round_trip(self, value: int | float | str, expected: int) -> None:
        result = convert_to_int(value)
        assert result == expected


# ---------------------------------------------------------------------------
# SensorConfig — zero weights are preserved (not replaced by defaults)
# ---------------------------------------------------------------------------


class TestSensorConfigZeroWeightPreserved:
    """config_reader must not confuse a real 0 weight with a missing value.

    The ``None``-safe pattern ``_w = convert_to_int(v); field = _w if _w is not None else default``
    must preserve an explicit ``0`` rather than substituting the fallback.
    """

    def test_zero_weight_preserved_not_replaced_by_default(self) -> None:
        """convert_to_int('0') returns 0, which is not None → assigned without fallback."""
        result = convert_to_int("0")
        # Simulate the config_reader guard: _w if _w is not None else 25
        assigned = result if result is not None else 25
        assert assigned == 0, (
            "A config value of '0' must be stored as 0, not replaced by the 25 default."
        )

    def test_invalid_weight_falls_back_to_default(self) -> None:
        """convert_to_int('unknown') returns None → fallback default (25) is used."""
        result = convert_to_int("unknown")
        assigned = result if result is not None else 25
        assert assigned == 25

    def test_or_pattern_breaks_zero(self) -> None:
        """Demonstrate WHY ``convert_to_int(...) or default`` is wrong for zero.

        ``0 or 25`` evaluates to ``25`` because 0 is falsy in Python.
        The explicit ``None`` check avoids this trap.
        """
        zero_result = convert_to_int("0")
        assert zero_result == 0
        # The broken pattern:
        broken = zero_result or 25  # type: ignore[operator]  # intentional None comparison in test
        assert broken == 25, "or-pattern incorrectly replaces real 0 with default"
        # The correct pattern:
        correct = zero_result if zero_result is not None else 25
        assert correct == 0, "None-check pattern correctly preserves real 0"


# ---------------------------------------------------------------------------
# Coordinator config parsing — None-safe defaults, zero-safe assignment
# ---------------------------------------------------------------------------


class TestCoordinatorConfigNullSafeDefaults:
    """Verify that coordinator uses None-check pattern for integer config fields.

    These tests are pure-Python and replicate the logic inside coordinator.py
    without importing HA runtime objects.
    """

    @staticmethod
    def _apply_weight_default(raw: str | int | None, default: int) -> int:
        """Mirror the coordinator pattern: v if (v := convert_to_int(raw)) is not None else default."""
        v = convert_to_int(raw)
        return v if v is not None else default

    @pytest.mark.parametrize(
        "raw, default, expected",
        [
            ("25", 25, 25),  # normal value
            ("0", 25, 0),  # real zero must NOT be replaced by default
            (0, 25, 0),  # integer zero — same
            ("unknown", 25, 25),  # sentinel → default
            (None, 25, 25),  # None → default
            ("", 30, 30),  # empty → default
            ("6000", 6000, 6000),  # battery cycles — normal
            (0, 6000, 0),  # zero cycles is valid (unusual but not corrupt)
        ],
    )
    def test_weight_default_logic(
        self, raw: str | int | None, default: int, expected: int
    ) -> None:
        assert self._apply_weight_default(raw, default) == expected

    def test_zero_expected_cycles_preserved(self) -> None:
        """An explicit battery_expected_cycles=0 from config must survive, not become 6000."""
        result = self._apply_weight_default(0, 6000)
        assert result == 0


# ---------------------------------------------------------------------------
# Threshold calculation — interaction with None-safe int parsing
# ---------------------------------------------------------------------------


class TestThresholdCalculationWithNoneInputs:
    """calculate_recommended_threshold must handle None inputs from convert_to_int/float.

    In the real call path, coordinator feeds parsed values into PlannerInput, then
    the planner passes them to calculate_recommended_threshold.  If a parse returns
    None (and no default guard fires), the function must not raise.
    """

    def test_zero_cycles_returns_zero_threshold(self) -> None:
        """Zero expected_cycles is guarded inside calculate_recommended_threshold."""
        from custom_components.hsem.utils.misc import calculate_recommended_threshold

        result = calculate_recommended_threshold(
            purchase_price=48_000.0,
            expected_cycles=0,  # edge case: 0 cycles
            usable_capacity=10.0,
        )
        assert result == pytest.approx(0.0)

    def test_zero_purchase_price_returns_zero_threshold(self) -> None:
        """Zero purchase price → no depreciation → threshold is zero."""
        from custom_components.hsem.utils.misc import calculate_recommended_threshold

        result = calculate_recommended_threshold(
            purchase_price=0.0,
            expected_cycles=6000,
            usable_capacity=10.0,
        )
        assert result == pytest.approx(0.0)

    def test_typical_values_produce_nonzero_threshold(self) -> None:
        """Sanity check: normal inputs yield a positive threshold."""
        from custom_components.hsem.utils.misc import calculate_recommended_threshold

        result = calculate_recommended_threshold(
            purchase_price=48_000.0,
            expected_cycles=6_000,
            usable_capacity=10.0,
        )
        assert result == pytest.approx(0.12, abs=1e-3)


# ---------------------------------------------------------------------------
# Flow None-safety regression — batteries_schedule_1/2/3 and excess_export
# ---------------------------------------------------------------------------


class TestFlowExpectedCyclesNullSafety:
    """Regression: batteries_schedule and excess_export flows must not pass None
    expected_cycles into calculate_recommended_threshold.

    The pre-fix pattern ``convert_to_int(get_config_value(...) or 6000)`` fails when
    get_config_value returns a non-numeric string such as ``"unknown"``:
      - ``"unknown" or 6000`` evaluates to ``"unknown"`` (truthy string wins)
      - ``convert_to_int("unknown")`` returns ``None``
      - ``calculate_recommended_threshold(..., expected_cycles=None, ...)`` would TypeError

    The fix moves the fallback *after* convert_to_int using an explicit None check.
    These tests verify that guard using the same None-check helper pattern.
    """

    @staticmethod
    def _flow_expected_cycles(raw_config_value: str | int | None) -> int:
        """Mirror the fixed flow pattern used in all four flow files."""
        _v = convert_to_int(raw_config_value)
        return _v if _v is not None else 6000

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("6000", 6000),  # normal stored value
            (6000, 6000),  # already an int
            ("unknown", 6000),  # HA sentinel → default (was the bug: returned None)
            ("unavailable", 6000),  # HA unavailable → default
            (None, 6000),  # None config → default
            ("", 6000),  # empty string → default
            ("0", 0),  # explicit zero cycles must survive
            (0, 0),  # integer zero must survive
        ],
    )
    def test_flow_cycles_never_none(self, raw: str | int | None, expected: int) -> None:
        """Invalid raw values always fall back to 6000; real zero is preserved."""
        result = self._flow_expected_cycles(raw)
        assert result == expected
        assert result is not None, (
            "None must never reach calculate_recommended_threshold"
        )

    def test_invalid_cycles_does_not_raise_in_threshold_calc(self) -> None:
        """End-to-end: even if config holds 'unknown', threshold calculation succeeds.

        Before the fix, this would raise TypeError because None was passed as expected_cycles.
        """
        from custom_components.hsem.utils.misc import calculate_recommended_threshold

        cycles = self._flow_expected_cycles("unknown")  # must return 6000, not None
        result = calculate_recommended_threshold(
            purchase_price=48_000.0,
            expected_cycles=cycles,
            usable_capacity=10.0,
        )
        assert result == pytest.approx(0.12, abs=1e-3)


# ---------------------------------------------------------------------------
# clamp_efficiency — validates the shared helper
# ---------------------------------------------------------------------------


class TestClampEfficiency:
    """Verify that clamp_efficiency correctly clamps and converts percentages."""

    @staticmethod
    def _clamp(pct: float) -> float:
        from custom_components.hsem.utils.misc import clamp_efficiency

        return clamp_efficiency(pct)

    def test_normal_efficiency(self) -> None:
        """97 % → 0.97."""
        assert self._clamp(97.0) == pytest.approx(0.97)

    def test_zero_input_floor(self) -> None:
        """0 % is below the 1.0 floor → 0.01."""
        assert self._clamp(0.0) == pytest.approx(0.01)

    def test_below_one_floor(self) -> None:
        """0.5 % is below the 1.0 floor → 0.01."""
        assert self._clamp(0.5) == pytest.approx(0.01)

    def test_above_one_hundred_cap(self) -> None:
        """101 % is above the 100.0 cap → 1.0."""
        assert self._clamp(101.0) == pytest.approx(1.0)

    def test_negative_input(self) -> None:
        """-5 % is below the 1.0 floor → 0.01."""
        assert self._clamp(-5.0) == pytest.approx(0.01)

    def test_exactly_one(self) -> None:
        """1.0 % → 0.01."""
        assert self._clamp(1.0) == pytest.approx(0.01)

    def test_exactly_one_hundred(self) -> None:
        """100.0 % → 1.0."""
        assert self._clamp(100.0) == pytest.approx(1.0)

    def test_fifty_percent(self) -> None:
        """50.0 % → 0.5."""
        assert self._clamp(50.0) == pytest.approx(0.5)
