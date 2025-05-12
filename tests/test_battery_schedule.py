from datetime import time

import pytest

from custom_components.hsem.custom_sensors.working_mode_sensor import (
    HSEMWorkingModeSensor,
)
from custom_components.hsem.models.battery_schedule import BatterySchedule


@pytest.fixture
def mock_config_entry():
    """Mock configuration entry for testing."""
    return {
        "hsem_batteries_enable_batteries_schedule_1": True,
        "hsem_batteries_enable_batteries_schedule_1_start": "06:00:00",
        "hsem_batteries_enable_batteries_schedule_1_end": "10:00:00",
        "hsem_batteries_enable_batteries_schedule_1_avg_import_price": 0.15,
        "hsem_batteries_enable_batteries_schedule_1_needed_batteries_capacity": 5.0,
        "hsem_batteries_enable_batteries_schedule_1_needed_batteries_capacity_cost": 0.75,
        "hsem_batteries_enable_batteries_schedule_1_min_price_difference": 0.05,
        "hsem_batteries_enable_batteries_schedule_2": True,
        "hsem_batteries_enable_batteries_schedule_2_start": "12:00:00",
        "hsem_batteries_enable_batteries_schedule_2_end": "16:00:00",
        "hsem_batteries_enable_batteries_schedule_2_avg_import_price": 0.20,
        "hsem_batteries_enable_batteries_schedule_2_needed_batteries_capacity": 8.0,
        "hsem_batteries_enable_batteries_schedule_2_needed_batteries_capacity_cost": 1.60,
        "hsem_batteries_enable_batteries_schedule_2_min_price_difference": 0.10,
        "hsem_batteries_enable_batteries_schedule_3": False,
    }


def test_battery_schedule_model():
    """
    Test the BatterySchedule model to ensure it initializes correctly
    and handles its attributes as expected.
    """
    schedule = BatterySchedule(
        enabled=True,
        start=time(6, 0),
        end=time(10, 0),
        avg_import_price=0.15,
        needed_batteries_capacity=5.0,
        needed_batteries_capacity_cost=0.75,
        min_price_difference=0.05,
    )

    assert schedule.enabled is True
    assert schedule.start == time(6, 0)
    assert schedule.end == time(10, 0)
    assert schedule.avg_import_price == 0.15
    assert schedule.needed_batteries_capacity == 5.0
    assert schedule.needed_batteries_capacity_cost == 0.75
    assert schedule.min_price_difference == 0.05


def test_hsem_working_mode_sensor_battery_schedules(mock_config_entry):
    """
    Test the HSEMWorkingModeSensor to ensure it correctly initializes
    and updates battery schedules based on the configuration entry.
    """
    sensor = HSEMWorkingModeSensor(mock_config_entry)

    # Validate the first schedule
    schedule_1 = sensor._battery_schedules[0]
    assert schedule_1.enabled is True
    assert schedule_1.start == time(6, 0)
    assert schedule_1.end == time(10, 0)
    assert schedule_1.avg_import_price == 0.15
    assert schedule_1.needed_batteries_capacity == 5.0
    assert schedule_1.needed_batteries_capacity_cost == 0.75
    assert schedule_1.min_price_difference == 0.05

    # Validate the second schedule
    schedule_2 = sensor._battery_schedules[1]
    assert schedule_2.enabled is True
    assert schedule_2.start == time(12, 0)
    assert schedule_2.end == time(16, 0)
    assert schedule_2.avg_import_price == 0.20
    assert schedule_2.needed_batteries_capacity == 8.0
    assert schedule_2.needed_batteries_capacity_cost == 1.60
    assert schedule_2.min_price_difference == 0.10

    # Validate the third schedule (disabled by default)
    schedule_3 = sensor._battery_schedules[2]
    assert schedule_3.enabled is False
    assert schedule_3.start == time.min
    assert schedule_3.end == time.max
    assert schedule_3.avg_import_price == 0.0
    assert schedule_3.needed_batteries_capacity == 0.0
    assert schedule_3.needed_batteries_capacity_cost == 0.0
    assert schedule_3.min_price_difference == 0.0
