from custom_components.hsem.const import DOMAIN


# Integral sensor
def get_integral_sensor_name(hour_start, hour_end):
    return f"House Consumption {hour_start:02d}-{hour_end:02d} Energy (Integral)"


def get_integral_sensor_unique_id(hour_start, hour_end):
    return f"{DOMAIN}_house_consumption_energy_integral_{hour_start:02d}_{hour_end:02d}"


# Energy Average sensor
def get_energy_average_sensor_name(hour_start, hour_end, avg):
    return f"House Consumption {hour_start:02d}-{hour_end:02d} Energy Average {avg}d"


def get_energy_average_sensor_unique_id(hour_start, hour_end, avg):
    return (
        f"{DOMAIN}_house_consumption_energy_avg_{hour_start:02d}_{hour_end:02d}_{avg}d"
    )


# Utility Meter sensor
def get_utility_meter_sensor_name(hour_start, hour_end):
    return f"House Consumption {hour_start:02d}-{hour_end:02d} Energy (Utility Meter)"


def get_utility_meter_sensor_unique_id(hour_start, hour_end):
    return f"{DOMAIN}_house_consumption_energy_{hour_start:02d}_{hour_end:02d}_utility_meter"


# House Consumption Power sensor
def get_house_consumption_power_sensor_name(hour_start, hour_end):
    return f"House Consumption {hour_start:02d}-{hour_end:02d} Hourly Power"


def get_house_consumption_power_sensor_unique_id(hour_start, hour_end):
    return f"{DOMAIN}_house_consumption_power_{hour_start:02d}_{hour_end:02d}"


# Working Mode sensor
def get_working_mode_sensor_name():
    return f"Working Mode Sensor"


def get_working_mode_sensor_unique_id():
    return f"{DOMAIN}_workingmode_sensor"
