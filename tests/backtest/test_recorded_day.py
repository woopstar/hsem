"""Tests for fully recorded days (issue #1229).

The regret attribution needs a planner cycle for (almost) every slot of a
day.  ``tests/backtest/days/`` holds such days apart from the corpus; these
tests cover how one is written and loaded, keep the committed ones honest,
and pin the attribution of the committed day so it is reproducible from the
repository.
"""

from __future__ import annotations

import copy
import importlib.util
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.planner.milp_optimizer import is_scipy_available
from tests.backtest.actuals import ACTUALS_SCHEMA, ENERGY_SERIES
from tests.backtest.attribution import (
    MIN_CYCLE_COVERAGE,
    DayAttribution,
    attribute_day,
    cycles_by_slot,
)
from tests.backtest.harvest import expected_slots, leaks_entity_ids
from tests.backtest.recorded_day import (
    CYCLES_FILE,
    DAYS_DIR,
    load_recorded_day,
    recorded_days,
    refresh_recorded_days,
    write_recorded_day,
)
from tests.backtest.replay import generous_solver_limit, planner_input_from_dict
from tests.backtest.scoring import SiteLimits, day_slot_keys
from tests.backtest.site import site_of
from tests.backtest.test_attribution import (
    _DAY,
    _LOAD,
    _PRICES,
    _START_SOC_PCT,
    _SUNNY,
    _ZONE,
    _cycles,
    _site,
)

pytestmark = pytest.mark.skipif(
    not is_scipy_available(), reason="scipy not available in this environment"
)

_SITE = "site-t"
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _actuals_payload(days: int = 2) -> dict[str, Any]:
    """Return hourly actuals for the synthetic day and the days after it."""
    energy: dict[str, list[list[Any]]] = {series: [] for series in ENERGY_SERIES}
    values: dict[str, list[list[Any]]] = {
        "import_price": [],
        "export_price": [],
        "battery_soc_pct": [],
    }
    for offset in range(days):
        keys = day_slot_keys(_DAY + timedelta(days=offset), _ZONE, 60)
        for key, load, sun, price in zip(keys, _LOAD, _SUNNY, _PRICES):
            stamp = key.astimezone(UTC).isoformat()
            net = load - sun
            energy["grid_import"].append([stamp, max(net, 0.0)])
            energy["grid_export"].append([stamp, max(-net, 0.0)])
            energy["battery_charged"].append([stamp, 0.0])
            energy["battery_discharged"].append([stamp, 0.0])
            energy["pv_produced"].append([stamp, sun])
            energy["house_load"].append([stamp, load])
            values["import_price"].append([stamp, price])
            values["export_price"].append([stamp, price - 0.3])
            values["battery_soc_pct"].append([stamp, _START_SOC_PCT])
    return {
        "schema": ACTUALS_SCHEMA,
        "slot_minutes": 60,
        "slot_energy_kwh": energy,
        "slot_values": values,
    }


def _error_sum(result: DayAttribution) -> float:
    """Return execution + forecast + planner error of an attributed day."""
    assert result.regret is not None
    assert result.execution_error is not None
    assert result.forecast_error is not None
    assert result.planner_error is not None
    return result.execution_error + result.forecast_error + result.planner_error


def _write(tmp_path: Path, **overrides: Any) -> Any:
    arguments: dict[str, Any] = {
        "payloads": _cycles(_SUNNY),
        "actuals": _actuals_payload(),
        "day": _DAY,
        "zone": _ZONE,
        "site": _SITE,
        "days_dir": tmp_path / "days",
    }
    arguments.update(overrides)
    return write_recorded_day(**arguments)


class TestWriteRecordedDay:
    def test_a_day_is_written_one_cycle_per_slot_with_two_days_of_actuals(
        self, tmp_path: Path
    ) -> None:
        result = _write(tmp_path)

        assert result.written
        assert result.cycles == result.slots == 24
        directory = tmp_path / "days" / _DAY.isoformat()
        assert [path.name for path, _size in result.files] == [
            f"actuals-{_DAY}.json",
            f"actuals-{_DAY + timedelta(days=1)}.json",
            CYCLES_FILE,
        ]
        assert all(size == path.stat().st_size for path, size in result.files)
        lines = (directory / CYCLES_FILE).read_text(encoding="utf-8").splitlines()
        assert len(lines) == 24
        assert all(json.loads(line)["site"] == _SITE for line in lines)
        assert str(directory) in result.describe()

    def test_the_first_cycle_of_a_slot_is_the_one_kept(self, tmp_path: Path) -> None:
        """As ``cycles_by_slot`` picks them: several dumps carry one input."""
        cycles = _cycles(_SUNNY)
        later = copy.deepcopy(cycles[5])
        moment = datetime.fromisoformat(later["planner_input"]["now_iso"])
        later["planner_input"]["now_iso"] = (moment + timedelta(minutes=20)).isoformat()

        _write(tmp_path, payloads=[*cycles, later])

        payloads, _actuals = load_recorded_day(tmp_path / "days" / _DAY.isoformat())
        assert len(payloads) == 24
        assert payloads[5]["planner_input"]["now_iso"] == moment.isoformat()

    def test_a_written_day_loads_and_attributes_like_the_live_one(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path)

        payloads, actuals = load_recorded_day(tmp_path / "days" / _DAY.isoformat())
        assert actuals.site_tag == _SITE
        with generous_solver_limit():
            recorded = attribute_day(actuals, payloads, _DAY, _ZONE, _site())

        assert recorded.is_attributed
        assert _error_sum(recorded) == pytest.approx(recorded.regret)

    def test_untagged_live_cycles_and_actuals_get_the_site_tag(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path)

        directory = tmp_path / "days" / _DAY.isoformat()
        for path in directory.glob("actuals-*.json"):
            assert json.loads(path.read_text(encoding="utf-8"))["site"] == _SITE

    def test_a_site_tag_is_required(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="needs a site tag"):
            _write(tmp_path, site=None)
        with pytest.raises(ValueError, match="invalid site tag"):
            _write(tmp_path, site="Elm Street 4")
        assert not (tmp_path / "days").exists()

    def test_actuals_of_another_installation_are_refused(self, tmp_path: Path) -> None:
        result = _write(tmp_path, actuals={**_actuals_payload(), "site": "site-other"})

        assert not result.written
        assert "site 'site-other'" in result.refused[0]
        assert not (tmp_path / "days").exists()

    def test_cycles_of_another_installation_are_refused(self, tmp_path: Path) -> None:
        cycles = [{**cycle, "site": "site-other"} for cycle in _cycles(_SUNNY)]

        result = _write(tmp_path, payloads=cycles)

        assert not result.written
        assert any("site 'site-other'" in reason for reason in result.refused)

    def test_a_day_with_too_few_cycles_is_refused(self, tmp_path: Path) -> None:
        result = _write(tmp_path, payloads=_cycles(_SUNNY, range(11)))

        assert not result.written
        assert result.refused == [
            "only 11 of 24 slot(s) have a planner cycle recorded in them"
        ]
        assert "not written" in result.describe()
        assert not (tmp_path / "days").exists()

    def test_half_the_slots_are_enough(self, tmp_path: Path) -> None:
        result = _write(tmp_path, payloads=_cycles(_SUNNY, range(12)))

        assert result.written
        assert result.cycles == 12
        assert result.cycles >= MIN_CYCLE_COVERAGE * result.slots

    def test_incomplete_actuals_on_the_following_day_are_refused(
        self, tmp_path: Path
    ) -> None:
        result = _write(tmp_path, actuals=_actuals_payload(days=1))

        assert not result.written
        assert result.refused == [
            f"the actuals of {_DAY + timedelta(days=1)} are not complete"
        ]

    def test_a_cycle_with_an_entity_id_is_refused(self, tmp_path: Path) -> None:
        cycles = _cycles(_SUNNY)
        cycles[3]["apply_result"] = {"entity": "sensor.batteries_state_of_capacity"}

        result = _write(tmp_path, payloads=cycles)

        assert not result.written
        assert len(result.refused) == 1
        assert "contains an entity id" in result.refused[0]

    def test_a_cycle_that_cannot_round_trip_is_refused(self, tmp_path: Path) -> None:
        cycles = _cycles(_SUNNY)
        cycles[3]["planner_input"]["ev_planned_load_deadline"] = "not a time"

        result = _write(tmp_path, payloads=cycles)

        assert not result.written
        assert "does not round-trip" in result.refused[0]

    def test_a_field_the_cycles_predate_gets_its_default_and_is_reported(
        self, tmp_path: Path
    ) -> None:
        cycles = _cycles(_SUNNY)
        for cycle in cycles:
            del cycle["planner_input"]["battery_target_soc_enabled"]

        result = _write(tmp_path, payloads=cycles)

        assert result.written
        assert result.filled == {"battery_target_soc_enabled": False}
        assert "battery_target_soc_enabled = False" in result.describe()
        payloads, _actuals = load_recorded_day(tmp_path / "days" / _DAY.isoformat())
        assert all(planner_input_from_dict(p)[1].is_faithful for p in payloads)

    def test_an_existing_day_is_left_alone(self, tmp_path: Path) -> None:
        _write(tmp_path)
        before = sorted((tmp_path / "days" / _DAY.isoformat()).iterdir())

        result = _write(tmp_path)

        assert not result.written
        assert "already exists" in result.refused[0]
        assert sorted((tmp_path / "days" / _DAY.isoformat()).iterdir()) == before

    def test_dry_run_checks_everything_and_writes_nothing(self, tmp_path: Path) -> None:
        result = _write(tmp_path, dry_run=True)

        assert result.written
        assert result.cycles == 24
        assert result.files == []
        assert not (tmp_path / "days").exists()


class TestRecordedDays:
    def test_days_are_found_by_their_directory(self, tmp_path: Path) -> None:
        _write(tmp_path)
        (tmp_path / "days" / "2026-06-11").mkdir()  # no cycles: not a day
        (tmp_path / "days" / "README.md").write_text("x", encoding="utf-8")

        assert recorded_days(tmp_path / "days") == {
            _DAY: tmp_path / "days" / _DAY.isoformat()
        }

    def test_no_directory_no_days(self, tmp_path: Path) -> None:
        assert recorded_days(tmp_path / "missing") == {}


class TestRefreshRecordedDays:
    """A new ``PlannerInput`` field must not strand a recorded day."""

    def test_a_day_in_step_is_left_alone(self, tmp_path: Path) -> None:
        _write(tmp_path)
        path = tmp_path / "days" / _DAY.isoformat() / CYCLES_FILE
        before = path.read_text(encoding="utf-8")

        result = refresh_recorded_days(tmp_path / "days")

        assert result.unchanged == 24
        assert result.updated == []
        assert path.read_text(encoding="utf-8") == before

    def _stale_day(self, tmp_path: Path) -> Path:
        """Write a day, then drop a field and add an unknown one in two cycles."""
        _write(tmp_path)
        path = tmp_path / "days" / _DAY.isoformat() / CYCLES_FILE
        payloads = [json.loads(line) for line in path.read_text().splitlines()]
        for payload in payloads[:2]:
            del payload["planner_input"]["battery_target_soc_enabled"]
            payload["planner_input"]["removed_field"] = 1
        path.write_text(
            "".join(json.dumps(payload) + "\n" for payload in payloads),
            encoding="utf-8",
        )
        return path

    def test_missing_fields_are_filled_and_unknown_ones_dropped(
        self, tmp_path: Path
    ) -> None:
        path = self._stale_day(tmp_path)

        result = refresh_recorded_days(tmp_path / "days")

        assert result.unchanged == 22
        assert [name for name, _filled, _removed in result.updated] == [
            f"{_DAY}/{CYCLES_FILE}:1",
            f"{_DAY}/{CYCLES_FILE}:2",
        ]
        assert result.updated[0][1] == {"battery_target_soc_enabled": False}
        assert result.updated[0][2] == ["removed_field"]
        payloads = [json.loads(line) for line in path.read_text().splitlines()]
        assert len(payloads) == 24
        assert all(planner_input_from_dict(p)[1].is_faithful for p in payloads)

    def test_dry_run_reports_and_writes_nothing(self, tmp_path: Path) -> None:
        path = self._stale_day(tmp_path)
        before = path.read_text(encoding="utf-8")

        result = refresh_recorded_days(tmp_path / "days", dry_run=True)

        assert len(result.updated) == 2
        assert path.read_text(encoding="utf-8") == before

    def test_a_cycle_that_cannot_be_repaired_is_named(self, tmp_path: Path) -> None:
        _write(tmp_path)
        path = tmp_path / "days" / _DAY.isoformat() / CYCLES_FILE
        payloads = [json.loads(line) for line in path.read_text().splitlines()]
        payloads[4]["planner_input"]["ev_planned_load_deadline"] = "not a time"
        path.write_text(
            "".join(json.dumps(payload) + "\n" for payload in payloads),
            encoding="utf-8",
        )

        result = refresh_recorded_days(tmp_path / "days")

        assert [name for name, _reason in result.unfixable] == [
            f"{_DAY}/{CYCLES_FILE}:5"
        ]


# ---------------------------------------------------------------------------
# The committed day
# ---------------------------------------------------------------------------

#: The day committed with issue #1229 and what its attribution must reproduce.
_COMMITTED_DAY = date(2026, 9, 29)
_COMMITTED = {
    "realized_cost": 15.293,
    "regret": 2.294,
    "forecast_run_cost": 11.668,
    "hindsight_run_cost": 10.421,
    "execution_error": 0.789,
    "forecast_error": 1.247,
    "planner_error": 0.258,
}


def _committed_zone(payloads: list[dict[str, Any]]) -> ZoneInfo:
    return ZoneInfo(payloads[0]["planner_input"]["time_zone"])


class TestCommittedDays:
    """Every recorded day in the repository is complete, tagged and readable."""

    def test_the_day_of_issue_1229_is_committed(self) -> None:
        assert _COMMITTED_DAY in recorded_days()

    def test_cycles_round_trip_and_carry_no_entity_id(self) -> None:
        for directory in recorded_days().values():
            text = (directory / CYCLES_FILE).read_text(encoding="utf-8")
            assert not leaks_entity_ids(text), directory.name
            payloads, _actuals = load_recorded_day(directory)
            for payload in payloads:
                assert set(payload) <= {
                    "hsem_version",
                    "dump_timestamp",
                    "planner_input",
                    "apply_result",
                    "site",
                }
                _inp, report = planner_input_from_dict(payload)
                assert report.is_faithful, f"{directory.name}: {report.describe()}"

    def test_cycles_and_actuals_are_from_one_installation(self) -> None:
        for directory in recorded_days().values():
            payloads, actuals = load_recorded_day(directory)
            assert actuals.site_tag is not None, directory.name
            assert {site_of(payload) for payload in payloads} == {actuals.site_tag}

    def test_at_least_half_of_the_slots_have_a_cycle(self) -> None:
        for day, directory in recorded_days().items():
            payloads, actuals = load_recorded_day(directory)
            zone = _committed_zone(payloads)
            covered = cycles_by_slot(payloads, day, zone, actuals.slot_minutes)
            slots = expected_slots(day, zone, actuals.slot_minutes)
            assert len(covered) == len(payloads), directory.name
            assert len(covered) >= MIN_CYCLE_COVERAGE * slots, directory.name

    def test_actuals_are_complete_for_the_day_and_the_following_day(self) -> None:
        for day, directory in recorded_days().items():
            payloads, actuals = load_recorded_day(directory)
            zone = _committed_zone(payloads)
            for wanted in (day, day + timedelta(days=1)):
                keys = day_slot_keys(wanted, zone, actuals.slot_minutes)
                for series in ENERGY_SERIES:
                    held = actuals.energy_kwh.get(series, {})
                    missing = [key for key in keys if key not in held]
                    assert not missing, f"{directory.name}: {series} on {wanted}"


@pytest.fixture(scope="module")
def committed_attribution() -> DayAttribution:
    """Attribute the committed day once: every slot is planned twice."""
    payloads, actuals = load_recorded_day(recorded_days()[_COMMITTED_DAY])
    planner_input, _report = planner_input_from_dict(payloads[0])
    with generous_solver_limit():
        return attribute_day(
            actuals,
            payloads,
            _COMMITTED_DAY,
            _committed_zone(payloads),
            SiteLimits.from_planner_input(planner_input),
        )


@pytest.mark.slow
@pytest.mark.timeout(600)
class TestCommittedDayAttribution:
    """A real regret attribution, reproducible from the repository alone.

    The numbers move when the planner's decisions on this day move.  That is
    the point of pinning them: re-run ``python3 scripts/backtest_attribute.py``,
    check that the change is the one intended, and update ``_COMMITTED``.
    """

    def test_the_day_is_attributed_without_notes(
        self, committed_attribution: DayAttribution
    ) -> None:
        assert committed_attribution.unscorable is None
        assert committed_attribution.is_attributed
        assert committed_attribution.stale_slots == 0

    def test_the_three_errors_add_up_to_the_regret(
        self, committed_attribution: DayAttribution
    ) -> None:
        assert _error_sum(committed_attribution) == pytest.approx(
            committed_attribution.regret
        )

    def test_every_run_is_at_or_above_its_oracle(
        self, committed_attribution: DayAttribution
    ) -> None:
        for regret in (
            committed_attribution.regret,
            committed_attribution.forecast_run_regret,
            committed_attribution.hindsight_run_regret,
        ):
            assert regret is not None
            assert regret >= -1e-6

    @pytest.mark.parametrize("field", sorted(_COMMITTED))
    def test_the_attribution_is_the_committed_one(
        self, committed_attribution: DayAttribution, field: str
    ) -> None:
        assert getattr(committed_attribution, field) == pytest.approx(
            _COMMITTED[field], abs=0.01
        )


# ---------------------------------------------------------------------------
# The script's default
# ---------------------------------------------------------------------------


def _load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        name, _REPO_ROOT / "scripts" / f"{name}.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestAttributeScriptDefault:
    """``backtest_attribute.py`` without arguments attributes the committed days."""

    def test_no_arguments_means_the_committed_days(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        script = _load_script("backtest_attribute")
        seen: list[tuple[str | None, int, list[date]]] = []

        def fake_attribute(
            actuals: Any, payloads: list[dict[str, Any]], days: list[date], zone: Any
        ) -> list[DayAttribution]:
            seen.append((actuals.site_tag, len(payloads), days))
            return [
                DayAttribution(
                    day=days[0],
                    realized_cost=3.0,
                    regret=2.0,
                    forecast_run_cost=2.5,
                    forecast_run_regret=1.5,
                    hindsight_run_cost=2.0,
                    hindsight_run_regret=0.5,
                )
            ]

        monkeypatch.setattr(script, "_attribute", fake_attribute)

        assert script.main([]) == 0

        committed = recorded_days()
        assert [days for _tag, _count, days in seen] == [[day] for day in committed]
        assert all(tag is not None for tag, _count, _days in seen)
        assert str(_COMMITTED_DAY) in capsys.readouterr().out

    def test_a_day_that_is_not_committed_is_named(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        script = _load_script("backtest_attribute")

        assert script.main(["--day", "2001-01-01"]) == 1

        assert "no recorded day is committed" in capsys.readouterr().out

    def test_actuals_need_a_corpus_and_a_day(self) -> None:
        script = _load_script("backtest_attribute")

        with pytest.raises(SystemExit, match="--corpus and --day are required"):
            script.main([str(DAYS_DIR)])
