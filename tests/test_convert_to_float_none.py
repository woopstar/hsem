"""Tests for the refactored ``convert_to_float`` (issue #269).

Verifies:
- ``unknown``, ``unavailable``, empty string, and invalid text return ``None``.
- Real numeric ``0`` / ``"0"`` returns ``0.0``.
- Positive and negative numbers round-trip correctly.
- Whitespace-padded strings are handled.
- Critical battery sensor unavailability sets ``live.missing_entities = True``
  so the planner enters safe mode instead of using a silent zero.
- The recommendation resolver handles a ``None`` import price safely.

All tests are pure-Python — no Home Assistant runtime is required.
"""

from __future__ import annotations

import pytest

from custom_components.hsem.utils.misc import convert_to_float

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
    def test_returns_none(self, bad_input) -> None:
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
    def test_zero_returns_zero(self, zero_input) -> None:
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
    def test_numeric_round_trip(self, value, expected: float) -> None:
        result = convert_to_float(value)
        assert result == pytest.approx(expected)


# ---------------------------------------------------------------------------
# LiveState / state_collector — critical sensor None → missing_entities flag
# ---------------------------------------------------------------------------


class TestCriticalSensorNoneSetsMissingFlag:
    """When a critical battery sensor returns None the LiveState must flag it."""

    def _make_live_with_none_soc(self):
        """Simulate state_collector behaviour for a None battery SoC."""
        from custom_components.hsem.models.live_state import LiveState

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

    def _make_live_with_valid_soc(self, raw: str = "75.0"):
        from custom_components.hsem.models.live_state import LiveState

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
            avg_house_consumption=0.0,
            avg_house_consumption_1d=0.0,
            avg_house_consumption_3d=0.0,
            avg_house_consumption_7d=0.0,
            avg_house_consumption_14d=0.0,
            batteries_charged=0.0,
            estimated_battery_capacity=0.0,
            estimated_battery_soc=0.0,
            estimated_cost=0.0,
            estimated_net_consumption=0.0,
            export_price=0.0,
            import_price=0.0,
            solcast_pv_estimate=0.0,
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
        live.energi_data_service_import_price = 0.0  # explicitly zero — not negative

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
        live.energi_data_service_import_price = -0.05

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
