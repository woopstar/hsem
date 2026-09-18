"""Tests for solar-corrector input guards and persisted-state validation.

The corrector learns per-hour PV correction factors, so a restored state that
does not internally agree (factors that do not match their own sample history,
duplicate hours, unbounded lists) must be discarded rather than trusted. Its
physical-time inputs must also be timezone-aware, or slot distances would be
wrong across a DST fold.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from custom_components.hsem.utils.solar_corrector import (
    SOLAR_CORRECTOR_STATE_VERSION,
    SolarForecastCorrector,
    _parse_energy_history,
    _parse_hour_key,
)

_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
_NAIVE = datetime(2026, 6, 1, 12, 0)


class TestAwareTimeInputs:
    """Physical-time inputs must carry a timezone."""

    def test_reference_time_must_be_aware(self) -> None:
        """A naive reference time would make slot distances ambiguous."""
        corrector = SolarForecastCorrector()

        with pytest.raises(ValueError, match="reference time"):
            corrector.set_reference_time(_NAIVE)

    def test_processed_watermark_must_be_aware(self) -> None:
        """A naive watermark could not be compared across a DST fold."""
        corrector = SolarForecastCorrector()

        with pytest.raises(ValueError, match="processed slot start"):
            corrector.mark_processed(_NAIVE)

    def test_watermark_only_moves_forward(self) -> None:
        """An older slot never rewinds the replay watermark."""
        corrector = SolarForecastCorrector()
        corrector.mark_processed(_NOW)
        corrector.mark_processed(_NOW.replace(hour=10))

        assert corrector.processed_through == _NOW


class TestSlotsAheadFor:
    """Slot distance is measured in physical UTC time."""

    def test_without_a_reference_time_the_fallback_is_used(self) -> None:
        """Before the first cycle the caller's own estimate stands."""
        corrector = SolarForecastCorrector()

        assert corrector.slots_ahead_for(_NOW, 15, fallback=3) == 3
        assert corrector.slots_ahead_for(_NOW, 15, fallback=-5) == 0

    def test_naive_slot_start_is_refused(self) -> None:
        """A naive slot start cannot be placed on the physical timeline."""
        corrector = SolarForecastCorrector()
        corrector.set_reference_time(_NOW)

        with pytest.raises(ValueError, match="slot start"):
            corrector.slots_ahead_for(_NAIVE, 15, fallback=0)

    def test_non_positive_interval_is_refused(self) -> None:
        """A zero-length slot has no meaningful distance."""
        corrector = SolarForecastCorrector()
        corrector.set_reference_time(_NOW)

        with pytest.raises(ValueError, match="interval_minutes"):
            corrector.slots_ahead_for(_NOW, 0, fallback=0)

    @pytest.mark.parametrize(
        ("hours_ahead", "expected"),
        [
            pytest.param(0, 0, id="current_slot"),
            pytest.param(1, 4, id="one_hour_at_15_min"),
            pytest.param(2, 8, id="two_hours_at_15_min"),
        ],
    )
    def test_distance_is_measured_in_slots(
        self, hours_ahead: int, expected: int
    ) -> None:
        """A 15-minute interval yields four slots per hour."""
        corrector = SolarForecastCorrector()
        corrector.set_reference_time(_NOW)

        assert (
            corrector.slots_ahead_for(
                _NOW.replace(hour=_NOW.hour + hours_ahead), 15, fallback=0
            )
            == expected
        )


class TestUpdateHour:
    """Only a usable forecast/actual pair can move a correction factor."""

    @pytest.mark.parametrize(
        ("hour", "forecast", "actual"),
        [
            pytest.param(10, 0.0, 5.0, id="zero_forecast"),
            pytest.param(10, float("nan"), 5.0, id="non_finite_forecast"),
            pytest.param(-1, 1.0, 0.8, id="hour_below_range"),
            pytest.param(24, 1.0, 0.8, id="hour_above_range"),
        ],
    )
    def test_unusable_samples_are_ignored(
        self, hour: int, forecast: float, actual: float
    ) -> None:
        """A sample that cannot form a ratio never records a factor."""
        corrector = SolarForecastCorrector()

        corrector.update_hour(hour, forecast, actual)

        assert corrector.hour_factors == {}

    def test_a_usable_sample_records_the_measured_ratio(self) -> None:
        """An 80 % delivery against forecast becomes a 0.8 factor."""
        corrector = SolarForecastCorrector()

        corrector.update_hour(10, 1.0, 0.8)

        assert corrector.hour_factors[10] == pytest.approx(0.8)


class TestParseHourKey:
    """Persisted hour keys are validated before use."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param(0, 0, id="int_zero"),
            pytest.param(23, 23, id="int_max"),
            pytest.param("7", 7, id="canonical_string"),
        ],
    )
    def test_canonical_keys_are_accepted(self, value: Any, expected: int) -> None:
        """Ints and their canonical string form both parse."""
        assert _parse_hour_key(value) == expected

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(True, id="bool"),
            pytest.param(-1, id="below_range"),
            pytest.param(24, id="above_range"),
            pytest.param("07", id="non_canonical_string"),
            pytest.param("seven", id="text"),
            pytest.param(7.0, id="float"),
            pytest.param(None, id="missing"),
        ],
    )
    def test_unusable_keys_are_rejected(self, value: Any) -> None:
        """Anything that is not a canonical 0–23 hour is discarded."""
        assert _parse_hour_key(value) is None


class TestParseEnergyHistory:
    """A persisted sample list must be bounded and well formed."""

    def test_valid_pairs_are_parsed(self) -> None:
        """A list of forecast/actual pairs round-trips."""
        assert _parse_energy_history([[1.0, 0.8], [2.0, 2.2]], maximum_length=5) == [
            (1.0, 0.8),
            (2.0, 2.2),
        ]

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("not a list", id="not_a_list"),
            pytest.param([[1.0, 0.8], [2.0, 2.2], [3.0, 3.0]], id="too_long"),
            pytest.param([[1.0]], id="short_pair"),
            pytest.param([[1.0, 0.8, 0.9]], id="long_pair"),
            pytest.param(["1.0,0.8"], id="pair_not_a_sequence"),
            pytest.param([[0.0, 0.8]], id="zero_forecast"),
            pytest.param([[float("nan"), 0.8]], id="non_finite"),
        ],
    )
    def test_malformed_history_is_rejected(self, value: Any) -> None:
        """A single bad entry discards the whole list."""
        assert _parse_energy_history(value, maximum_length=2) is None


def _state(
    factors: dict[str, float],
    history: dict[str, list[list[float]]],
    residuals: Any = None,
) -> dict[str, Any]:
    """Return a persisted corrector state payload."""
    return {
        "schema_version": SOLAR_CORRECTOR_STATE_VERSION,
        "hour_factors": factors,
        "hour_history": history,
        "recent_residuals": residuals if residuals is not None else [],
    }


class TestLoadFromDict:
    """Only internally consistent state is restored."""

    @staticmethod
    def _consistent_payload() -> dict[str, Any]:
        """Return a payload whose factors match their own history."""
        source = SolarForecastCorrector()
        source.update_hour(10, 1.0, 0.8)
        source.update_residual(1.0, 0.8)
        return source.to_dict()

    def test_consistent_state_is_restored(self) -> None:
        """A payload written by the corrector itself loads back."""
        corrector = SolarForecastCorrector()

        corrector.load_from_dict(self._consistent_payload(), restored_at=_NOW)

        assert corrector.hour_factors

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param("not a mapping", id="not_a_mapping"),
            pytest.param({}, id="no_schema_version"),
            pytest.param({"schema_version": "3"}, id="non_int_schema_version"),
            pytest.param(
                {"schema_version": SOLAR_CORRECTOR_STATE_VERSION - 1},
                id="older_schema_version",
            ),
        ],
    )
    def test_unrecognised_payloads_are_discarded(self, payload: Any) -> None:
        """Pre-v3 or malformed state is rebuilt from forecast baselines."""
        corrector = SolarForecastCorrector()

        corrector.load_from_dict(payload, restored_at=_NOW)

        assert corrector.hour_factors == {}

    def test_naive_restore_reference_is_discarded(self) -> None:
        """Without an aware restore instant the state cannot be aged."""
        corrector = SolarForecastCorrector()

        corrector.load_from_dict(self._consistent_payload(), restored_at=_NAIVE)

        assert corrector.hour_factors == {}

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(
                _state({"10": 0.8}, {"10": [[1.0, 0.8]], "11": [[1.0, 1.2]]}),
                id="history_without_a_factor",
            ),
            pytest.param(
                _state({"10": 0.5}, {"10": [[1.0, 0.8]]}),
                id="factor_disagrees_with_history",
            ),
            pytest.param(
                _state({"10": 0.8}, {"invalid": [[1.0, 0.8]]}),
                id="unusable_hour_key",
            ),
            pytest.param(
                _state({"10": 0.8}, {"10": []}), id="empty_history_for_an_hour"
            ),
            pytest.param(
                _state({"10": 0.8}, {"10": [[1.0, 0.8]]}, residuals="bad"),
                id="malformed_residuals",
            ),
        ],
    )
    def test_inconsistent_state_is_discarded(self, payload: Any) -> None:
        """A factor must be reproducible from its own stored samples."""
        corrector = SolarForecastCorrector()

        corrector.load_from_dict(payload, restored_at=_NOW)

        assert corrector.hour_factors == {}
