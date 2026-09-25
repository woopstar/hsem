"""Tests for loading realized actuals and aligning them to slots (#1037)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from custom_components.hsem.models.planned_slot import PlannedSlot
from tests.backtest.actuals import (
    ACTUALS_SCHEMA,
    ENERGY_SERIES,
    VALUE_SERIES,
    Actuals,
    align_to_slots,
    build_actuals_payload,
    compare_prices,
    load_actuals,
    readings_from_ha_history,
    slot_energy_from_readings,
    slot_values_from_readings,
)

_CEST = timezone(timedelta(hours=2))
_CET = timezone(timedelta(hours=1))
_START = datetime(2026, 9, 14, 0, 0, tzinfo=_CEST)


def _slots(
    count: int, minutes: int = 15, start: datetime = _START
) -> list[PlannedSlot]:
    """Build a contiguous run of empty planner slots."""
    return [
        PlannedSlot(
            start=start + timedelta(minutes=minutes * i),
            end=start + timedelta(minutes=minutes * (i + 1)),
        )
        for i in range(count)
    ]


def _payload(
    slots: list[PlannedSlot],
    *,
    energy: dict[str, float] | None = None,
    values: dict[str, float] | None = None,
    slot_minutes: int = 15,
    covered: int | None = None,
) -> dict[str, Any]:
    """Build an actuals payload covering the first *covered* slots."""
    energy = energy if energy is not None else dict.fromkeys(ENERGY_SERIES, 0.5)
    values = values if values is not None else dict.fromkeys(VALUE_SERIES, 2.0)
    n = len(slots) if covered is None else covered
    stamps = [s.start.isoformat() for s in slots[:n]]
    return {
        "schema": ACTUALS_SCHEMA,
        "slot_minutes": slot_minutes,
        "slot_energy_kwh": {
            name: [[t, v] for t in stamps] for name, v in energy.items()
        },
        "slot_values": {name: [[t, v] for t in stamps] for name, v in values.items()},
    }


def _write(tmp_path: Path, payload: dict[str, Any]) -> Path:
    path = tmp_path / "actuals.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestLoading:
    """The file format must be validated, not trusted."""

    def test_round_trips_every_known_series(self, tmp_path: Path) -> None:
        slots = _slots(8)
        actuals = load_actuals(_write(tmp_path, _payload(slots)))
        assert actuals.slot_minutes == 15
        assert set(actuals.energy_kwh) == set(ENERGY_SERIES)
        assert set(actuals.values) == set(VALUE_SERIES)
        assert not actuals.unknown_series

    def test_wrong_schema_is_refused(self, tmp_path: Path) -> None:
        payload = _payload(_slots(4))
        payload["schema"] = "hsem-actuals-99"
        with pytest.raises(ValueError, match="expected schema"):
            load_actuals(_write(tmp_path, payload))

    def test_missing_schema_is_refused(self, tmp_path: Path) -> None:
        payload = _payload(_slots(4))
        del payload["schema"]
        with pytest.raises(ValueError, match="expected schema"):
            load_actuals(_write(tmp_path, payload))

    @pytest.mark.parametrize("bad", [0, -15, "15", None])
    def test_bad_slot_minutes_is_refused(self, tmp_path: Path, bad: Any) -> None:
        payload = _payload(_slots(4))
        payload["slot_minutes"] = bad
        with pytest.raises(ValueError, match="slot_minutes"):
            load_actuals(_write(tmp_path, payload))

    def test_unknown_series_is_reported_and_dropped(self, tmp_path: Path) -> None:
        slots = _slots(4)
        payload = _payload(slots)
        payload["slot_values"]["inverter_temperature"] = [
            [slots[0].start.isoformat(), 41.0]
        ]
        actuals = load_actuals(_write(tmp_path, payload))
        assert actuals.unknown_series == ("inverter_temperature",)
        assert "inverter_temperature" not in actuals.values

    def test_absent_buckets_load_as_empty(self, tmp_path: Path) -> None:
        path = _write(tmp_path, {"schema": ACTUALS_SCHEMA, "slot_minutes": 60})
        actuals = load_actuals(path)
        assert actuals.energy_kwh == {}
        assert actuals.values == {}


class TestAlignment:
    """Alignment is by canonical slot key, and missing stays missing."""

    def test_covered_slots_carry_their_observations(self, tmp_path: Path) -> None:
        slots = _slots(8)
        actuals = load_actuals(
            _write(
                tmp_path,
                _payload(
                    slots,
                    energy={
                        "pv_produced": 1.25,
                        "house_load": 0.4,
                        "grid_import": 0.0,
                        "grid_export": 0.85,
                    },
                    values={
                        "battery_soc_pct": 64.0,
                        "import_price": 2.1,
                        "export_price": 1.7,
                    },
                ),
            )
        )
        rows, report = align_to_slots(actuals, slots)
        assert len(rows) == 8
        assert rows[0].pv_produced_kwh == pytest.approx(1.25)
        assert rows[0].grid_export_kwh == pytest.approx(0.85)
        assert rows[0].battery_soc_pct == pytest.approx(64.0)
        assert report.is_complete
        assert report.scorable_slots == 8

    def test_uncovered_slots_are_none_not_zero(self, tmp_path: Path) -> None:
        """The whole point: an unobserved slot must not read as free energy."""
        slots = _slots(8)
        actuals = load_actuals(_write(tmp_path, _payload(slots, covered=3)))
        rows, report = align_to_slots(actuals, slots)
        assert rows[2].is_scorable
        for row in rows[3:]:
            assert row.pv_produced_kwh is None
            assert row.grid_import_kwh is None
            assert not row.is_scorable
        assert report.scorable_slots == 3
        assert not report.is_complete
        assert all(report.covered[name] == 3 for name in ENERGY_SERIES)

    def test_an_observed_zero_is_an_observation(self, tmp_path: Path) -> None:
        slots = _slots(4)
        actuals = load_actuals(
            _write(tmp_path, _payload(slots, energy=dict.fromkeys(ENERGY_SERIES, 0.0)))
        )
        rows, _ = align_to_slots(actuals, slots)
        assert rows[0].grid_import_kwh == pytest.approx(0.0)
        assert rows[0].has_energy_balance

    def test_alignment_survives_a_different_tzinfo(self, tmp_path: Path) -> None:
        """A UTC export must land on the same slots as a +02:00 plan."""
        slots = _slots(6)
        payload = _payload(slots)
        for bucket in ("slot_energy_kwh", "slot_values"):
            for name, pairs in payload[bucket].items():
                payload[bucket][name] = [
                    [
                        datetime.fromisoformat(t).astimezone(UTC).isoformat(),
                        v,
                    ]
                    for t, v in pairs
                ]
        rows, report = align_to_slots(load_actuals(_write(tmp_path, payload)), slots)
        assert report.is_complete
        assert rows[0].house_load_kwh is not None

    def test_autumn_repeated_hour_stays_two_distinct_slots(
        self, tmp_path: Path
    ) -> None:
        """Both folds of a DST-repeated hour must not collapse onto each other."""
        first_fold = datetime(2026, 10, 25, 2, 0, tzinfo=_CEST)
        second_fold = datetime(2026, 10, 25, 2, 0, tzinfo=_CET)
        slots = [
            PlannedSlot(start=first_fold, end=first_fold + timedelta(minutes=60)),
            PlannedSlot(start=second_fold, end=second_fold + timedelta(minutes=60)),
        ]
        payload = {
            "schema": ACTUALS_SCHEMA,
            "slot_minutes": 60,
            "slot_energy_kwh": {
                "house_load": [[second_fold.isoformat(), 0.9]],
            },
            "slot_values": {},
        }
        rows, report = align_to_slots(load_actuals(_write(tmp_path, payload)), slots)
        assert rows[0].house_load_kwh is None
        assert rows[1].house_load_kwh == pytest.approx(0.9)
        assert report.covered["house_load"] == 1

    def test_slot_width_mismatch_is_refused(self, tmp_path: Path) -> None:
        """Resampling is a scoring decision, not something to do silently."""
        slots = _slots(4, minutes=15)
        actuals = load_actuals(_write(tmp_path, _payload(slots, slot_minutes=60)))
        with pytest.raises(ValueError, match="re-export the actuals"):
            align_to_slots(actuals, slots)

    def test_empty_plan_aligns_to_nothing(self, tmp_path: Path) -> None:
        actuals = load_actuals(_write(tmp_path, _payload(_slots(4))))
        rows, report = align_to_slots(actuals, [])
        assert rows == []
        assert report.slot_count == 0
        assert not report.is_complete


class TestExplicitZeroFill:
    """Absent-means-zero must be requested, never assumed."""

    def test_zero_fill_makes_an_absent_series_scorable(self, tmp_path: Path) -> None:
        slots = _slots(4)
        payload = _payload(slots)
        del payload["slot_energy_kwh"]["pv_produced"]
        actuals = load_actuals(_write(tmp_path, payload))

        rows, report = align_to_slots(actuals, slots)
        assert rows[0].pv_produced_kwh is None
        assert report.scorable_slots == 0

        actuals.fill_absent_with_zero("pv_produced")
        rows, report = align_to_slots(actuals, slots)
        assert rows[0].pv_produced_kwh == pytest.approx(0.0)
        assert report.scorable_slots == 4
        assert report.zero_filled == ("pv_produced",)
        assert "zero-filled by request" in report.describe()

    def test_zero_fill_never_overrides_a_real_observation(self, tmp_path: Path) -> None:
        slots = _slots(4)
        actuals = load_actuals(
            _write(tmp_path, _payload(slots, energy=dict.fromkeys(ENERGY_SERIES, 1.5)))
        )
        actuals.fill_absent_with_zero("pv_produced")
        rows, _ = align_to_slots(actuals, slots)
        assert rows[0].pv_produced_kwh == pytest.approx(1.5)

    def test_unknown_series_name_is_refused(self) -> None:
        actuals = Actuals(slot_minutes=15)
        with pytest.raises(KeyError, match="unknown actuals series"):
            actuals.fill_absent_with_zero("solar_wind_speed")


class TestSlotActualsProperties:
    """Scorability is a property of the row, and has to be explicit."""

    def test_nothing_observed_is_not_scorable(self) -> None:
        slots = _slots(1)
        row = align_to_slots(
            Actuals(
                slot_minutes=15,
                energy_kwh={name: {} for name in ENERGY_SERIES},
            ),
            slots,
        )[0][0]
        assert not row.has_energy_balance
        assert not row.has_prices
        assert not row.has_battery_flows
        assert not row.is_scorable

    def test_scorability_does_not_require_prices(self, tmp_path: Path) -> None:
        """Prices come from the dump, not the actuals — see has_prices."""
        slots = _slots(4)
        payload = _payload(slots)
        payload["slot_values"].pop("import_price")
        payload["slot_values"].pop("export_price")
        rows, report = align_to_slots(load_actuals(_write(tmp_path, payload)), slots)
        assert not rows[0].has_prices
        assert rows[0].has_energy_balance
        assert rows[0].is_scorable
        assert report.scorable_slots == 4

    def test_realized_battery_flows_are_optional_but_reported(
        self, tmp_path: Path
    ) -> None:
        slots = _slots(4)
        payload = _payload(slots)
        rows, _ = align_to_slots(load_actuals(_write(tmp_path, payload)), slots)
        assert rows[0].has_battery_flows
        assert rows[0].battery_charged_kwh is not None

        payload["slot_energy_kwh"].pop("battery_charged")
        payload["slot_energy_kwh"].pop("battery_discharged")
        rows, _ = align_to_slots(load_actuals(_write(tmp_path, payload)), slots)
        assert not rows[0].has_battery_flows
        assert rows[0].is_scorable

    def test_partial_energy_is_not_a_balance(self, tmp_path: Path) -> None:
        slots = _slots(2)
        payload = _payload(slots)
        del payload["slot_energy_kwh"]["grid_export"]
        rows, _ = align_to_slots(load_actuals(_write(tmp_path, payload)), slots)
        assert rows[0].house_load_kwh is not None
        assert not rows[0].has_energy_balance
        assert rows[0].has_prices
        assert not rows[0].is_scorable


def _minutes(offset: int) -> datetime:
    """Return ``_START`` shifted by *offset* minutes."""
    return _START + timedelta(minutes=offset)


def _ramp(
    first: int, last: int, start_value: float, per_minute: float
) -> list[tuple[datetime, float | None]]:
    """Accumulator readings every minute over ``[first, last)``."""
    return [
        (_minutes(m), round(start_value + per_minute * (m - first + 1), 6))
        for m in range(first, last)
    ]


def _heartbeat(last: int) -> list[tuple[datetime, float | None]]:
    """A chatty entity reporting every minute — proof the recorder was running."""
    return _ramp(0, last, 50.0, 0.005)


def _energy(
    readings: dict[str, list[tuple[datetime, float | None]]],
    now_minutes: int,
    entity: str = "sensor.meter",
    **kwargs: Any,
) -> dict[str, float]:
    """Build a payload for one mapped meter; return energy keyed by local HH:MM."""
    payload = build_actuals_payload(
        readings, {entity: "grid_import"}, _minutes(now_minutes), 15, **kwargs
    )
    return {
        datetime.fromisoformat(stamp).astimezone(_CEST).strftime("%H:%M"): value
        for stamp, value in payload["slot_energy_kwh"].get("grid_import", [])
    }


class TestSlotEnergy:
    """Energy comes from the value in force at each slot boundary."""

    def test_steady_flow_becomes_per_slot_energy(self) -> None:
        got = _energy({"sensor.meter": _ramp(0, 120, 100.0, 0.01)}, 120)
        assert got["00:15"] == pytest.approx(0.15)
        assert got["01:30"] == pytest.approx(0.15)

    def test_a_flat_meter_is_zero_not_absent(self) -> None:
        """No rows because nothing changed is an observation, not a gap."""
        meter = _ramp(0, 30, 100.0, 0.01)  # moves until 00:30, then flat
        got = _energy({"sensor.meter": meter, "sensor.house": _heartbeat(120)}, 120)
        assert got["00:45"] == pytest.approx(0.0)
        assert got["01:30"] == pytest.approx(0.0)

    def test_first_slot_after_a_quiet_stretch_keeps_its_energy(self) -> None:
        """The case the ML layer's delta routine drops: no reading in the prior slot."""
        meter = _ramp(0, 30, 100.0, 0.01) + _ramp(97, 120, 100.3, 0.01)
        got = _energy({"sensor.meter": meter, "sensor.house": _heartbeat(120)}, 120)
        # 01:37..01:44 inclusive is 8 increments, plus the 01:45 reading at the boundary.
        assert got["01:30"] == pytest.approx(0.09)
        assert got["01:15"] == pytest.approx(0.0)

    def test_total_energy_is_conserved(self) -> None:
        meter = _ramp(0, 30, 100.0, 0.01) + _ramp(97, 120, 100.3, 0.01)
        got = _energy({"sensor.meter": meter, "sensor.house": _heartbeat(120)}, 120)
        recorded = sum(got.values())
        truth = meter[-1][1] - meter[0][1]  # type: ignore[operator]
        assert recorded == pytest.approx(truth, abs=1e-6)

    def test_meter_reset_slot_is_absent_and_the_next_one_recovers(self) -> None:
        meter = _ramp(0, 20, 100.0, 0.01) + _ramp(20, 60, 0.0, 0.01)
        got = _energy({"sensor.meter": meter}, 60)
        assert "00:15" not in got  # contains the drop from 100.2 to 0.01
        assert got["00:30"] == pytest.approx(0.15)

    def test_implausible_delta_is_absent(self) -> None:
        meter = _ramp(0, 20, 100.0, 0.01) + _ramp(20, 60, 200.0, 0.01)
        got = _energy({"sensor.meter": meter}, 60)
        assert "00:15" not in got  # a 100 kWh jump in 15 minutes
        assert got["00:30"] == pytest.approx(0.15)

    def test_unavailable_stretch_is_absent_not_bridged(self) -> None:
        meter: list[tuple[datetime, float | None]] = [
            *_ramp(0, 20, 100.0, 0.01),
            (_minutes(20), None),
            *_ramp(50, 90, 100.5, 0.01),
        ]
        got = _energy({"sensor.meter": meter, "sensor.house": _heartbeat(90)}, 90)
        assert "00:15" not in got  # ends inside the unavailable stretch
        assert "00:30" not in got
        assert "00:45" not in got  # starts inside it
        assert got["01:00"] == pytest.approx(0.15)

    def test_recorder_silence_is_unobserved_even_though_the_meter_is_flat(
        self,
    ) -> None:
        """A flat meter and a stopped recorder only differ across all entities."""
        house = _ramp(0, 30, 50.0, 0.005) + _ramp(90, 150, 50.2, 0.005)
        meter = _ramp(0, 30, 100.0, 0.01) + _ramp(90, 150, 100.3, 0.01)
        got = _energy({"sensor.meter": meter, "sensor.house": house}, 150)
        for hhmm in ("00:30", "00:45", "01:00", "01:15"):
            assert hhmm not in got, f"{hhmm} spans the outage"
        assert got["01:45"] == pytest.approx(0.15)

    def test_max_silence_is_configurable(self) -> None:
        """A 21-minute silence is an outage at 10 minutes' tolerance, not at 30."""
        house = _ramp(0, 30, 50.0, 0.005) + _ramp(50, 120, 50.2, 0.005)
        meter = _ramp(0, 30, 100.0, 0.01)  # flat after 00:29
        readings = {"sensor.meter": meter, "sensor.house": house}
        assert "00:30" not in _energy(readings, 120)
        relaxed = _energy(readings, 120, max_silence=timedelta(minutes=30))
        assert relaxed["00:30"] == pytest.approx(0.0)

    def test_silence_is_judged_across_all_entities(self) -> None:
        """One entity pausing is not an outage while another keeps reporting."""
        house = _ramp(0, 30, 50.0, 0.005) + _ramp(50, 120, 50.2, 0.005)
        meter = _ramp(0, 120, 100.0, 0.01)
        got = _energy({"sensor.meter": meter, "sensor.house": house}, 120)
        assert got["00:30"] == pytest.approx(0.15)

    def test_slot_before_the_first_reading_is_absent(self) -> None:
        meter = _ramp(20, 60, 100.0, 0.01)
        got = _energy({"sensor.meter": meter, "sensor.house": _heartbeat(60)}, 60)
        assert "00:00" not in got
        assert "00:15" not in got  # its start boundary predates the first reading

    def test_incomplete_slot_is_dropped(self) -> None:
        got = _energy({"sensor.meter": _ramp(0, 60, 100.0, 0.01)}, 52)
        assert "00:30" in got
        assert "00:45" not in got  # still running at 00:52

    def test_keys_are_canonical_utc(self) -> None:
        keys = [_minutes(15 * i).astimezone(UTC) for i in range(4)]
        energy = slot_energy_from_readings(_ramp(0, 90, 100.0, 0.01), keys, 15)
        assert energy
        assert all(key.tzinfo is UTC for key in energy)


class TestSlotValues:
    """A level is the value in force at the slot start."""

    def test_level_carries_forward_between_changes(self) -> None:
        soc: list[tuple[datetime, float | None]] = [
            (_minutes(0), 80.0),
            (_minutes(60), 70.0),
        ]
        keys = [_minutes(15 * i).astimezone(UTC) for i in range(6)]
        values = slot_values_from_readings(soc, keys)
        assert [values[k] for k in keys] == [80.0, 80.0, 80.0, 80.0, 70.0, 70.0]

    def test_unavailable_level_is_absent(self) -> None:
        soc: list[tuple[datetime, float | None]] = [
            (_minutes(0), 80.0),
            (_minutes(20), None),
        ]
        keys = [_minutes(15 * i).astimezone(UTC) for i in range(3)]
        values = slot_values_from_readings(soc, keys)
        assert keys[0] in values and keys[1] in values
        assert keys[2] not in values


def _history_block(
    entity_id: str, values: list[str], step_minutes: int = 5
) -> list[dict[str, Any]]:
    """Build one HA history block, minimal-response style."""
    rows: list[dict[str, Any]] = []
    for index, value in enumerate(values):
        entry: dict[str, Any] = {
            "state": value,
            "last_changed": (
                _START + timedelta(minutes=step_minutes * index)
            ).isoformat(),
        }
        if index == 0:
            entry["entity_id"] = entity_id
        rows.append(entry)
    return rows


class TestHaHistoryParsing:
    """A recorder export must not turn unavailability into a number."""

    def test_entity_id_carries_through_minimal_response(self) -> None:
        readings = readings_from_ha_history(
            [_history_block("sensor.pv", ["1.0", "2.0", "3.0"])]
        )
        assert list(readings) == ["sensor.pv"]
        assert len(readings["sensor.pv"]) == 3

    @pytest.mark.parametrize(
        "bad", ["unknown", "unavailable", "", "none", "n/a", "nan", "inf"]
    )
    def test_non_numeric_states_are_kept_as_gaps(self, bad: str) -> None:
        """Dropping the row would let the previous value bridge the gap."""
        readings = readings_from_ha_history(
            [_history_block("sensor.pv", ["1.0", bad, "3.0"])]
        )
        assert [v for _t, v in readings["sensor.pv"]] == [1.0, None, 3.0]

    def test_readings_come_back_sorted(self) -> None:
        block = _history_block("sensor.pv", ["1.0", "2.0", "3.0"])
        readings = readings_from_ha_history([list(reversed(block))])
        stamps = [t for t, _v in readings["sensor.pv"]]
        assert stamps == sorted(stamps)

    def test_entry_without_a_timestamp_is_skipped(self) -> None:
        readings = readings_from_ha_history(
            [[{"entity_id": "sensor.pv", "state": "1.0"}]]
        )
        assert readings == {}

    def test_last_updated_is_accepted_when_last_changed_is_absent(self) -> None:
        readings = readings_from_ha_history(
            [
                [
                    {
                        "entity_id": "sensor.pv",
                        "state": "1.0",
                        "last_updated": _START.isoformat(),
                    }
                ]
            ]
        )
        assert readings["sensor.pv"][0][1] == pytest.approx(1.0)

    def test_block_without_an_entity_id_is_ignored(self) -> None:
        assert readings_from_ha_history([[{"state": "1.0", "last_changed": "x"}]]) == {}


class TestBuildActualsPayload:
    """The built payload must be loadable and honest about gaps."""

    def _readings(self) -> dict[str, list[tuple[datetime, float | None]]]:
        return readings_from_ha_history(
            [
                _history_block(
                    "sensor.pv", [f"{100 + 0.1 * i:.3f}" for i in range(24)]
                ),
                _history_block(
                    "sensor.soc", [f"{100 - 0.2 * i:.1f}" for i in range(24)]
                ),
            ]
        )

    def test_payload_round_trips_through_the_loader(self, tmp_path: Path) -> None:
        payload = build_actuals_payload(
            self._readings(),
            {"sensor.pv": "pv_produced", "sensor.soc": "battery_soc_pct"},
            _START + timedelta(hours=3),
            15,
        )
        actuals = load_actuals(_write(tmp_path, payload))
        assert actuals.slot_minutes == 15
        assert actuals.energy_kwh["pv_produced"]
        assert actuals.values["battery_soc_pct"]
        assert not actuals.unknown_series

    def test_energy_is_integrated_and_levels_are_sampled(self) -> None:
        payload = build_actuals_payload(
            self._readings(),
            {"sensor.pv": "pv_produced", "sensor.soc": "battery_soc_pct"},
            _START + timedelta(hours=3),
            15,
        )
        pv = payload["slot_energy_kwh"]["pv_produced"]
        soc = payload["slot_values"]["battery_soc_pct"]
        assert all(value == pytest.approx(0.3, abs=1e-6) for _t, value in pv)
        # A level takes the earliest reading inside the slot, not a sum.
        assert soc[0][1] == pytest.approx(100.0)

    def test_an_entity_with_no_readings_is_absent_not_zero(self) -> None:
        payload = build_actuals_payload(
            self._readings(),
            {"sensor.pv": "pv_produced", "sensor.nothing": "grid_import"},
            _START + timedelta(hours=3),
            15,
        )
        assert "grid_import" not in payload["slot_energy_kwh"]

    def test_unknown_series_name_is_refused(self) -> None:
        with pytest.raises(KeyError, match="unknown actuals series"):
            build_actuals_payload(
                self._readings(), {"sensor.pv": "moon_phase"}, _START, 15
            )

    def test_empty_readings_produce_an_empty_but_valid_payload(
        self, tmp_path: Path
    ) -> None:
        payload = build_actuals_payload({}, {"sensor.pv": "pv_produced"}, _START, 15)
        actuals = load_actuals(_write(tmp_path, payload))
        assert actuals.energy_kwh == {}


def _priced(
    prices: list[tuple[float, float]],
) -> tuple[list[Any], list[PlannedSlot]]:
    """Return aligned rows and plan slots carrying ``(actual, planned)`` prices."""
    from custom_components.hsem.utils.prices import SlotPrice
    from tests.backtest.actuals import SlotActuals

    rows, slots = [], []
    for i, (actual, planned) in enumerate(prices):
        start, end = _minutes(15 * i), _minutes(15 * (i + 1))
        rows.append(SlotActuals(start=start, end=end, import_price=actual))
        slots.append(
            PlannedSlot(start=start, end=end, price=SlotPrice(planned, planned))
        )
    return rows, slots


class TestComparePrices:
    """The cross-check must name *why* prices differ, not just that they do."""

    def test_identical_prices_are_consistent(self) -> None:
        result = compare_prices(*_priced([(2.0, 2.0), (2.1, 2.1), (2.2, 2.2)]))
        assert result.slots == 3
        assert result.mean_diff == pytest.approx(0.0)
        assert "slot for slot" in result.verdict

    def test_constant_fee_is_called_an_offset(self) -> None:
        result = compare_prices(*_priced([(2.25, 2.0), (2.35, 2.1), (2.45, 2.2)]))
        assert result.mean_diff == pytest.approx(0.25)
        assert "systematic offset" in result.verdict
        assert "Do not score" in result.verdict

    def test_hourly_plan_against_subhourly_export_is_consistent(self) -> None:
        """The real case, using measured values from a 2026-09-15 export."""
        measured = [
            (2.1438, 2.2040),
            (2.3009, 2.2040),
            (2.2829, 2.2040),
            (2.1722, 2.2040),
            (2.0445, 2.1030),
            (2.2230, 2.1030),
            (2.1033, 2.1030),
            (2.0646, 2.1030),
            (2.0067, 2.0120),
            (2.0235, 2.0120),
            (1.9984, 2.0120),
            (2.0092, 2.0120),
        ]
        result = compare_prices(*_priced(measured))
        assert result.plan_is_hourly
        assert result.actuals_are_subhourly
        assert result.stdev_diff > 0.05  # wide
        assert "hourly prices while the export is" in result.verdict
        # Averaging per hour cancels most of the intra-hour structure.
        assert result.hourly_max_diff < 0.025

    def test_a_small_sample_of_granularity_noise_is_not_called_a_fee(self) -> None:
        """One hour is too little to distinguish noise from an offset."""
        plan = 2.2040
        result = compare_prices(
            *_priced([(2.1438, plan), (2.3009, plan), (2.2829, plan), (2.1722, plan)])
        )
        assert "systematic offset" not in result.verdict

    def test_a_small_constant_fee_is_still_caught(self) -> None:
        """A fee has no spread, so even a slight one stands clear of noise."""
        result = compare_prices(*_priced([(2.02, 2.0), (2.12, 2.1), (2.22, 2.2)]))
        assert result.mean_diff == pytest.approx(0.02)
        assert "systematic offset" in result.verdict

    def test_matching_means_with_wrong_slots_is_flagged(self) -> None:
        """Equal averages must not hide a per-slot disagreement."""
        result = compare_prices(
            *_priced([(2.5, 2.0), (2.0, 2.5), (2.5, 2.0), (2.0, 2.5)])
        )
        assert result.mean_diff == pytest.approx(0.0)
        assert not result.plan_is_hourly
        assert "timing offset" in result.verdict

    def test_a_few_wrong_prices_are_named_not_absorbed(self) -> None:
        """A day-long mean hides four bad slots; the outlier count must not."""
        measured = [(2.0 + 0.001 * i, 2.0 + 0.001 * i) for i in range(92)]
        measured += [(2.5, 2.0), (2.5, 2.0), (2.5, 2.0), (2.5, 2.0)]
        result = compare_prices(*_priced(measured))
        assert result.outliers == 4
        assert result.worst_diff == pytest.approx(0.5)
        assert "individual" in result.verdict

    def test_a_lone_outlier_is_reported_but_not_a_verdict(self) -> None:
        """Three-sigma samples happen; one in ninety-odd is not a fault.

        The intra-hour offsets sum to zero, as real hours do across a day --
        repeating a single hour's pattern would be a constant bias, not noise,
        and the check rightly calls that an offset.
        """
        plan = 2.2040
        shape = [-0.06, 0.10, 0.08, -0.12]
        measured = [(plan + d, plan) for _ in range(23) for d in shape]
        measured.append((plan + 0.48, plan))
        result = compare_prices(*_priced(measured))
        assert result.outliers == 1
        assert "individual" not in result.verdict
        assert "hourly prices while the export is" in result.verdict
        assert "1 outlier(s)" in result.describe()

    def test_the_worst_slot_is_named(self) -> None:
        result = compare_prices(*_priced([(2.0, 2.0), (2.0, 2.0), (2.9, 2.0)]))
        assert result.worst_at == _minutes(30)
        assert "at 2026-09-14 00:30" in result.describe()

    def test_granularity_noise_produces_no_outliers(self) -> None:
        plan = 2.2040
        measured = [(2.1438, plan), (2.3009, plan), (2.2829, plan), (2.1722, plan)] * 8
        result = compare_prices(*_priced(measured))
        assert result.outliers == 0

    def test_no_prices_is_reported_not_crashed(self) -> None:
        rows, slots = _priced([(2.0, 2.0)])
        rows[0] = type(rows[0])(start=rows[0].start, end=rows[0].end)
        result = compare_prices(rows, slots)
        assert result.slots == 0
        assert "--refresh" in result.verdict
        assert "prices:" in result.describe()
