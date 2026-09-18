"""Edge cases for the EV charger current-limit sensors.

This sensor publishes an actuator ceiling, so it must fail closed on anything
it cannot interpret: a snapshot with no configuration, a planned power that is
not a number, and a forward schedule that must start at the live slot and stop
at a bounded length. The second charger publishes the same contract under its
own entity ID.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.hsem.custom_sensors.ev_charger_current_limit_sensor import (
    HSEMEVChargerCurrentLimitSensor,
    HSEMEVSecondChargerCurrentLimitSensor,
)
from custom_components.hsem.utils.phase_power import (
    EV_TOPOLOGY_SINGLE_PHASE,
    EV_TOPOLOGY_THREE_PHASE_BALANCED,
    EV_TOPOLOGY_THREE_PHASE_SWITCHABLE,
)

_START = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
_SLOT = timedelta(minutes=15)


def _rec(index: int, power_w: Any = 11_040.0) -> SimpleNamespace:
    """Return a recommendation slot commanding *power_w* on the primary EV."""
    start = _START + index * _SLOT
    return SimpleNamespace(
        start=start,
        end=start + _SLOT,
        ev_charger_calculated_power=power_w,
        ev_second_charger_calculated_power=power_w,
    )


def _data(
    recs: list[SimpleNamespace],
    *,
    current_index: int = 0,
    cfg: Any = ...,
) -> SimpleNamespace:
    """Return a coordinator snapshot around *recs*."""
    if cfg is ...:
        cfg = SimpleNamespace(
            ev_planned_load_charger_phase_topology=EV_TOPOLOGY_THREE_PHASE_BALANCED,
            ev_second_planned_load_charger_phase_topology=(
                EV_TOPOLOGY_THREE_PHASE_BALANCED
            ),
        )
    return SimpleNamespace(
        cfg=cfg,
        hourly_recommendation=recs[current_index],
        hourly_recommendations=recs,
    )


def _sensor(data: Any) -> HSEMEVChargerCurrentLimitSensor:
    """Return a primary current-limit sensor bound to *data*."""
    sensor = object.__new__(HSEMEVChargerCurrentLimitSensor)
    sensor.coordinator = SimpleNamespace(  # type: ignore[assignment]  # test stub
        last_update_success=True, data=data
    )
    return sensor


class TestSnapshotWithoutConfiguration:
    """A snapshot missing its config cannot imply a phase topology."""

    def test_the_topology_falls_back_to_the_default(self) -> None:
        """No config means the conservative default topology."""
        sensor = _sensor(_data([_rec(0)], cfg=None))

        assert (
            sensor.extra_state_attributes["phase_topology"] == EV_TOPOLOGY_SINGLE_PHASE
        )

    def test_no_rated_current_is_derived(self) -> None:
        """Without a configured charger power there is no mode boundary."""
        sensor = _sensor(_data([_rec(0)], cfg=None))

        assert (
            sensor._rated_current_a(
                sensor.coordinator.data, EV_TOPOLOGY_THREE_PHASE_SWITCHABLE
            )
            is None
        )


class TestUnusablePlannedPower:
    """A planned power that is not a number commands nothing."""

    @pytest.mark.parametrize(
        "power",
        [
            pytest.param("not a number", id="unparseable"),
            pytest.param(None, id="missing"),
        ],
    )
    def test_an_unusable_value_reads_as_zero_amps(self, power: Any) -> None:
        """Bad data must never become a positive actuator ceiling."""
        sensor = _sensor(_data([_rec(0, power)]))

        assert sensor.native_value == 0


class TestForwardSchedule:
    """The published schedule starts at the live slot and is bounded."""

    def test_past_slots_are_left_out(self) -> None:
        """A slot before the live one is history, not a command."""
        recs = [_rec(i) for i in range(4)]
        sensor = _sensor(_data(recs, current_index=2))

        schedule = sensor.extra_state_attributes["schedule"]

        assert [entry["start"] for entry in schedule] == [
            recs[2].start.isoformat(),
            recs[3].start.isoformat(),
        ]

    def test_the_schedule_is_capped_at_24_slots(self) -> None:
        """A 48-slot horizon publishes only the first day of ceilings."""
        recs = [_rec(i) for i in range(48)]
        sensor = _sensor(_data(recs))

        assert len(sensor.extra_state_attributes["schedule"]) == 24


class TestSecondChargerSensor:
    """The second charger owns its own entity ID and unique ID."""

    def test_it_is_registered_under_its_own_ids(self) -> None:
        """The two chargers must never collide on one entity."""
        entry = MagicMock()
        entry.entry_id = "test_entry"
        entry.options = {}
        entry.data = {}
        coordinator = MagicMock()
        coordinator.data = None

        second = HSEMEVSecondChargerCurrentLimitSensor(entry, coordinator)
        primary = HSEMEVChargerCurrentLimitSensor(entry, coordinator)

        assert second.unique_id != primary.unique_id
        assert second.entity_id != primary.entity_id
        assert "second" in second.entity_id
