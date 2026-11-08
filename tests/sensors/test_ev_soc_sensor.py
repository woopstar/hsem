"""Tests for the EV planning-SoC diagnostic sensors (issue #1388).

Acceptance criteria
-------------------
- The state equals ``LiveState.ev_planning_soc_pct()`` for each EV.
- While an effective SoC is known the state follows it, otherwise the
  reported SoC; ``soc_kind`` says which.
- No SoC known: the sensor is unavailable with a ``None`` value, never ``0``.
- Battery device class, measurement state class, ``%``, diagnostic.
- The second EV sensor exists only when the second EV is configured.
- ``source_entity`` / ``source_last_updated`` follow the configured sensor.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import PERCENTAGE, EntityCategory

from custom_components.hsem.coordinator_data import CoordinatorData
from custom_components.hsem.custom_sensors.ev_soc_sensor import HSEMEVSoCSensor
from custom_components.hsem.devices import HSEMDevice
from custom_components.hsem.entity import HSEMCoordinatorEntity
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.sensor import async_setup_entry as sensor_setup_entry

_ENTRY_ID = "test_entry"
_SOC_SENSOR = "sensor.car_soc"
_SOC_SECOND_SENSOR = "sensor.car2_soc"


def _entry(**options: Any) -> MagicMock:
    """Return a minimal config entry with *options*."""
    entry = MagicMock()
    entry.entry_id = _ENTRY_ID
    entry.options = options
    entry.data = {}
    return entry


def _sensor(
    live: LiveState | None,
    *,
    is_second: bool = False,
    success: bool = True,
    options: dict[str, Any] | None = None,
) -> HSEMEVSoCSensor:
    """Build a sensor against a coordinator publishing *live*."""
    coordinator = MagicMock()
    coordinator.data = None if live is None else CoordinatorData(live=live)
    coordinator.last_update_success = success
    return HSEMEVSoCSensor(_entry(**(options or {})), coordinator, is_second=is_second)


def _live(
    *,
    reported: float | None = None,
    effective: float | None = None,
    credit: float = 0.0,
    is_second: bool = False,
) -> LiveState:
    """Return a live state with SoC values for one EV."""
    live = LiveState()
    ev = live.ev_second if is_second else live.ev
    ev.soc_pct = reported
    ev.effective_soc_pct = effective
    ev.delivered_energy_credit_kwh = credit
    if is_second:
        live.ev_second_planned_load_current_soc_pct = reported
    else:
        live.ev_planned_load_current_soc_pct = reported
    return live


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


class TestMetadata:
    """Entity identity and HA classification."""

    def test_primary_identity(self) -> None:
        sensor = _sensor(None)
        assert sensor.unique_id == f"hsem_{_ENTRY_ID}_ev_soc"
        assert sensor.entity_id == "sensor.hsem_ev_soc"
        assert sensor._attr_translation_key == "ev_soc"
        assert sensor._hsem_device is HSEMDevice.EV_PRIMARY

    def test_second_identity(self) -> None:
        sensor = _sensor(None, is_second=True)
        assert sensor.unique_id == f"hsem_{_ENTRY_ID}_ev_second_soc"
        assert sensor.entity_id == "sensor.hsem_ev_second_soc"
        assert sensor._attr_translation_key == "ev_second_soc"
        assert sensor._hsem_device is HSEMDevice.EV_SECONDARY

    def test_classification(self) -> None:
        sensor = _sensor(None)
        assert sensor.device_class is SensorDeviceClass.BATTERY
        assert sensor.state_class is SensorStateClass.MEASUREMENT
        assert sensor.native_unit_of_measurement == PERCENTAGE
        assert sensor.entity_category is EntityCategory.DIAGNOSTIC
        assert sensor.should_poll is False


# ---------------------------------------------------------------------------
# State and attributes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("is_second", [False, True])
class TestState:
    """State follows ``ev_planning_soc_pct`` for both EVs."""

    def test_effective_soc_wins(self, is_second: bool) -> None:
        live = _live(reported=40.0, effective=45.5, credit=2.2, is_second=is_second)
        sensor = _sensor(live, is_second=is_second)

        assert sensor.native_value == pytest.approx(45.5)
        assert sensor.native_value == pytest.approx(
            live.ev_planning_soc_pct(is_second=is_second)
        )
        assert sensor.available is True
        attrs = sensor.extra_state_attributes
        assert attrs["soc_kind"] == "effective"
        assert attrs["reported_soc_pct"] == pytest.approx(40.0)
        assert attrs["effective_soc_pct"] == pytest.approx(45.5)
        assert attrs["delivered_energy_credit_kwh"] == pytest.approx(2.2)

    def test_reported_soc_without_effective(self, is_second: bool) -> None:
        live = _live(reported=62.0, is_second=is_second)
        sensor = _sensor(live, is_second=is_second)

        assert sensor.native_value == pytest.approx(62.0)
        assert sensor.available is True
        attrs = sensor.extra_state_attributes
        assert attrs["soc_kind"] == "reported"
        assert attrs["reported_soc_pct"] == pytest.approx(62.0)
        assert attrs["effective_soc_pct"] is None
        assert attrs["delivered_energy_credit_kwh"] == pytest.approx(0.0)

    def test_unknown_soc_is_unavailable_not_zero(self, is_second: bool) -> None:
        sensor = _sensor(_live(is_second=is_second), is_second=is_second)

        assert sensor.native_value is None
        assert sensor.available is False
        attrs = sensor.extra_state_attributes
        assert attrs["soc_kind"] is None
        assert attrs["reported_soc_pct"] is None
        assert attrs["effective_soc_pct"] is None

    def test_real_zero_is_kept(self, is_second: bool) -> None:
        sensor = _sensor(_live(reported=0.0, is_second=is_second), is_second=is_second)
        assert sensor.native_value == pytest.approx(0.0)
        assert sensor.available is True

    def test_failed_cycle_is_unavailable(self, is_second: bool) -> None:
        live = _live(reported=50.0, is_second=is_second)
        sensor = _sensor(live, is_second=is_second, success=False)
        assert sensor.available is False


def test_other_ev_values_are_ignored() -> None:
    """Each sensor reads only its own EV."""
    live = _live(reported=30.0, effective=35.0)
    assert _sensor(live, is_second=True).native_value is None
    assert _sensor(live).native_value == pytest.approx(35.0)


def test_attributes_before_first_cycle() -> None:
    """Without a snapshot the attributes are placeholders."""
    sensor = _sensor(None)
    assert sensor.native_value is None
    assert sensor.available is False
    assert sensor.extra_state_attributes == {
        "reported_soc_pct": None,
        "effective_soc_pct": None,
        "delivered_energy_credit_kwh": 0.0,
        "source_entity": None,
        "source_last_updated": None,
        "soc_kind": None,
    }


# ---------------------------------------------------------------------------
# Source sensor
# ---------------------------------------------------------------------------


class TestSource:
    """``source_entity`` and ``source_last_updated`` follow the options."""

    def test_source_entity_per_ev(self) -> None:
        options = {"hsem_ev_soc": _SOC_SENSOR, "hsem_ev_second_soc": _SOC_SECOND_SENSOR}
        primary = _sensor(_live(reported=50.0), options=options)
        second = _sensor(
            _live(reported=50.0, is_second=True), is_second=True, options=options
        )
        assert primary.extra_state_attributes["source_entity"] == _SOC_SENSOR
        assert second.extra_state_attributes["source_entity"] == _SOC_SECOND_SENSOR

    def test_source_last_updated_from_hass(self) -> None:
        sensor = _sensor(_live(reported=50.0), options={"hsem_ev_soc": _SOC_SENSOR})
        updated = datetime(2026, 11, 8, 7, 30, tzinfo=UTC)
        sensor.hass = MagicMock()
        sensor.hass.states.get.return_value = MagicMock(last_updated=updated)

        attrs = sensor.extra_state_attributes
        sensor.hass.states.get.assert_called_once_with(_SOC_SENSOR)
        assert attrs["source_last_updated"] == updated.isoformat()

    def test_source_missing_in_hass(self) -> None:
        sensor = _sensor(_live(reported=50.0), options={"hsem_ev_soc": _SOC_SENSOR})
        sensor.hass = MagicMock()
        sensor.hass.states.get.return_value = None
        assert sensor.extra_state_attributes["source_last_updated"] is None

    def test_no_source_configured(self) -> None:
        sensor = _sensor(_live(reported=50.0))
        sensor.hass = MagicMock()
        attrs = sensor.extra_state_attributes
        assert attrs["source_entity"] is None
        assert attrs["source_last_updated"] is None
        sensor.hass.states.get.assert_not_called()

    def test_switching_source_keeps_entity_id(self) -> None:
        first = _sensor(_live(reported=50.0), options={"hsem_ev_soc": _SOC_SENSOR})
        second = _sensor(
            _live(reported=50.0), options={"hsem_ev_soc": "sensor.abrp_soc"}
        )
        assert first.unique_id == second.unique_id
        assert first.entity_id == second.entity_id
        assert second.extra_state_attributes["source_entity"] == "sensor.abrp_soc"


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


async def _restore(sensor: HSEMEVSoCSensor, state: str | None) -> None:
    """Run ``async_added_to_hass`` with *state* as the restored state."""
    restored = None if state is None else MagicMock(state=state)
    sensor.async_get_last_state = AsyncMock(return_value=restored)  # type: ignore[method-assign]
    with patch.object(HSEMCoordinatorEntity, "async_added_to_hass", AsyncMock()):
        await sensor.async_added_to_hass()


class TestRestore:
    """The previous SoC carries the sensor until the first cycle."""

    @pytest.mark.asyncio
    async def test_restored_value(self) -> None:
        sensor = _sensor(None)
        await _restore(sensor, "71.5")
        assert sensor.native_value == pytest.approx(71.5)
        assert sensor.available is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", [None, "unavailable", "unknown", "garbage"])
    async def test_unusable_state_not_restored(self, state: str | None) -> None:
        sensor = _sensor(None)
        await _restore(sensor, state)
        assert sensor.native_value is None
        assert sensor.available is False

    @pytest.mark.asyncio
    async def test_live_value_overrides_restored(self) -> None:
        sensor = _sensor(_live())
        await _restore(sensor, "71.5")
        assert sensor.native_value is None
        assert sensor.available is False


# ---------------------------------------------------------------------------
# Platform setup
# ---------------------------------------------------------------------------


def _setup_entry(**options: Any) -> MagicMock:
    entry = _entry(**options)
    entry.runtime_data.coordinator = MagicMock()
    return entry


async def _soc_sensors(**options: Any) -> list[HSEMEVSoCSensor]:
    added: list[Any] = []

    def _add(entities: Any, _update_before_add: bool = False) -> None:
        added.extend(entities)

    add_entities: Any = _add
    await sensor_setup_entry(MagicMock(), _setup_entry(**options), add_entities)
    return [e for e in added if isinstance(e, HSEMEVSoCSensor)]


class TestPlatformSetup:
    """One sensor per configured EV."""

    @pytest.mark.asyncio
    async def test_no_ev_configured(self) -> None:
        sensors = await _soc_sensors(
            hsem_ev_planned_load_enabled=False,
            hsem_ev_second_planned_load_enabled=False,
        )
        assert sensors == []

    @pytest.mark.asyncio
    async def test_second_ev_absent(self) -> None:
        sensors = await _soc_sensors(
            hsem_ev_planned_load_enabled=True,
            hsem_ev_second_planned_load_enabled=False,
        )
        assert [s.entity_id for s in sensors] == ["sensor.hsem_ev_soc"]

    @pytest.mark.asyncio
    async def test_second_ev_present(self) -> None:
        sensors = await _soc_sensors(
            hsem_ev_planned_load_enabled=True,
            hsem_ev_second_planned_load_enabled=True,
        )
        assert [s.entity_id for s in sensors] == [
            "sensor.hsem_ev_soc",
            "sensor.hsem_ev_second_soc",
        ]
