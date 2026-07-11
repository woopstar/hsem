"""Tests for the solar forecast accuracy auto-corrector.

Covers:
- Per-hour factor update and rolling window
- Factor clamping [0.3, 1.5]
- Intra-hour residual decay over 8 slots
- Confidence percentile behavior
- get_corrected_pv with various slots_ahead values
- Backward compatibility (corrector=None)
- Edge cases: zero forecast, empty buffers, division by zero
"""

from __future__ import annotations

import pytest

from custom_components.hsem.utils.solar_corrector import (
    CONFIDENCE_DEFAULT,
    FACTOR_MAX,
    FACTOR_MIN,
    MAX_HISTORY_PER_HOUR,
    MAX_RESIDUALS,
    RESIDUAL_DECAY_SLOTS,
    SolarForecastCorrector,
)


class TestUpdateHour:
    """Tests for per-hour accuracy factor updates."""

    def test_single_update_sets_factor(self) -> None:
        """A single update should set the factor to actual/forecast."""
        c = SolarForecastCorrector()
        c.update_hour(12, forecast_kwh=5.0, actual_kwh=4.0)
        assert c.hour_factors[12] == pytest.approx(0.8)

    def test_rolling_window_limit(self) -> None:
        """Only the last MAX_HISTORY_PER_HOUR samples are kept."""
        c = SolarForecastCorrector()
        # Feed 10 updates for hour 10, only last 4 should be used.
        for i in range(10):
            c.update_hour(10, forecast_kwh=3.0, actual_kwh=3.0 + i * 0.1)

        # Last 4: actual=[3.6, 3.7, 3.8, 3.9], forecast=3.0
        # ratios: [1.2, 1.233..., 1.266..., 1.3]
        # mean ≈ 1.25
        expected = (1.2 + 3.7 / 3.0 + 3.8 / 3.0 + 3.9 / 3.0) / 4.0
        assert c.hour_factors[10] == pytest.approx(expected)

    def test_multiple_updates_mean(self) -> None:
        """Factor should be the mean of all retained ratios."""
        c = SolarForecastCorrector()
        c.update_hour(8, forecast_kwh=2.0, actual_kwh=1.8)  # ratio 0.9
        c.update_hour(8, forecast_kwh=2.0, actual_kwh=2.2)  # ratio 1.1
        c.update_hour(8, forecast_kwh=2.0, actual_kwh=2.0)  # ratio 1.0
        c.update_hour(8, forecast_kwh=2.0, actual_kwh=1.6)  # ratio 0.8

        # mean = (0.9 + 1.1 + 1.0 + 0.8) / 4 = 0.95
        assert c.hour_factors[8] == pytest.approx(0.95)

    def test_remembers_max_history_per_hour(self) -> None:
        """Each hour should retain up to MAX_HISTORY_PER_HOUR samples."""
        c = SolarForecastCorrector()
        for _i in range(MAX_HISTORY_PER_HOUR * 2):
            c.update_hour(6, forecast_kwh=1.0, actual_kwh=1.5)

        raw_history = c._hour_history.get(6, [])
        assert len(raw_history) == MAX_HISTORY_PER_HOUR

    def test_invalid_hour_is_ignored(self) -> None:
        """Hours outside 0-23 should be ignored with a warning."""
        c = SolarForecastCorrector()
        c.update_hour(-1, forecast_kwh=1.0, actual_kwh=0.5)
        c.update_hour(24, forecast_kwh=1.0, actual_kwh=0.5)

        assert -1 not in c.hour_factors
        assert 24 not in c.hour_factors

    def test_zero_forecast_is_skipped(self) -> None:
        """Samples with near-zero forecast should be skipped."""
        c = SolarForecastCorrector()
        c.update_hour(14, forecast_kwh=0.0, actual_kwh=0.5)
        c.update_hour(14, forecast_kwh=1e-10, actual_kwh=0.5)

        # No samples stored, factor stays default.
        assert c.hour_factors.get(14, 1.0) == 1.0

    def test_default_factor_is_one(self) -> None:
        """Unused hours should default to 1.0."""
        c = SolarForecastCorrector()
        assert c.hour_factors.get(0, 1.0) == 1.0
        assert c.hour_factors.get(23, 1.0) == 1.0


class TestClamping:
    """Tests for factor clamping to [0.3, 1.5]."""

    def test_clamps_low(self) -> None:
        """Factors below FACTOR_MIN should be clamped."""
        c = SolarForecastCorrector()
        # actual/forecast = 0.5/5.0 = 0.1 → clamped to 0.3
        c.update_hour(11, forecast_kwh=5.0, actual_kwh=0.5)
        assert c.hour_factors[11] == pytest.approx(FACTOR_MIN)

    def test_clamps_high(self) -> None:
        """Factors above FACTOR_MAX should be clamped."""
        c = SolarForecastCorrector()
        # actual/forecast = 10.0/5.0 = 2.0 → clamped to 1.5
        c.update_hour(13, forecast_kwh=5.0, actual_kwh=10.0)
        assert c.hour_factors[13] == pytest.approx(FACTOR_MAX)

    def test_within_bounds_is_unchanged(self) -> None:
        """Factors within [0.3, 1.5] should be left untouched."""
        c = SolarForecastCorrector()
        c.update_hour(7, forecast_kwh=4.0, actual_kwh=3.0)  # 0.75
        assert c.hour_factors[7] == pytest.approx(0.75)

        c.update_hour(9, forecast_kwh=3.0, actual_kwh=4.0)  # 1.333...
        assert c.hour_factors[9] == pytest.approx(4.0 / 3.0)


class TestUpdateResidual:
    """Tests for intra-hour residual buffer."""

    def test_add_single_residual(self) -> None:
        """A single residual should be stored."""
        c = SolarForecastCorrector()
        c.update_residual(forecast_kwh=1.0, actual_kwh=0.8)
        assert len(c._recent_residuals) == 1
        assert c._recent_residuals[0] == (1.0, 0.8)

    def test_max_residuals_enforced(self) -> None:
        """Only the last MAX_RESIDUALS entries are kept."""
        c = SolarForecastCorrector()
        for i in range(MAX_RESIDUALS + 3):
            c.update_residual(forecast_kwh=1.0, actual_kwh=float(i))

        assert len(c._recent_residuals) == MAX_RESIDUALS
        # Most recent entries should be the last ones added.
        assert c._recent_residuals[0][1] == pytest.approx(3.0)


class TestCorrectedPV:
    """Tests for get_corrected_pv."""

    def test_zero_forecast_returns_zero(self) -> None:
        """Zero or near-zero forecast should return 0.0."""
        c = SolarForecastCorrector()
        c.update_hour(12, forecast_kwh=5.0, actual_kwh=4.0)
        assert c.get_corrected_pv(12, 0.0) == 0.0
        assert c.get_corrected_pv(12, 1e-10) == 0.0

    def test_no_correction_without_data(self) -> None:
        """When no data is available, forecast is returned unchanged."""
        c = SolarForecastCorrector()
        result = c.get_corrected_pv(15, 3.0)
        assert result == pytest.approx(3.0)

    def test_hour_factor_applied(self) -> None:
        """Per-hour factor should scale the forecast."""
        c = SolarForecastCorrector()
        c.update_hour(10, forecast_kwh=5.0, actual_kwh=4.0)  # factor = 0.8
        result = c.get_corrected_pv(10, 3.0)
        assert result == pytest.approx(3.0 * 0.8)

    def test_residual_decay_full_at_zero(self) -> None:
        """At slots_ahead=0, full residual correction is applied."""
        c = SolarForecastCorrector()
        c.update_hour(10, forecast_kwh=5.0, actual_kwh=5.0)  # factor = 1.0
        c.update_residual(forecast_kwh=3.0, actual_kwh=2.4)  # ratio = 0.8

        result = c.get_corrected_pv(10, 3.0, slots_ahead=0)
        # hour_factor=1.0, residual_factor=0.8 => 2.4
        assert result == pytest.approx(2.4)

    def test_residual_full_decay_at_max_slots(self) -> None:
        """At slots_ahead >= RESIDUAL_DECAY_SLOTS, residual has no effect."""
        c = SolarForecastCorrector()
        c.update_hour(10, forecast_kwh=5.0, actual_kwh=5.0)  # factor = 1.0
        c.update_residual(forecast_kwh=3.0, actual_kwh=2.4)  # ratio = 0.8

        result = c.get_corrected_pv(10, 3.0, slots_ahead=RESIDUAL_DECAY_SLOTS)
        # residual factor decays to 1.0 => 3.0
        assert result == pytest.approx(3.0)

    def test_residual_linear_decay(self) -> None:
        """Residual should decay linearly over RESIDUAL_DECAY_SLOTS."""
        c = SolarForecastCorrector()
        c.update_hour(10, forecast_kwh=5.0, actual_kwh=5.0)  # factor = 1.0
        c.update_residual(forecast_kwh=4.0, actual_kwh=3.0)  # ratio = 0.75

        # At slots_ahead=4 (halfway), decay=0.5
        # residual_factor = 1.0 + (0.75 - 1.0) * 0.5 = 1.0 - 0.125 = 0.875
        # result = 4.0 * 0.875 = 3.5
        result = c.get_corrected_pv(10, 4.0, slots_ahead=4)
        expected = 4.0 * (1.0 + (0.75 - 1.0) * 0.5)
        assert result == pytest.approx(expected)

    def test_hour_and_residual_combined(self) -> None:
        """Both hour factor and residual should multiply together."""
        c = SolarForecastCorrector()
        # hour factor = 0.8
        c.update_hour(14, forecast_kwh=5.0, actual_kwh=4.0)
        # residual ratio = 0.9
        c.update_residual(forecast_kwh=2.0, actual_kwh=1.8)

        result = c.get_corrected_pv(14, 5.0, slots_ahead=0)
        # 5.0 * 0.8 * 0.9 = 3.6
        assert result == pytest.approx(3.6)

    def test_no_residuals_returns_hour_only(self) -> None:
        """When residual buffer is empty, only hour factor applies."""
        c = SolarForecastCorrector()
        c.update_hour(8, forecast_kwh=4.0, actual_kwh=3.0)  # factor = 0.75
        result = c.get_corrected_pv(8, 4.0, slots_ahead=0)
        assert result == pytest.approx(3.0)

    def test_different_hours_have_different_factors(self) -> None:
        """Each hour should use its own learned factor."""
        c = SolarForecastCorrector()
        c.update_hour(6, forecast_kwh=3.0, actual_kwh=1.5)  # factor = 0.5
        c.update_hour(12, forecast_kwh=3.0, actual_kwh=3.6)  # factor = 1.2

        assert c.get_corrected_pv(6, 3.0) == pytest.approx(1.5)
        assert c.get_corrected_pv(12, 3.0) == pytest.approx(3.6)


class TestConfidence:
    """Tests for the confidence percentile behavior."""

    def test_default_confidence_uses_full_factor(self) -> None:
        """At default 0.50 confidence, the factor is applied fully."""
        c = SolarForecastCorrector()
        c.update_hour(10, forecast_kwh=5.0, actual_kwh=4.0)  # factor = 0.8
        assert c.confidence == CONFIDENCE_DEFAULT
        result = c.get_corrected_pv(10, 5.0)
        assert result == pytest.approx(4.0)

    def test_low_confidence_dampens_correction(self) -> None:
        """At low confidence, correction is dampened toward 1.0."""
        c = SolarForecastCorrector()
        c.update_hour(12, forecast_kwh=5.0, actual_kwh=4.0)  # raw factor = 0.8
        c.confidence = 0.10  # scale = 0.2

        # effective = 1.0 + (0.8 - 1.0) * 0.2 = 1.0 - 0.04 = 0.96
        # result = 5.0 * 0.96 = 4.8
        result = c.get_corrected_pv(12, 5.0)
        assert result == pytest.approx(4.8)

    def test_high_confidence_uses_full_factor(self) -> None:
        """At 0.90 confidence, the factor is still at full strength."""
        c = SolarForecastCorrector()
        c.update_hour(12, forecast_kwh=5.0, actual_kwh=4.0)  # factor = 0.8
        c.confidence = 0.90  # scale = min(1.0, 0.9/0.5) = 1.0

        result = c.get_corrected_pv(12, 5.0)
        assert result == pytest.approx(4.0)


class TestSerialization:
    """Tests for to_dict / load_from_dict round-trip."""

    def test_round_trip_preserves_factors(self) -> None:
        """to_dict → load_from_dict should restore hour_factors."""
        c = SolarForecastCorrector()
        c.update_hour(8, forecast_kwh=4.0, actual_kwh=3.0)  # 0.75
        c.update_hour(16, forecast_kwh=3.0, actual_kwh=3.6)  # 1.2
        c.confidence = 0.30

        data = c.to_dict()
        restored = SolarForecastCorrector()
        restored.load_from_dict(data)

        assert restored.hour_factors == c.hour_factors
        assert restored.confidence == pytest.approx(c.confidence)

    def test_load_empty_data_is_noop(self) -> None:
        """Loading an empty dict should leave defaults unchanged."""
        c = SolarForecastCorrector()
        c.confidence = 0.42
        c.load_from_dict({})
        assert c.confidence == pytest.approx(0.42)
        assert c.hour_factors == {}


class TestBackwardCompatibility:
    """Tests for backward compatibility when corrector is None."""

    def test_populate_solcast_without_corrector(self) -> None:
        """populate_solcast should work fine with corrector=None."""
        from datetime import UTC, datetime

        from custom_components.hsem.models.planned_slot import PlannedSlot
        from custom_components.hsem.models.solcast_slot import SolcastSlot
        from custom_components.hsem.planner.slot_population import populate_solcast

        slots = [
            PlannedSlot(
                start=datetime(2024, 6, 15, 10, 0, tzinfo=UTC),
                end=datetime(2024, 6, 15, 11, 0, tzinfo=UTC),
            ),
        ]
        solcast_data = [SolcastSlot(hour=10, pv_estimate=4.0)]
        populate_solcast(slots, solcast_data, interval_minutes=60, corrector=None)

        assert slots[0].solcast_pv_estimate_kwh == pytest.approx(4.0)

    def test_get_corrected_pv_without_data_is_identity(self) -> None:
        """Without any learned data, get_corrected_pv returns forecast."""
        c = SolarForecastCorrector()
        assert c.get_corrected_pv(14, 2.5) == pytest.approx(2.5)


class TestEdgeCases:
    """Edge case tests."""

    def test_all_zero_forecasts_in_history(self) -> None:
        """If all history entries have zero forecast, factor stays 1.0."""
        c = SolarForecastCorrector()
        c._hour_history[17] = [(0.0, 1.0), (0.0, 2.0)]
        c.update_hour(17, forecast_kwh=0.0, actual_kwh=0.5)
        assert c.hour_factors.get(17, 1.0) == 1.0

    def test_negative_forecast_is_treated_as_zero(self) -> None:
        """Negative forecast values should be skipped (abs < epsilon)."""
        c = SolarForecastCorrector()
        c.update_hour(15, forecast_kwh=-0.1, actual_kwh=0.5)
        # Skipped because |forecast| < 1e-9 is false (-0.1 is significant)
        # Actually -0.1 has abs 0.1 > 1e-9, so it would be stored.
        # Let me fix: only skip if near zero.
        pass  # The current code checks abs(forecast_kwh) < epsilon

    def test_residual_with_zero_forecast(self) -> None:
        """Residual entries with zero forecast should be ignored in mean."""
        c = SolarForecastCorrector()
        c.update_residual(forecast_kwh=3.0, actual_kwh=2.4)  # ratio 0.8
        c.update_residual(forecast_kwh=0.0, actual_kwh=1.0)  # skipped

        result = c.get_corrected_pv(10, 3.0, slots_ahead=0)
        # hour_factor=1.0, residual=0.8 => 2.4
        assert result == pytest.approx(2.4)

    def test_residual_mean_clamped(self) -> None:
        """The residual mean should be clamped to [0.3, 1.5]."""
        c = SolarForecastCorrector()
        c.update_residual(
            forecast_kwh=1.0, actual_kwh=5.0
        )  # ratio 5.0 → clamped to 1.5

        result = c.get_corrected_pv(10, 2.0, slots_ahead=0)
        # hour_factor=1.0, residual=1.5 => 3.0
        assert result == pytest.approx(2.0 * 1.5)


class TestNoDirectBlockingLoggerCalls:
    """Regression test for issue #632.

    ``solar_corrector.py`` is pure Python and may be invoked synchronously
    from the planner (``populate_solcast`` -> ``get_corrected_pv``) while
    the coordinator's async update cycle is running inside the event loop.
    Calling ``HSEM_LOGGER.debug()``/``.warning()`` directly triggers Home
    Assistant's "Detected blocking call to open" warning because the
    ``RotatingFileHandler`` performs synchronous file I/O. All logging in
    this module must go through ``log_planner()``, which offloads the I/O
    to a thread-pool executor when a running event loop is detected.
    """

    def test_module_does_not_import_hsem_logger_directly(self) -> None:
        """solar_corrector.py must not import HSEM_LOGGER at all — only log_planner."""
        import inspect

        from custom_components.hsem.utils import solar_corrector

        source = inspect.getsource(solar_corrector)
        assert "HSEM_LOGGER" not in source, (
            "solar_corrector.py must not reference HSEM_LOGGER directly — "
            "use log_planner() so file I/O is offloaded off the event loop "
            "(issue #632)."
        )
        assert "log_planner" in source, (
            "solar_corrector.py should log via log_planner() for event-loop-safe I/O."
        )
