"""Guard-clause tests for the shared state-conversion helpers.

``test_convert_to_float_none.py`` covers the missing-vs-zero contract for
sensor readings. These tests cover the remaining input shapes: non-numeric
objects, booleans, integers, unrecognised state strings, and a missing EV
power reading.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from custom_components.hsem.utils.conversion import (
    convert_to_boolean,
    convert_to_float,
    convert_to_int,
    normalize_ev_power_w,
)


class TestConvertToFloat:
    """Only genuinely numeric input becomes a float."""

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(object(), id="arbitrary_object"),
            pytest.param([1.0], id="list"),
            pytest.param({"value": 1.0}, id="dict"),
        ],
    )
    def test_non_numeric_objects_are_missing(self, value: Any) -> None:
        """An unconvertible object reads as missing, not as zero."""
        assert convert_to_float(value) is None

    def test_numeric_values_pass_through(self) -> None:
        """Numbers and numeric strings convert, including a genuine zero."""
        assert convert_to_float(2.5) == pytest.approx(2.5)
        assert convert_to_float("2.5") == pytest.approx(2.5)
        assert convert_to_float(0) == pytest.approx(0.0)


class TestConvertToInt:
    """Only genuinely numeric input becomes an int."""

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(object(), id="arbitrary_object"),
            pytest.param([1], id="list"),
            pytest.param("not a number", id="text"),
        ],
    )
    def test_unconvertible_values_are_missing(self, value: Any) -> None:
        """Anything ``int()`` refuses reads as missing, not as zero."""
        assert convert_to_int(value) is None

    def test_numeric_values_pass_through(self) -> None:
        """Integers and numeric strings convert, including a genuine zero."""
        assert convert_to_int(7) == 7
        assert convert_to_int("7") == 7
        assert convert_to_int(0) == 0
        # A decimal string is truncated rather than rejected.
        assert convert_to_int("2.5") == 2


class TestConvertToBoolean:
    """Charger and switch states resolve to a definite boolean."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param(None, False, id="missing"),
            pytest.param(True, True, id="true"),
            pytest.param(False, False, id="false"),
            pytest.param(1, True, id="nonzero_int"),
            pytest.param(0, False, id="zero_int"),
            pytest.param(-1, True, id="negative_int"),
            pytest.param("on", True, id="on"),
            pytest.param("OFF", False, id="off_uppercase"),
            pytest.param("Charging", True, id="charging_mixed_case"),
            pytest.param("not_charging", False, id="not_charging"),
            pytest.param("connected", True, id="connected"),
            pytest.param("unavailable", False, id="unavailable"),
            pytest.param("unknown", False, id="unknown"),
            pytest.param("something_new", False, id="unrecognised_string"),
            pytest.param(2.5, False, id="float_is_not_a_state"),
            pytest.param(object(), False, id="arbitrary_object"),
        ],
    )
    def test_states_resolve_as_documented(self, value: Any, expected: bool) -> None:
        """Every input shape resolves to the documented boolean."""
        assert convert_to_boolean(value) is expected


class TestNormalizeEvPowerW:
    """EV charger power is normalised to Watts or reported as missing."""

    def test_missing_reading_stays_missing(self) -> None:
        """An unavailable charger power is never fabricated as zero."""
        assert (
            normalize_ev_power_w(
                None,
                unit_of_measurement="W",
                entity_id="sensor.ev_power",
                label="ev_charger_power",
                is_charging=True,
                logger=logging.getLogger(__name__),
            )
            is None
        )

    def test_kilowatts_are_scaled_to_watts(self) -> None:
        """A kW reading is multiplied out so the planner sees Watts."""
        assert normalize_ev_power_w(
            7.4,
            unit_of_measurement="kW",
            entity_id="sensor.ev_power",
            label="ev_charger_power",
            is_charging=True,
            logger=logging.getLogger(__name__),
        ) == pytest.approx(7400.0)
