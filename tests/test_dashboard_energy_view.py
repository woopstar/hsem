"""Tests for the Energy view of the bundled dashboard (issue #1435).

The view cannot be rendered in the test environment, so its Markdown
templates are rendered here against the attributes a real
:class:`EnergyTracker` publishes: a template that reads an attribute the
monthly energy sensors do not publish would show a dash, never an error.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from jinja2.sandbox import ImmutableSandboxedEnvironment

from custom_components.hsem.custom_sensors.monthly_energy_sensors import (
    MONTHLY_ENERGY_SENSORS,
)
from custom_components.hsem.models import energy_tracker
from custom_components.hsem.models.energy_tracker import EnergyDayEntry, EnergyTracker
from custom_components.hsem.utils.dashboard import _bundled_dashboard_path

_REPO = Path(__file__).resolve().parent.parent
_TODAY = date(2026, 6, 10)
_SENSOR = "sensor.hsem_monthly_self_sufficiency"


def _view(path: Path | None = None) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(
        (path or _bundled_dashboard_path()).read_text(encoding="utf-8")
    )
    view: dict[str, Any] = next(
        view for view in loaded["views"] if view["title"] == "Energy"
    )
    return view


def _cards(card_type: str) -> list[dict[str, Any]]:
    return [
        card
        for section in _view()["sections"]
        for card in section["cards"]
        if card["type"] == card_type
    ]


def _render(tracker: EnergyTracker) -> tuple[str, str]:
    """Return the Status card and the month table rendered for *tracker*."""
    attributes = tracker.as_sensor_attributes(_TODAY)

    def state_attr(entity: str, name: str) -> Any:
        assert entity == _SENSOR
        return attributes.get(name)

    environment = ImmutableSandboxedEnvironment(autoescape=False)
    status, table = (
        environment.from_string(card["content"]).render(state_attr=state_attr)
        for card in _cards("markdown")
    )
    return status, table


def _day(day: str, *, charged: float = 60.0, wear_cost: float = 1.5) -> EnergyDayEntry:
    return EnergyDayEntry(
        day,
        observed_hours=24.0,
        house_consumption_kwh=20.0,
        grid_import_kwh=5.0,
        grid_export_kwh=3.0,
        pv_kwh=18.0,
        battery_charged_kwh=charged,
        battery_discharged_kwh=charged * 0.9,
        wear_cost=wear_cost,
        paired_consumption_kwh=20.0,
        paired_import_kwh=5.0,
        measured_charged_kwh=charged,
        measured_discharged_kwh=charged * 0.9,
        usable_capacity_kwh=9.0,
        rated_capacity_kwh=10.0,
        house_hours=24.0,
        grid_import_hours=24.0,
        grid_export_hours=24.0,
        pv_hours=24.0,
        battery_hours=24.0,
        priced_throughput_kwh=charged * 1.9,
    )


def _recorded() -> EnergyTracker:
    return EnergyTracker(
        daily_log={
            entry.date: entry
            for entry in (_day("2026-05-30", charged=10.0), _day("2026-06-09"))
        }
    )


class TestTheView:
    def test_the_view_has_the_tiles_and_two_sections(self) -> None:
        view = _view()

        assert view["path"] == "energy"
        assert view["type"] == "sections"
        assert [section["cards"][0]["heading"] for section in view["sections"]] == [
            "This Month",
            "Last 12 Months",
        ]
        assert [card["entity"] for card in _cards("tile")] == [
            f"sensor.hsem_monthly_{description.key}"
            for description in MONTHLY_ENERGY_SENSORS
        ]

    def test_the_docs_dashboard_carries_the_same_view(self) -> None:
        assert _view(_REPO / "docs" / "dashboard.yaml") == _view()


class TestStatusCard:
    def test_a_new_installation_says_so(self) -> None:
        status, _table = _render(EnergyTracker())

        assert "**Month:** 2026-06 · **Days recorded:** 0" in status
        assert (
            "**Self-sufficiency unknown:** nothing has been recorded this month yet."
            in status
        )
        assert status.count("unknown:") == 4
        assert "All figures" not in status

    def test_a_recorded_month(self) -> None:
        status, _table = _render(_recorded())

        assert (
            "**Month:** 2026-06 · **Days recorded:** 1 · **Hours observed:** 24"
            in status
        )
        assert "All figures of this month are available." in status
        assert "unknown" not in status

    def test_only_the_unknown_figure_is_explained(self) -> None:
        tracker = EnergyTracker(
            daily_log={"2026-06-09": _day("2026-06-09", charged=10.0)}
        )

        status, _table = _render(tracker)

        assert status.count("unknown:") == 1
        assert "**Round-trip loss unknown:** too little energy" in status

    def test_every_reason_the_sensors_publish_is_put_in_words(self) -> None:
        reasons = {
            value
            for name, value in vars(energy_tracker).items()
            if name.startswith("REASON_")
        }
        (status_card, _table_card) = _cards("markdown")

        explained = set(re.findall(r"^\s+'([a-z_]+)': '", status_card["content"], re.M))

        assert reasons <= explained


class TestMonthTable:
    def test_one_row_per_month_newest_first(self) -> None:
        _status, table = _render(_recorded())

        rows = [line for line in table.splitlines() if line.startswith("| 20")]
        assert rows == [
            "| 2026-06 | 20 | 5 | 3 | 18 | 60 | 54 | 75 % | 6.0 | 1.50 | 10.0 % |",
            "| 2026-05 | 20 | 5 | 3 | 18 | 10 | 9 | 75 % | 1.0 | 1.50 | – |",
        ]
        header = table.splitlines()[0]
        assert header.count("|") == rows[0].count("|")

    def test_a_new_installation_has_one_row_of_dashes(self) -> None:
        _status, table = _render(EnergyTracker())

        rows = [line for line in table.splitlines() if line.startswith("| 20")]
        assert rows == ["| 2026-06 | – | – | – | – | – | – | – | – | – | – |"]

    def test_every_field_the_templates_read_is_published(self) -> None:
        published = set(EnergyTracker().month_summary("2026-06"))

        for card in _cards("markdown"):
            read = set(re.findall(r"m\.get\('([a-z_]+)'", card["content"]))
            assert read
            assert read <= published
