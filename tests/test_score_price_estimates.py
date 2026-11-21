"""Tests for ``scripts/score_price_estimates.py`` (issue #1452).

The script scores the planner's missing-price estimates on real prices.  The
tests pin that its estimates are the planner's own, and that the committed
price sample reads as complete local days.
"""

from __future__ import annotations

import io
import json
import math
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.price_point import PricePoint
from custom_components.hsem.models.price_profile import PriceProfile
from custom_components.hsem.planner.slot_population import (
    build_slots,
    build_time_series_index,
)
from custom_components.hsem.planner.slot_price_population import populate_prices
from tests.script_loader import load_script

score = load_script("score_price_estimates")


def _rows(first: date, days: int) -> list[tuple[datetime, float]]:
    """Return *days* local days of spot prices with a daily and weekly shape."""
    start = datetime.combine(first, datetime.min.time(), score.ZONE).astimezone(UTC)
    end = datetime.combine(
        first + timedelta(days=days), datetime.min.time(), score.ZONE
    ).astimezone(UTC)
    rows = []
    moment = start
    while moment < end:
        local = moment.astimezone(score.ZONE)
        price = 500.0 + 20.0 * local.hour + 5.0 * local.weekday() + local.day
        rows.append((moment, price))
        moment += timedelta(minutes=15)
    return rows


@pytest.fixture
def small_days(tmp_path: Path) -> dict[date, Any]:
    """Return 14 synthetic local days read back from a price file."""
    path = tmp_path / "prices.csv"
    score.write_prices(path, _rows(date(2026, 3, 2), 14), "DK1")
    days: dict[date, Any] = score.load_days(path)
    return days


class TestPriceFile:
    """Reading and writing the price file."""

    def test_the_committed_sample_holds_complete_local_days(self) -> None:
        days = score.load_days(score.DEFAULT_PRICES)

        assert len(days) >= 8 * 7 + score.FULL_HISTORY_DAYS
        assert len(days[date(2026, 10, 25)]) == 100
        assert all(
            len(slots) == 96 for day, slots in days.items() if day != date(2026, 10, 25)
        )
        assert len(score.target_days(days)) >= 8 * 7
        header = score.DEFAULT_PRICES.read_text(encoding="utf-8").splitlines()[:3]
        assert "Energinet (www.energidataservice.dk)" in header[1]
        assert "CC BY 4.0" in header[2]

    def test_prices_are_per_kwh_with_vat_on_import(
        self, small_days: dict[date, Any]
    ) -> None:
        start, import_price, export_price = small_days[date(2026, 3, 2)][0]

        assert start == datetime(2026, 3, 1, 23, 0, tzinfo=UTC)
        assert export_price == pytest.approx(0.502)
        assert import_price == pytest.approx(0.502 * score.IMPORT_FACTOR)

    def test_an_incomplete_first_or_last_day_is_dropped(self, tmp_path: Path) -> None:
        rows = _rows(date(2026, 3, 2), 4)[8:-8]
        path = tmp_path / "prices.csv"
        score.write_prices(path, rows, "DK1")

        assert sorted(score.load_days(path)) == [date(2026, 3, 3), date(2026, 3, 4)]

    def test_fetch_reads_the_public_api(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[str] = []

        def fake_urlopen(url: str, timeout: int) -> io.StringIO:
            seen.append(url)
            return io.StringIO(
                json.dumps(
                    {
                        "records": [
                            {
                                "TimeUTC": "2026-03-01T23:00:00",
                                "DayAheadPriceDKK": 502.5,
                            }
                        ]
                    }
                )
            )

        monkeypatch.setattr(score, "urlopen", fake_urlopen)

        rows = score.fetch_prices("2026-03-02", "2026-03-03", "DK1")

        assert rows == [(datetime(2026, 3, 1, 23, 0, tzinfo=UTC), 502.5)]
        assert seen[0].startswith(
            "https://api.energidataservice.dk/dataset/DayAheadPrices?"
        )
        assert "DK1" in seen[0]


class TestEstimates:
    """The scored estimates are the planner's own."""

    def test_the_implemented_estimate_is_what_the_planner_fills(self) -> None:
        """Hide a day of the sample and compare with ``populate_prices``."""
        days = score.load_days(score.DEFAULT_PRICES)
        today, tomorrow = date(2026, 11, 10), date(2026, 11, 11)
        profile = PriceProfile.from_samples(
            score._samples(days, today - timedelta(days=1), 56), score.ZONE
        )
        estimates = score.estimate_day(days[tomorrow], days[today], profile)
        now = days[today][36][0].astimezone(score.ZONE)
        inp = PlannerInput(
            now_iso=now.isoformat(),
            time_zone="Europe/Copenhagen",
            interval_minutes=15,
            interval_length_hours=48,
        )
        tsi = build_time_series_index(inp, now)
        slots = build_slots(inp, now)
        points = [
            PricePoint(
                hour=index // 4,
                import_price=import_price,
                export_price=export_price,
                day_offset=0,
                slot_in_day=index,
            )
            for index, (_, import_price, export_price) in enumerate(days[today])
        ]

        populate_prices(slots, points, tsi, profile)

        planned = {slot.start.astimezone(UTC): slot.price for slot in slots}
        assert all(pair is not None for pair in estimates["shifted"])
        for (start, _, _), pair in zip(days[tomorrow], estimates["shifted"]):
            assert planned[start].import_price == pytest.approx(pair[0])
            assert planned[start].export_price == pytest.approx(pair[1])

    def test_the_nearest_day_is_the_same_clock_time(
        self, small_days: dict[date, Any]
    ) -> None:
        anchor, target = small_days[date(2026, 3, 9)], small_days[date(2026, 3, 10)]

        estimates = score.estimate_day(target, anchor, PriceProfile())

        assert estimates["nearest"] == [(slot[1], slot[2]) for slot in anchor]
        assert estimates["shifted"] == [None] * 96
        assert estimates["unshifted"] == [None] * 96

    def test_a_slot_no_estimate_covers_is_left_out(
        self, small_days: dict[date, Any]
    ) -> None:
        target = small_days[date(2026, 3, 10)]
        real = [(slot[1], slot[2]) for slot in target]
        estimate = [None, (real[1][0] + 0.5, real[1][1] - 0.25), *([None] * 94)]

        assert score.slot_errors(target, estimate) == [
            pytest.approx((0.5, 0.25)),
        ]
        errors = score.slot_errors(target, estimate, real)
        assert len(errors) == 96
        assert errors[0] == pytest.approx((0.0, 0.0))

    def test_bucket_tables_can_be_scored_alone(self) -> None:
        profile = PriceProfile(
            quarters={(0, 1, 0): (1.0, 1.0)},
            hours={(0, 1): (1.0, 1.0)},
            any_day_quarters={(1, 0): (2.0, 2.0)},
            any_day_hours={1: (2.0, 2.0)},
        )

        assert score.weekday_only(profile).lookup(1, 1, 0, 15) is None
        assert score.any_day_only(profile).lookup(0, 1, 0, 15) == (2.0, 2.0)


class TestReport:
    """Scores and the Markdown report."""

    def test_scores_compare_day_by_day(self) -> None:
        better = score.Score("a", [[(0.1, 0.1)], [(0.2, 0.2)], [(0.3, 0.6)]])
        worse = score.Score("b", [[(0.2, 0.2)], [(0.4, 0.4)], [(0.3, 0.5)]])

        difference, error, wins = score.compare(better, worse, 1)

        assert better.mae(1) == pytest.approx(0.3)
        assert better.worst(1) == pytest.approx(0.6)
        assert better.slot_count() == 3
        assert difference == pytest.approx(-2 / 30)
        assert error > 0
        assert wins == pytest.approx(2 / 3)

    def test_too_few_days_cannot_be_compared(self) -> None:
        one = score.Score("a", [[(0.1, 0.1)]])
        empty = score.Score("b", [])

        assert all(math.isnan(value) for value in score.compare(one, one, 0))
        assert math.isnan(empty.mae(0))
        assert math.isnan(empty.worst(0))

    def test_the_report_has_every_section(
        self, small_days: dict[date, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(score, "FULL_HISTORY_DAYS", 8)

        report = score.build_report(small_days)

        text = "\n".join(report)
        assert "Target days: 2026-03-12 to 2026-03-15 (4 days)" in text
        for heading in (
            "## 1. Fill order with a full history",
            "## 2a. Shifted profile by days in a bucket",
            "## 2b. Implemented fill order while the history grows",
            "## 3. Outage after the held prices expired",
        ):
            assert heading in report
        tables = [line for line in report if line.startswith("|")]
        assert all(line.endswith("|") for line in tables)
        assert any(line.startswith("| Nearest earlier day | 384 |") for line in tables)
        assert any(line.startswith("| 2 | Last recorded day |") for line in tables)

    def test_a_short_price_file_is_rejected(self, small_days: dict[date, Any]) -> None:
        with pytest.raises(ValueError, match="consecutive days"):
            score.build_report(small_days)

    def test_the_command_line_fetches_and_prints(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(score, "FULL_HISTORY_DAYS", 8)
        monkeypatch.setattr(
            score, "fetch_prices", lambda *_: _rows(date(2026, 3, 2), 14)
        )
        path = tmp_path / "data" / "prices.csv"

        code = score.main(
            ["--prices", str(path), "--fetch", "2026-03-02", "2026-03-16", "DK1"]
        )

        assert code == 0
        assert path.read_text(encoding="utf-8").startswith("# Day-ahead spot prices")
        assert "## 1. Fill order" in capsys.readouterr().out
