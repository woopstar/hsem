"""Tests for TOU schedule read-back and verification.

The Huawei TOU entity's *state* is the number of configured periods; the
schedule itself lives in its ``Period 1``…``Period 10`` attributes.  Verifying
a written schedule against the state can never succeed.
"""

from unittest.mock import MagicMock

from custom_components.hsem.custom_sensors.applier import _read_tou_periods
from custom_components.hsem.utils.huawei import extract_tou_periods

TOU_ENTITY = "sensor.batteries_tou"


class TestExtractTouPeriods:
    """The schedule lives in attributes; the state is only a period count."""

    def test_reads_period_attributes_in_order(self):
        attrs = {
            "Period 2": "06:00-08:00/1234567/-",
            "Period 1": "00:00-23:59/1234567/+",
            "friendly_name": "TOU",
        }
        assert extract_tou_periods(attrs) == [
            "00:00-23:59/1234567/+",
            "06:00-08:00/1234567/-",
        ]

    def test_no_period_attributes_returns_empty(self):
        assert extract_tou_periods({"friendly_name": "TOU"}) == []

    def test_state_value_is_never_used(self):
        """A count-like state must not leak into the extracted schedule."""
        assert extract_tou_periods({"Period 1": "00:00-00:01/1234567/+"}) != ["1"]


class TestReadTouPeriods:
    """``_read_tou_periods`` must reflect HA *after* a write, not LiveState."""

    def test_reads_live_attributes(self):
        sensor = MagicMock()
        state = MagicMock()
        state.state = "1"
        state.attributes = {"Period 1": "00:00-00:01/1234567/+"}
        sensor.hass.states.get.return_value = state
        assert _read_tou_periods(sensor, TOU_ENTITY) == ["00:00-00:01/1234567/+"]

    def test_missing_entity_returns_none(self):
        sensor = MagicMock()
        sensor.hass.states.get.return_value = None
        assert _read_tou_periods(sensor, TOU_ENTITY) is None

    def test_no_entity_id_returns_none(self):
        assert _read_tou_periods(MagicMock(), None) is None
