"""Tests for the HSEM diagnostics dump utilities.

Covers:
- Redaction of HA entity IDs and sensitive keys.
- build_diagnostics_dump structure and content.
- apply_summary serialisation (with and without results).
- Edge cases: no apply_summary, empty candidates, null battery_max_discharge_power_w.
"""

from __future__ import annotations

import pytest

from homeassistant.const import STATE_UNKNOWN

from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.planner import run_planner
from custom_components.hsem.utils.diagnostics import (
    _REDACTED,
    _serialise_value,
    build_diagnostics_dump,
    redact_dict,
)
from custom_components.hsem.utils.inverter_verify import (
    ApplyResult,
    ApplyStatus,
    CycleApplySummary,
)
from tests.planner.fixtures import make_summer_day_input

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_minimal_input() -> PlannerInput:
    """Return the smallest valid PlannerInput (no time-series data)."""
    return PlannerInput(
        now_iso="2024-06-15T10:00:00+02:00",
        interval_minutes=60,
        interval_length_hours=24,
        battery_rated_capacity_kwh=0.0,  # disabled — no battery simulation
    )


def _make_apply_summary(
    status: ApplyStatus = ApplyStatus.OK,
) -> CycleApplySummary:
    """Return a CycleApplySummary with one result entry."""
    result = ApplyResult(
        entity_id="select.batteries_working_mode",
        desired="time_of_use_lunar",
        actual="time_of_use_lunar",
        status=status,
        attempts=1,
    )
    summary = CycleApplySummary(results=[result], last_updated="2024-06-15T10:00:00")
    return summary


# ---------------------------------------------------------------------------
# redact_dict
# ---------------------------------------------------------------------------


class TestRedactDict:
    """Tests for redact_dict()."""

    def test_entity_id_string_is_redacted(self) -> None:
        data = {"some_sensor": "sensor.batteries_state_of_capacity"}
        result = redact_dict(data)
        assert result["some_sensor"] == _REDACTED

    def test_entity_id_in_list_is_redacted(self) -> None:
        data = {"sensors": ["sensor.foo", "number.bar_baz"]}
        result = redact_dict(data)
        assert result["sensors"] == [_REDACTED, _REDACTED]

    def test_numeric_value_not_redacted(self) -> None:
        data = {"battery_soc_pct": 75.0}
        result = redact_dict(data)
        assert result["battery_soc_pct"] == pytest.approx(75.0)

    def test_bool_value_not_redacted(self) -> None:
        data = {"is_read_only": True}
        result = redact_dict(data)
        assert result["is_read_only"] is True

    def test_sensitive_key_value_redacted(self) -> None:
        data = {"api_token": "abc123", "access_key": "secret_value"}
        result = redact_dict(data)
        assert result["api_token"] == _REDACTED
        assert result["access_key"] == _REDACTED

    def test_non_entity_string_not_redacted(self) -> None:
        data = {"now_iso": "2024-06-15T10:00:00+02:00"}
        result = redact_dict(data)
        assert result["now_iso"] == "2024-06-15T10:00:00+02:00"

    def test_nested_dict_is_recursed(self) -> None:
        data = {"outer": {"inner_entity": "sensor.foo"}}
        result = redact_dict(data)
        assert result["outer"]["inner_entity"] == _REDACTED

    def test_empty_dict(self) -> None:
        assert redact_dict({}) == {}

    def test_mixed_list_non_dict_items_not_touched(self) -> None:
        data = {"values": [1, 2.5, True, None]}
        result = redact_dict(data)
        assert result["values"] == [1, 2.5, True, None]


# ---------------------------------------------------------------------------
# build_diagnostics_dump structure
# ---------------------------------------------------------------------------


class TestBuildDiagnosticsDump:
    """Tests for build_diagnostics_dump() structure and content."""

    def test_top_level_keys_present(self) -> None:
        inp = make_summer_day_input()
        out = run_planner(inp)
        dump = build_diagnostics_dump(inp, out, integration_version="5.0.0")

        assert "hsem_version" in dump
        assert "dump_timestamp" in dump
        assert "planner_input" in dump
        assert "planner_output" in dump
        assert "apply_result" in dump

    def test_version_is_embedded(self) -> None:
        inp = _make_minimal_input()
        out = run_planner(inp)
        dump = build_diagnostics_dump(inp, out, integration_version="5.1.0")
        assert dump["hsem_version"] == "5.1.0"

    def test_no_version_defaults_to_unknown(self) -> None:
        inp = _make_minimal_input()
        out = run_planner(inp)
        dump = build_diagnostics_dump(inp, out)
        assert dump["hsem_version"] == STATE_UNKNOWN

    def test_apply_result_is_none_when_not_provided(self) -> None:
        inp = _make_minimal_input()
        out = run_planner(inp)
        dump = build_diagnostics_dump(inp, out)
        assert dump["apply_result"] is None

    def test_planner_output_has_required_keys(self) -> None:
        inp = make_summer_day_input()
        out = run_planner(inp)
        dump = build_diagnostics_dump(inp, out)
        po = dump["planner_output"]

        assert "slots" in po
        assert "charge_windows" in po
        assert "discharge_windows" in po
        assert "current_recommendation" in po
        assert "warnings" in po
        assert "data_quality" in po
        assert "explanation" in po
        assert "candidates" in po

    def test_slots_contain_expected_fields(self) -> None:
        inp = make_summer_day_input()
        out = run_planner(inp)
        dump = build_diagnostics_dump(inp, out)
        assert len(dump["planner_output"]["slots"]) == 24
        first_slot = dump["planner_output"]["slots"][0]
        assert "start" in first_slot
        assert "recommendation" in first_slot
        assert "import_price" in first_slot

    def test_numeric_input_fields_not_redacted(self) -> None:
        inp = make_summer_day_input()
        out = run_planner(inp)
        dump = build_diagnostics_dump(inp, out)
        assert dump["planner_input"]["battery_soc_pct"] == pytest.approx(50.0)
        assert dump["planner_input"]["battery_rated_capacity_kwh"] == pytest.approx(
            10.0
        )


# ---------------------------------------------------------------------------
# apply_summary serialisation
# ---------------------------------------------------------------------------


class TestApplySummarySerialization:
    """Tests for _apply_summary_to_dict via build_diagnostics_dump."""

    def test_apply_result_entity_id_redacted(self) -> None:
        inp = _make_minimal_input()
        out = run_planner(inp)
        summary = _make_apply_summary(ApplyStatus.OK)
        dump = build_diagnostics_dump(inp, out, summary)
        result_entry = dump["apply_result"]["results"][0]
        assert result_entry["entity_id"] == _REDACTED

    def test_apply_result_desired_and_actual_retained(self) -> None:
        inp = _make_minimal_input()
        out = run_planner(inp)
        summary = _make_apply_summary(ApplyStatus.OK)
        dump = build_diagnostics_dump(inp, out, summary)
        result_entry = dump["apply_result"]["results"][0]
        assert result_entry["desired"] == "time_of_use_lunar"
        assert result_entry["actual"] == "time_of_use_lunar"

    def test_apply_result_status_is_string(self) -> None:
        inp = _make_minimal_input()
        out = run_planner(inp)
        summary = _make_apply_summary(ApplyStatus.FAILED)
        dump = build_diagnostics_dump(inp, out, summary)
        assert dump["apply_result"]["overall_status"] == "failed"

    def test_apply_summary_with_no_results(self) -> None:
        inp = _make_minimal_input()
        out = run_planner(inp)
        summary = CycleApplySummary(results=[], last_updated="2024-06-15T10:00:00")
        dump = build_diagnostics_dump(inp, out, summary)
        assert dump["apply_result"]["results"] == []
        assert dump["apply_result"]["overall_status"] == "skipped"


class TestOrjsonSerialisable:
    """Home Assistant encodes service responses with orjson, not json.

    ``json`` accepts a ``numpy.float64`` because it subclasses ``float``;
    ``orjson`` dispatches on exact type and refuses it. The MILP runs on SciPy,
    so plan costs arrive as NumPy scalars and ``hsem.export_diagnostics`` failed
    for every caller while every ``json``-based test passed.
    """

    @staticmethod
    def _scalars(node: object, path: str = "") -> list[tuple[str, type]]:
        """Return every non-built-in scalar in a dump, with its path."""
        if isinstance(node, dict):
            return [
                item
                for key, value in node.items()
                for item in TestOrjsonSerialisable._scalars(value, f"{path}.{key}")
            ]
        if isinstance(node, list):
            return [
                item
                for index, value in enumerate(node)
                for item in TestOrjsonSerialisable._scalars(value, f"{path}[{index}]")
            ]
        if type(node) in (str, int, float, bool, type(None)):
            return []
        return [(path, type(node))]

    def test_real_dump_encodes_with_orjson(self) -> None:
        orjson = pytest.importorskip("orjson")
        planner_input = make_summer_day_input()
        dump = build_diagnostics_dump(
            planner_input, run_planner(planner_input), None, integration_version="t"
        )
        orjson.dumps(dump)

    def test_real_dump_has_only_builtin_scalars(self) -> None:
        planner_input = make_summer_day_input()
        dump = build_diagnostics_dump(
            planner_input, run_planner(planner_input), None, integration_version="t"
        )
        assert self._scalars(dump) == []

    def test_float_subclass_is_narrowed_to_exact_float(self) -> None:
        """A float subclass must not survive, whether or not NumPy is installed."""

        class Measured(float):
            pass

        result = _serialise_value({"cost": Measured(1.5)})
        assert type(result["cost"]) is float
        assert result["cost"] == pytest.approx(1.5)

    def test_numpy_scalars_are_narrowed(self) -> None:
        numpy = pytest.importorskip("numpy")
        result = _serialise_value(
            {"f": numpy.float64(2.5), "i": numpy.int64(7), "b": numpy.bool_(True)}
        )
        assert type(result["f"]) is float
        assert type(result["i"]) is int
        assert type(result["b"]) is bool

    def test_bool_survives_as_bool_not_int(self) -> None:
        result = _serialise_value({"on": True, "count": 3})
        assert result["on"] is True
        assert type(result["count"]) is int

    def test_unserialisable_object_degrades_to_repr(self) -> None:
        """A dump must never crash a service call."""

        class Opaque:
            def __repr__(self) -> str:
                return "<opaque>"

        assert _serialise_value({"x": Opaque()}) == {"x": "<opaque>"}
