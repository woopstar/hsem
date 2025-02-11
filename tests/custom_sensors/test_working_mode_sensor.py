"""Tests for the HSEMWorkingModeSensor."""

import logging
from datetime import datetime
from unittest.mock import patch

import pytest

from custom_components.hsem.custom_sensors.working_mode_sensor import (
    HSEMWorkingModeSensor,
)

_LOGGER = logging.getLogger(__name__)


def setup_test_scenario(sensor, battery_config, hourly_data):
    """Set up a test scenario with given configuration."""
    # Set battery configuration
    sensor._hsem_batteries_rated_capacity_max_state = battery_config["rated_capacity"]
    sensor._hsem_huawei_solar_batteries_maximum_charging_power_state = battery_config[
        "max_charging_power"
    ]
    sensor._hsem_huawei_solar_batteries_grid_charge_cutoff_soc_state = battery_config[
        "grid_charge_cutoff_soc"
    ]
    sensor._hsem_batteries_remaining_charge = battery_config["remaining_charge"]
    sensor._hsem_batteries_conversion_loss = battery_config["conversion_loss"]
    sensor._hsem_huawei_solar_batteries_state_of_capacity_state = battery_config[
        "current_soc"
    ]

    # Set hourly calculations
    sensor._hourly_calculations = hourly_data


def verify_charging_results(
    hourly_calculations,
    expected=None,
    expected_total=None,
    expected_grid=None,
    expected_solar=None,
):
    """Helper function to verify charging results with flexible argument handling."""
    total_charged = 0
    solar_charged = 0
    grid_charged = 0
    charging_hours = []

    # Handle both dictionary and individual arguments
    if expected is None and expected_total is not None:
        expected = {
            "total_charged": expected_total,
            "grid_charged": expected_grid,
            "solar_charged": expected_solar,
        }

    print("\n=== Detailed Charging Analysis ===")
    print("Expected Results:", expected)

    # Analyze each charging hour
    print("\nHourly Analysis:")
    for time_range, data in sorted(hourly_calculations.items()):
        if data.get("batteries_charged", 0) > 0:
            amount = data["batteries_charged"]
            recommendation = data.get("recommendation")
            net_consumption = data.get("estimated_net_consumption")
            import_price = data.get("import_price")

            total_charged += amount

            print(f"\nHour {time_range}:")
            print(f"  Amount Charged: {amount:.2f} kWh")
            print(f"  Recommendation: {recommendation}")
            if import_price is not None:
                print(f"  Import Price: {import_price:.4f}")
            if net_consumption is not None:
                print(f"  Net Consumption: {net_consumption:.2f} kWh")

            # Determine charging type
            is_solar_charging = (
                isinstance(recommendation, str) and "solar" in recommendation.lower()
            ) or (net_consumption is not None and net_consumption < 0)

            if is_solar_charging:
                solar_charged += amount
                print("  Type: Solar Charging")
            else:
                grid_charged += amount
                print("  Type: Grid Charging")

            charging_hours.append((time_range, data))

    # Print summary
    print("\n=== Summary ===")
    print(f"Total Charged: {total_charged:.2f} kWh")
    print(f"Solar Charged: {solar_charged:.2f} kWh")
    print(f"Grid Charged: {grid_charged:.2f} kWh")

    # Assert with tolerance
    tolerance = 0.1  # 10% tolerance
    min_tolerance = 0.1  # Minimum tolerance for small values

    def check_value(actual, expected, label):
        if expected == 0:
            assert actual == 0, f"{label}: Expected 0 but got {actual:.2f}"
        else:
            tolerance_value = max(expected * tolerance, min_tolerance)
            deviation = abs(actual - expected)
            assert deviation <= tolerance_value, (
                f"{label}: Expected {expected:.2f}, got {actual:.2f} "
                f"(deviation: {deviation:.2f}, tolerance: ±{tolerance_value:.2f})"
            )

    try:
        check_value(total_charged, expected["total_charged"], "Total charged")
        check_value(solar_charged, expected["solar_charged"], "Solar charged")
        check_value(grid_charged, expected["grid_charged"], "Grid charged")
    except AssertionError as e:
        print("\nDetailed Hourly Breakdown on Test Failure:")
        for time_range, data in sorted(hourly_calculations.items()):
            if data.get("batteries_charged", 0) > 0:
                print(f"\nHour {time_range}:")
                for key, value in data.items():
                    print(f"  {key}: {value}")
        raise

    return total_charged, grid_charged, solar_charged


@pytest.mark.asyncio
async def test_negative_price_charging(mock_coordinator):
    """Test charging behavior during negative price periods."""
    sensor = HSEMWorkingModeSensor(mock_coordinator)

    battery_config = {
        "rated_capacity": 10.0,
        "max_charging_power": 5000,
        "grid_charge_cutoff_soc": 85,
        "remaining_charge": 6.0,
        "conversion_loss": 5,
        "current_soc": 40,
    }

    hourly_data = {
        "14-15": {
            "estimated_net_consumption": 1.0,
            "import_price": -0.05,
            "solar_forecast": 0,
            "recommendation": None,
        },
        "15-16": {
            "estimated_net_consumption": 0.8,
            "import_price": -0.03,
            "solar_forecast": 0,
            "recommendation": None,
        },
        "16-17": {
            "estimated_net_consumption": 1.2,
            "import_price": -0.02,
            "solar_forecast": 0,
            "recommendation": None,
        },
    }

    setup_test_scenario(sensor, battery_config, hourly_data)

    with patch(
        "custom_components.hsem.custom_sensors.working_mode_sensor.datetime"
    ) as mock_datetime:
        mock_datetime.now.return_value = datetime(2024, 1, 1, 13, 0)

        await sensor._async_find_best_time_to_charge(start_hour=14, stop_hour=17)

        total, grid, solar = verify_charging_results(
            sensor._hourly_calculations,
            expected_total=6.0,
            expected_grid=6.0,
            expected_solar=0.0,
        )


@pytest.mark.asyncio
async def test_solar_surplus_charging(mock_coordinator):
    """Test charging behavior with solar surplus."""
    sensor = HSEMWorkingModeSensor(mock_coordinator)

    battery_config = {
        "rated_capacity": 10.0,
        "max_charging_power": 5000,
        "grid_charge_cutoff_soc": 85,
        "remaining_charge": 4.0,
        "conversion_loss": 5,
        "current_soc": 60,
    }

    hourly_data = {
        "14-15": {
            "estimated_net_consumption": -2.0,
            "import_price": 0.15,
            "solar_forecast": 3.0,
            "recommendation": None,
        },
        "15-16": {
            "estimated_net_consumption": -2.5,
            "import_price": 0.10,
            "solar_forecast": 3.5,
            "recommendation": None,
        },
        "16-17": {
            "estimated_net_consumption": -1.5,
            "import_price": 0.12,
            "solar_forecast": 2.5,
            "recommendation": None,
        },
    }

    setup_test_scenario(sensor, battery_config, hourly_data)

    with patch(
        "custom_components.hsem.custom_sensors.working_mode_sensor.datetime"
    ) as mock_datetime:
        mock_datetime.now.return_value = datetime(2024, 1, 1, 13, 0)

        await sensor._async_find_best_time_to_charge(start_hour=14, stop_hour=17)

        total, grid, solar = verify_charging_results(
            sensor._hourly_calculations,
            expected_total=4.0,
            expected_grid=0.0,
            expected_solar=4.0,
        )


@pytest.mark.asyncio
async def test_mixed_charging_scenario(mock_coordinator):
    """Test charging with both solar surplus and negative prices."""
    sensor = HSEMWorkingModeSensor(mock_coordinator)

    battery_config = {
        "rated_capacity": 10.0,
        "max_charging_power": 5000,
        "grid_charge_cutoff_soc": 85,
        "remaining_charge": 5.0,
        "conversion_loss": 5,
        "current_soc": 50,
    }

    hourly_data = {
        "14-15": {
            "estimated_net_consumption": 1.0,
            "import_price": -0.05,
            "solar_forecast": 0,
            "recommendation": None,
        },
        "15-16": {
            "estimated_net_consumption": -2.0,
            "import_price": 0.15,
            "solar_forecast": 3.0,
            "recommendation": None,
        },
        "16-17": {
            "estimated_net_consumption": -1.5,
            "import_price": 0.12,
            "solar_forecast": 2.0,
            "recommendation": None,
        },
    }

    setup_test_scenario(sensor, battery_config, hourly_data)

    with patch(
        "custom_components.hsem.custom_sensors.working_mode_sensor.datetime"
    ) as mock_datetime:
        mock_datetime.now.return_value = datetime(2024, 1, 1, 13, 0)

        await sensor._async_find_best_time_to_charge(start_hour=14, stop_hour=17)

        total, grid, solar = verify_charging_results(
            sensor._hourly_calculations,
            expected_total=5.0,
            expected_grid=1.5,
            expected_solar=3.5,
        )


@pytest.mark.asyncio
async def test_ac_cutoff_limit(mock_coordinator):
    """Test charging respects AC cutoff limits."""
    sensor = HSEMWorkingModeSensor(mock_coordinator)

    battery_config = {
        "rated_capacity": 10.0,
        "max_charging_power": 5000,
        "grid_charge_cutoff_soc": 55,  # Low cutoff to test limit
        "remaining_charge": 5.0,
        "conversion_loss": 5,
        "current_soc": 50,
    }

    hourly_data = {
        "14-15": {
            "estimated_net_consumption": 1.0,
            "import_price": -0.05,
            "solar_forecast": 0,
            "recommendation": None,
        },
        "15-16": {
            "estimated_net_consumption": -2.0,
            "import_price": 0.15,
            "solar_forecast": 3.0,
            "recommendation": None,
        },
    }

    setup_test_scenario(sensor, battery_config, hourly_data)

    with patch(
        "custom_components.hsem.custom_sensors.working_mode_sensor.datetime"
    ) as mock_datetime:
        mock_datetime.now.return_value = datetime(2024, 1, 1, 13, 0)

        await sensor._async_find_best_time_to_charge(start_hour=14, stop_hour=17)

        total, grid, solar = verify_charging_results(
            sensor._hourly_calculations,
            expected_total=0.5,  # Limited by AC cutoff
            expected_grid=0.5,
            expected_solar=0.0,
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    pytest.main([__file__, "-v"])
