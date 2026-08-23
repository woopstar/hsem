"""Fail-closed behavior for the executable EV current ceiling."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.hsem.custom_sensors.ev_charger_current_limit_sensor import (
    HSEMEVChargerCurrentLimitSensor,
)
from custom_components.hsem.entity import HSEMCoordinatorEntity
from custom_components.hsem.utils.phase_power import EV_TOPOLOGY_THREE_PHASE_BALANCED


def _recommendation(power_w: float) -> SimpleNamespace:
    start = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    return SimpleNamespace(
        start=start,
        end=start + timedelta(minutes=15),
        ev_charger_calculated_power=power_w,
        ev_second_charger_calculated_power=0.0,
    )


def _sensor(*, success: bool, data: object | None) -> HSEMEVChargerCurrentLimitSensor:
    sensor = object.__new__(HSEMEVChargerCurrentLimitSensor)
    sensor.coordinator = SimpleNamespace(  # type: ignore[assignment]
        last_update_success=success,
        data=data,
    )
    return sensor


def test_stale_positive_restore_is_never_an_available_command() -> None:
    """A failed coordinator cycle owns no ceiling, even with stale plan data."""
    rec = _recommendation(11_040.0)
    data = SimpleNamespace(
        cfg=SimpleNamespace(
            ev_planned_load_charger_phase_topology=EV_TOPOLOGY_THREE_PHASE_BALANCED
        ),
        hourly_recommendation=rec,
        hourly_recommendations=[rec],
    )
    sensor = _sensor(success=False, data=data)
    # Model an object restored by an older release.  The property deliberately
    # ignores this value; it cannot resurrect a positive actuator ceiling.
    sensor._restored_state = 16  # type: ignore[attr-defined]

    assert sensor.native_value == 0
    assert sensor.available is False
    assert sensor.extra_state_attributes == {"phase_topology": None, "schedule": []}


def test_successful_live_recommendation_publishes_the_floored_ceiling() -> None:
    """Only a successful coordinator-owned recommendation becomes available."""
    rec = _recommendation(11_000.0)
    data = SimpleNamespace(
        cfg=SimpleNamespace(
            ev_planned_load_charger_phase_topology=EV_TOPOLOGY_THREE_PHASE_BALANCED
        ),
        hourly_recommendation=rec,
        hourly_recommendations=[rec],
    )
    sensor = _sensor(success=True, data=data)

    assert sensor.available is True
    assert sensor.native_value == 15
    assert sensor.extra_state_attributes["schedule"] == [
        {
            "start": rec.start.isoformat(),
            "current_a": 15,
            "power_w": 11_000.0,
        }
    ]


@pytest.mark.asyncio
async def test_entity_registration_does_not_read_the_restored_state() -> None:
    """RestoreEntity remains in the MRO but startup never consumes its value."""
    sensor = _sensor(success=False, data=None)
    sensor.async_get_last_state = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(state="16")
    )

    with patch.object(
        HSEMCoordinatorEntity,
        "async_added_to_hass",
        new=AsyncMock(),
    ):
        await sensor.async_added_to_hass()

    sensor.async_get_last_state.assert_not_awaited()
    assert sensor.native_value == 0
    assert sensor.available is False
