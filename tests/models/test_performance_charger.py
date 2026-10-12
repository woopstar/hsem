"""Tests for the per-charger records behind the EV timing score (issue #1271)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from custom_components.hsem.models.performance_charger import (
    ChargerPiece,
    ChargerRecord,
    ChargerSample,
    charger_pieces,
    merge_charger_pieces,
)

_DEADLINE = datetime(2026, 6, 10, 5, 0, tzinfo=UTC)


def _sample(**values: object) -> ChargerSample:
    defaults: dict[str, object] = {
        "power_w": 7200.0,
        "connected": True,
        "managed": True,
        "deadline": _DEADLINE,
        "max_kw": 7.4,
    }
    return ChargerSample(**{**defaults, **values})  # type: ignore[arg-type]


class TestPieces:
    def test_energy_is_the_mean_power_held_for_the_interval(self) -> None:
        """6 kW and 8 kW, five minutes apart."""
        (piece,) = charger_pieces(
            [_sample(power_w=6000.0)], [_sample(power_w=8000.0)], 5 / 60
        )

        assert piece.kwh == pytest.approx(7.0 * 5 / 60)
        assert piece.connected is True
        assert piece.managed is True
        assert piece.deadline == _DEADLINE
        assert piece.max_kw == pytest.approx(7.4)

    @pytest.mark.parametrize("missing", [0, 1])
    def test_a_missing_power_reading_is_unknown_energy(self, missing: int) -> None:
        samples = [_sample(), _sample()]
        samples[missing] = _sample(power_w=None)

        (piece,) = charger_pieces([samples[0]], [samples[1]], 5 / 60)

        assert piece.kwh is None

    @pytest.mark.parametrize(
        ("before", "after", "expected"),
        [
            (True, True, True),
            (True, False, False),
            (False, True, False),
            (None, True, None),
            (True, None, None),
        ],
    )
    def test_connected_means_plugged_in_at_both_ends(
        self, before: bool | None, after: bool | None, expected: bool | None
    ) -> None:
        """A car that arrives or leaves in between was not there throughout."""
        (piece,) = charger_pieces(
            [_sample(connected=before)], [_sample(connected=after)], 5 / 60
        )

        assert piece.connected is expected

    def test_managed_means_managed_at_both_ends(self) -> None:
        (piece,) = charger_pieces([_sample(managed=False)], [_sample()], 5 / 60)

        assert piece.managed is False

    def test_a_charger_only_the_later_sample_knows_was_not_observed(self) -> None:
        pieces = charger_pieces([_sample()], [_sample(), _sample()], 5 / 60)

        assert len(pieces) == 2
        assert pieces[1].kwh is None
        assert pieces[1].connected is None
        assert pieces[1].managed is False

    def test_a_share_scales_the_energy_only(self) -> None:
        piece = ChargerPiece(
            kwh=1.0, connected=True, managed=True, deadline=_DEADLINE, max_kw=7.4
        )

        part = piece.share(0.4)

        assert part.kwh == pytest.approx(0.4)
        assert (part.connected, part.managed, part.deadline, part.max_kw) == (
            True,
            True,
            _DEADLINE,
            7.4,
        )
        assert ChargerPiece(None, True, True, None, None).share(0.4).kwh is None


def _piece(**values: object) -> ChargerPiece:
    defaults: dict[str, object] = {
        "kwh": 0.5,
        "connected": True,
        "managed": True,
        "deadline": _DEADLINE,
        "max_kw": 7.4,
    }
    return ChargerPiece(**{**defaults, **values})  # type: ignore[arg-type]


class TestRecord:
    def test_stretches_add_up(self) -> None:
        records: list[ChargerRecord] = []

        merge_charger_pieces(records, [_piece()], first=True)
        merge_charger_pieces(records, [_piece(kwh=0.25)], first=False)

        (record,) = records
        assert record.kwh == pytest.approx(0.75)
        assert record.connected is True
        assert record.managed is True
        assert record.deadline == _DEADLINE

    def test_one_unobserved_stretch_makes_the_energy_unknown(self) -> None:
        records: list[ChargerRecord] = []

        merge_charger_pieces(records, [_piece()], first=True)
        merge_charger_pieces(records, [_piece(kwh=None)], first=False)
        merge_charger_pieces(records, [_piece()], first=False)

        assert records[0].kwh is None

    def test_one_unplugged_stretch_means_not_connected_throughout(self) -> None:
        records: list[ChargerRecord] = []

        merge_charger_pieces(records, [_piece()], first=True)
        merge_charger_pieces(records, [_piece(connected=False)], first=False)
        merge_charger_pieces(records, [_piece()], first=False)

        assert records[0].connected is False

    def test_an_unknown_connection_stays_unknown(self) -> None:
        records: list[ChargerRecord] = []

        merge_charger_pieces(records, [_piece(connected=None)], first=True)
        merge_charger_pieces(records, [_piece()], first=False)

        assert records[0].connected is None

    def test_one_unmanaged_stretch_means_not_managed_throughout(self) -> None:
        records: list[ChargerRecord] = []

        merge_charger_pieces(records, [_piece(managed=False)], first=True)
        merge_charger_pieces(records, [_piece()], first=False)

        assert records[0].managed is False

    def test_a_charger_that_turns_up_mid_slot_starts_out_unknown(self) -> None:
        """It was not observed from the slot's start."""
        records: list[ChargerRecord] = []

        merge_charger_pieces(records, [_piece()], first=True)
        merge_charger_pieces(records, [_piece(), _piece()], first=False)

        assert records[1].kwh is None
        assert records[1].connected is None
        assert records[1].managed is False

    def test_a_charger_that_disappears_mid_slot_becomes_unknown(self) -> None:
        records: list[ChargerRecord] = []

        merge_charger_pieces(records, [_piece(), _piece()], first=True)
        merge_charger_pieces(records, [_piece()], first=False)

        assert records[0].kwh == pytest.approx(1.0)
        assert records[1].kwh is None
        assert records[1].managed is False

    def test_the_record_is_what_the_scoring_takes(self) -> None:
        record = ChargerRecord(
            kwh=1.5, connected=True, managed=True, deadline=_DEADLINE, max_kw=7.4
        )

        slot = record.as_slot()

        assert slot.movable
        assert (slot.kwh, slot.deadline, slot.max_kw) == (1.5, _DEADLINE, 7.4)


class TestJson:
    def test_a_sample_round_trips(self) -> None:
        sample = _sample()

        assert ChargerSample.from_dict(json.loads(json.dumps(sample.as_dict()))) == (
            sample
        )

    def test_a_record_round_trips(self) -> None:
        record = ChargerRecord(
            kwh=1.5, connected=False, managed=True, deadline=_DEADLINE, max_kw=7.4
        )

        assert ChargerRecord.from_dict(json.loads(json.dumps(record.as_dict()))) == (
            record
        )

    def test_unknowns_round_trip_as_unknowns(self) -> None:
        record = ChargerRecord(kwh=None, connected=None, managed=False)

        assert ChargerRecord.from_dict(record.as_dict()) == record
        assert ChargerSample.from_dict(ChargerSample().as_dict()) == ChargerSample()

    def test_anything_unusable_is_unknown_not_a_guess(self) -> None:
        assert ChargerSample.from_dict("nonsense") == ChargerSample()
        record = ChargerRecord.from_dict(["nonsense"])
        assert (record.kwh, record.connected, record.managed) == (None, None, False)
        partial = ChargerRecord.from_dict(
            {"kwh": "many", "connected": "yes", "managed": 1, "deadline": "soon"}
        )
        assert (partial.kwh, partial.connected, partial.managed, partial.deadline) == (
            None,
            None,
            False,
            None,
        )
