"""Tests for config-gated entity creation (issue #859).

Verifies that OCPP, EV1, and EV2 entities across the sensor, switch,
number, and time platforms are only created when the corresponding
feature is actually configured, instead of always being registered.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.hsem.custom_sensors.ev_charger_calculated_power_sensor import (
    HSEMEVChargerCalculatedPowerSensor,
    HSEMEVSecondChargerCalculatedPowerSensor,
)
from custom_components.hsem.custom_sensors.ev_charging_sensor import (
    HSEMEVChargingSensor,
)
from custom_components.hsem.custom_sensors.ev_optimal_charging_plan_sensor import (
    HSEMEVOptimalChargingPlanSensor,
)
from custom_components.hsem.custom_sensors.ev_second_optimal_charging_plan_sensor import (
    HSEMEVSecondOptimalChargingPlanSensor,
)
from custom_components.hsem.custom_sensors.ocpp_sensors import (
    HSEMOCPPChargerStatusSensor,
)
from custom_components.hsem.number import async_setup_entry as number_setup_entry
from custom_components.hsem.sensor import async_setup_entry as sensor_setup_entry
from custom_components.hsem.switch import async_setup_entry as switch_setup_entry
from custom_components.hsem.time import async_setup_entry as time_setup_entry
from custom_components.hsem.utils.sensornames.ev import (
    get_ev_deadline_time_key,
    get_ev_second_deadline_time_key,
    get_ev_second_target_soc_number_key,
    get_ev_target_soc_number_key,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config_entry(**options: Any) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    entry.options = options
    entry.data = {}
    return entry


def _collector() -> tuple[list[Any], Any]:
    added: list[Any] = []

    def add_entities(entities: Any, _update_before_add: bool = False) -> None:
        added.extend(entities)

    return added, add_entities


# ---------------------------------------------------------------------------
# switch.py
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_setup_excludes_ev_switches_when_disabled() -> None:
    """No EV switches are created when neither EV's planned load is enabled."""
    from custom_components.hsem.utils.sensornames.controls import (
        get_read_only_switch_key,
    )
    from custom_components.hsem.utils.sensornames.ev import (
        get_ev_auto_full_negative_price_switch_key,
        get_ev_force_charge_now_switch_key,
        get_ev_force_discharge_switch_key,
        get_ev_second_force_charge_now_switch_key,
        get_ev_second_smart_charging_switch_key,
        get_ev_smart_charging_switch_key,
    )

    config_entry = _make_config_entry(
        hsem_ev_planned_load_enabled=False,
        hsem_ev_second_planned_load_enabled=False,
    )
    added, add_entities = _collector()
    await switch_setup_entry(MagicMock(), config_entry, add_entities)
    keys = {e.entity_description.key for e in added}
    for ev_key in (
        get_ev_force_discharge_switch_key(),
        get_ev_smart_charging_switch_key(),
        get_ev_force_charge_now_switch_key(),
        get_ev_auto_full_negative_price_switch_key(),
        get_ev_second_smart_charging_switch_key(),
        get_ev_second_force_charge_now_switch_key(),
    ):
        assert ev_key not in keys
    # Non-EV switches (e.g. read-only) are still present.
    assert get_read_only_switch_key() in keys


@pytest.mark.asyncio
async def test_switch_setup_includes_ev1_switches_when_enabled() -> None:
    """EV1 switches appear when only the primary EV's planned load is enabled."""
    from custom_components.hsem.utils.sensornames.ev import (
        get_ev_second_smart_charging_switch_key,
        get_ev_smart_charging_switch_key,
    )

    config_entry = _make_config_entry(
        hsem_ev_planned_load_enabled=True,
        hsem_ev_second_planned_load_enabled=False,
    )
    added, add_entities = _collector()
    await switch_setup_entry(MagicMock(), config_entry, add_entities)
    keys = {e.entity_description.key for e in added}
    assert get_ev_smart_charging_switch_key() in keys
    assert get_ev_second_smart_charging_switch_key() not in keys


@pytest.mark.asyncio
async def test_switch_setup_includes_all_ev_switches_when_both_enabled() -> None:
    """Both EVs' switches appear when both planned loads are enabled."""
    from custom_components.hsem.switch import SWITCH_DESCRIPTIONS

    config_entry = _make_config_entry(
        hsem_ev_planned_load_enabled=True,
        hsem_ev_second_planned_load_enabled=True,
    )
    added, add_entities = _collector()
    await switch_setup_entry(MagicMock(), config_entry, add_entities)
    assert len(added) == len(SWITCH_DESCRIPTIONS)


# ---------------------------------------------------------------------------
# number.py
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_number_setup_excludes_ev_target_soc_when_disabled() -> None:
    """EV target-SoC numbers are absent when planned load is disabled."""
    config_entry = _make_config_entry(
        hsem_ev_planned_load_enabled=False,
        hsem_ev_second_planned_load_enabled=False,
    )
    added, add_entities = _collector()
    await number_setup_entry(MagicMock(), config_entry, add_entities)
    keys = {e.entity_description.key for e in added}
    assert get_ev_target_soc_number_key() not in keys
    assert get_ev_second_target_soc_number_key() not in keys
    # Battery efficiency numbers are never gated.
    from custom_components.hsem.utils.sensornames.controls import (
        get_charge_efficiency_number_key,
    )

    assert get_charge_efficiency_number_key() in keys


@pytest.mark.asyncio
async def test_number_setup_includes_ev_target_soc_when_enabled() -> None:
    """EV target-SoC numbers appear once planned load is enabled."""
    config_entry = _make_config_entry(
        hsem_ev_planned_load_enabled=True,
        hsem_ev_second_planned_load_enabled=True,
    )
    added, add_entities = _collector()
    await number_setup_entry(MagicMock(), config_entry, add_entities)
    keys = {e.entity_description.key for e in added}
    assert get_ev_target_soc_number_key() in keys
    assert get_ev_second_target_soc_number_key() in keys


# ---------------------------------------------------------------------------
# time.py
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_time_setup_excludes_ev_deadline_when_disabled() -> None:
    """EV deadline time entities are absent when planned load is disabled."""
    config_entry = _make_config_entry(
        hsem_ev_planned_load_enabled=False,
        hsem_ev_second_planned_load_enabled=False,
    )
    added, add_entities = _collector()
    await time_setup_entry(MagicMock(), config_entry, add_entities)
    keys = {e.entity_description.key for e in added}
    assert get_ev_deadline_time_key() not in keys
    assert get_ev_second_deadline_time_key() not in keys


@pytest.mark.asyncio
async def test_time_setup_includes_ev_deadline_when_enabled() -> None:
    """EV deadline time entities appear once planned load is enabled."""
    config_entry = _make_config_entry(
        hsem_ev_planned_load_enabled=True,
        hsem_ev_second_planned_load_enabled=True,
    )
    added, add_entities = _collector()
    await time_setup_entry(MagicMock(), config_entry, add_entities)
    keys = {e.entity_description.key for e in added}
    assert get_ev_deadline_time_key() in keys
    assert get_ev_second_deadline_time_key() in keys


# ---------------------------------------------------------------------------
# sensor.py
# ---------------------------------------------------------------------------


def _make_sensor_config_entry(**options: Any) -> MagicMock:
    entry = _make_config_entry(**options)
    entry.runtime_data.coordinator = MagicMock()
    return entry


@pytest.mark.asyncio
async def test_sensor_setup_excludes_ocpp_and_ev_when_all_disabled() -> None:
    """No OCPP or EV sensors are created when none of the features are enabled."""
    config_entry = _make_sensor_config_entry(
        hsem_ocpp_enabled=False,
        hsem_ocpp_second_enabled=False,
        hsem_ev_planned_load_enabled=False,
        hsem_ev_second_planned_load_enabled=False,
    )
    added, add_entities = _collector()
    await sensor_setup_entry(MagicMock(), config_entry, add_entities)

    assert not any(isinstance(e, HSEMOCPPChargerStatusSensor) for e in added)
    assert not any(isinstance(e, HSEMEVChargingSensor) for e in added)
    assert not any(isinstance(e, HSEMEVOptimalChargingPlanSensor) for e in added)
    assert not any(isinstance(e, HSEMEVChargerCalculatedPowerSensor) for e in added)
    assert not any(isinstance(e, HSEMEVSecondOptimalChargingPlanSensor) for e in added)
    assert not any(
        isinstance(e, HSEMEVSecondChargerCalculatedPowerSensor) for e in added
    )


@pytest.mark.asyncio
async def test_sensor_setup_includes_ocpp_and_ev1_when_enabled() -> None:
    """Primary OCPP and EV1 sensors appear when those flags are enabled."""
    config_entry = _make_sensor_config_entry(
        hsem_ocpp_enabled=True,
        hsem_ocpp_second_enabled=False,
        hsem_ev_planned_load_enabled=True,
        hsem_ev_second_planned_load_enabled=False,
    )
    added, add_entities = _collector()
    await sensor_setup_entry(MagicMock(), config_entry, add_entities)

    ocpp_status_sensors = [
        e for e in added if isinstance(e, HSEMOCPPChargerStatusSensor)
    ]
    assert len(ocpp_status_sensors) == 1
    assert ocpp_status_sensors[0]._charger_index == 1
    assert any(isinstance(e, HSEMEVChargingSensor) for e in added)
    assert any(isinstance(e, HSEMEVOptimalChargingPlanSensor) for e in added)
    assert not any(isinstance(e, HSEMEVSecondOptimalChargingPlanSensor) for e in added)
