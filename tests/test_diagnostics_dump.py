"""Tests for the HSEM diagnostics dump utilities.

Covers:
- Redaction of HA entity IDs and sensitive keys.
- PlannerInput round-trip serialisation / deserialisation.
- build_diagnostics_dump structure and content.
- load_planner_input_from_dump reproduces identical planner output.
- dump_to_json produces valid JSON.
- apply_summary serialisation (with and without results).
- Edge cases: no apply_summary, empty candidates, null battery_max_discharge_power_w.
"""

from __future__ import annotations

import json

import pytest

from homeassistant.const import STATE_UNKNOWN

from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.planner import run_planner
from custom_components.hsem.utils.diagnostics import (
    _REDACTED,
    build_diagnostics_dump,
    dump_to_json,
    load_planner_input_from_dump,
    redact_dict,
)
from custom_components.hsem.utils.inverter_verify import (
    ApplyResult,
    ApplyStatus,
    CycleApplySummary,
)
from tests.planner.fixtures import make_summer_day_input, make_winter_day_input

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
# PlannerInput round-trip
# ---------------------------------------------------------------------------


class TestPlannerInputRoundTrip:
    """Tests that PlannerInput → dict → PlannerInput preserves data."""

    def test_summer_input_roundtrip(self) -> None:
        original = make_summer_day_input()
        dump = build_diagnostics_dump(original, run_planner(original))
        reconstructed = load_planner_input_from_dump(dump)

        assert reconstructed.now_iso == original.now_iso
        assert reconstructed.interval_minutes == original.interval_minutes
        assert reconstructed.battery_soc_pct == pytest.approx(original.battery_soc_pct)
        assert reconstructed.battery_rated_capacity_kwh == pytest.approx(
            original.battery_rated_capacity_kwh
        )
        assert len(reconstructed.price_points) == len(original.price_points)
        assert len(reconstructed.solcast_slots) == len(original.solcast_slots)
        assert len(reconstructed.consumption_averages) == len(
            original.consumption_averages
        )
        assert len(reconstructed.battery_schedules) == len(original.battery_schedules)

    def test_battery_schedule_times_preserved(self) -> None:
        original = make_summer_day_input()
        dump = build_diagnostics_dump(original, run_planner(original))
        reconstructed = load_planner_input_from_dump(dump)

        for orig_sched, recon_sched in zip(
            original.battery_schedules, reconstructed.battery_schedules
        ):
            assert recon_sched.start == orig_sched.start
            assert recon_sched.end == orig_sched.end
            assert recon_sched.enabled == orig_sched.enabled

    def test_null_battery_max_discharge_preserved(self) -> None:
        original = make_summer_day_input()
        original.battery_max_discharge_power_w = None
        dump = build_diagnostics_dump(original, run_planner(original))
        reconstructed = load_planner_input_from_dump(dump)
        assert reconstructed.battery_max_discharge_power_w is None

    def test_winter_input_roundtrip(self) -> None:
        original = make_winter_day_input()
        dump = build_diagnostics_dump(original, run_planner(original))
        reconstructed = load_planner_input_from_dump(dump)
        assert reconstructed.now_iso == original.now_iso
        assert reconstructed.battery_soc_pct == pytest.approx(original.battery_soc_pct)

    def test_minimal_input_roundtrip(self) -> None:
        original = _make_minimal_input()
        dump = build_diagnostics_dump(original, run_planner(original))
        reconstructed = load_planner_input_from_dump(dump)
        assert reconstructed.battery_rated_capacity_kwh == pytest.approx(0.0)

    def test_missing_planner_input_key_raises(self) -> None:
        with pytest.raises(KeyError):
            load_planner_input_from_dump({})  # no "planner_input" key


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

    def test_entity_ids_in_extra_are_redacted(self) -> None:
        inp = _make_minimal_input()
        inp.extra["debug_entity"] = "sensor.batteries_state_of_capacity"
        out = run_planner(inp)
        dump = build_diagnostics_dump(inp, out)
        assert dump["planner_input"]["extra"]["debug_entity"] == _REDACTED

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


# ---------------------------------------------------------------------------
# dump_to_json
# ---------------------------------------------------------------------------


class TestDumpToJson:
    """Tests for dump_to_json()."""

    def test_output_is_valid_json(self) -> None:
        inp = make_summer_day_input()
        out = run_planner(inp)
        dump = build_diagnostics_dump(inp, out, integration_version="5.1.0")
        json_str = dump_to_json(dump)
        parsed = json.loads(json_str)
        assert parsed["hsem_version"] == "5.1.0"

    def test_json_contains_24_slots(self) -> None:
        inp = make_summer_day_input()
        out = run_planner(inp)
        dump = build_diagnostics_dump(inp, out)
        json_str = dump_to_json(dump)
        parsed = json.loads(json_str)
        assert len(parsed["planner_output"]["slots"]) == 24

    def test_custom_indent(self) -> None:
        inp = _make_minimal_input()
        out = run_planner(inp)
        dump = build_diagnostics_dump(inp, out)
        json_str_4 = dump_to_json(dump, indent=4)
        assert "    " in json_str_4


# ---------------------------------------------------------------------------
# Reproducibility: round-trip planner output
# ---------------------------------------------------------------------------


class TestReproducibility:
    """End-to-end tests: dump → load_planner_input_from_dump → run_planner."""

    def test_summer_plan_reproduced(self) -> None:
        original_inp = make_summer_day_input()
        original_out = run_planner(original_inp)

        dump = build_diagnostics_dump(original_inp, original_out)
        replayed_inp = load_planner_input_from_dump(dump)
        replayed_out = run_planner(replayed_inp)

        # Same number of slots.
        assert len(replayed_out.slots) == len(original_out.slots)

        # Same recommendation for each slot.
        for orig_slot, replay_slot in zip(original_out.slots, replayed_out.slots):
            assert replay_slot.recommendation == orig_slot.recommendation

    def test_winter_plan_reproduced(self) -> None:
        original_inp = make_winter_day_input()
        original_out = run_planner(original_inp)

        dump = build_diagnostics_dump(original_inp, original_out)
        replayed_inp = load_planner_input_from_dump(dump)
        replayed_out = run_planner(replayed_inp)

        assert len(replayed_out.slots) == len(original_out.slots)
        for orig_slot, replay_slot in zip(original_out.slots, replayed_out.slots):
            assert replay_slot.recommendation == orig_slot.recommendation

    def test_replayed_cost_matches_original(self) -> None:
        original_inp = make_summer_day_input()
        original_out = run_planner(original_inp)

        dump = build_diagnostics_dump(original_inp, original_out)
        replayed_inp = load_planner_input_from_dump(dump)
        replayed_out = run_planner(replayed_inp)

        # Total estimated cost must be identical after round-trip.
        orig_total = sum(s.estimated_cost_currency for s in original_out.slots)
        replay_total = sum(s.estimated_cost_currency for s in replayed_out.slots)
        assert replay_total == pytest.approx(orig_total, rel=1e-6)
