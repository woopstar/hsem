"""Tests for what the energy tracker does with missing and uncertain inputs.

Each of these failed against the first version of the tracker (review of the
pull request for issue #1435).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from custom_components.hsem.models.energy_tracker import (
    REASON_BATTERY_NOT_MEASURED,
    REASON_CYCLE_COST_UNKNOWN,
    REASON_SOC_CHANGE_TOO_LARGE,
    REASON_THROUGHPUT_BELOW_MINIMUM,
    BatterySite,
    EnergyDayEntry,
    EnergySample,
    EnergyTracker,
)

_TZ = ZoneInfo("Europe/Copenhagen")
_T0 = datetime(2026, 6, 10, 8, 0, tzinfo=_TZ)
_SITE = BatterySite(
    rated_capacity_kwh=10.0, usable_capacity_kwh=9.0, cycle_cost_per_kwh=0.5
)
_UNPRICED = BatterySite(rated_capacity_kwh=10.0, usable_capacity_kwh=9.0)


def _feed(
    tracker: EnergyTracker, *samples: EnergySample, site: BatterySite = _SITE
) -> list[bool]:
    return [
        tracker.accumulate(sample, site, max_gap_seconds=600.0) for sample in samples
    ]


class TestMissingIsNotZero:
    def test_an_unavailable_house_sensor_is_not_a_house_that_uses_nothing(
        self,
    ) -> None:
        """4 h at 2 kW, then 4 h without a reading, 1 kW imported throughout."""
        tracker = EnergyTracker()

        for step in range(97):
            _feed(
                tracker,
                EnergySample(
                    at=_T0 + timedelta(minutes=5 * step),
                    grid_import_energy_kwh=100.0 + step / 12.0,
                    house_power_w=2000.0 if step <= 48 else None,
                ),
            )

        summary = tracker.month_summary("2026-06")
        entry = tracker.daily_log["2026-06-10"]
        assert entry.house_hours == pytest.approx(4.0)
        assert entry.grid_import_hours == pytest.approx(8.0)
        assert summary["house_consumption_kwh"] == pytest.approx(8.0)
        assert summary["grid_import_kwh"] == pytest.approx(8.0)
        # Half of what the house used in the measured four hours was imported.
        assert summary["self_sufficiency"] == pytest.approx(50.0)

    def test_a_meter_that_was_never_read_is_unknown_in_the_month(self) -> None:
        tracker = EnergyTracker()

        _feed(
            tracker,
            EnergySample(at=_T0, house_power_w=1000.0),
            EnergySample(at=_T0 + timedelta(minutes=6), house_power_w=1000.0),
        )

        summary = tracker.month_summary("2026-06")
        assert summary["house_consumption_kwh"] == pytest.approx(0.1)
        for name in (
            "grid_import_kwh",
            "grid_export_kwh",
            "pv_kwh",
            "battery_charged_kwh",
            "battery_discharged_kwh",
        ):
            assert summary[name] is None
        assert summary["battery_cycles"] is None
        assert summary["battery_cycles_reason"] == REASON_BATTERY_NOT_MEASURED
        assert summary["battery_wear_cost"] is None
        assert summary["battery_wear_cost_reason"] == REASON_BATTERY_NOT_MEASURED

    def test_a_measured_zero_is_a_zero(self) -> None:
        tracker = EnergyTracker()

        _feed(
            tracker,
            EnergySample(
                at=_T0, grid_export_energy_kwh=5.0, pv_power_w=0.0, soc_pct=50.0
            ),
            EnergySample(
                at=_T0 + timedelta(minutes=6),
                grid_export_energy_kwh=5.0,
                pv_power_w=0.0,
                soc_pct=50.0,
            ),
        )

        summary = tracker.month_summary("2026-06")
        assert summary["grid_export_kwh"] == pytest.approx(0.0)
        assert summary["pv_kwh"] == pytest.approx(0.0)
        assert summary["battery_discharged_kwh"] == pytest.approx(0.0)
        assert summary["battery_cycles"] == pytest.approx(0.0)
        assert summary["battery_wear_cost"] == pytest.approx(0.0)


class TestDaylightSaving:
    @pytest.mark.parametrize(
        ("before", "after"),
        [
            # Clocks go forward: 01:58 is five minutes before 03:03.
            (
                datetime(2026, 3, 29, 1, 58, tzinfo=_TZ),
                datetime(2026, 3, 29, 3, 3, tzinfo=_TZ),
            ),
            # Clocks go back: the second 02:03 is five minutes after 02:58.
            (
                datetime(2026, 10, 25, 2, 58, tzinfo=_TZ),
                datetime(2026, 10, 25, 2, 3, fold=1, tzinfo=_TZ),
            ),
        ],
    )
    def test_the_interval_is_elapsed_time(
        self, before: datetime, after: datetime
    ) -> None:
        tracker = EnergyTracker()

        booked = _feed(
            tracker,
            EnergySample(at=before, grid_import_energy_kwh=10.0),
            EnergySample(at=after, grid_import_energy_kwh=10.5),
        )

        assert booked == [False, True]
        entry = tracker.daily_log[after.date().isoformat()]
        assert entry.observed_hours == pytest.approx(5.0 / 60.0)
        assert entry.grid_import_kwh == pytest.approx(0.5)

    def test_a_save_is_due_on_elapsed_time_too(self) -> None:
        """When the clocks go back, the samples must not go unsaved for an hour."""
        tracker = EnergyTracker()
        tracker.accumulate(
            EnergySample(at=datetime(2026, 10, 25, 2, 3, fold=1, tzinfo=_TZ)),
            _SITE,
            max_gap_seconds=600.0,
        )
        tracker._saved_at = datetime(2026, 10, 25, 2, 58, tzinfo=_TZ)

        assert tracker.save_due(120.0) is True


class TestWearCost:
    def test_a_throughput_without_a_cycle_cost_has_no_wear_cost(self) -> None:
        tracker = EnergyTracker()

        _feed(
            tracker,
            EnergySample(at=_T0, battery_power_w=3000.0, soc_pct=50.0),
            EnergySample(
                at=_T0 + timedelta(minutes=6), battery_power_w=3000.0, soc_pct=53.0
            ),
            site=_UNPRICED,
        )

        entry = tracker.daily_log["2026-06-10"]
        assert entry.battery_charged_kwh == pytest.approx(0.3)
        assert entry.priced_throughput_kwh == pytest.approx(0.0)
        summary = tracker.month_summary("2026-06")
        assert summary["battery_wear_cost"] is None
        assert summary["battery_wear_cost_reason"] == REASON_CYCLE_COST_UNKNOWN
        assert summary["battery_cycles"] == pytest.approx(0.0)

    def test_a_short_spell_without_a_cycle_cost_does_not_hide_the_month(self) -> None:
        tracker = EnergyTracker(
            daily_log={
                "2026-06-01": EnergyDayEntry(
                    "2026-06-01",
                    battery_charged_kwh=50.0,
                    battery_discharged_kwh=50.0,
                    battery_hours=24.0,
                    wear_cost=9.8,
                    priced_throughput_kwh=98.0,
                )
            }
        )

        summary = tracker.month_summary("2026-06")

        assert summary["battery_wear_cost"] == pytest.approx(9.8)
        assert summary["battery_wear_cost_reason"] is None


class TestRoundTripUncertainty:
    @staticmethod
    def _month(charged: float, discharged: float, stored: float) -> dict[str, object]:
        tracker = EnergyTracker(
            daily_log={
                "2026-06-01": EnergyDayEntry(
                    "2026-06-01",
                    measured_charged_kwh=charged,
                    measured_discharged_kwh=discharged,
                    measured_stored_delta_kwh=stored,
                    rated_capacity_kwh=10.0,
                )
            }
        )
        return tracker.month_summary("2026-06")

    def test_early_in_the_month_the_soc_term_is_too_large_for_a_figure(self) -> None:
        """44 kWh charged and ending full: the SoC term is a fifth of it."""
        summary = self._month(44.0, 32.4, 9.0)

        assert summary["round_trip_loss"] is None
        assert summary["round_trip_loss_uncertainty"] is None
        assert summary["round_trip_loss_reason"] == REASON_THROUGHPUT_BELOW_MINIMUM

    def test_enough_throughput_but_a_large_soc_change_is_still_unknown(self) -> None:
        summary = self._month(60.0, 47.0, 9.0)

        assert summary["round_trip_loss"] is None
        assert summary["round_trip_loss_reason"] == REASON_SOC_CHANGE_TOO_LARGE

    def test_the_published_loss_carries_its_uncertainty(self) -> None:
        summary = self._month(300.0, 276.0, 6.0)

        assert summary["round_trip_loss"] == pytest.approx(6.0)
        # 6 kWh of SoC change may be 15 % off: 0.9 kWh of 300 kWh charged.
        assert summary["round_trip_loss_uncertainty"] == pytest.approx(0.3)
        assert summary["round_trip_loss_reason"] is None

    def test_an_aged_battery_moves_the_loss_by_less_than_the_uncertainty(
        self,
    ) -> None:
        """At 90 % health the 6 kWh above were 5.4 kWh in truth."""
        summary = self._month(300.0, 276.0, 6.0)
        true_loss = (1.0 - (276.0 + 5.4) / 300.0) * 100.0

        loss, uncertainty = (
            summary["round_trip_loss"],
            summary["round_trip_loss_uncertainty"],
        )
        assert isinstance(loss, float)
        assert isinstance(uncertainty, float)
        assert abs(true_loss - loss) <= uncertainty


class TestPublishedOnce:
    def test_the_month_tables_are_built_once_per_recorded_interval(self) -> None:
        tracker = EnergyTracker()
        today = date(2026, 6, 10)
        _feed(
            tracker,
            EnergySample(at=_T0, pv_power_w=500.0),
            EnergySample(at=_T0 + timedelta(minutes=5), pv_power_w=500.0),
        )

        first = tracker.as_sensor_attributes(today)
        first["reason"] = "one sensor's own"
        with patch.object(EnergyTracker, "month_summary", side_effect=AssertionError):
            second = tracker.as_sensor_attributes(today)
        assert "reason" not in second
        assert second["monthly"] == first["monthly"]

        _feed(tracker, EnergySample(at=_T0 + timedelta(minutes=10), pv_power_w=500.0))
        third = tracker.as_sensor_attributes(today)
        assert third["this_month"]["pv_kwh"] > second["this_month"]["pv_kwh"]

    def test_a_new_day_and_a_restored_history_rebuild_them(self) -> None:
        tracker = EnergyTracker()
        assert tracker.as_sensor_attributes(date(2026, 6, 30))["month"] == "2026-06"
        assert tracker.as_sensor_attributes(date(2026, 7, 1))["month"] == "2026-07"

        tracker.restore_from(
            EnergyTracker(
                daily_log={"2026-07-01": EnergyDayEntry("2026-07-01", pv_hours=1.0)}
            )
        )

        assert tracker.as_sensor_attributes(date(2026, 7, 1))["this_month"]["days"] == 1
