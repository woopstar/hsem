"""Regression tests for HSEM datetime normalisation and slot-key helpers.

Issue: #402 — Normalise datetime handling and fix planner slot matching for
EV planned load.

These tests verify:
1. ``slot_key`` maps the same instant expressed in UTC and HA-local timezone
   to the same canonical key.
2. ``slot_key`` strips microseconds so sub-second jitter never breaks matching.
3. 15-minute interval slot normalisation floors to the correct minute.
4. 60-minute interval slot normalisation floors to the correct hour.
5. ``normalize_datetime`` converts naive datetimes to HA-local aware ones.
6. ``normalize_datetime`` converts UTC-aware datetimes to HA-local aware ones.
7. ``now()`` returns a timezone-aware datetime with microsecond=0.
8. ``_apply_planner_output`` copies ``ev_planned_load_kwh`` to final recommendations.
9. ``estimated_net_consumption_kwh`` is correct when EV load is included.
10. Unmatched planner slots emit a WARNING log — silent mismatch is disallowed.
11. The recommendation resolver does NOT erase ``ev_planned_load_kwh`` or
    ``estimated_net_consumption_kwh`` when it changes the recommendation label.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

# ---------------------------------------------------------------------------
# Helpers – build lightweight planner/coordinator objects for testing
# ---------------------------------------------------------------------------


def _make_bare_coordinator():
    """Return an ``HSEMDataUpdateCoordinator`` with mocked HA dependencies."""
    from custom_components.hsem.coordinator import HSEMDataUpdateCoordinator

    config_entry = MagicMock()
    config_entry.options = {}
    config_entry.data = {}

    coord = HSEMDataUpdateCoordinator.__new__(HSEMDataUpdateCoordinator)
    coord._config_entry = config_entry
    coord._batteries_schedules = []
    coord._batteries_schedules_remaining_capacity_needed = 0.0
    coord._plan_explanation = MagicMock()
    coord._data_quality = MagicMock()
    coord.logger = logging.getLogger("test")
    return coord


def _make_planned_slot(start: datetime, end: datetime, **kwargs):
    """Return a minimal ``PlannedSlot`` for testing."""
    from custom_components.hsem.models.planner_outputs import PlannedSlot
    from custom_components.hsem.utils.prices import SlotPrice

    defaults = {
        "price": SlotPrice(import_price=0.20, export_price=0.05),
        "avg_house_consumption_kwh": 1.0,
        "solcast_pv_estimate_kwh": 0.5,
        "ev_planned_load_kwh": 0.0,
        "estimated_net_consumption_kwh": 0.5,
        "recommendation": "batteries_wait_mode",
        "batteries_charged_kwh": 0.0,
        "batteries_discharged_kwh": 0.0,
        "estimated_battery_soc_pct": 50.0,
        "estimated_battery_capacity_kwh": 5.0,
        "estimated_cost_currency": 0.05,
        "grid_import_kwh": 0.0,
        "grid_export_kwh": 0.0,
    }
    defaults.update(kwargs)
    return PlannedSlot(start=start, end=end, **defaults)


def _make_hourly_recommendation(start: datetime, end: datetime, **kwargs):
    """Return a minimal ``HourlyRecommendation`` for testing."""
    from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation

    defaults = {
        "avg_house_consumption_kwh": 1.0,
        "avg_house_consumption_1d_kwh": 1.0,
        "avg_house_consumption_3d_kwh": 1.0,
        "avg_house_consumption_7d_kwh": 1.0,
        "avg_house_consumption_14d_kwh": 1.0,
        "batteries_charged_kwh": 0.0,
        "batteries_discharged_kwh": 0.0,
        "estimated_battery_capacity_kwh": 5.0,
        "estimated_battery_soc_pct": 50,
        "estimated_cost_currency": 0.0,
        "estimated_net_consumption_kwh": 0.0,
        "ev_planned_load_kwh": 0.0,
        "export_price": 0.05,
        "grid_export_kwh": 0.0,
        "grid_import_kwh": 0.0,
        "import_price": 0.20,
        "recommendation": None,
        "solcast_pv_estimate_kwh": 0.5,
    }
    defaults.update(kwargs)
    return HourlyRecommendation(start=start, end=end, **defaults)


# ---------------------------------------------------------------------------
# Module-level mock for dt_util.now() so tests don't need a real HA instance
# ---------------------------------------------------------------------------

_FIXED_LOCAL_TZ = ZoneInfo("Europe/Copenhagen")
_FIXED_NOW = datetime(2026, 5, 14, 22, 0, 0, tzinfo=_FIXED_LOCAL_TZ)


@pytest.fixture(autouse=True)
def mock_dt_util_now():
    """Patch ``homeassistant.util.dt.now`` and ``as_local`` for all tests in this file."""
    with (
        patch("homeassistant.util.dt.now", return_value=_FIXED_NOW),
        patch(
            "homeassistant.util.dt.as_local",
            side_effect=lambda dt: dt.astimezone(_FIXED_LOCAL_TZ),
        ),
        patch(
            "homeassistant.util.dt.DEFAULT_TIME_ZONE",
            _FIXED_LOCAL_TZ,
        ),
    ):
        yield


# ===========================================================================
# Test 1: slot_key – same instant in UTC and local timezone → same key
# ===========================================================================


class TestSlotKeyTimezoneEquivalence:
    """slot_key must map the same real instant in any timezone to the same key."""

    def test_utc_and_local_same_instant_60min(self):
        """UTC 20:00 and Copenhagen +02:00 22:00 are the same instant → same key.

        This covers the core production scenario where the planner builds slot
        starts from ``datetime.fromisoformat(now_iso)`` carrying a fixed
        numeric offset (+02:00) while the coordinator builds recommendation
        slots from ``dt_util.now()`` which carries a ``ZoneInfo`` tzinfo.
        """
        from custom_components.hsem.utils.datetime_utils import slot_key

        utc_time = datetime(2026, 5, 14, 20, 0, 0, tzinfo=UTC)
        local_time = datetime(2026, 5, 14, 22, 0, 0, tzinfo=_FIXED_LOCAL_TZ)

        key_utc = slot_key(utc_time, interval_minutes=60)
        key_local = slot_key(local_time, interval_minutes=60)

        assert key_utc == key_local, (
            f"Same instant expressed in UTC and local timezone must produce the same "
            f"slot_key. UTC key={key_utc!r}, local key={key_local!r}"
        )

    def test_fixed_offset_and_zoneinfo_same_instant_60min(self):
        """ZoneInfo and fixed +02:00 offset for the same instant → same key.

        This is the exact mismatch that could break EV planned-load matching:
        coordinator recs use ZoneInfo('Europe/Copenhagen'), planner slots use
        a fixed +02:00 numeric offset from parsing now_iso.
        """
        from custom_components.hsem.utils.datetime_utils import slot_key

        tz_fixed = timezone(timedelta(hours=2))
        tz_zone = ZoneInfo("Europe/Copenhagen")

        t_fixed = datetime(2026, 5, 14, 22, 0, 0, tzinfo=tz_fixed)
        t_zone = datetime(2026, 5, 14, 22, 0, 0, tzinfo=tz_zone)

        assert slot_key(t_fixed, 60) == slot_key(t_zone, 60)

    def test_utc_and_local_same_instant_15min(self):
        """Same instant in UTC vs local works for 15-minute slots too."""
        from custom_components.hsem.utils.datetime_utils import slot_key

        utc_time = datetime(2026, 5, 14, 20, 17, 0, tzinfo=UTC)
        local_time = datetime(2026, 5, 14, 22, 17, 0, tzinfo=_FIXED_LOCAL_TZ)

        key_utc = slot_key(utc_time, interval_minutes=15)
        key_local = slot_key(local_time, interval_minutes=15)

        assert key_utc == key_local, (
            f"15-min slot: UTC key={key_utc!r}, local key={key_local!r}"
        )


# ===========================================================================
# Test 2: slot_key strips microseconds
# ===========================================================================


class TestSlotKeyMicroseconds:
    """slot_key must strip microseconds so jitter from dt_util.now() is ignored."""

    def test_microseconds_stripped_60min(self):
        """Datetimes differing only in microseconds must yield the same slot key (60 min)."""
        from custom_components.hsem.utils.datetime_utils import slot_key

        tz = _FIXED_LOCAL_TZ
        t_clean = datetime(2026, 5, 14, 22, 0, 0, microsecond=0, tzinfo=tz)
        t_dirty = datetime(2026, 5, 14, 22, 0, 0, microsecond=123456, tzinfo=tz)

        assert slot_key(t_clean, 60) == slot_key(t_dirty, 60), (
            "Microseconds must not prevent slot matching for 60-min intervals"
        )

    def test_microseconds_stripped_15min(self):
        """Same check for 15-minute slots."""
        from custom_components.hsem.utils.datetime_utils import slot_key

        tz = _FIXED_LOCAL_TZ
        t_clean = datetime(2026, 5, 14, 22, 15, 0, microsecond=0, tzinfo=tz)
        t_dirty = datetime(2026, 5, 14, 22, 15, 0, microsecond=999999, tzinfo=tz)

        assert slot_key(t_clean, 15) == slot_key(t_dirty, 15)

    def test_utc_microsecond_vs_local_zero(self):
        """A UTC datetime with microseconds must match a local datetime with microsecond=0."""
        from custom_components.hsem.utils.datetime_utils import slot_key

        # Simulate rec.start from dt_util.now() → microsecond != 0
        rec_start = datetime(2026, 5, 14, 20, 0, 0, microsecond=654321, tzinfo=UTC)
        # Planner builds slot.start via timedelta arithmetic → microsecond=0
        slot_start = datetime(
            2026, 5, 14, 22, 0, 0, microsecond=0, tzinfo=_FIXED_LOCAL_TZ
        )

        assert slot_key(rec_start, 60) == slot_key(slot_start, 60), (
            "UTC rec.start with microseconds must match local slot.start with microsecond=0"
        )


# ===========================================================================
# Test 3: 15-minute interval slot normalisation
# ===========================================================================


class TestNormalizeSlotStart15Min:
    """normalize_slot_start must floor to the correct 15-minute boundary."""

    @pytest.mark.parametrize(
        "minute_in, minute_out",
        [
            (0, 0),
            (1, 0),
            (14, 0),
            (15, 15),
            (16, 15),
            (29, 15),
            (30, 30),
            (44, 30),
            (45, 45),
            (59, 45),
        ],
    )
    def test_floor_to_15min_boundary(self, minute_in: int, minute_out: int):
        """22:mm:ss → 22:floored:00 for various minutes."""
        from custom_components.hsem.utils.datetime_utils import normalize_slot_start

        tz = _FIXED_LOCAL_TZ
        value = datetime(2026, 5, 14, 22, minute_in, 42, tzinfo=tz)
        result = normalize_slot_start(value, interval_minutes=15)

        assert result.minute == minute_out, (
            f"minute={minute_in} should floor to {minute_out}, got {result.minute}"
        )
        assert result.second == 0, "second must be 0 after normalisation"
        assert result.microsecond == 0, "microsecond must be 0 after normalisation"

    def test_issue_example_22_17_42(self):
        """Issue example: 22:17:42 with 15-min interval → 22:15:00."""
        from custom_components.hsem.utils.datetime_utils import normalize_slot_start

        value = datetime(2026, 5, 14, 22, 17, 42, tzinfo=_FIXED_LOCAL_TZ)
        result = normalize_slot_start(value, interval_minutes=15)

        assert result.hour == 22
        assert result.minute == 15
        assert result.second == 0
        assert result.microsecond == 0


# ===========================================================================
# Test 4: 60-minute interval slot normalisation
# ===========================================================================


class TestNormalizeSlotStart60Min:
    """normalize_slot_start must floor to the correct 60-minute boundary."""

    @pytest.mark.parametrize(
        "minute_in",
        [0, 1, 17, 30, 42, 59],
    )
    def test_floor_to_60min_boundary(self, minute_in: int):
        """Any 22:mm:ss → 22:00:00 for 60-min slots."""
        from custom_components.hsem.utils.datetime_utils import normalize_slot_start

        tz = _FIXED_LOCAL_TZ
        value = datetime(2026, 5, 14, 22, minute_in, 42, tzinfo=tz)
        result = normalize_slot_start(value, interval_minutes=60)

        assert result.minute == 0, (
            f"minute={minute_in} should floor to 0 for 60-min, got {result.minute}"
        )
        assert result.second == 0
        assert result.microsecond == 0

    def test_issue_example_22_17_42(self):
        """Issue example: 22:17:42 with 60-min interval → 22:00:00."""
        from custom_components.hsem.utils.datetime_utils import normalize_slot_start

        value = datetime(2026, 5, 14, 22, 17, 42, tzinfo=_FIXED_LOCAL_TZ)
        result = normalize_slot_start(value, interval_minutes=60)

        assert result.hour == 22
        assert result.minute == 0
        assert result.second == 0
        assert result.microsecond == 0

    def test_already_on_boundary_unchanged(self):
        """22:00:00 floored to 60-min boundary must still be 22:00:00."""
        from custom_components.hsem.utils.datetime_utils import normalize_slot_start

        value = datetime(2026, 5, 14, 22, 0, 0, tzinfo=_FIXED_LOCAL_TZ)
        result = normalize_slot_start(value, interval_minutes=60)

        assert result.hour == 22
        assert result.minute == 0


# ===========================================================================
# Bonus: normalize_datetime and now() helpers
# ===========================================================================


class TestNormalizeDateTime:
    """normalize_datetime must convert to HA-local and strip microseconds."""

    def test_utc_aware_converted_to_local(self):
        """A UTC-aware datetime must be converted to HA-local timezone."""
        from custom_components.hsem.utils.datetime_utils import normalize_datetime

        utc_dt = datetime(2026, 5, 14, 20, 0, 0, tzinfo=UTC)
        result = normalize_datetime(utc_dt)

        # Result must be timezone-aware
        assert result.tzinfo is not None
        # The instant is the same: 22:00 local = 20:00 UTC
        assert result.hour == 22
        assert result.microsecond == 0

    def test_microseconds_stripped(self):
        """normalize_datetime must always strip microseconds."""
        from custom_components.hsem.utils.datetime_utils import normalize_datetime

        dt_with_us = datetime(
            2026, 5, 14, 22, 0, 0, microsecond=999999, tzinfo=_FIXED_LOCAL_TZ
        )
        result = normalize_datetime(dt_with_us)

        assert result.microsecond == 0

    def test_naive_datetime_gets_local_tz(self):
        """A naive datetime must get the HA-local timezone attached."""
        from custom_components.hsem.utils.datetime_utils import normalize_datetime

        naive_dt = datetime(2026, 5, 14, 22, 0, 0)
        result = normalize_datetime(naive_dt)

        assert result.tzinfo is not None
        assert result.microsecond == 0


class TestNow:
    """now() must return timezone-aware current time with microsecond=0."""

    def test_now_is_timezone_aware(self):
        """now() must be timezone-aware (not naive)."""
        from custom_components.hsem.utils.datetime_utils import now

        result = now()
        assert result.tzinfo is not None

    def test_now_has_no_microseconds(self):
        """now() must return microsecond=0."""
        from custom_components.hsem.utils.datetime_utils import now

        result = now()
        assert result.microsecond == 0


# ===========================================================================
# Test 5: EV planned load reaches final recommendation
# ===========================================================================


class TestEvPlannedLoadReachesRecommendation:
    """ev_planned_load_kwh from the planner must appear in final recommendations."""

    def _build_slots_and_recs(
        self, midnight: datetime, ev_hours: dict[int, float], interval_minutes: int = 60
    ):
        """Build matching slots and recs for the given horizon."""
        from custom_components.hsem.models.planner_outputs import PlannerOutput

        recs = []
        slots = []
        total_slots = (24 * 60) // interval_minutes
        for i in range(total_slots):
            t_start = midnight + timedelta(minutes=i * interval_minutes)
            t_end = t_start + timedelta(minutes=interval_minutes)
            ev = ev_hours.get(t_start.hour, 0.0)
            recs.append(_make_hourly_recommendation(t_start, t_end))
            slots.append(
                _make_planned_slot(
                    t_start,
                    t_end,
                    ev_planned_load_kwh=ev,
                    estimated_net_consumption_kwh=round(1.0 + ev - 0.5, 3),
                )
            )

        return recs, PlannerOutput(slots=slots)

    def test_ev_load_nonzero_in_final_recommendation(self):
        """ev_planned_load_kwh > 0 must appear in final recs when planner produced EV load.

        This is the primary regression test for issue #402.  It verifies that the
        slot matching step actually propagates the EV load — the test would fail on
        the old code if the slot keys did not match.
        """
        midnight = datetime(2026, 5, 14, 0, 0, 0, tzinfo=_FIXED_LOCAL_TZ)
        ev_hour = 22
        ev_load = 3.5

        coord = _make_bare_coordinator()
        recs, output = self._build_slots_and_recs(midnight, {ev_hour: ev_load})
        coord._hourly_recommendations = recs

        coord._apply_planner_output(output)

        ev_rec = next(r for r in recs if r.start.hour == ev_hour)
        assert ev_rec.ev_planned_load_kwh == pytest.approx(ev_load, abs=1e-9), (
            f"ev_planned_load_kwh should be {ev_load} but got {ev_rec.ev_planned_load_kwh}"
        )

    def test_ev_load_stays_zero_in_non_ev_hours(self):
        """Hours without EV planned load must remain at ev_planned_load_kwh=0."""
        midnight = datetime(2026, 5, 14, 0, 0, 0, tzinfo=_FIXED_LOCAL_TZ)

        coord = _make_bare_coordinator()
        recs, output = self._build_slots_and_recs(midnight, {22: 3.5})
        coord._hourly_recommendations = recs

        coord._apply_planner_output(output)

        non_ev_recs = [r for r in recs if r.start.hour != 22]
        for rec in non_ev_recs:
            assert rec.ev_planned_load_kwh == pytest.approx(0.0, abs=1e-9), (
                f"Hour {rec.start.hour} should have ev_planned_load_kwh=0, "
                f"got {rec.ev_planned_load_kwh}"
            )

    def test_ev_load_with_fixed_offset_timezone(self):
        """EV load must propagate even when planner slots use a fixed +02:00 offset.

        This simulates the production case where the coordinator creates recs from
        dt_util.now() (ZoneInfo tzinfo) while the planner parses now_iso and
        creates slots with a fixed numeric offset.
        """
        from custom_components.hsem.models.planner_outputs import PlannerOutput

        tz_fixed = timezone(timedelta(hours=2))  # +02:00 — same as Copenhagen CEST

        midnight_local = datetime(2026, 5, 14, 0, 0, 0, tzinfo=_FIXED_LOCAL_TZ)
        midnight_fixed = datetime(2026, 5, 14, 0, 0, 0, tzinfo=tz_fixed)

        # Recs use ZoneInfo; slots use fixed offset
        recs = []
        slots = []
        for h in range(24):
            t_rec_start = midnight_local + timedelta(hours=h)
            t_rec_end = t_rec_start + timedelta(hours=1)
            t_slot_start = midnight_fixed + timedelta(hours=h)
            t_slot_end = t_slot_start + timedelta(hours=1)
            ev = 4.2 if h == 20 else 0.0
            recs.append(_make_hourly_recommendation(t_rec_start, t_rec_end))
            slots.append(
                _make_planned_slot(
                    t_slot_start,
                    t_slot_end,
                    ev_planned_load_kwh=ev,
                    estimated_net_consumption_kwh=round(1.0 + ev - 0.5, 3),
                )
            )

        coord = _make_bare_coordinator()
        coord._hourly_recommendations = recs
        coord._apply_planner_output(PlannerOutput(slots=slots))

        ev_rec = next(r for r in recs if r.start.hour == 20)
        assert ev_rec.ev_planned_load_kwh == pytest.approx(4.2, abs=1e-9), (
            f"ZoneInfo/fixed-offset: ev_planned_load_kwh={ev_rec.ev_planned_load_kwh}"
        )

    def test_ev_load_with_microsecond_jitter_in_recs(self):
        """EV load must propagate even when rec.start carries non-zero microseconds."""
        from custom_components.hsem.models.planner_outputs import PlannerOutput

        midnight = datetime(2026, 5, 14, 0, 0, 0, tzinfo=_FIXED_LOCAL_TZ)

        recs = []
        slots = []
        for h in range(24):
            # Rec starts carry microseconds (simulating dt_util.now() jitter)
            t_rec_start = midnight + timedelta(hours=h, microseconds=123456)
            t_rec_end = t_rec_start + timedelta(hours=1)
            # Slot starts have microsecond=0 (planner uses midnight + timedelta)
            t_slot_start = midnight + timedelta(hours=h)
            t_slot_end = t_slot_start + timedelta(hours=1)
            ev = 2.8 if h == 14 else 0.0
            recs.append(_make_hourly_recommendation(t_rec_start, t_rec_end))
            slots.append(
                _make_planned_slot(
                    t_slot_start,
                    t_slot_end,
                    ev_planned_load_kwh=ev,
                    estimated_net_consumption_kwh=round(1.0 + ev - 0.5, 3),
                )
            )

        coord = _make_bare_coordinator()
        coord._hourly_recommendations = recs
        coord._apply_planner_output(PlannerOutput(slots=slots))

        ev_rec = next(r for r in recs if r.start.hour == 14)
        assert ev_rec.ev_planned_load_kwh == pytest.approx(2.8, abs=1e-9), (
            f"Microsecond mismatch: ev_planned_load_kwh={ev_rec.ev_planned_load_kwh}"
        )


# ===========================================================================
# Test 6: estimated_net_consumption_kwh includes EV load
# ===========================================================================


class TestEstimatedNetConsumptionIncludesEVLoad:
    """estimated_net_consumption_kwh must equal avg_house_consumption_kwh + ev_load - pv."""

    def test_formula_correct(self):
        """1.5 + 3.0 - 0.5 = 4.0 as per issue spec."""
        from custom_components.hsem.models.planner_outputs import PlannerOutput

        avg_house_consumption_kwh = 1.5
        ev_planned_load_kwh = 3.0
        solcast_pv_estimate_kwh = 0.5
        expected_net = (
            avg_house_consumption_kwh + ev_planned_load_kwh - solcast_pv_estimate_kwh
        )

        midnight = datetime(2026, 5, 14, 0, 0, 0, tzinfo=_FIXED_LOCAL_TZ)
        h = 10
        t_start = midnight + timedelta(hours=h)
        t_end = t_start + timedelta(hours=1)

        rec = _make_hourly_recommendation(
            t_start,
            t_end,
            avg_house_consumption_kwh=avg_house_consumption_kwh,
            solcast_pv_estimate_kwh=solcast_pv_estimate_kwh,
        )
        slot = _make_planned_slot(
            t_start,
            t_end,
            avg_house_consumption_kwh=avg_house_consumption_kwh,
            solcast_pv_estimate_kwh=solcast_pv_estimate_kwh,
            ev_planned_load_kwh=ev_planned_load_kwh,
            estimated_net_consumption_kwh=round(expected_net, 3),
        )

        coord = _make_bare_coordinator()
        coord._hourly_recommendations = [rec]
        coord._apply_planner_output(PlannerOutput(slots=[slot]))

        assert rec.estimated_net_consumption_kwh == pytest.approx(
            expected_net, abs=1e-6
        ), (
            f"Expected estimated_net_consumption_kwh={expected_net}, "
            f"got {rec.estimated_net_consumption_kwh}"
        )
        assert rec.ev_planned_load_kwh == pytest.approx(ev_planned_load_kwh, abs=1e-9)

    def test_zero_ev_load_net_consumption_is_consumption_minus_pv(self):
        """Without EV load: net = house - pv."""
        from custom_components.hsem.models.planner_outputs import PlannerOutput

        avg_house_consumption_kwh = 1.2
        solcast_pv_estimate_kwh = 0.8
        expected_net = avg_house_consumption_kwh - solcast_pv_estimate_kwh

        midnight = datetime(2026, 5, 14, 0, 0, 0, tzinfo=_FIXED_LOCAL_TZ)
        t_start = midnight + timedelta(hours=12)
        t_end = t_start + timedelta(hours=1)

        rec = _make_hourly_recommendation(
            t_start, t_end, avg_house_consumption_kwh=avg_house_consumption_kwh
        )
        slot = _make_planned_slot(
            t_start,
            t_end,
            avg_house_consumption_kwh=avg_house_consumption_kwh,
            solcast_pv_estimate_kwh=solcast_pv_estimate_kwh,
            ev_planned_load_kwh=0.0,
            estimated_net_consumption_kwh=round(expected_net, 3),
        )

        coord = _make_bare_coordinator()
        coord._hourly_recommendations = [rec]
        coord._apply_planner_output(PlannerOutput(slots=[slot]))

        assert rec.estimated_net_consumption_kwh == pytest.approx(
            expected_net, abs=1e-6
        )


# ===========================================================================
# Test 7: Unmatched planner slot logs a WARNING
# ===========================================================================


class TestUnmatchedSlotLogsWarning:
    """An unmatched planner slot must emit a WARNING — silent mismatch is forbidden."""

    def test_warning_logged_for_unmatched_rec(self):
        """A rec with no matching planner slot must produce a WARNING log."""
        import io
        import logging

        from custom_components.hsem.models.planner_outputs import PlannerOutput
        from custom_components.hsem.utils.logger import HSEM_LOGGER

        midnight = datetime(2026, 5, 14, 0, 0, 0, tzinfo=UTC)

        # One rec at hour 23 but planner only has slots for hours 0-22
        orphan_rec = _make_hourly_recommendation(
            midnight + timedelta(hours=23),
            midnight + timedelta(hours=24),
        )

        slots = [
            _make_planned_slot(
                midnight + timedelta(hours=h),
                midnight + timedelta(hours=h + 1),
            )
            for h in range(23)
        ]

        coord = _make_bare_coordinator()
        coord._hourly_recommendations = [orphan_rec]

        # Capture WARNING from coordinator logger via HSEM_LOGGER
        capture = io.StringIO()
        handler = logging.StreamHandler(capture)
        handler.setLevel(logging.WARNING)
        HSEM_LOGGER.addHandler(handler)
        try:
            coord._apply_planner_output(PlannerOutput(slots=slots))
            output = capture.getvalue()
        finally:
            HSEM_LOGGER.removeHandler(handler)

        assert any(word in output.lower() for word in ("unmatched", "no matching")), (
            f"Expected WARNING about unmatched slot. Logged output: {output}"
        )

    def test_unmatched_rec_fields_remain_at_defaults(self):
        """An unmatched rec must not have its energy fields mutated."""
        from custom_components.hsem.models.planner_outputs import PlannerOutput

        midnight = datetime(2026, 5, 14, 0, 0, 0, tzinfo=UTC)

        orphan = _make_hourly_recommendation(
            midnight + timedelta(hours=23),
            midnight + timedelta(hours=24),
            ev_planned_load_kwh=0.0,
            estimated_net_consumption_kwh=0.0,
        )

        # Planner has slots for 0-22 only; slot at 23 does not exist
        slots = [
            _make_planned_slot(
                midnight + timedelta(hours=h),
                midnight + timedelta(hours=h + 1),
                ev_planned_load_kwh=9.9,
            )
            for h in range(23)
        ]

        coord = _make_bare_coordinator()
        coord._hourly_recommendations = [orphan]
        coord._apply_planner_output(PlannerOutput(slots=slots))

        assert orphan.ev_planned_load_kwh == pytest.approx(0.0, abs=1e-9)
        assert orphan.recommendation is None


# ===========================================================================
# Test 8: Resolver does not erase EV fields
# ===========================================================================


class TestResolverDoesNotEraseEVFields:
    """The recommendation resolver must not overwrite ev_planned_load_kwh or
    estimated_net_consumption_kwh when it changes the recommendation label."""

    def _make_live_state(self, **kwargs):
        """Build a minimal LiveState-like mock."""
        from custom_components.hsem.models.live_state import LiveState

        defaults = {
            "energi_data_service_import_price": "0.25",
            "ev": MagicMock(is_charging=False),
            "ev_second": MagicMock(is_charging=False),
            "battery_current_capacity_kwh": 5.0,
        }
        defaults.update(kwargs)

        live = MagicMock(spec=LiveState)
        for k, v in defaults.items():
            setattr(live, k, v)
        return live

    def test_resolver_preserves_ev_load_when_relabelling(self):
        """Resolver changing the label must not zero out ev_planned_load_kwh."""
        from custom_components.hsem.custom_sensors.recommendation_resolver import (
            resolve_current_recommendation,
        )

        midnight = datetime(2026, 5, 14, 0, 0, 0, tzinfo=_FIXED_LOCAL_TZ)
        t_start = midnight + timedelta(hours=14)
        t_end = t_start + timedelta(hours=1)

        rec = _make_hourly_recommendation(
            t_start,
            t_end,
            ev_planned_load_kwh=3.5,
            estimated_net_consumption_kwh=4.0,
            recommendation="batteries_wait_mode",
        )

        # EV is actively charging — resolver will relabel to ev_smart_charging
        live = self._make_live_state(ev=MagicMock(is_charging=True))

        resolve_current_recommendation(
            rec, live, batteries_schedules_remaining_capacity_needed=0.0
        )

        # Label changes but energy fields must be preserved
        assert rec.recommendation == "ev_smart_charging"
        assert rec.ev_planned_load_kwh == pytest.approx(3.5, abs=1e-9), (
            f"ev_planned_load_kwh was erased by resolver: {rec.ev_planned_load_kwh}"
        )
        assert rec.estimated_net_consumption_kwh == pytest.approx(4.0, abs=1e-9), (
            f"estimated_net_consumption_kwh was erased by resolver: {rec.estimated_net_consumption_kwh}"
        )

    def test_resolver_preserves_ev_load_on_negative_price_override(self):
        """ForceExport override must not clear ev_planned_load_kwh."""
        from custom_components.hsem.custom_sensors.recommendation_resolver import (
            resolve_current_recommendation,
        )

        midnight = datetime(2026, 5, 14, 0, 0, 0, tzinfo=_FIXED_LOCAL_TZ)
        t_start = midnight + timedelta(hours=11)
        t_end = t_start + timedelta(hours=1)

        rec = _make_hourly_recommendation(
            t_start,
            t_end,
            ev_planned_load_kwh=2.1,
            estimated_net_consumption_kwh=2.8,
            recommendation="batteries_charge_solar",
        )

        # Negative import price → ForceExport
        live = self._make_live_state(
            energi_data_service_import_price="-0.05",
            ev=MagicMock(is_charging=False),
        )

        resolve_current_recommendation(
            rec, live, batteries_schedules_remaining_capacity_needed=0.0
        )

        assert rec.recommendation == "force_export"
        assert rec.ev_planned_load_kwh == pytest.approx(2.1, abs=1e-9)
        assert rec.estimated_net_consumption_kwh == pytest.approx(2.8, abs=1e-9)

    def test_resolver_preserves_ev_load_on_discharge_override(self):
        """BatteriesDischargeMode override must not clear ev_planned_load_kwh."""
        from custom_components.hsem.custom_sensors.recommendation_resolver import (
            resolve_current_recommendation,
        )

        midnight = datetime(2026, 5, 14, 0, 0, 0, tzinfo=_FIXED_LOCAL_TZ)
        t_start = midnight + timedelta(hours=17)
        t_end = t_start + timedelta(hours=1)

        rec = _make_hourly_recommendation(
            t_start,
            t_end,
            ev_planned_load_kwh=1.5,
            estimated_net_consumption_kwh=2.0,
            recommendation="batteries_wait_mode",
        )

        # Battery above schedule need → discharge override
        live = self._make_live_state(
            energi_data_service_import_price="0.30",
            ev=MagicMock(is_charging=False),
            ev_second=MagicMock(is_charging=False),
            battery_current_capacity_kwh=8.0,
        )

        resolve_current_recommendation(
            rec, live, batteries_schedules_remaining_capacity_needed=5.0
        )

        assert rec.recommendation == "batteries_discharge_mode"
        assert rec.ev_planned_load_kwh == pytest.approx(1.5, abs=1e-9)
        assert rec.estimated_net_consumption_kwh == pytest.approx(2.0, abs=1e-9)


# ===========================================================================
# Test: slot_key invalid interval raises ValueError
# ===========================================================================


class TestSlotKeyEdgeCases:
    """Edge cases and error handling for datetime utility functions."""

    def test_invalid_interval_raises(self):
        """slot_key with interval_minutes <= 0 must raise ValueError."""
        from custom_components.hsem.utils.datetime_utils import slot_key

        t = datetime(2026, 5, 14, 22, 0, 0, tzinfo=_FIXED_LOCAL_TZ)

        with pytest.raises(ValueError, match="interval_minutes must be positive"):
            slot_key(t, interval_minutes=0)

        with pytest.raises(ValueError, match="interval_minutes must be positive"):
            slot_key(t, interval_minutes=-15)

    def test_slot_key_is_idempotent(self):
        """Calling slot_key on an already-normalised datetime returns the same value."""
        from custom_components.hsem.utils.datetime_utils import slot_key

        t = datetime(2026, 5, 14, 22, 0, 0, tzinfo=_FIXED_LOCAL_TZ)
        key1 = slot_key(t, 60)
        key2 = slot_key(key1, 60)

        assert key1 == key2

    def test_60min_multiple_calls_stable(self):
        """slot_key is deterministic: same input always produces same output."""
        from custom_components.hsem.utils.datetime_utils import slot_key

        t = datetime(
            2026, 5, 14, 22, 33, 44, microsecond=500000, tzinfo=_FIXED_LOCAL_TZ
        )
        expected = slot_key(t, 60)

        for _ in range(5):
            assert slot_key(t, 60) == expected
