"""Tests for the generic sensor unit-normalization utility (issue #945).

Verifies that :func:`normalize_to_unit` correctly delegates to Home
Assistant's own ``unit_conversion`` converters for temperature and speed,
and falls back sensibly (never raises) for missing or unrecognised units.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from homeassistant.const import UnitOfSpeed, UnitOfTemperature
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.unit_conversion import SpeedConverter, TemperatureConverter

from custom_components.hsem.utils.unit_normalize import normalize_to_unit

# ---------------------------------------------------------------------------
# Temperature (°C / °F / K)
# ---------------------------------------------------------------------------


class TestTemperatureNormalization:
    """Tests for temperature conversion via TemperatureConverter."""

    def test_fahrenheit_to_celsius(self) -> None:
        """98.6°F (body temp) → ~37.0°C."""
        result = normalize_to_unit(
            98.6, UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.CELSIUS
        )
        assert result == pytest.approx(37.0, abs=0.01)

    def test_freezing_fahrenheit_to_celsius(self) -> None:
        """32°F → 0.0°C."""
        result = normalize_to_unit(
            32.0, UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.CELSIUS
        )
        assert result == pytest.approx(0.0, abs=1e-9)

    def test_celsius_to_fahrenheit(self) -> None:
        """0°C → 32.0°F."""
        result = normalize_to_unit(
            0.0, UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT
        )
        assert result == pytest.approx(32.0)

    def test_matching_unit_is_unchanged(self) -> None:
        """Already-Celsius readings pass through unchanged (no-op path)."""
        result = normalize_to_unit(
            21.5, UnitOfTemperature.CELSIUS, UnitOfTemperature.CELSIUS
        )
        assert result == pytest.approx(21.5)

    def test_matches_ha_converter_directly(self) -> None:
        """Result must match TemperatureConverter.convert() exactly."""
        expected = TemperatureConverter.convert(
            100.0, UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.CELSIUS
        )
        result = normalize_to_unit(
            100.0, UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.CELSIUS
        )
        assert result == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Speed (km/h, m/s, mph)
# ---------------------------------------------------------------------------


class TestSpeedNormalization:
    """Tests for speed conversion via SpeedConverter."""

    def test_ms_to_kmh(self) -> None:
        """10 m/s → 36.0 km/h."""
        result = normalize_to_unit(
            10.0,
            UnitOfSpeed.METERS_PER_SECOND,
            UnitOfSpeed.KILOMETERS_PER_HOUR,
        )
        assert result == pytest.approx(36.0)

    def test_mph_to_kmh(self) -> None:
        """10 mph → ~16.09 km/h."""
        result = normalize_to_unit(
            10.0,
            UnitOfSpeed.MILES_PER_HOUR,
            UnitOfSpeed.KILOMETERS_PER_HOUR,
        )
        assert result == pytest.approx(16.0934, abs=0.001)

    def test_kmh_to_ms(self) -> None:
        """36 km/h → 10.0 m/s."""
        result = normalize_to_unit(
            36.0,
            UnitOfSpeed.KILOMETERS_PER_HOUR,
            UnitOfSpeed.METERS_PER_SECOND,
        )
        assert result == pytest.approx(10.0)

    def test_matches_ha_converter_directly(self) -> None:
        """Result must match SpeedConverter.convert() exactly."""
        expected = SpeedConverter.convert(
            25.0, UnitOfSpeed.MILES_PER_HOUR, UnitOfSpeed.METERS_PER_SECOND
        )
        result = normalize_to_unit(
            25.0,
            UnitOfSpeed.MILES_PER_HOUR,
            UnitOfSpeed.METERS_PER_SECOND,
        )
        assert result == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Fallback behaviour — missing / unrecognised units, None values
# ---------------------------------------------------------------------------


class TestFallbackBehaviour:
    """Tests for safe, non-raising fallback paths."""

    def test_none_value_returns_none(self) -> None:
        """A missing reading stays missing — never coerced to a number."""
        result = normalize_to_unit(
            None, UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.CELSIUS
        )
        assert result is None

    def test_missing_unit_assumes_canonical(self) -> None:
        """No declared unit (e.g. unit-less template sensor) → value unchanged."""
        result = normalize_to_unit(21.5, None, UnitOfTemperature.CELSIUS)
        assert result == pytest.approx(21.5)

    def test_empty_string_unit_assumes_canonical(self) -> None:
        """Empty-string unit is treated the same as a missing unit."""
        result = normalize_to_unit(21.5, "", UnitOfTemperature.CELSIUS)
        assert result == pytest.approx(21.5)

    def test_unrecognised_unit_for_canonical_family_is_unchanged(self) -> None:
        """A power unit declared for a temperature field is not misconverted."""
        result = normalize_to_unit(3.6, "kW", UnitOfTemperature.CELSIUS)
        assert result == pytest.approx(3.6)

    def test_unknown_canonical_unit_is_unchanged(self) -> None:
        """A canonical unit with no registered converter never raises."""
        result = normalize_to_unit(42.0, UnitOfTemperature.CELSIUS, "furlongs")
        assert result == pytest.approx(42.0)

    def test_never_raises_on_bogus_source_unit(self) -> None:
        """A nonsense declared unit degrades to the raw value, not an exception."""
        result = normalize_to_unit(21.5, "bogus-unit", UnitOfTemperature.CELSIUS)
        assert result == pytest.approx(21.5)

    def test_entity_id_and_label_are_optional(self) -> None:
        """Default entity_id/label kwargs must not break conversion or logging."""
        result = normalize_to_unit(
            32.0, UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.CELSIUS
        )
        assert result == pytest.approx(0.0, abs=1e-9)

    def test_converter_error_falls_back_to_raw_value(self) -> None:
        """Even a raising converter must not propagate — value passes through."""
        with patch.object(
            TemperatureConverter,
            "convert",
            side_effect=HomeAssistantError("boom"),
        ):
            result = normalize_to_unit(
                21.5,
                UnitOfTemperature.FAHRENHEIT,
                UnitOfTemperature.CELSIUS,
                entity_id="sensor.outdoor_temperature",
                label="temperature",
            )
        assert result == pytest.approx(21.5)
