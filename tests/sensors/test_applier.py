"""Tests for custom_sensors/applier.py.

The :func:`_parse_power_control_pct` helper is pure Python and fully testable
without Home Assistant.  The async hardware-write functions are covered by
integration tests; here we only test the deterministic helper.
"""

from __future__ import annotations


from custom_components.hsem.custom_sensors.applier import _parse_power_control_pct


class TestParsePowerControlPct:
    """Unit tests for the inverter power control state parser."""

    def test_unlimited_returns_100(self):
        assert _parse_power_control_pct("Unlimited") == 100

    def test_unlimited_case_insensitive(self):
        assert _parse_power_control_pct("unlimited") == 100
        assert _parse_power_control_pct("UNLIMITED") == 100

    def test_limited_to_80_percent(self):
        assert _parse_power_control_pct("Limited to 80%") == 80

    def test_limited_to_0_percent(self):
        assert _parse_power_control_pct("Limited to 0%") == 0

    def test_fractional_rounds_to_int(self):
        assert _parse_power_control_pct("Limited to 79.6%") == 80

    def test_none_returns_none(self):
        assert _parse_power_control_pct(None) is None

    def test_integer_returns_none(self):
        assert _parse_power_control_pct(100) is None  # type: ignore[arg-type]

    def test_empty_string_returns_none(self):
        assert _parse_power_control_pct("") is None

    def test_unknown_string_returns_none(self):
        assert _parse_power_control_pct("some other value") is None

    def test_whitespace_stripped(self):
        assert _parse_power_control_pct("  Limited to 50%  ") == 50
