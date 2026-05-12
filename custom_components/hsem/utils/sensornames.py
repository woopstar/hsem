from homeassistant.components import sensor
from homeassistant.util import slugify as s

from custom_components.hsem.const import DOMAIN


# Integral Sensor
def get_integral_sensor_name(hour_start: int, hour_end: int) -> str:
    """Generate the display name for the integral sensor.

    Args:
        hour_start (int): Start hour of the time range.
        hour_end (int): End hour of the time range.

    Returns:
        str: Display name of the integral sensor.

    """
    return f"House Consumption {hour_start:02d}-{hour_end:02d} Energy (Integral)"


def get_integral_sensor_unique_id(hour_start: int, hour_end: int) -> str:
    """Generate a unique ID for the integral sensor.

    Args:
        hour_start (int): Start hour of the time range.
        hour_end (int): End hour of the time range.

    Returns:
        str: Unique ID of the integral sensor.

    """
    return f"{DOMAIN}_house_consumption_energy_integral_{hour_start:02d}_{hour_end:02d}"


def get_integral_sensor_entity_id(hour_start: int, hour_end: int) -> str:
    """Generate an Entity ID for the integral sensor.

    Args:
        hour_start (int): Start hour of the time range.
        hour_end (int): End hour of the time range.

    Returns:
        str: Entity ID of the integral sensor.

    """
    return sensor.ENTITY_ID_FORMAT.format(
        s(f"{DOMAIN}_house_consumption_energy_integral_{hour_start:02d}_{hour_end:02d}")
    )


# Energy Average Sensor
def get_energy_average_sensor_name(hour_start: int, hour_end: int, avg: int) -> str:
    """Generate the display name for the energy average sensor.

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
    """Generate a unique ID for the energy average sensor.

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


def get_energy_average_sensor_entity_id(
    hour_start: int, hour_end: int, avg: int
) -> str:
    """Generate an Entity ID for the energy average sensor.

    Args:
        hour_start (int): Start hour of the time range.
        hour_end (int): End hour of the time range.
        avg (int): Averaging period in days.

    Returns:
        str: Entity ID of the energy average sensor.

    """
    return sensor.ENTITY_ID_FORMAT.format(
        s(
            f"{DOMAIN}_house_consumption_energy_avg_{hour_start:02d}_{hour_end:02d}_{avg}d"
        )
    )


# Utility Meter Sensor
def get_utility_meter_sensor_name(hour_start: int, hour_end: int) -> str:
    """Generate the display name for the utility meter sensor.

    Args:
        hour_start (int): Start hour of the time range.
        hour_end (int): End hour of the time range.

    Returns:
        str: Display name of the utility meter sensor.

    """
    return f"House Consumption {hour_start:02d}-{hour_end:02d} Energy (Utility Meter)"


def get_utility_meter_sensor_unique_id(hour_start: int, hour_end: int) -> str:
    """Generate a unique ID for the utility meter sensor.

    Args:
        hour_start (int): Start hour of the time range.
        hour_end (int): End hour of the time range.

    Returns:
        str: Unique ID of the utility meter sensor.

    """
    return f"{DOMAIN}_house_consumption_energy_{hour_start:02d}_{hour_end:02d}_utility_meter"


def get_utility_meter_sensor_entity_id(hour_start: int, hour_end: int) -> str:
    """Generate a Entity ID for the utility meter sensor.

    Args:
        hour_start (int): Start hour of the time range.
        hour_end (int): End hour of the time range.

    Returns:
        str: Entity ID of the utility meter sensor.

    """
    return sensor.ENTITY_ID_FORMAT.format(
        s(
            f"{DOMAIN}_house_consumption_energy_{hour_start:02d}_{hour_end:02d}_utility_meter"
        )
    )


# House Consumption Power Sensor
def get_house_consumption_power_sensor_name(hour_start: int, hour_end: int) -> str:
    """Generate the display name for the house consumption power sensor.

    Args:
        hour_start (int): Start hour of the time range.
        hour_end (int): End hour of the time range.

    Returns:
        str: Display name of the house consumption power sensor.

    """
    return f"House Consumption {hour_start:02d}-{hour_end:02d} Hourly Power"


def get_house_consumption_power_sensor_unique_id(hour_start: int, hour_end: int) -> str:
    """Generate a unique ID for the house consumption power sensor.

    Args:
        hour_start (int): Start hour of the time range.
        hour_end (int): End hour of the time range.

    Returns:
        str: Unique ID of the house consumption power sensor.

    """
    return f"{DOMAIN}_house_consumption_power_{hour_start:02d}_{hour_end:02d}"


def get_house_consumption_power_sensor_entity_id(hour_start: int, hour_end: int) -> str:
    """Generate a Entity ID for the house consumption power sensor.

    Args:
        hour_start (int): Start hour of the time range.
        hour_end (int): End hour of the time range.

    Returns:
        str: Entity ID of the house consumption power sensor.

    """
    return sensor.ENTITY_ID_FORMAT.format(
        s(f"{DOMAIN}_house_consumption_power_{hour_start:02d}_{hour_end:02d}")
    )


# Working Mode Sensor
def get_working_mode_sensor_name() -> str:
    """Generate the display name for the working mode sensor.

    Returns:
        str: Display name of the working mode sensor.

    """
    return "Working Mode Sensor"


def get_working_mode_sensor_unique_id() -> str:
    """Generate a unique ID for the working mode sensor.

    Returns:
        str: Unique ID of the working mode sensor.

    """
    return f"{DOMAIN}_workingmode_sensor"


def get_working_mode_sensor_entity_id() -> str:
    """Generate a Entity ID for the working mode sensor.

    Returns:
        str: Entity ID of the working mode sensor.

    """
    return sensor.ENTITY_ID_FORMAT.format(s(f"{DOMAIN}_workingmode_sensor"))


# Degraded Mode Sensor
def get_degraded_mode_sensor_name() -> str:
    """Return the display name for the degraded-mode diagnostic sensor.

    Returns:
        str: Display name.

    """
    return "System Health"


def get_degraded_mode_sensor_unique_id() -> str:
    """Return a unique ID for the degraded-mode sensor.

    Returns:
        str: Unique ID.

    """
    return f"{DOMAIN}_degraded_mode_sensor"


def get_degraded_mode_sensor_entity_id() -> str:
    """Return the entity_id for the degraded-mode sensor.

    Returns:
        str: Entity ID.

    """
    return sensor.ENTITY_ID_FORMAT.format(s(f"{DOMAIN}_degraded_mode_sensor"))


# Read-Only Mode Sensor
def get_read_only_sensor_name() -> str:
    """Return the display name for the read-only mode diagnostic sensor.

    Returns:
        str: Display name.

    """
    return "Read-Only Mode"


def get_read_only_sensor_unique_id() -> str:
    """Return a unique ID for the read-only mode sensor.

    Returns:
        str: Unique ID.

    """
    return f"{DOMAIN}_read_only_sensor"


def get_read_only_sensor_entity_id() -> str:
    """Return the entity_id for the read-only mode sensor.

    Returns:
        str: Entity ID.

    """
    return sensor.ENTITY_ID_FORMAT.format(s(f"{DOMAIN}_read_only_sensor"))


# Next Update Sensor
def get_next_update_sensor_name() -> str:
    """Return the display name for the next-update diagnostic sensor."""
    return "Next Update"


def get_next_update_sensor_unique_id() -> str:
    """Return a unique ID for the next-update sensor."""
    return f"{DOMAIN}_next_update_sensor"


def get_next_update_sensor_entity_id() -> str:
    """Return the entity_id for the next-update sensor."""
    return sensor.ENTITY_ID_FORMAT.format(s(f"{DOMAIN}_next_update_sensor"))


# Missing Entities Sensor
def get_missing_entities_sensor_name() -> str:
    """Return the display name for the missing-entities count diagnostic sensor."""
    return "Missing Input Entities"


def get_missing_entities_sensor_unique_id() -> str:
    """Return a unique ID for the missing-entities sensor."""
    return f"{DOMAIN}_missing_entities_sensor"


def get_missing_entities_sensor_entity_id() -> str:
    """Return the entity_id for the missing-entities sensor."""
    return sensor.ENTITY_ID_FORMAT.format(s(f"{DOMAIN}_missing_entities_sensor"))


# Hardware Writes Blocked Sensor
def get_hardware_writes_sensor_name() -> str:
    """Return the display name for the hardware-writes-blocked diagnostic sensor."""
    return "Hardware Writes"


def get_hardware_writes_sensor_unique_id() -> str:
    """Return a unique ID for the hardware-writes-blocked sensor."""
    return f"{DOMAIN}_hardware_writes_sensor"


def get_hardware_writes_sensor_entity_id() -> str:
    """Return the entity_id for the hardware-writes-blocked sensor."""
    return sensor.ENTITY_ID_FORMAT.format(s(f"{DOMAIN}_hardware_writes_sensor"))


# Net Consumption Sensor
def get_net_consumption_sensor_name() -> str:
    """Return the display name for the net-consumption diagnostic sensor."""
    return "Net Consumption"


def get_net_consumption_sensor_unique_id() -> str:
    """Return a unique ID for the net-consumption sensor."""
    return f"{DOMAIN}_net_consumption_sensor"


def get_net_consumption_sensor_entity_id() -> str:
    """Return the entity_id for the net-consumption sensor."""
    return sensor.ENTITY_ID_FORMAT.format(s(f"{DOMAIN}_net_consumption_sensor"))


# Force Working Mode Sensor
def get_force_mode_sensor_name() -> str:
    """Return the display name for the force-working-mode diagnostic sensor."""
    return "Force Working Mode"


def get_force_mode_sensor_unique_id() -> str:
    """Return a unique ID for the force-working-mode sensor."""
    return f"{DOMAIN}_force_mode_sensor"


def get_force_mode_sensor_entity_id() -> str:
    """Return the entity_id for the force-working-mode sensor."""
    return sensor.ENTITY_ID_FORMAT.format(s(f"{DOMAIN}_force_mode_sensor"))
