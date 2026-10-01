"""Tests for the diagnostics-dump reconstruction shim (issue #1037)."""

from __future__ import annotations

import json
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from custom_components.hsem.models.hourly_consumption_average import (
    HourlyConsumptionAverage,
)
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.price_point import PricePoint
from custom_components.hsem.models.solcast_slot import SolcastSlot
from custom_components.hsem.utils.diagnostics import _planner_input_to_dict
from tests.backtest.replay import (
    DATETIME_FIELDS,
    load_dump,
    load_planner_input,
    planner_input_from_dict,
)

_TZ = timezone(timedelta(hours=2))
_DEADLINE = datetime(2026, 9, 15, 7, 0, tzinfo=_TZ)


def _payload(inp: PlannerInput) -> dict[str, Any]:
    """Serialise an input the way ``hsem.export_diagnostics`` does."""
    return {
        "hsem_version": "7.0.0-test",
        "dump_timestamp": "2026-09-14T17:24:38.517328+02:00",
        "planner_input": _planner_input_to_dict(inp),
    }


def _rich_input() -> PlannerInput:
    """Return an input exercising every field shape the shim has to rebuild."""
    return PlannerInput(
        now_iso="2026-09-14T17:21:09+02:00",
        interval_minutes=15,
        interval_length_hours=48,
        consumption_averages=[
            HourlyConsumptionAverage(hour=h, avg_1d=0.4, day_offset=d)
            for d in (0, 1)
            for h in range(24)
        ],
        price_points=[
            PricePoint(hour=h, import_price=2.1, export_price=1.7, slot_in_day=h * 4)
            for h in range(24)
        ],
        solcast_slots=[SolcastSlot(hour=h, pv_estimate=0.3) for h in range(24)],
        ev_planned_load_deadline=_DEADLINE,
        ev_second_planned_load_deadline=_DEADLINE + timedelta(hours=3),
        ev_held_slot_start=_DEADLINE - timedelta(minutes=15),
        ev_second_held_slot_start=_DEADLINE - timedelta(minutes=30),
        max_grid_export_power_kw=None,
        main_fuse_amps=25.0,
    )


class TestFieldRoundTrip:
    """The shim must reproduce the serialised input field for field."""

    def test_every_field_round_trips(self) -> None:
        original = _rich_input()
        rebuilt, report = planner_input_from_dict(_payload(original))
        assert report.is_faithful, report.describe()
        assert rebuilt == original

    def test_report_records_source_metadata(self) -> None:
        _, report = planner_input_from_dict(_payload(PlannerInput()))
        assert report.source_version == "7.0.0-test"
        assert report.dump_timestamp == "2026-09-14T17:24:38.517328+02:00"
        assert "faithful=True" in report.describe()

    def test_nested_lists_are_rebuilt_as_dataclasses(self) -> None:
        rebuilt, _ = planner_input_from_dict(_payload(_rich_input()))
        assert all(
            isinstance(x, HourlyConsumptionAverage)
            for x in rebuilt.consumption_averages
        )
        assert all(isinstance(x, PricePoint) for x in rebuilt.price_points)
        assert all(isinstance(x, SolcastSlot) for x in rebuilt.solcast_slots)
        assert rebuilt.price_points[3].slot_in_day == 12

    def test_solar_corrector_is_pinned_to_none(self) -> None:
        """The dump nulls the runtime corrector; the shim must not invent one."""
        payload = _payload(PlannerInput())
        payload["planner_input"]["solar_corrector"] = {"unexpected": "object"}
        rebuilt, _ = planner_input_from_dict(payload)
        assert rebuilt.solar_corrector is None


class TestDatetimeFields:
    """ISO strings must become datetimes, not stay strings (issue #1037)."""

    def test_deadlines_and_holds_parse_back_to_datetime(self) -> None:
        rebuilt, report = planner_input_from_dict(_payload(_rich_input()))
        assert report.is_faithful
        for name in DATETIME_FIELDS:
            value = getattr(rebuilt, name)
            assert isinstance(value, datetime), f"{name} replayed as {type(value)}"
            assert value.tzinfo is not None

    def test_datetime_field_set_is_derived_from_the_dataclass(self) -> None:
        """A datetime field added to PlannerInput must be picked up for free."""
        annotated = {f.name for f in fields(PlannerInput) if "datetime" in str(f.type)}
        assert annotated == DATETIME_FIELDS
        assert {
            "ev_planned_load_deadline",
            "ev_second_planned_load_deadline",
            "ev_held_slot_start",
            "ev_second_held_slot_start",
        } <= DATETIME_FIELDS

    def test_null_datetimes_stay_none(self) -> None:
        rebuilt, report = planner_input_from_dict(_payload(PlannerInput()))
        assert report.is_faithful
        assert all(getattr(rebuilt, name) is None for name in DATETIME_FIELDS)

    def test_malformed_datetime_is_reported_not_raised(self) -> None:
        payload = _payload(PlannerInput())
        payload["planner_input"]["ev_planned_load_deadline"] = "tomorrow-ish"
        rebuilt, report = planner_input_from_dict(payload)
        assert rebuilt.ev_planned_load_deadline is None
        assert report.malformed_datetimes == {
            "ev_planned_load_deadline": "tomorrow-ish"
        }
        assert not report.is_faithful

    def test_non_string_datetime_is_reported(self) -> None:
        payload = _payload(PlannerInput())
        payload["planner_input"]["ev_held_slot_start"] = 1757862069
        rebuilt, report = planner_input_from_dict(payload)
        assert rebuilt.ev_held_slot_start is None
        assert "ev_held_slot_start" in report.malformed_datetimes


class TestFidelityReporting:
    """Anything the shim cannot map must be reported, never dropped quietly."""

    def test_retired_field_is_reported_as_dropped(self) -> None:
        payload = _payload(PlannerInput())
        payload["planner_input"]["battery_schedules"] = [{"enabled": True}]
        rebuilt, report = planner_input_from_dict(payload)
        assert report.dropped == ["battery_schedules"]
        assert not report.is_faithful
        assert "battery_schedules" in report.describe()
        assert not hasattr(rebuilt, "battery_schedules")

    def test_absent_field_is_reported_as_defaulted(self) -> None:
        payload = _payload(PlannerInput())
        del payload["planner_input"]["live_solar_production_available"]
        rebuilt, report = planner_input_from_dict(payload)
        assert report.missing == ["live_solar_production_available"]
        assert not report.is_faithful
        assert rebuilt.live_solar_production_available is None

    def test_retired_nested_key_is_reported(self) -> None:
        payload = _payload(_rich_input())
        for row in payload["planner_input"]["price_points"]:
            row["legacy_tariff"] = 0.5
        _, report = planner_input_from_dict(payload)
        assert report.dropped_nested == {"price_points": ["legacy_tariff"]}
        assert not report.is_faithful
        assert "legacy_tariff" in report.describe()

    def test_describe_is_quiet_when_faithful(self) -> None:
        _, report = planner_input_from_dict(_payload(PlannerInput()))
        assert report.describe().splitlines() == [
            "hsem_version=7.0.0-test dumped=2026-09-14T17:24:38.517328+02:00",
            "faithful=True",
        ]


class TestDumpLoading:
    """Both shapes ``hsem.export_diagnostics`` produces must load."""

    def test_service_response_shape(self, tmp_path: Path) -> None:
        path = tmp_path / "service.json"
        path.write_text(json.dumps(_payload(_rich_input())), encoding="utf-8")
        rebuilt, report = load_planner_input(path)
        assert report.is_faithful
        assert rebuilt.interval_minutes == 15

    def test_ha_diagnostics_download_shape(self, tmp_path: Path) -> None:
        """The HA download nests the same payload under a ``data`` key."""
        path = tmp_path / "download.json"
        path.write_text(
            json.dumps({"home_assistant": {}, "data": _payload(_rich_input())}),
            encoding="utf-8",
        )
        rebuilt, report = load_planner_input(path)
        assert report.is_faithful
        assert rebuilt.interval_length_hours == 48
        assert load_dump(path)["hsem_version"] == "7.0.0-test"

    def test_dump_without_planner_input_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.json"
        path.write_text(json.dumps({"hsem_version": "7.0.0"}), encoding="utf-8")
        with pytest.raises(KeyError):
            load_planner_input(path)
