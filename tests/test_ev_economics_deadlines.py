"""Tests for the EV SoC economics deadline option (issue #1344).

Covers the shared parser in ``utils/ev_economics_deadlines.py``, the
``parse_time_of_day`` helper it builds on, the flow validator, the schema
field and the config reader wiring.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.hsem.const import DEFAULT_CONFIG_VALUES
from custom_components.hsem.custom_sensors.config_reader import build_sensor_config
from custom_components.hsem.flows.ev_planned_load_helpers import (
    build_ev_planned_load_schema,
    validate_ev_planned_load_schema_input,
)
from custom_components.hsem.flows.migrations import _V2_NEW_KEY_DEFAULTS
from custom_components.hsem.utils.config_validator import (
    validate_economics_deadlines,
)
from custom_components.hsem.utils.datetime_utils import parse_time_of_day
from custom_components.hsem.utils.ev_economics_deadlines import (
    DEFAULT_ECONOMICS_DEADLINES,
    MAX_ECONOMICS_DEADLINES,
    economics_deadline_entries,
    format_economics_deadline,
    parse_economics_deadlines,
)

_PREFIXES = ["hsem_ev_planned_load", "hsem_ev_second_planned_load"]


class TestParseTimeOfDay:
    """``parse_time_of_day`` reads ``HH:MM[:SS]`` and nothing else."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("08:00", (8, 0)),
            ("8:05", (8, 5)),
            (" 23:59 ", (23, 59)),
            ("06:30:00", (6, 30)),
            ("00:00", (0, 0)),
        ],
    )
    def test_valid(self, value: str, expected: tuple[int, int]) -> None:
        """Valid times parse to (hour, minute); seconds are dropped."""
        assert parse_time_of_day(value) == expected

    @pytest.mark.parametrize("value", ["24:00", "08:60", "0800", "8", "", "ab:cd"])
    def test_invalid_string(self, value: str) -> None:
        """Malformed or out-of-range strings are rejected."""
        assert parse_time_of_day(value) is None

    @pytest.mark.parametrize("value", [None, 8, 8.0, ["08:00"]])
    def test_non_string(self, value: object) -> None:
        """Non-string values are rejected."""
        assert parse_time_of_day(value) is None


class TestParseEconomicsDeadlines:
    """``parse_economics_deadlines`` normalises the stored option."""

    def test_none_is_the_default_pair(self) -> None:
        """A never-stored option keeps the pre-#1344 08:00/17:00 table."""
        assert parse_economics_deadlines(None) == ((8, 0), (17, 0))
        assert parse_economics_deadlines(None) == DEFAULT_ECONOMICS_DEADLINES

    def test_empty_list_means_no_configured_deadlines(self) -> None:
        """An explicitly cleared list is honoured, not replaced by the default."""
        assert parse_economics_deadlines([]) == ()

    def test_sorted_and_deduplicated(self) -> None:
        """Duplicates (also differently written ones) collapse; order is by time."""
        assert parse_economics_deadlines(["17:00", "06:30", "6:30", "17:00:00"]) == (
            (6, 30),
            (17, 0),
        )

    def test_comma_separated_string(self) -> None:
        """A hand-edited comma-separated string is read like a list."""
        assert parse_economics_deadlines("07:00, 18:15") == ((7, 0), (18, 15))

    def test_invalid_entries_dropped(self) -> None:
        """Entries the validator would reject are ignored when read."""
        assert parse_economics_deadlines(["25:00", "07:00", "soon"]) == ((7, 0),)

    def test_capped_at_maximum(self) -> None:
        """More than the maximum keeps the earliest times of day."""
        raw = ["05:00", "06:00", "07:00", "08:00", "09:00"]
        parsed = parse_economics_deadlines(raw)
        assert len(parsed) == MAX_ECONOMICS_DEADLINES
        assert parsed == ((5, 0), (6, 0), (7, 0))

    def test_unsupported_type_yields_nothing(self) -> None:
        """A non-list, non-string value yields no configured deadlines."""
        assert parse_economics_deadlines(800) == ()

    def test_entries_strip_blanks(self) -> None:
        """Blank entries (an empty text field in the form) are ignored."""
        assert economics_deadline_entries(["08:00", "", "  ", " 17:00 "]) == [
            "08:00",
            "17:00",
        ]
        assert economics_deadline_entries(None) == []

    def test_format(self) -> None:
        """Labels are zero-padded ``HH:MM``."""
        assert format_economics_deadline((6, 5)) == "06:05"

    def test_const_default_matches_parser_default(self) -> None:
        """The stored default and the parser default describe the same pair."""
        for prefix in _PREFIXES:
            key = f"{prefix}_economics_deadlines"
            assert (
                parse_economics_deadlines(DEFAULT_CONFIG_VALUES[key])
                == DEFAULT_ECONOMICS_DEADLINES
            )
            assert _V2_NEW_KEY_DEFAULTS[key] == DEFAULT_CONFIG_VALUES[key]


class TestValidateEconomicsDeadlines:
    """The flow validator for the deadline list."""

    @pytest.mark.parametrize(
        "value",
        [["08:00", "17:00"], [], ["06:30", "", "06:30", "18:00", "21:15"], None],
    )
    def test_valid(self, value: object) -> None:
        """Valid, empty, absent and duplicated-within-limit lists pass."""
        assert validate_economics_deadlines({"f": value}, "f") == {}

    def test_malformed_time_rejected(self) -> None:
        """A malformed entry reports ``invalid_time_value``."""
        errors = validate_economics_deadlines({"f": ["08:00", "8am"]}, "f")
        assert errors == {"f": "invalid_time_value"}

    def test_too_many_rejected(self) -> None:
        """More distinct times than the maximum report the dedicated error."""
        value = ["05:00", "06:00", "07:00", "08:00"]
        assert len(value) > MAX_ECONOMICS_DEADLINES
        errors = validate_economics_deadlines({"f": value}, "f")
        assert errors == {"f": "too_many_economics_deadlines"}


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", _PREFIXES)
async def test_step_validator_rejects_bad_deadlines(prefix: str) -> None:
    """The EV planned-load step reports deadline errors on its own field."""
    user_input = {
        f"{prefix}_enabled": True,
        f"{prefix}_battery_capacity_kwh": 75.0,
        f"{prefix}_charger_min_power_w": 1400.0,
        f"{prefix}_economics_deadlines": ["07:00", "99:00"],
    }
    errors = await validate_ev_planned_load_schema_input(user_input, prefix)
    assert errors == {f"{prefix}_economics_deadlines": "invalid_time_value"}


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", _PREFIXES)
async def test_step_validator_skips_deadlines_when_disabled(prefix: str) -> None:
    """A disabled EV step is not validated, deadlines included."""
    user_input = {
        f"{prefix}_enabled": False,
        f"{prefix}_economics_deadlines": ["99:00"],
    }
    assert await validate_ev_planned_load_schema_input(user_input, prefix) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", _PREFIXES)
async def test_schema_offers_deadlines_with_stored_default(prefix: str) -> None:
    """The step schema has the field, pre-filled with the stored list."""
    entry = MagicMock()
    entry.options = {f"{prefix}_economics_deadlines": ["06:30", "18:00"]}
    entry.data = {}
    schema = await build_ev_planned_load_schema(entry, prefix)
    key = f"{prefix}_economics_deadlines"
    marker = next(k for k in schema.schema if k == key)
    assert marker.default() == ["06:30", "18:00"]


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", _PREFIXES)
async def test_schema_default_for_new_entry(prefix: str) -> None:
    """A new entry is offered the 08:00/17:00 default."""
    schema = await build_ev_planned_load_schema(None, prefix)
    key = f"{prefix}_economics_deadlines"
    marker = next(k for k in schema.schema if k == key)
    assert marker.default() == ["08:00", "17:00"]


def test_config_reader_wires_both_evs() -> None:
    """Each EV's option reaches its own SensorConfig field, normalised."""
    entry = MagicMock()
    entry.options = {
        "hsem_ev_planned_load_economics_deadlines": ["17:30", "07:00"],
        "hsem_ev_second_planned_load_economics_deadlines": [],
    }
    entry.data = {}
    cfg = build_sensor_config(entry)
    assert cfg.ev_planned_load_economics_deadlines == ((7, 0), (17, 30))
    assert cfg.ev_second_planned_load_economics_deadlines == ()


def test_config_reader_defaults_for_existing_entry() -> None:
    """An entry saved before #1344 (no option stored) reads the default pair."""
    entry = MagicMock()
    entry.options = {}
    entry.data = {}
    cfg = build_sensor_config(entry)
    assert cfg.ev_planned_load_economics_deadlines == DEFAULT_ECONOMICS_DEADLINES
    assert cfg.ev_second_planned_load_economics_deadlines == DEFAULT_ECONOMICS_DEADLINES
