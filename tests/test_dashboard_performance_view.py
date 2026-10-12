"""Tests for the Performance view of the bundled dashboard (issue #1284).

The view cannot be rendered in the test environment, so what can go wrong
silently is pinned here instead: a Markdown template or a chart that reads an
attribute the performance sensors do not publish shows a dash or an empty
chart, never an error.  The templates are rendered against the attributes a
real :class:`PerformanceTracker` produces, and every field a chart reads is
checked against them.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml
from jinja2.sandbox import ImmutableSandboxedEnvironment

from custom_components.hsem import coordinator_performance
from custom_components.hsem.custom_sensors.performance_sensors import (
    HSEMPerformanceSensor,
)
from custom_components.hsem.models import performance_record
from custom_components.hsem.models.performance_record import (
    REASON_INSUFFICIENT_COVERAGE,
    REASON_NO_EV_SESSION,
    PerformanceScore,
)
from custom_components.hsem.models.performance_tracker import (
    DAILY_ATTRIBUTE_DAYS,
    REASON_NOT_EVALUATED,
    PerformanceTracker,
)
from custom_components.hsem.utils.dashboard import _bundled_dashboard_path

_REPO = Path(__file__).resolve().parent.parent
_NOW = datetime(2026, 6, 10, 12, 0, tzinfo=ZoneInfo("Europe/Copenhagen"))
_SENSOR = "sensor.hsem_performance_savings_capture"


def _dashboard() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(
        _bundled_dashboard_path().read_text(encoding="utf-8")
    )
    return loaded


def _view() -> dict[str, Any]:
    return next(
        view for view in _dashboard()["views"] if view["title"] == "Performance"
    )


def _cards(card_type: str) -> list[dict[str, Any]]:
    return [
        card
        for section in _view()["sections"]
        for card in section["cards"]
        if card["type"] == card_type
    ]


def _score(
    day: str,
    *,
    realized: float = 3.4,
    oracle: float = 3.0,
    pv_as_realized: float | None = 3.1,
    ev_missed: float | None = None,
    reason: str | None = None,
    control: float = 1.0,
) -> PerformanceScore:
    """Return a day's score: baseline 5, potential 2, best EV timing 2 cheaper."""
    start = datetime.fromisoformat(day).replace(tzinfo=UTC)
    return PerformanceScore(
        start=start,
        end=start + timedelta(days=1),
        slot_count=96,
        reason=reason,
        coverage=1.0,
        control_coverage=control,
        realized_cost=realized,
        oracle_cost=oracle,
        baseline_cost=5.0,
        potential=2.0,
        pv_as_realized_cost=pv_as_realized,
        ev_shiftable_kwh=None if ev_missed is None else 20.0,
        ev_joint_oracle_cost=None if ev_missed is None else oracle - ev_missed,
        ev_plug_and_charge_cost=None if ev_missed is None else oracle + 2.0,
        start_kwh=4.0,
        realized_end_kwh=4.0,
        baseline_end_kwh=4.0,
        day=day,
    )


def _render(tracker: PerformanceTracker) -> tuple[str, str]:
    """Return the Status and Periods cards rendered for *tracker*."""
    attributes = tracker.as_sensor_attributes(_NOW)

    def state_attr(entity: str, name: str) -> Any:
        assert entity == _SENSOR
        return attributes.get(name)

    environment = ImmutableSandboxedEnvironment(autoescape=False)
    status, periods = (
        environment.from_string(card["content"]).render(state_attr=state_attr)
        for card in _cards("markdown")
    )
    return status, periods


def _scored_with_ev() -> PerformanceTracker:
    tracker = PerformanceTracker(window=_score("2026-06-09", ev_missed=1.0))
    tracker.days["2026-06-08"] = _score("2026-06-08", realized=4.4)
    tracker.days["2026-06-09"] = _score("2026-06-09", ev_missed=1.0)
    return tracker


class TestTheView:
    def test_the_dashboard_has_a_performance_view(self) -> None:
        dashboard = _dashboard()

        assert [view["title"] for view in dashboard["views"]] == ["HSEM", "Performance"]
        view = _view()
        assert view["path"] == "performance"
        assert view["type"] == "sections"
        assert [section["cards"][0]["heading"] for section in view["sections"]] == [
            "Last 24 Hours",
            "Periods",
            "Where It Was Lost",
            "Trend",
        ]

    def test_the_main_view_keeps_its_summary_tiles(self) -> None:
        main = _dashboard()["views"][0]

        headings = [section["cards"][0].get("heading") for section in main["sections"]]
        assert "Performance" in headings

    def test_every_sensor_has_a_tile_and_the_ev_tile_is_conditional(self) -> None:
        assert [card["entity"] for card in _cards("tile")] == [
            "sensor.hsem_performance_savings_capture",
            "sensor.hsem_performance_actual_benefit",
            "sensor.hsem_performance_missed_opportunity",
            "sensor.hsem_performance_battery_utilisation",
            "sensor.hsem_performance_solar_utilisation",
        ]
        (conditional,) = _cards("conditional")
        assert conditional["card"]["entity"] == (
            "sensor.hsem_performance_ev_charging_efficiency"
        )
        assert conditional["conditions"] == [
            {
                "entity": "sensor.hsem_performance_ev_charging_efficiency",
                "state_not": "unavailable",
            }
        ]

    def test_the_docs_carry_the_same_dashboard(self) -> None:
        """``docs/dashboard-setup.md`` embeds the YAML for a manual import."""
        text = (_REPO / "docs" / "dashboard-setup.md").read_text(encoding="utf-8")
        start = text.index("```yaml\n", text.index("## Full dashboard YAML")) + 8
        # Up to and including the newline that ends the block's last line.
        embedded = text[start : text.index("\n```", start) + 1]

        assert yaml.safe_load(embedded) == _dashboard()


class TestStatusCard:
    def test_a_new_installation_says_so(self) -> None:
        status, _periods = _render(PerformanceTracker())

        assert "**Window:** nothing scored yet" in status
        assert "**Observed:** – · **HSEM in control:** –" in status
        assert "**Savings figures unknown:** nothing has been scored yet." in status
        assert "All figures" not in status

    def test_a_scored_window(self) -> None:
        status, _periods = _render(_scored_with_ev())

        assert "**Window:** 2026-06-09 02:00 to 2026-06-10 02:00" in status
        assert "**Observed:** 100 % · **HSEM in control:** 100 %" in status
        assert "All figures of the last 24 hours are available." in status
        assert "unknown" not in status
        assert "not in control" not in status

    def test_an_unscored_window_explains_itself_once(self) -> None:
        tracker = PerformanceTracker(
            window=_score("2026-06-09", reason=REASON_INSUFFICIENT_COVERAGE)
        )

        status, _periods = _render(tracker)

        assert "**Savings figures unknown:** part of the last 24 hours" in status
        # The split and the EV figure are unknown for the same reason; it is
        # not repeated for them.
        assert status.count("unknown") == 1

    def test_only_the_split_missing(self) -> None:
        status, _periods = _render(
            PerformanceTracker(window=_score("2026-06-09", pv_as_realized=None))
        )

        assert "Savings figures unknown" not in status
        assert "**Battery / solar split unknown:** it could not be computed" in status
        assert "All figures" not in status

    def test_no_ev_session_is_not_reported_as_a_problem(self) -> None:
        """Most installations have no EV; that is not something to explain."""
        tracker = PerformanceTracker(window=_score("2026-06-09"))
        assert tracker.as_sensor_attributes(_NOW)["ev_reason"] == REASON_NO_EV_SESSION

        status, _periods = _render(tracker)

        assert "EV" not in status
        assert "All figures of the last 24 hours are available." in status

    def test_a_window_hsem_did_not_control_is_flagged(self) -> None:
        status, _periods = _render(
            PerformanceTracker(window=_score("2026-06-09", control=0.5))
        )

        assert "**HSEM in control:** 50 %" in status
        assert "HSEM was not in control for part of this window" in status

    def test_every_reason_the_sensors_publish_is_put_in_words(self) -> None:
        """No reason code is shown raw."""
        reasons = {
            value
            for module in (performance_record, coordinator_performance)
            for name, value in vars(module).items()
            if name.startswith("REASON_")
        } | {REASON_NOT_EVALUATED}
        assert len(reasons) >= 12
        template = _cards("markdown")[0]["content"]
        environment = ImmutableSandboxedEnvironment(autoescape=False)

        for reason in sorted(reasons - {REASON_NO_EV_SESSION}):
            attributes = {
                "reason": reason,
                "breakdown_reason": reason,
                "ev_reason": reason,
            }
            rendered = environment.from_string(template).render(
                state_attr=lambda _entity, name, attributes=attributes: attributes.get(
                    name
                )
            )

            assert reason not in rendered, reason
            assert "unknown:**" in rendered, reason


class TestPeriodsCard:
    def test_a_new_installation_shows_dashes_not_zeros(self) -> None:
        _status, periods = _render(PerformanceTracker())

        assert "| **Savings capture** | – | – | – | – |" in periods
        assert "| Missed opportunity | – | – | – | – |" in periods
        assert "0.00" not in periods
        assert "Days scored: 0 of 7 and 0 of 30." in periods
        assert "EV" not in periods.split("Days scored")[0]

    def test_every_figure_for_every_period(self) -> None:
        """24 h and yesterday: 0.40 missed of 2. Two days: 1.80 missed of 4."""
        _status, periods = _render(_scored_with_ev())
        rows = dict(
            (cells[1], cells[2:6])
            for cells in (
                [cell.strip() for cell in line.split("|")]
                for line in periods.splitlines()
                if line.startswith("| ")
            )
        )

        assert rows[""] == ["24 h", "Yesterday", "7 days", "30 days"]
        assert rows["**Savings capture**"] == ["80 %", "80 %", "55 %", "55 %"]
        assert rows["Actual benefit"] == ["1.60", "1.60", "2.20", "2.20"]
        assert rows["Missed opportunity"] == ["0.40", "0.40", "1.80", "1.80"]
        assert rows["Potential"] == ["2.00", "2.00", "4.00", "4.00"]
        assert rows["**Battery utilisation**"] == ["85 %", "85 %", "60 %", "60 %"]
        assert rows["Battery loss"] == ["0.30", "0.30", "1.60", "1.60"]
        assert rows["**Solar utilisation**"] == ["95 %", "95 %", "95 %", "95 %"]
        assert rows["Solar loss"] == ["0.10", "0.10", "0.20", "0.20"]
        assert rows["**EV charging efficiency**"] == ["67 %", "67 %", "67 %", "67 %"]
        assert rows["EV missed opportunity"] == ["1.00", "1.00", "1.00", "1.00"]
        assert "Days scored: 2 of 7 and 2 of 30." in periods

    def test_the_table_is_one_block_of_rows(self) -> None:
        """A blank line inside it would end the Markdown table."""
        _status, periods = _render(_scored_with_ev())
        lines = periods.splitlines()
        table = [index for index, line in enumerate(lines) if line.startswith("|")]

        assert table == list(range(table[0], table[0] + 12))
        assert lines[table[0] + 1] == "|:--|--:|--:|--:|--:|"

    def test_without_ev_figures_there_are_no_ev_rows(self) -> None:
        tracker = PerformanceTracker(window=_score("2026-06-09"))
        tracker.days["2026-06-09"] = _score("2026-06-09")

        _status, periods = _render(tracker)

        assert "EV charging efficiency" not in periods
        assert "| Solar loss | 0.10 | 0.10 | 0.10 | 0.10 |" in periods

    def test_a_negative_share_is_shown_as_it_is(self) -> None:
        _status, periods = _render(
            PerformanceTracker(window=_score("2026-06-09", realized=6.0))
        )

        assert "| **Savings capture** | -50 % |" in periods


class TestWhatTheCardsRead:
    """Nothing reads an attribute the sensors do not publish."""

    def test_the_markdown_cards_read_published_attributes(self) -> None:
        attributes = _scored_with_ev().as_sensor_attributes(_NOW)
        nested = set(attributes["last_7_days"]) | set(attributes["yesterday"])

        for card in _cards("markdown"):
            content = card["content"]
            for name in re.findall(r"state_attr\(s, '([a-z0-9_]+)'\)", content):
                assert name in attributes, name
            for name in re.findall(r"(?:pct|money)\('([a-z0-9_]+)'\)", content):
                assert name in attributes and name in nested, name
            for name in re.findall(r"\.get\('([a-z0-9_]+)'", content):
                assert name in nested, name
            assert set(re.findall(r"set s = '([a-z0-9_.]+)'", content)) == {_SENSOR}

    def test_the_charts_read_fields_of_a_daily_entry(self) -> None:
        (day, *_rest) = _scored_with_ev().as_sensor_attributes(_NOW)["daily"]
        series = [
            series
            for card in _cards("custom:apexcharts-card")
            for series in card["series"]
        ]

        assert len(series) == 8
        read: set[str] = set()
        for entry in series:
            generator = entry["data_generator"]
            assert "entity.attributes.daily" in generator
            assert entry["entity"].startswith("sensor.hsem_performance_")
            fields = set(re.findall(r"\bday\.([a-z0-9_]+)", generator))
            assert fields - {"date"}, entry["name"]
            read |= fields
        assert read <= set(day)
        assert read == {
            "date",
            "battery_loss",
            "solar_loss",
            "ev_missed_opportunity",
            "missed_opportunity",
            "savings_capture",
            "battery_utilisation",
            "solar_utilisation",
            "ev_charging_efficiency",
        }

    def test_the_charts_cover_the_days_the_attribute_holds(self) -> None:
        for card in _cards("custom:apexcharts-card"):
            assert card["graph_span"] == f"{DAILY_ATTRIBUTE_DAYS}d"
            assert card["span"] == {"end": "day"}


class TestDailyAttribute:
    def test_the_newest_completed_days_oldest_first(self) -> None:
        tracker = PerformanceTracker()
        first = date(2026, 4, 1)
        for offset in range(80):
            day = (first + timedelta(days=offset)).isoformat()
            tracker.days[day] = _score(day)
        today = _NOW.date().isoformat()
        tracker.days[today] = _score(today)

        daily = tracker.as_sensor_attributes(_NOW)["daily"]

        assert len(daily) == DAILY_ATTRIBUTE_DAYS
        dates = [entry["date"] for entry in daily]
        assert dates == sorted(dates)
        assert dates[-1] == "2026-06-09"
        assert today not in dates

    def test_a_days_figures(self) -> None:
        tracker = PerformanceTracker()
        tracker.days["2026-06-09"] = _score("2026-06-09", ev_missed=1.0)

        (entry,) = tracker.as_sensor_attributes(_NOW)["daily"]

        assert entry == {
            "date": "2026-06-09",
            "reason": None,
            "actual_benefit": pytest.approx(1.6),
            "missed_opportunity": pytest.approx(0.4),
            "savings_capture": pytest.approx(80.0),
            "potential": pytest.approx(2.0),
            "battery_loss": pytest.approx(0.3),
            "solar_loss": pytest.approx(0.1),
            "battery_utilisation": pytest.approx(85.0),
            "solar_utilisation": pytest.approx(95.0),
            "ev_missed_opportunity": pytest.approx(1.0),
            "ev_charging_efficiency": pytest.approx(66.7),
        }

    def test_an_unscored_day_is_listed_with_its_reason_and_no_figures(self) -> None:
        tracker = PerformanceTracker()
        tracker.days["2026-06-09"] = _score(
            "2026-06-09", reason=REASON_INSUFFICIENT_COVERAGE
        )

        (entry,) = tracker.as_sensor_attributes(_NOW)["daily"]

        assert entry["reason"] == REASON_INSUFFICIENT_COVERAGE
        assert entry["missed_opportunity"] is None
        assert entry["battery_loss"] is None

    def test_no_days_is_an_empty_list(self) -> None:
        assert PerformanceTracker().as_sensor_attributes(_NOW)["daily"] == []

    def test_the_list_stays_out_of_the_recorder(self) -> None:
        assert "daily" in HSEMPerformanceSensor._unrecorded_attributes
