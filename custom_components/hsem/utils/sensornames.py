from custom_components.hsem.const import DOMAIN


# Integral Sensor
def get_integral_sensor_name(hour_start: int, hour_end: int) -> str:
    """
    Generate the display name for the integral sensor.

    Args:
        hour_start (int): Start hour of the time range.
        hour_end (int): End hour of the time range.

    Returns:
        str: Display name of the integral sensor.
    """
    return f"House Consumption {hour_start:02d}-{hour_end:02d} Energy (Integral)"


def get_integral_sensor_unique_id(hour_start: int, hour_end: int) -> str:
    """
    Generate a unique ID for the integral sensor.

    Args:
        hour_start (int): Start hour of the time range.
        hour_end (int): End hour of the time range.

    Returns:
        str: Unique ID of the integral sensor.
    """
    return f"{DOMAIN}_house_consumption_energy_integral_{hour_start:02d}_{hour_end:02d}"


# Energy Average Sensor
def get_energy_average_sensor_name(hour_start: int, hour_end: int, avg: int) -> str:
    """
    Generate the display name for the energy average sensor.

    Args:
        hour_start (int): Start hour of the time range.
        hour_end (int): End hour of the time range.
        avg (int): Averaging period in days.

    Returns:
        str: Display name of the energy average sensor.
    """
    return f"House Consumption {hour_start:02d}-{hour_end:02d} Energy Average {avg}d"


def get_energy_average_sensor_unique_id(
    hour_start: int, hour_end: int, avg: int
) -> str:
    """
    Generate a unique ID for the energy average sensor.

    Args:
        hour_start (int): Start hour of the time range.
        hour_end (int): End hour of the time range.
        avg (int): Averaging period in days.

    Returns:
        str: Unique ID of the energy average sensor.
    """
    return (
        f"{DOMAIN}_house_consumption_energy_avg_{hour_start:02d}_{hour_end:02d}_{avg}d"
    )


# Utility Meter Sensor
def get_utility_meter_sensor_name(hour_start: int, hour_end: int) -> str:
    """
    Generate the display name for the utility meter sensor.

    Args:
        hour_start (int): Start hour of the time range.
        hour_end (int): End hour of the time range.

    Returns:
        str: Display name of the utility meter sensor.
    """
    return f"House Consumption {hour_start:02d}-{hour_end:02d} Energy (Utility Meter)"


def get_utility_meter_sensor_unique_id(hour_start: int, hour_end: int) -> str:
    """
    Generate a unique ID for the utility meter sensor.

    Args:
        hour_start (int): Start hour of the time range.
        hour_end (int): End hour of the time range.

    Returns:
        str: Unique ID of the utility meter sensor.
    """
    return f"{DOMAIN}_house_consumption_energy_{hour_start:02d}_{hour_end:02d}_utility_meter"


# House Consumption Power Sensor
def get_house_consumption_power_sensor_name(hour_start: int, hour_end: int) -> str:
    """
    Generate the display name for the house consumption power sensor.

    Args:
        hour_start (int): Start hour of the time range.
        hour_end (int): End hour of the time range.

    Returns:
        str: Display name of the house consumption power sensor.
    """
    return f"House Consumption {hour_start:02d}-{hour_end:02d} Hourly Power"


def get_house_consumption_power_sensor_unique_id(hour_start: int, hour_end: int) -> str:
    """
    Generate a unique ID for the house consumption power sensor.

    Args:
        hour_start (int): Start hour of the time range.
        hour_end (int): End hour of the time range.

    Returns:
        str: Unique ID of the house consumption power sensor.
    """
    return f"{DOMAIN}_house_consumption_power_{hour_start:02d}_{hour_end:02d}"


# Working Mode Sensor
def get_working_mode_sensor_name() -> str:
    """
    Generate the display name for the working mode sensor.

    Returns:
        str: Display name of the working mode sensor.
    """
    return "Working Mode Sensor"


def get_working_mode_sensor_unique_id() -> str:
    """
    Generate a unique ID for the working mode sensor.

    Returns:
        str: Unique ID of the working mode sensor.
    """
    return f"{DOMAIN}_workingmode_sensor"
