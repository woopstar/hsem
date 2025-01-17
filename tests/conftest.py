"""Fixtures for HSEM integration tests."""

from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component


@pytest.fixture
def hass(event_loop, tmp_path):
    """Fixture to provide a test instance of Home Assistant."""
    hass = HomeAssistant()
    hass.config.config_dir = tmp_path

    async def async_setup():
        await hass.async_start()
        await async_setup_component(hass, "logger", {"logger": {"default": "debug"}})

    event_loop.run_until_complete(async_setup())

    yield hass

    event_loop.run_until_complete(hass.async_stop())


@pytest.fixture
def mock_working_mode_coordinator():
    """Fixture to provide a mocked coordinator."""
    coordinator = MagicMock()
    coordinator.data = {}
    return coordinator


@pytest.fixture
def mock_config_entry():
    """Fixture to provide a mocked config entry."""
    return MagicMock(
        data={
            "host": "192.168.1.100",
            "username": "test_user",
            "password": "test_password",
        },
        unique_id="test_unique_id",
    )


@pytest.fixture
def mock_battery_data():
    """Fixture to provide mock battery data."""
    return {
        "rated_capacity": 10.0,
        "max_charging_power": 5000,
        "grid_charge_cutoff_soc": 85,
        "remaining_charge": 5.0,
        "conversion_loss": 5,
        "current_soc": 50,
    }
