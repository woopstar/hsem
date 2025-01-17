"""Constants for HSEM tests."""

from custom_components.hsem.const import DOMAIN

# Mock config data
MOCK_CONFIG = {
    "host": "192.168.1.100",
    "username": "test_user",
    "password": "test_password",
}

# Mock entity IDs
MOCK_ENTITY_ID = f"sensor.{DOMAIN}_working_mode"

# Mock device info
MOCK_DEVICE_INFO = {
    "identifiers": {(DOMAIN, "test_unique_id")},
    "name": "Huawei Solar Energy Management",
    "manufacturer": "Huawei",
    "model": "SUN2000",
    "sw_version": "1.0.0",
}

# Mock battery data
MOCK_BATTERY_DATA = {
    "rated_capacity": 10.0,
    "max_charging_power": 5000,
    "grid_charge_cutoff_soc": 85,
    "remaining_charge": 5.0,
    "conversion_loss": 5,
    "current_soc": 50,
}

# Mock hourly calculations
MOCK_HOURLY_CALCULATIONS = {
    "14-15": {
        "estimated_net_consumption": -2.0,
        "import_price": 0.15,
        "solar_forecast": 3,
        "recommendation": None,
    },
    "15-16": {
        "estimated_net_consumption": -2.5,
        "import_price": 0.10,
        "solar_forecast": 3.5,
        "recommendation": None,
    },
    "16-17": {
        "estimated_net_consumption": -2.0,
        "import_price": 0.08,
        "solar_forecast": 3,
        "recommendation": None,
    },
}
