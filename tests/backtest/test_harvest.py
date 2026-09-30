"""Tests for growing the committed corpus from a live one (issue #1037)."""

from __future__ import annotations

import copy
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tests.backtest import harvest as harvest_module
from tests.backtest.actuals import ACTUALS_SCHEMA, ENERGY_SERIES, load_actuals
from tests.backtest.conftest import CORPUS_DIR
from tests.backtest.harvest import (
    committed_actuals,
    cycle_file_name,
    expected_slots,
    harvest,
    leaks_entity_ids,
    situation_of,
    slim_payload,
    write_actuals_days,
)
from tests.backtest.invariants import InvariantViolation
from tests.backtest.replay import load_dump, load_planner_input

_ZONE = ZoneInfo("Europe/Copenhagen")
_SOURCE = CORPUS_DIR / "cycle-2026-09-14-1721.json"


def _payload(minute: int = 21, soc: float | None = None) -> dict[str, Any]:
    """A full live dump, varied by planning minute and starting SoC."""
    payload = copy.deepcopy(load_dump(_SOURCE))
    payload["planner_input"]["now_iso"] = f"2026-09-14T17:{minute:02d}:09+02:00"
    payload["dump_timestamp"] = f"2026-09-14T17:{minute:02d}:30+02:00"
    payload["planner_input"].pop("time_zone", None)  # as a pre-#1169 dump
    if soc is not None:
        payload["planner_input"]["battery_soc_pct"] = soc
    return payload


def _live(tmp_path: Path, payloads: list[dict[str, Any]]) -> Path:
    path = tmp_path / "live" / "hsem-corpus.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "Home Assistant notifications (Log started: x)\n"
        + "".join(json.dumps(p) + "\n" for p in payloads),
        encoding="utf-8",
    )
    return path


class TestSlimPayload:
    def test_keeps_only_what_the_harness_reads(self) -> None:
        slim, _ = slim_payload(load_dump(_SOURCE), "Europe/Copenhagen")
        assert set(slim) == {
            "hsem_version",
            "dump_timestamp",
            "planner_input",
            "apply_result",
        }

    def test_fills_a_missing_time_zone(self) -> None:
        slim, filled = slim_payload(_payload(), "Europe/Copenhagen")
        assert slim["planner_input"]["time_zone"] == "Europe/Copenhagen"
        assert filled == ["time_zone"]

    def test_never_overwrites_a_recorded_time_zone(self) -> None:
        payload = _payload()
        payload["planner_input"]["time_zone"] = "Europe/Berlin"
        slim, filled = slim_payload(payload, "Europe/Copenhagen")
        assert slim["planner_input"]["time_zone"] == "Europe/Berlin"
        assert filled == []

    def test_does_not_mutate_the_source(self) -> None:
        payload = _payload()
        slim_payload(payload, "Europe/Copenhagen")
        assert "time_zone" not in payload["planner_input"]


class TestPrivacyAndNaming:
    @pytest.mark.parametrize(
        "text", ['"sensor.batteries_state_of_capacity"', "switch.ev_charger"]
    )
    def test_entity_ids_are_caught(self, text: str) -> None:
        assert leaks_entity_ids(text)

    @pytest.mark.parametrize("text", ['"**REDACTED**"', '"soc": 1.5', "sensorial"])
    def test_redaction_markers_and_numbers_are_not(self, text: str) -> None:
        assert not leaks_entity_ids(text)

    def test_committed_corpus_carries_no_entity_ids(self) -> None:
        for path in CORPUS_DIR.glob("*.json"):
            assert not leaks_entity_ids(path.read_text(encoding="utf-8")), path

    def test_file_name_comes_from_the_planning_moment(self) -> None:
        inp, _ = load_planner_input(_SOURCE)
        assert cycle_file_name(inp) == "cycle-2026-09-14-1721.json"


class TestSituation:
    def test_classifies_the_committed_cycle(self) -> None:
        from custom_components.hsem.planner.engine_core import run_planner

        inp, _ = load_planner_input(_SOURCE)
        situation = situation_of(inp, run_planner(inp))
        assert situation.winner == "milp"
        assert situation.soc_band == 3
        assert situation.interval_minutes == 15
        assert not situation.crosses_dst
        assert "time_passed" not in situation.recommendations
        assert situation.describe().startswith("milp, soc band 3")


class TestHarvest:
    def test_a_new_situation_is_committed_slim_and_faithful(
        self, tmp_path: Path
    ) -> None:
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        result = harvest(
            [_live(tmp_path, [_payload()])], corpus, time_zone="Europe/Copenhagen"
        )
        assert result.checked == 1
        assert [name for name, _ in result.added] == ["cycle-2026-09-14-1721.json"]
        written = corpus / "cycle-2026-09-14-1721.json"
        assert "planner_output" not in json.loads(written.read_text())
        _, report = load_planner_input(written)
        assert report.is_faithful
        assert date(2026, 9, 14) in result.horizon_days

    def test_a_repeated_situation_is_not_committed(self, tmp_path: Path) -> None:
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        result = harvest(
            [_live(tmp_path, [_payload(21), _payload(26)])],
            corpus,
            time_zone="Europe/Copenhagen",
        )
        assert len(result.added) == 1
        assert result.skipped["situation already covered"] == 1

    def test_situations_already_committed_count(self, tmp_path: Path) -> None:
        """The committed corpus is the baseline, not just this run."""
        result = harvest(
            [_live(tmp_path, [_payload(26)])],
            CORPUS_DIR,
            time_zone="Europe/Copenhagen",
            dry_run=True,
        )
        assert result.added == []
        assert result.skipped["situation already covered"] == 1

    def test_only_cycles_after_the_resume_point_are_replayed(
        self, tmp_path: Path
    ) -> None:
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        result = harvest(
            [_live(tmp_path, [_payload(21), _payload(26, soc=10.0)])],
            corpus,
            time_zone="Europe/Copenhagen",
            since=datetime.fromisoformat("2026-09-14T17:21:30+02:00"),
        )
        assert result.checked == 1
        assert result.latest == "2026-09-14T17:26:30+02:00"

    def test_max_new_caps_a_run(self, tmp_path: Path) -> None:
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        result = harvest(
            [_live(tmp_path, [_payload(21), _payload(26, soc=10.0)])],
            corpus,
            time_zone="Europe/Copenhagen",
            max_new=1,
        )
        assert len(result.added) == 1
        assert result.capped == 1

    def test_max_corpus_caps_the_total(self, tmp_path: Path) -> None:
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        (corpus / "cycle-2026-09-14-1721.json").write_text(
            _SOURCE.read_text(encoding="utf-8"), encoding="utf-8"
        )
        result = harvest(
            [_live(tmp_path, [_payload(26, soc=10.0)])],
            corpus,
            time_zone="Europe/Copenhagen",
            max_corpus=1,
        )
        assert result.added == []
        assert result.capped == 1

    def test_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        result = harvest(
            [_live(tmp_path, [_payload()])],
            corpus,
            time_zone="Europe/Copenhagen",
            dry_run=True,
        )
        assert len(result.added) == 1
        assert list(corpus.iterdir()) == []

    def test_a_dump_that_does_not_round_trip_is_not_committed(
        self, tmp_path: Path
    ) -> None:
        """Without a zone to fill, a pre-#1169 dump is missing a field."""
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        result = harvest([_live(tmp_path, [_payload()])], corpus, time_zone=None)
        assert result.added == []
        assert result.skipped["does not round-trip (time_zone)"] == 1

    def test_a_dump_with_an_entity_id_is_not_committed(self, tmp_path: Path) -> None:
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        payload = _payload()
        payload["apply_result"] = {"entity": "sensor.batteries_state_of_capacity"}
        result = harvest(
            [_live(tmp_path, [payload])], corpus, time_zone="Europe/Copenhagen"
        )
        assert result.added == []
        assert result.skipped["contains an entity id"] == 1

    def test_a_violating_cycle_is_quarantined_not_committed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            harvest_module,
            "check_invariants",
            lambda _inp, _out: [InvariantViolation("soc_bounds", "forced")],
        )
        corpus, quarantine = tmp_path / "corpus", tmp_path / "quarantine"
        corpus.mkdir()
        result = harvest(
            [_live(tmp_path, [_payload()])],
            corpus,
            time_zone="Europe/Copenhagen",
            quarantine_dir=quarantine,
        )
        assert result.added == []
        assert [name for name, _ in result.violations] == ["cycle-2026-09-14-1721.json"]
        assert (quarantine / "cycle-2026-09-14-1721.json").exists()
        assert list(corpus.iterdir()) == []
        assert "violated an invariant" in result.describe()

    def test_the_report_names_what_was_added_and_why_not(self, tmp_path: Path) -> None:
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        result = harvest(
            [_live(tmp_path, [_payload(21), _payload(26)])],
            corpus,
            time_zone="Europe/Copenhagen",
        )
        text = result.describe()
        assert "added 1 cycle(s)" in text
        assert "situation already covered (1)" in text


class TestActualsDays:
    @pytest.mark.parametrize(
        ("day", "slots"),
        [(date(2026, 9, 15), 96), (date(2026, 10, 25), 100), (date(2026, 3, 29), 92)],
    )
    def test_expected_slots_follow_dst(self, day: date, slots: int) -> None:
        assert expected_slots(day, _ZONE, 15) == slots

    @staticmethod
    def _actuals(day: date, slots: int) -> dict[str, Any]:
        start = datetime.combine(day, datetime.min.time(), _ZONE)
        from datetime import UTC, timedelta

        stamps = [
            (start.astimezone(UTC) + timedelta(minutes=15 * i)).isoformat()
            for i in range(slots)
        ]
        return {
            "schema": ACTUALS_SCHEMA,
            "slot_minutes": 15,
            "slot_energy_kwh": {s: [[t, 0.1] for t in stamps] for s in ENERGY_SERIES},
            "slot_values": {"battery_soc_pct": [[t, 50.0] for t in stamps]},
        }

    def test_a_complete_day_is_written_and_loads(self, tmp_path: Path) -> None:
        day = date(2026, 9, 15)
        written, incomplete = write_actuals_days(
            self._actuals(day, 96), tmp_path, [day], _ZONE
        )
        assert written == [day]
        assert incomplete == []
        path = tmp_path / "actuals-2026-09-15.json"
        document = json.loads(path.read_text())
        assert document["day"] == "2026-09-15"
        assert document["time_zone"] == "Europe/Copenhagen"
        assert len(load_actuals(path).energy_kwh["house_load"]) == 96
        assert committed_actuals([path]) == {day: path}

    def test_an_incomplete_day_is_reported_not_written(self, tmp_path: Path) -> None:
        day = date(2026, 9, 15)
        written, incomplete = write_actuals_days(
            self._actuals(day, 90), tmp_path, [day], _ZONE
        )
        assert written == []
        assert incomplete == [day]
        assert not any(tmp_path.iterdir())

    def test_only_wanted_days_are_written(self, tmp_path: Path) -> None:
        wanted = date(2026, 9, 16)
        written, _ = write_actuals_days(
            self._actuals(date(2026, 9, 15), 96), tmp_path, [wanted], _ZONE
        )
        assert written == []

    def test_a_committed_day_is_left_alone(self, tmp_path: Path) -> None:
        day = date(2026, 9, 15)
        (tmp_path / "actuals-2026-09-15.json").write_text("{}")
        written, incomplete = write_actuals_days(
            self._actuals(day, 96), tmp_path, [day], _ZONE
        )
        assert (written, incomplete) == ([], [])
        assert (tmp_path / "actuals-2026-09-15.json").read_text() == "{}"

    def test_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        day = date(2026, 9, 15)
        written, _ = write_actuals_days(
            self._actuals(day, 96), tmp_path, [day], _ZONE, dry_run=True
        )
        assert written == [day]
        assert not any(tmp_path.iterdir())
