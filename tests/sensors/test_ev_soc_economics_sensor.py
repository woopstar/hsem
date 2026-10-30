"""Tests for HSEMEVSoCEconomicsSensor and HSEMEVSecondSoCEconomicsSensor (issue #903).

Acceptance criteria
--------------------
- ``state`` reflects ``EVSoCEconomicsResult.state`` from the coordinator snapshot.
- ``state`` falls back to ``STATE_UNAVAILABLE`` when data/result is missing or the
  state string is not one of the known values.
- ``state`` falls back to the restored state before the first coordinator cycle.
- ``extra_state_attributes`` returns ``EVSoCEconomicsResult.as_attributes()``.
- ``available`` mirrors whether the coordinator has data.
- Restore-state wiring only accepts values in the known state set.
- The second-EV sensor reads ``ev_second_soc_economics``, not the primary field.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.const import STATE_UNAVAILABLE, EntityCategory

from custom_components.hsem.coordinator import CoordinatorData
from custom_components.hsem.custom_sensors.ev_second_soc_economics_sensor import (
    HSEMEVSecondSoCEconomicsSensor,
)
from custom_components.hsem.custom_sensors.ev_soc_economics_sensor import (
    EV_SOC_ECONOMICS_UNRECORDED_ATTRIBUTES,
    HSEMEVSoCEconomicsSensor,
)
from custom_components.hsem.devices import HSEMDevice
from custom_components.hsem.entity import HSEMCoordinatorEntity
from custom_components.hsem.planner.ev_soc_economics import (
    EVSoCEconomicsPoint,
    EVSoCEconomicsResult,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_result(**kwargs: Any) -> EVSoCEconomicsResult:
    defaults: dict[str, Any] = {
        "state": "ready",
        "current_soc_pct": 50.0,
        "points": [
            EVSoCEconomicsPoint(
                target_soc_pct=60.0,
                deadline_label="08:00",
                deadline=datetime(2026, 8, 30, 8, 0, tzinfo=UTC),
                total_cost=1.5,
                feasible=True,
                delta_from_previous=None,
                delta_per_10pct=None,
            ),
            EVSoCEconomicsPoint(
                target_soc_pct=80.0,
                deadline_label="08:00",
                deadline=datetime(2026, 8, 30, 8, 0, tzinfo=UTC),
                total_cost=3.2,
                feasible=False,
                delta_from_previous=1.7,
                delta_per_10pct=0.85,
            ),
        ],
    }
    defaults.update(kwargs)
    return EVSoCEconomicsResult(**defaults)


def _make_coordinator_data(
    *,
    primary: EVSoCEconomicsResult | None = None,
    second: EVSoCEconomicsResult | None = None,
) -> CoordinatorData:
    return CoordinatorData(ev_soc_economics=primary, ev_second_soc_economics=second)


def _make_primary_sensor(
    data: CoordinatorData | None = None,
) -> HSEMEVSoCEconomicsSensor:
    coordinator = MagicMock()
    coordinator.data = data
    coordinator.last_update_success = data is not None

    sensor = object.__new__(HSEMEVSoCEconomicsSensor)
    sensor.coordinator = coordinator
    sensor._config_entry = MagicMock()
    sensor._hsem_device = HSEMDevice.EV_PRIMARY
    sensor._attr_unique_id = "hsem_ev_soc_economics"
    sensor.entity_id = "sensor.hsem_ev_soc_economics"
    sensor._restored_state = None
    return sensor


def _make_second_sensor(
    data: CoordinatorData | None = None,
) -> HSEMEVSecondSoCEconomicsSensor:
    coordinator = MagicMock()
    coordinator.data = data
    coordinator.last_update_success = data is not None

    sensor = object.__new__(HSEMEVSecondSoCEconomicsSensor)
    sensor.coordinator = coordinator
    sensor._config_entry = MagicMock()
    sensor._hsem_device = HSEMDevice.EV_SECONDARY
    sensor._attr_unique_id = "hsem_ev_second_soc_economics"
    sensor.entity_id = "sensor.hsem_ev_second_soc_economics"
    sensor._restored_state = None
    return sensor


# ===========================================================================
# Entity metadata
# ===========================================================================


class TestEntityMetadata:
    """Both sensors are diagnostic ENUM entities."""

    def test_primary_is_diagnostic(self) -> None:
        sensor = _make_primary_sensor()
        assert sensor._attr_entity_category is EntityCategory.DIAGNOSTIC

    def test_second_is_diagnostic(self) -> None:
        sensor = _make_second_sensor()
        assert sensor._attr_entity_category is EntityCategory.DIAGNOSTIC

    def test_primary_options_cover_all_states(self) -> None:
        sensor = _make_primary_sensor()
        assert sensor._attr_options == sorted(
            {"not_connected", "ready", "smart_charging_disabled", STATE_UNAVAILABLE}
        )

    def test_second_options_cover_all_states(self) -> None:
        sensor = _make_second_sensor()
        assert sensor._attr_options == sorted(
            {"not_connected", "ready", "smart_charging_disabled", STATE_UNAVAILABLE}
        )


# ===========================================================================
# State — primary sensor
# ===========================================================================


class TestPrimaryState:
    """state property for HSEMEVSoCEconomicsSensor."""

    def test_state_ready(self) -> None:
        data = _make_coordinator_data(primary=_make_result(state="ready"))
        sensor = _make_primary_sensor(data)
        assert sensor.state == "ready"

    def test_state_not_connected(self) -> None:
        data = _make_coordinator_data(primary=_make_result(state="not_connected"))
        sensor = _make_primary_sensor(data)
        assert sensor.state == "not_connected"

    def test_state_smart_charging_disabled(self) -> None:
        data = _make_coordinator_data(
            primary=_make_result(state="smart_charging_disabled")
        )
        sensor = _make_primary_sensor(data)
        assert sensor.state == "smart_charging_disabled"

    def test_state_unavailable_when_no_coordinator_data(self) -> None:
        sensor = _make_primary_sensor(data=None)
        assert sensor.state == STATE_UNAVAILABLE

    def test_state_unavailable_when_result_is_none(self) -> None:
        data = _make_coordinator_data(primary=None)
        sensor = _make_primary_sensor(data)
        assert sensor.state == STATE_UNAVAILABLE

    def test_state_unavailable_for_unknown_string(self) -> None:
        data = _make_coordinator_data(primary=_make_result(state="bogus"))
        sensor = _make_primary_sensor(data)
        assert sensor.state == STATE_UNAVAILABLE

    def test_state_falls_back_to_restored_state(self) -> None:
        sensor = _make_primary_sensor(data=None)
        sensor._restored_state = "ready"
        assert sensor.state == "ready"

    def test_state_prefers_live_over_restored(self) -> None:
        data = _make_coordinator_data(primary=_make_result(state="not_connected"))
        sensor = _make_primary_sensor(data)
        sensor._restored_state = "ready"
        assert sensor.state == "not_connected"


# ===========================================================================
# State — second sensor reads the second field only
# ===========================================================================


class TestSecondState:
    """state property for HSEMEVSecondSoCEconomicsSensor reads the second field."""

    def test_state_uses_second_field_not_primary(self) -> None:
        data = _make_coordinator_data(
            primary=_make_result(state="ready"),
            second=_make_result(state="not_connected"),
        )
        sensor = _make_second_sensor(data)
        assert sensor.state == "not_connected"

    def test_state_unavailable_when_second_is_none(self) -> None:
        data = _make_coordinator_data(primary=_make_result(state="ready"), second=None)
        sensor = _make_second_sensor(data)
        assert sensor.state == STATE_UNAVAILABLE


# ===========================================================================
# Availability
# ===========================================================================


class TestAvailability:
    """available mirrors whether the coordinator has data."""

    def test_available_when_data_present(self) -> None:
        sensor = _make_primary_sensor(_make_coordinator_data(primary=_make_result()))
        assert sensor.available is True

    def test_not_available_when_no_data(self) -> None:
        sensor = _make_primary_sensor(data=None)
        assert sensor.available is False

    def test_second_available_when_data_present(self) -> None:
        sensor = _make_second_sensor(_make_coordinator_data(second=_make_result()))
        assert sensor.available is True


# ===========================================================================
# Extra state attributes
# ===========================================================================


class TestExtraStateAttributes:
    """extra_state_attributes returns EVSoCEconomicsResult.as_attributes()."""

    def test_matches_as_attributes(self) -> None:
        result = _make_result()
        sensor = _make_primary_sensor(_make_coordinator_data(primary=result))
        assert sensor.extra_state_attributes == result.as_attributes()

    def test_empty_when_no_data(self) -> None:
        sensor = _make_primary_sensor(data=None)
        assert sensor.extra_state_attributes == {}

    def test_empty_when_result_is_none(self) -> None:
        sensor = _make_primary_sensor(_make_coordinator_data(primary=None))
        assert sensor.extra_state_attributes == {}

    def test_points_list_shape(self) -> None:
        result = _make_result()
        sensor = _make_primary_sensor(_make_coordinator_data(primary=result))
        points = sensor.extra_state_attributes["points"]
        assert isinstance(points, list)
        assert len(points) == 2
        assert points[0]["target_soc_pct"] == 60.0
        assert points[0]["deadline_label"] == "08:00"
        assert points[1]["feasible"] is False
        assert points[1]["delta_from_previous"] == pytest.approx(1.7)

    def test_second_sensor_reads_second_result(self) -> None:
        primary = _make_result(state="ready", current_soc_pct=10.0)
        second = _make_result(state="ready", current_soc_pct=90.0)
        data = _make_coordinator_data(primary=primary, second=second)
        sensor = _make_second_sensor(data)
        assert sensor.extra_state_attributes["current_soc_pct"] == pytest.approx(90.0)

    def test_target_deadline_attributes(self) -> None:
        """The target deadline, configured list and per-point flag (#1344)."""
        target = datetime(2026, 8, 31, 7, 15, tzinfo=UTC)
        point = EVSoCEconomicsPoint(
            target_soc_pct=80.0,
            deadline_label="07:15",
            deadline=target,
            total_cost=2.0,
            is_target_deadline=True,
        )
        result = _make_result(
            points=[point],
            target_deadline=target,
            configured_deadlines=((8, 0), (17, 0)),
        )
        attrs = _make_second_sensor(
            _make_coordinator_data(second=result)
        ).extra_state_attributes
        assert attrs["target_deadline"] == target.isoformat()
        assert attrs["configured_deadlines"] == ["08:00", "17:00"]
        assert attrs["points"][0]["is_target_deadline"] is True

    def test_target_deadline_shown_when_not_ready(self) -> None:
        """A not-connected EV still shows its target deadline."""
        target = datetime(2026, 8, 31, 7, 0, tzinfo=UTC)
        result = _make_result(state="not_connected", points=[], target_deadline=target)
        attrs = _make_primary_sensor(
            _make_coordinator_data(primary=result)
        ).extra_state_attributes
        assert attrs["target_deadline"] == target.isoformat()

    def test_charging_cost_attributes(self) -> None:
        """Points carry plan_total_cost; the result carries the baseline (#1348)."""
        point = EVSoCEconomicsPoint(
            target_soc_pct=80.0,
            deadline_label="08:00",
            deadline=datetime(2026, 8, 31, 8, 0, tzinfo=UTC),
            total_cost=18.22,
            plan_total_cost=151.6,
        )
        result = _make_result(points=[point], baseline_plan_cost=133.38)
        attrs = _make_primary_sensor(
            _make_coordinator_data(primary=result)
        ).extra_state_attributes
        assert attrs["baseline_plan_cost"] == pytest.approx(133.38)
        assert attrs["points"][0]["total_cost"] == pytest.approx(18.22)
        assert attrs["points"][0]["plan_total_cost"] == pytest.approx(151.6)

    def test_lists_are_not_recorded(self) -> None:
        """The structured attributes stay out of the recorder (#1099)."""
        assert {"points", "configured_deadlines", "baseline_plan_cost"} <= (
            EV_SOC_ECONOMICS_UNRECORDED_ATTRIBUTES
        )
        assert "target_deadline" not in EV_SOC_ECONOMICS_UNRECORDED_ATTRIBUTES


# ===========================================================================
# Restore-state wiring (async_added_to_hass)
# ===========================================================================


class TestRestoreState:
    """async_added_to_hass restores only known state strings."""

    @pytest.mark.asyncio
    async def test_restores_known_state(self) -> None:
        sensor = _make_primary_sensor(data=None)
        sensor.async_get_last_state = AsyncMock(  # type: ignore[method-assign]
            return_value=MagicMock(state="ready")
        )
        with patch.object(
            HSEMCoordinatorEntity, "async_added_to_hass", new=AsyncMock()
        ):
            await sensor.async_added_to_hass()
        assert sensor._restored_state == "ready"

    @pytest.mark.asyncio
    async def test_ignores_unknown_restored_state(self) -> None:
        sensor = _make_primary_sensor(data=None)
        sensor.async_get_last_state = AsyncMock(  # type: ignore[method-assign]
            return_value=MagicMock(state="bogus")
        )
        with patch.object(
            HSEMCoordinatorEntity, "async_added_to_hass", new=AsyncMock()
        ):
            await sensor.async_added_to_hass()
        assert sensor._restored_state is None

    @pytest.mark.asyncio
    async def test_no_last_state_leaves_restored_state_none(self) -> None:
        sensor = _make_primary_sensor(data=None)
        sensor.async_get_last_state = AsyncMock(  # type: ignore[method-assign]
            return_value=None
        )
        with patch.object(
            HSEMCoordinatorEntity, "async_added_to_hass", new=AsyncMock()
        ):
            await sensor.async_added_to_hass()
        assert sensor._restored_state is None
