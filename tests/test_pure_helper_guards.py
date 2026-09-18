"""Guard-clause tests for small pure helpers in ``utils/`` and ``models/``.

These helpers all fail closed on unusable input: a bad reading becomes
``None``, a misconfigured window raises at construction rather than producing
a silently wrong estimate, and a disabled power cap yields ``0.0``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from custom_components.hsem.models.financial_tracker import FinancialTracker
from custom_components.hsem.utils.ev_delivered_energy import (
    EVDeliveredEnergyTracker,
    _aware_timestamp,
    _finite_in_range,
    _finite_positive,
    _valid_charging_power_w,
)
from custom_components.hsem.utils.live_power import LivePowerEstimate, LivePowerWindow
from custom_components.hsem.utils.units import (
    export_max_energy_per_slot_kwh,
    max_energy_per_slot_kwh,
)
from custom_components.hsem.utils.weekday_profile import WeekdayProfile
from tests.test_ev_delivered_energy import _update as _update_tracker

_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


class TestAwareTimestamp:
    """Only a timezone-aware datetime yields a usable timestamp."""

    def test_aware_datetime_converts(self) -> None:
        """An aware datetime becomes its POSIX timestamp."""
        assert _aware_timestamp(_NOW) == pytest.approx(_NOW.timestamp())

    def test_naive_datetime_is_rejected(self) -> None:
        """A naive datetime has no defined instant."""
        assert _aware_timestamp(datetime(2026, 6, 1, 12, 0)) is None

    def test_non_datetime_is_rejected(self) -> None:
        """An object that is not a datetime cannot be timestamped."""
        assert _aware_timestamp("2026-06-01T12:00:00+00:00") is None  # type: ignore[arg-type]  # defensive input


class TestFinitePositive:
    """Only a finite value above zero is accepted."""

    @pytest.mark.parametrize(
        "value", [0.0, -1.0, float("nan"), float("inf"), "not a number", None]
    )
    def test_unusable_values_are_rejected(self, value: Any) -> None:
        """Zero, negatives, non-finite values, and junk are refused."""
        assert _finite_positive(value) is None

    def test_positive_value_passes(self) -> None:
        """A normal positive reading is returned."""
        assert _finite_positive(60.0) == pytest.approx(60.0)


class TestFiniteInRange:
    """A reading must be finite and inside the inclusive range."""

    @pytest.mark.parametrize(
        "value",
        [None, -0.1, 100.1, float("nan"), float("inf"), "not a number"],
    )
    def test_out_of_range_or_unusable_is_rejected(self, value: Any) -> None:
        """Anything outside 0–100 (or unparseable) is refused."""
        assert _finite_in_range(value, 0.0, 100.0) is None

    @pytest.mark.parametrize("value", [0.0, 50.0, 100.0])
    def test_bounds_are_inclusive(self, value: float) -> None:
        """Both ends of the range are valid readings."""
        assert _finite_in_range(value, 0.0, 100.0) == pytest.approx(value)


class TestValidChargingPowerW:
    """A charging-power endpoint is only trusted while charging."""

    @pytest.mark.parametrize(
        ("charging", "power_w"),
        [
            pytest.param(False, 7400.0, id="not_charging"),
            pytest.param(True, None, id="no_reading"),
            pytest.param(True, "not a number", id="unparseable"),
            pytest.param(True, float("nan"), id="non_finite"),
            pytest.param(True, -1.0, id="negative"),
            pytest.param(True, 99_000.0, id="above_charger_rating"),
        ],
    )
    def test_unusable_endpoints_are_skipped(self, charging: bool, power_w: Any) -> None:
        """An implausible or absent reading is not used as an endpoint."""
        assert (
            _valid_charging_power_w(
                charging=charging, power_w=power_w, max_power_w=22_000.0
            )
            is None
        )

    def test_plausible_charging_power_is_accepted(self) -> None:
        """A reading within the charger's rating is used."""
        assert _valid_charging_power_w(
            charging=True, power_w=7400.0, max_power_w=22_000.0
        ) == pytest.approx(7400.0)


class TestDeliveredEnergyCapacityChange:
    """A changed EV battery capacity invalidates the stored credit."""

    def test_capacity_change_discards_the_credit(self) -> None:
        """The kWh-to-SoC mapping cannot survive a capacity change."""
        tracker = EVDeliveredEnergyTracker()
        _update_tracker(tracker, _NOW, capacity_kwh=100.0)
        credited = _update_tracker(
            tracker, _NOW + timedelta(minutes=10), capacity_kwh=100.0
        )
        assert credited.credit_kwh > 0.0

        # The user corrects the configured EV battery capacity.
        rebased = _update_tracker(
            tracker, _NOW + timedelta(minutes=20), capacity_kwh=80.0
        )

        assert rebased.credit_kwh == pytest.approx(0.0)


class TestLivePowerEstimateAvailability:
    """Availability mirrors whether a channel produced a number."""

    def test_channels_report_their_own_availability(self) -> None:
        """Each channel is available only when it has a value."""
        both = LivePowerEstimate(house_power_w=1200.0, solar_power_w=500.0)
        neither = LivePowerEstimate(house_power_w=None, solar_power_w=None)

        assert both.house_available is True
        assert both.solar_available is True
        assert neither.house_available is False
        assert neither.solar_available is False


class TestLivePowerWindowConstruction:
    """A misconfigured window raises instead of producing a wrong estimate."""

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            pytest.param({"window_seconds": 0}, "window_seconds", id="window_seconds"),
            pytest.param(
                {"minimum_samples": 0}, "minimum_samples", id="minimum_samples"
            ),
            pytest.param(
                {"maximum_sample_age_seconds": 0},
                "maximum_sample_age_seconds",
                id="maximum_sample_age_seconds",
            ),
        ],
    )
    def test_non_positive_parameters_are_refused(
        self, kwargs: dict[str, Any], message: str
    ) -> None:
        """Every window parameter must be positive."""
        valid: dict[str, Any] = {
            "window_seconds": 60,
            "minimum_samples": 3,
            "maximum_sample_age_seconds": 20,
        }

        with pytest.raises(ValueError, match=message):
            LivePowerWindow(**{**valid, **kwargs})

    def test_naive_sample_timestamps_are_refused(self) -> None:
        """A naive timestamp cannot be placed in the rolling window."""
        window = LivePowerWindow(
            window_seconds=60, minimum_samples=1, maximum_sample_age_seconds=20
        )

        with pytest.raises(ValueError, match="timezone-aware"):
            window.add_sample(
                datetime(2026, 6, 1, 12, 0),
                house_power_w=1200.0,
                solar_power_w=0.0,
                house_available=True,
                solar_available=True,
            )


class TestLivePowerWindowSampling:
    """The window drops unusable and stale samples."""

    @staticmethod
    def _window() -> LivePowerWindow:
        """Return a window needing two samples inside a 60 s window."""
        return LivePowerWindow(
            window_seconds=60, minimum_samples=2, maximum_sample_age_seconds=30
        )

    def test_unavailable_channel_clears_its_samples(self) -> None:
        """An unavailable reading discards that channel's history."""
        window = self._window()
        for offset in (0, 10):
            window.add_sample(
                _NOW + timedelta(seconds=offset),
                house_power_w=1200.0,
                solar_power_w=500.0,
                house_available=True,
                solar_available=True,
            )
        assert window.estimate(_NOW + timedelta(seconds=10)).house_power_w is not None

        window.add_sample(
            _NOW + timedelta(seconds=20),
            house_power_w=None,
            solar_power_w=500.0,
            house_available=False,
            solar_available=True,
        )

        estimate = window.estimate(_NOW + timedelta(seconds=20))
        assert estimate.house_power_w is None
        assert estimate.solar_power_w is not None

    def test_samples_older_than_the_window_are_pruned(self) -> None:
        """A sample outside the window no longer counts toward the estimate."""
        window = self._window()
        window.add_sample(
            _NOW,
            house_power_w=1200.0,
            solar_power_w=500.0,
            house_available=True,
            solar_available=True,
        )
        window.add_sample(
            _NOW + timedelta(seconds=5),
            house_power_w=1200.0,
            solar_power_w=500.0,
            house_available=True,
            solar_available=True,
        )

        # Two minutes later both samples have aged out of the 60 s window.
        assert window.estimate(_NOW + timedelta(minutes=2)).house_power_w is None

    def test_too_few_samples_produce_no_estimate(self) -> None:
        """A single sample does not meet the minimum evidence bar."""
        window = self._window()
        window.add_sample(
            _NOW,
            house_power_w=1200.0,
            solar_power_w=500.0,
            house_available=True,
            solar_available=True,
        )

        assert window.estimate(_NOW).house_power_w is None


class TestSlotEnergyCaps:
    """A disabled or nonsensical power cap yields no energy allowance."""

    @pytest.mark.parametrize(
        ("power_w", "interval_minutes"),
        [
            pytest.param(0.0, 15, id="no_power"),
            pytest.param(-1.0, 15, id="negative_power"),
            pytest.param(5000.0, 0, id="no_interval"),
        ],
    )
    def test_charge_cap_is_zero_when_disabled(
        self, power_w: float, interval_minutes: int
    ) -> None:
        """Without power or time there is no per-slot allowance."""
        assert max_energy_per_slot_kwh(power_w, interval_minutes) == pytest.approx(0.0)

    def test_charge_cap_applies_efficiency_and_slot_length(self) -> None:
        """A 5 kW limit over 15 minutes at 90 % gives 1.125 kWh."""
        assert max_energy_per_slot_kwh(5000.0, 15, 0.9) == pytest.approx(1.125)

    @pytest.mark.parametrize(
        ("power_kw", "slot_hours"),
        [
            pytest.param(0.0, 0.25, id="cap_disabled"),
            pytest.param(-1.0, 0.25, id="negative_cap"),
            pytest.param(5.0, 0.0, id="no_slot_time"),
        ],
    )
    def test_export_cap_is_zero_when_disabled(
        self, power_kw: float, slot_hours: float
    ) -> None:
        """A disabled export cap allows no export energy."""
        assert export_max_energy_per_slot_kwh(power_kw, slot_hours) == pytest.approx(
            0.0
        )

    def test_export_cap_scales_with_slot_length(self) -> None:
        """A 5 kW export cap over 15 minutes gives 1.25 kWh."""
        assert export_max_energy_per_slot_kwh(5.0, 0.25) == pytest.approx(1.25)


class TestWeekdayProfileInitialisation:
    """A restored profile always has one slot per planning slot."""

    def test_empty_profiles_are_sized_from_slots_per_day(self) -> None:
        """Restoring from empty lists rebuilds them at the configured size."""
        profile = WeekdayProfile(slots_per_day=96, weekday=[], weekend=[])

        assert profile.weekday == [0.0] * 96
        assert profile.weekend == [0.0] * 96

    def test_existing_profiles_are_preserved(self) -> None:
        """A restored profile with data is left untouched."""
        profile = WeekdayProfile(
            slots_per_day=24, weekday=[1.0] * 24, weekend=[2.0] * 24
        )

        assert profile.weekday[0] == pytest.approx(1.0)
        assert profile.weekend[0] == pytest.approx(2.0)


class TestFinancialTrackingDate:
    """Period rollups need a usable calendar day."""

    def test_valid_iso_date_is_used(self) -> None:
        """The tracker's own day drives the rollup window."""
        tracker = FinancialTracker(today="2026-06-01")

        assert tracker._tracking_date() == date(2026, 6, 1)

    def test_corrupt_date_falls_back_to_today(self) -> None:
        """A malformed stored day does not break the sensors."""
        tracker = FinancialTracker(today="not-a-date")

        assert tracker._tracking_date() == date.today()
