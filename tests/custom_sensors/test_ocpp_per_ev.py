"""Tests for per-EV OCPP server configuration and sensors (issue #782).

Covers:
- Sensor name/unique-id/entity-id helpers for charger_index 1 and 2
- Second-server config fields in the OCPP flow schema (only with second EV)
- Port-conflict validation between the two OCPP servers
- Config reader defaults for the second server
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from homeassistant.exceptions import HomeAssistantError  # noqa: F401

from custom_components.hsem.custom_sensors.config_reader import build_sensor_config
from custom_components.hsem.flows.ocpp import (
    get_ocpp_step_schema,
    validate_ocpp_step_input,
)
from custom_components.hsem.utils.sensornames.ocpp import (
    get_ocpp_charger_power_sensor_entity_id,
    get_ocpp_charger_power_sensor_name,
    get_ocpp_charger_power_sensor_unique_id,
    get_ocpp_charger_status_sensor_entity_id,
    get_ocpp_charger_status_sensor_unique_id,
)

# ---------------------------------------------------------------------------
# Sensor name helpers
# ---------------------------------------------------------------------------


def test_primary_sensor_names_unchanged():
    """charger_index=1 keeps the original entity IDs and unique IDs."""
    assert (
        get_ocpp_charger_status_sensor_entity_id()
        == "sensor.hsem_ocpp_charger_status_sensor"
    )
    assert get_ocpp_charger_status_sensor_unique_id("entry") == (
        "hsem_entry_ocpp_charger_status_sensor"
    )


def test_second_sensor_names_are_distinct():
    """charger_index=2 produces distinct, slugified second-server entities.

    The display name itself is identical to the primary sensor's — the EV
    Primary / EV Secondary device (issue #875) disambiguates them instead of
    a "Second"/"2" name marker.
    """
    entity_id = get_ocpp_charger_power_sensor_entity_id(charger_index=2)
    unique_id = get_ocpp_charger_power_sensor_unique_id("entry", charger_index=2)
    name = get_ocpp_charger_power_sensor_name()

    assert entity_id == "sensor.hsem_ocpp_charger_power_sensor_second"
    assert unique_id == "hsem_entry_ocpp_charger_power_sensor_second"
    assert name == "Charger Power"
    assert entity_id != get_ocpp_charger_power_sensor_entity_id()


# ---------------------------------------------------------------------------
# Flow schema gating on second EV
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schema_without_second_ev_has_no_second_fields():
    """The second OCPP fields are absent when the second EV is disabled."""
    schema = await get_ocpp_step_schema(None, second_ev_enabled=False)
    keys = str(schema)
    assert "hsem_ocpp_second_enabled" not in keys
    assert "hsem_ocpp_port" in keys


@pytest.mark.asyncio
async def test_schema_with_second_ev_includes_second_fields():
    """The second OCPP fields appear when the second EV is enabled."""
    schema = await get_ocpp_step_schema(None, second_ev_enabled=True)
    keys = str(schema)
    assert "hsem_ocpp_second_enabled" in keys
    assert "hsem_ocpp_second_port" in keys
    assert "hsem_ocpp_second_cpid" in keys


@pytest.mark.asyncio
async def test_validation_rejects_conflicting_ports():
    """Second-server port must differ from the primary port."""
    errors = await validate_ocpp_step_input(
        MagicMock(),
        {
            "hsem_ocpp_enabled": True,
            "hsem_ocpp_port": 9000,
            "hsem_ocpp_start_window_s": 60,
            "hsem_ocpp_stop_window_s": 180,
            "hsem_ocpp_second_enabled": True,
            "hsem_ocpp_second_port": 9000,
        },
    )
    assert errors.get("hsem_ocpp_second_port") == "port_conflict"


@pytest.mark.asyncio
async def test_validation_accepts_distinct_ports():
    """Distinct ports validate cleanly."""
    errors = await validate_ocpp_step_input(
        MagicMock(),
        {
            "hsem_ocpp_enabled": True,
            "hsem_ocpp_port": 9000,
            "hsem_ocpp_start_window_s": 60,
            "hsem_ocpp_stop_window_s": 180,
            "hsem_ocpp_second_enabled": True,
            "hsem_ocpp_second_port": 9001,
        },
    )
    assert errors == {}


# ---------------------------------------------------------------------------
# Config reader defaults
# ---------------------------------------------------------------------------


def test_config_reader_second_server_defaults():
    """Second-server config falls back to disabled / port 9001 / empty CPID."""
    entry = MagicMock()
    entry.options = {}
    entry.data = {}
    cfg = build_sensor_config(entry)
    assert cfg.ocpp_second_enabled is False
    assert cfg.ocpp_second_port == 9001
    assert cfg.ocpp_second_cpid == ""
