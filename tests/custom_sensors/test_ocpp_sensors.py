"""Tests for the OCPP charger status sensor's config-awareness (issue #858).

Covers:
- ``state`` reports ``not_configured`` (not the misleading ``disconnected``)
  when this EV's OCPP server isn't enabled in config.
- ``state`` still reports ``disconnected`` when enabled but idle, and the
  live charger status when a charger is connected.
- ``extra_state_attributes`` exposes ``listening``/``port``/``url`` only
  when this EV's OCPP server is enabled.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.hsem.coordinator_data import CoordinatorData
from custom_components.hsem.custom_sensors.ocpp_sensors import (
    HSEMOCPPChargerStatusSensor,
)
from custom_components.hsem.models.ocpp_session import ChargerSession
from custom_components.hsem.models.sensor_config import SensorConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config_entry() -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    return entry


def _make_coordinator(data: CoordinatorData | None) -> MagicMock:
    coordinator = MagicMock()
    coordinator.data = data
    coordinator.last_update_success = data is not None
    return coordinator


def _make_hass_without_url() -> MagicMock:
    """A hass mock where ``get_url`` cleanly raises ``NoURLAvailableError``."""
    hass = MagicMock()
    hass.config.api = None
    hass.config.internal_url = None
    hass.config.external_url = None
    return hass


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------


def test_state_not_configured_when_ocpp_disabled() -> None:
    """Primary sensor reports not_configured, not disconnected, when off."""
    data = CoordinatorData(cfg=SensorConfig(ocpp_enabled=False))
    sensor = HSEMOCPPChargerStatusSensor(_make_config_entry(), _make_coordinator(data))
    assert sensor.state == "not_configured"


def test_state_disconnected_when_enabled_but_idle() -> None:
    """Enabled server with no connected charger still reports disconnected."""
    data = CoordinatorData(cfg=SensorConfig(ocpp_enabled=True), ocpp_chargers={})
    sensor = HSEMOCPPChargerStatusSensor(_make_config_entry(), _make_coordinator(data))
    assert sensor.state == "disconnected"


def test_state_reflects_connected_charger() -> None:
    """Enabled server with a connected charger reports its live status."""
    data = CoordinatorData(
        cfg=SensorConfig(ocpp_enabled=True),
        ocpp_chargers={"CP1": ChargerSession(cpid="CP1", status="Charging")},
    )
    sensor = HSEMOCPPChargerStatusSensor(_make_config_entry(), _make_coordinator(data))
    assert sensor.state == "Charging"


def test_second_charger_state_uses_second_flag() -> None:
    """charger_index=2 checks ocpp_second_enabled, not the primary flag."""
    data = CoordinatorData(
        cfg=SensorConfig(ocpp_enabled=True, ocpp_second_enabled=False)
    )
    sensor = HSEMOCPPChargerStatusSensor(
        _make_config_entry(), _make_coordinator(data), charger_index=2
    )
    assert sensor.state == "not_configured"


# ---------------------------------------------------------------------------
# extra_state_attributes
# ---------------------------------------------------------------------------


def test_attributes_empty_when_not_configured() -> None:
    """No diagnostic attributes are exposed when OCPP isn't enabled."""
    data = CoordinatorData(cfg=SensorConfig(ocpp_enabled=False))
    sensor = HSEMOCPPChargerStatusSensor(_make_config_entry(), _make_coordinator(data))
    sensor.hass = _make_hass_without_url()
    assert sensor.extra_state_attributes == {}


def test_attributes_include_listening_and_port_when_enabled() -> None:
    """listening/port are always present when this EV's server is enabled."""
    data = CoordinatorData(
        cfg=SensorConfig(ocpp_enabled=True, ocpp_port=9000),
        ocpp_listening=True,
    )
    sensor = HSEMOCPPChargerStatusSensor(_make_config_entry(), _make_coordinator(data))
    sensor.hass = _make_hass_without_url()
    attrs = sensor.extra_state_attributes
    assert attrs["listening"] is True
    assert attrs["port"] == 9000
    assert "url" not in attrs  # no resolvable HA URL in this mock hass


def test_attributes_include_per_charger_session_details() -> None:
    """Connected charger details are merged alongside server diagnostics."""
    data = CoordinatorData(
        cfg=SensorConfig(ocpp_enabled=True, ocpp_port=9000),
        ocpp_listening=True,
        ocpp_chargers={
            "CP1": ChargerSession(cpid="CP1", status="Charging", current_power_w=7400.0)
        },
    )
    sensor = HSEMOCPPChargerStatusSensor(_make_config_entry(), _make_coordinator(data))
    sensor.hass = _make_hass_without_url()
    attrs = sensor.extra_state_attributes
    assert attrs["CP1"]["status"] == "Charging"
    assert attrs["CP1"]["power_w"] == 7400.0
