"""Sensor name, unique ID, and entity ID generators for HSEM entities.

Each entity type has a family of getter functions that return the
configuration key, display name, unique ID, and Home Assistant entity ID
for use in config flow, entity registration, and state collection.
"""

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
    return f"sensor.{s(f'{DOMAIN}_house_consumption_energy_integral_{hour_start:02d}_{hour_end:02d}')}"


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
    return f"sensor.{s(f'{DOMAIN}_house_consumption_energy_avg_{hour_start:02d}_{hour_end:02d}_{avg}d')}"


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
    return f"sensor.{s(f'{DOMAIN}_house_consumption_energy_{hour_start:02d}_{hour_end:02d}_utility_meter')}"


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
    return f"sensor.{s(f'{DOMAIN}_house_consumption_power_{hour_start:02d}_{hour_end:02d}')}"


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
    return f"sensor.{s(f'{DOMAIN}_workingmode_sensor')}"


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
    return f"sensor.{s(f'{DOMAIN}_degraded_mode_sensor')}"


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
    return f"sensor.{s(f'{DOMAIN}_read_only_sensor')}"


# Next Update Sensor
def get_next_update_sensor_name() -> str:
    """Return the display name for the next-update diagnostic sensor."""
    return "Next Update"


def get_next_update_sensor_unique_id() -> str:
    """Return a unique ID for the next-update sensor."""
    return f"{DOMAIN}_next_update_sensor"


def get_next_update_sensor_entity_id() -> str:
    """Return the entity_id for the next-update sensor."""
    return f"sensor.{s(f'{DOMAIN}_next_update_sensor')}"


# Missing Entities Sensor
def get_missing_entities_sensor_name() -> str:
    """Return the display name for the missing-entities count diagnostic sensor."""
    return "Missing Input Entities"


def get_missing_entities_sensor_unique_id() -> str:
    """Return a unique ID for the missing-entities sensor."""
    return f"{DOMAIN}_missing_entities_sensor"


def get_missing_entities_sensor_entity_id() -> str:
    """Return the entity_id for the missing-entities sensor."""
    return f"sensor.{s(f'{DOMAIN}_missing_entities_sensor')}"


# Hardware Writes Blocked Sensor
def get_hardware_writes_sensor_name() -> str:
    """Return the display name for the hardware-writes-blocked diagnostic sensor."""
    return "Hardware Writes"


def get_hardware_writes_sensor_unique_id() -> str:
    """Return a unique ID for the hardware-writes-blocked sensor."""
    return f"{DOMAIN}_hardware_writes_sensor"


def get_hardware_writes_sensor_entity_id() -> str:
    """Return the entity_id for the hardware-writes-blocked sensor."""
    return f"sensor.{s(f'{DOMAIN}_hardware_writes_sensor')}"


# Net Consumption Sensor
def get_net_consumption_sensor_name() -> str:
    """Return the display name for the net-consumption diagnostic sensor."""
    return "Net Consumption"


def get_net_consumption_sensor_unique_id() -> str:
    """Return a unique ID for the net-consumption sensor."""
    return f"{DOMAIN}_net_consumption_sensor"


def get_net_consumption_sensor_entity_id() -> str:
    """Return the entity_id for the net-consumption sensor."""
    return f"sensor.{s(f'{DOMAIN}_net_consumption_sensor')}"


# Force Working Mode Sensor
def get_force_mode_sensor_name() -> str:
    """Return the display name for the force-working-mode diagnostic sensor."""
    return "Force Working Mode"


def get_force_mode_sensor_unique_id() -> str:
    """Return a unique ID for the force-working-mode sensor."""
    return f"{DOMAIN}_force_mode_sensor"


def get_force_mode_sensor_entity_id() -> str:
    """Return the entity_id for the force-working-mode sensor."""
    return f"sensor.{s(f'{DOMAIN}_force_mode_sensor')}"


# Update Interval Sensor
def get_update_interval_sensor_name() -> str:
    """Return the display name for the update-interval diagnostic sensor."""
    return "Update Interval"


def get_update_interval_sensor_unique_id() -> str:
    """Return a unique ID for the update-interval sensor."""
    return f"{DOMAIN}_update_interval_sensor"


def get_update_interval_sensor_entity_id() -> str:
    """Return the entity_id for the update-interval sensor."""
    return f"sensor.{s(f'{DOMAIN}_update_interval_sensor')}"


# Last Updated Sensor
def get_last_updated_sensor_name() -> str:
    """Return the display name for the last-updated diagnostic sensor."""
    return "Last Updated"


def get_last_updated_sensor_unique_id() -> str:
    """Return a unique ID for the last-updated sensor."""
    return f"{DOMAIN}_last_updated_sensor"


def get_last_updated_sensor_entity_id() -> str:
    """Return the entity_id for the last-updated sensor."""
    return f"sensor.{s(f'{DOMAIN}_last_updated_sensor')}"


# Battery SoC Sensor
def get_battery_soc_sensor_name() -> str:
    """Return the display name for the battery-SoC diagnostic sensor."""
    return "Battery State of Charge"


def get_battery_soc_sensor_unique_id() -> str:
    """Return a unique ID for the battery-SoC sensor."""
    return f"{DOMAIN}_battery_soc_sensor"


def get_battery_soc_sensor_entity_id() -> str:
    """Return the entity_id for the battery-SoC sensor."""
    return f"sensor.{s(f'{DOMAIN}_battery_soc_sensor')}"


# Recommendation Interval Sensor
def get_recommendation_interval_sensor_name() -> str:
    """Return the display name for the recommendation-interval diagnostic sensor."""
    return "Recommendation Interval"


def get_recommendation_interval_sensor_unique_id() -> str:
    """Return a unique ID for the recommendation-interval sensor."""
    return f"{DOMAIN}_recommendation_interval_sensor"


def get_recommendation_interval_sensor_entity_id() -> str:
    """Return the entity_id for the recommendation-interval sensor."""
    return f"sensor.{s(f'{DOMAIN}_recommendation_interval_sensor')}"


# EV Charging Active Sensor
def get_ev_charging_sensor_name() -> str:
    """Return the display name for the EV-charging-active diagnostic sensor."""
    return "EV Charging Active"


def get_ev_charging_sensor_unique_id() -> str:
    """Return a unique ID for the EV-charging sensor."""
    return f"{DOMAIN}_ev_charging_sensor"


def get_ev_charging_sensor_entity_id() -> str:
    """Return the entity_id for the EV-charging sensor."""
    return f"sensor.{s(f'{DOMAIN}_ev_charging_sensor')}"


# EV Optimal Charging Plan Sensor
def get_ev_optimal_charging_plan_sensor_name() -> str:
    """Return the display name for the EV optimal charging plan sensor."""
    return "EV Optimal Charging Plan"


def get_ev_optimal_charging_plan_sensor_unique_id() -> str:
    """Return a unique ID for the EV optimal charging plan sensor."""
    return f"{DOMAIN}_ev_optimal_charging_plan"


def get_ev_optimal_charging_plan_sensor_entity_id() -> str:
    """Return the entity_id for the EV optimal charging plan sensor."""
    return f"sensor.{s(f'{DOMAIN}_ev_optimal_charging_plan')}"


# EV Second Optimal Charging Plan Sensor
def get_ev_second_optimal_charging_plan_sensor_name() -> str:
    """Return the display name for the second EV optimal charging plan sensor."""
    return "EV 2 Optimal Charging Plan"


def get_ev_second_optimal_charging_plan_sensor_unique_id() -> str:
    """Return a unique ID for the second EV optimal charging plan sensor."""
    return f"{DOMAIN}_ev_second_optimal_charging_plan"


def get_ev_second_optimal_charging_plan_sensor_entity_id() -> str:
    """Return the entity_id for the second EV optimal charging plan sensor."""
    return f"sensor.{s(f'{DOMAIN}_ev_second_optimal_charging_plan')}"


# Plan Explanation Sensor
def get_plan_explanation_sensor_name() -> str:
    """Return the display name for the plan-explanation diagnostic sensor."""
    return "Plan Strategy"


def get_plan_explanation_sensor_unique_id() -> str:
    """Return a unique ID for the plan-explanation sensor."""
    return f"{DOMAIN}_plan_explanation_sensor"


def get_plan_explanation_sensor_entity_id() -> str:
    """Return the entity_id for the plan-explanation sensor."""
    return f"sensor.{s(f'{DOMAIN}_plan_explanation_sensor')}"


# Force Working Mode Selector
def get_force_working_mode_selector_key() -> str:
    """Return the entity description key for the force-working-mode select entity."""
    return f"{DOMAIN}_force_working_mode"


def get_force_working_mode_selector_name() -> str:
    """Return the display name for the force-working-mode select entity."""
    return "Force Working Mode"


def get_force_working_mode_selector_entity_id() -> str:
    """Return the entity_id for the force-working-mode select entity."""
    return f"select.{s(f'{DOMAIN}_force_working_mode')}"


# Solcast PV Forecast Likelihood Selector
def get_solcast_likelihood_selector_key() -> str:
    """Return the entity description key for the solcast likelihood select entity."""
    return f"{DOMAIN}_solcast_likelihood"


def get_solcast_likelihood_selector_name() -> str:
    """Return the display name for the solcast likelihood select entity."""
    return "Solcast PV Forecast Likelihood"


def get_solcast_likelihood_selector_entity_id() -> str:
    """Return the entity_id for the solcast likelihood select entity."""
    return f"select.{s(f'{DOMAIN}_solcast_likelihood')}"


# Battery Charge Efficiency Number
def get_charge_efficiency_number_key() -> str:
    """Return the entity description key for the charge efficiency number entity."""
    return f"{DOMAIN}_charge_efficiency"


def get_charge_efficiency_number_name() -> str:
    """Return the display name for the charge efficiency number entity."""
    return "Battery Charge Efficiency"


def get_charge_efficiency_number_unique_id() -> str:
    """Return the unique_id for the charge efficiency number entity."""
    return f"{DOMAIN}_battery_charge_efficiency"


def get_charge_efficiency_number_entity_id() -> str:
    """Return the entity_id for the charge efficiency number entity."""
    return f"number.{s(f'{DOMAIN}_battery_charge_efficiency')}"


# Battery Discharge Efficiency Number
def get_discharge_efficiency_number_key() -> str:
    """Return the entity description key for the discharge efficiency number entity."""
    return f"{DOMAIN}_discharge_efficiency"


def get_discharge_efficiency_number_name() -> str:
    """Return the display name for the discharge efficiency number entity."""
    return "Battery Discharge Efficiency"


def get_discharge_efficiency_number_unique_id() -> str:
    """Return the unique_id for the discharge efficiency number entity."""
    return f"{DOMAIN}_battery_discharge_efficiency"


def get_discharge_efficiency_number_entity_id() -> str:
    """Return the entity_id for the discharge efficiency number entity."""
    return f"number.{s(f'{DOMAIN}_battery_discharge_efficiency')}"


# EV Target SoC Number
def get_ev_target_soc_number_key() -> str:
    """Return the entity description key for the EV target SoC number entity."""
    return f"{DOMAIN}_ev_target_soc"


def get_ev_target_soc_number_name() -> str:
    """Return the display name for the EV target SoC number entity."""
    return "EV Target SoC"


def get_ev_target_soc_number_unique_id() -> str:
    """Return the unique_id for the EV target SoC number entity."""
    return f"{DOMAIN}_{get_ev_target_soc_number_key()}_number"


def get_ev_target_soc_number_entity_id() -> str:
    """Return the entity_id for the EV target SoC number entity."""
    return f"number.{s(get_ev_target_soc_number_unique_id())}"


# EV 2 Target SoC Number
def get_ev_second_target_soc_number_key() -> str:
    """Return the entity description key for the EV 2 target SoC number entity."""
    return f"{DOMAIN}_ev_second_target_soc"


def get_ev_second_target_soc_number_name() -> str:
    """Return the display name for the EV 2 target SoC number entity."""
    return "EV 2 Target SoC"


def get_ev_second_target_soc_number_unique_id() -> str:
    """Return the unique_id for the EV 2 target SoC number entity."""
    return f"{DOMAIN}_{get_ev_second_target_soc_number_key()}_number"


def get_ev_second_target_soc_number_entity_id() -> str:
    """Return the entity_id for the EV 2 target SoC number entity."""
    return f"number.{s(get_ev_second_target_soc_number_unique_id())}"


# Applier Status Sensor
def get_applier_status_sensor_name() -> str:
    """Return the display name for the applier-status diagnostic sensor."""
    return "Inverter Apply Status"


def get_applier_status_sensor_unique_id() -> str:
    """Return a unique ID for the applier-status sensor."""
    return f"{DOMAIN}_applier_status_sensor"


def get_applier_status_sensor_entity_id() -> str:
    """Return the entity_id for the applier-status sensor."""
    return f"sensor.{s(f'{DOMAIN}_applier_status_sensor')}"


# Forecast Accuracy Sensor
def get_forecast_accuracy_sensor_name() -> str:
    """Return the display name for the forecast accuracy diagnostic sensor."""
    return "Forecast Accuracy"


def get_forecast_accuracy_sensor_unique_id() -> str:
    """Return a unique ID for the forecast accuracy sensor."""
    return f"{DOMAIN}_forecast_accuracy_sensor"


def get_forecast_accuracy_sensor_entity_id() -> str:
    """Return the entity_id for the forecast accuracy sensor."""
    return f"sensor.{s(f'{DOMAIN}_forecast_accuracy_sensor')}"


# ---------------------------------------------------------------------------
# Switch entities
# ---------------------------------------------------------------------------


def get_read_only_switch_key() -> str:
    """Return the config-entry key / unique_id basis for the read-only switch."""
    return f"{DOMAIN}_read_only"


def get_read_only_switch_name() -> str:
    """Return the display name for the read-only switch."""
    return "Read Only"


def get_read_only_switch_unique_id() -> str:
    """Return the unique_id for the read-only switch."""
    return f"{DOMAIN}_{get_read_only_switch_key()}_switch"


def get_read_only_switch_entity_id() -> str:
    """Return the entity_id for the read-only switch."""
    return f"switch.{s(get_read_only_switch_unique_id())}"


def get_extended_attributes_switch_key() -> str:
    """Return the config-entry key / unique_id basis for the extended-attributes switch."""
    return f"{DOMAIN}_extended_attributes"


def get_extended_attributes_switch_name() -> str:
    """Return the display name for the extended-attributes switch."""
    return "Extended Attributes"


def get_extended_attributes_switch_unique_id() -> str:
    """Return the unique_id for the extended-attributes switch."""
    return f"{DOMAIN}_{get_extended_attributes_switch_key()}_switch"


def get_extended_attributes_switch_entity_id() -> str:
    """Return the entity_id for the extended-attributes switch."""
    return f"switch.{s(get_extended_attributes_switch_unique_id())}"


def get_verbose_logging_switch_key() -> str:
    """Return the config-entry key / unique_id basis for the verbose-logging switch."""
    return f"{DOMAIN}_verbose_logging"


def get_verbose_logging_switch_name() -> str:
    """Return the display name for the verbose-logging switch."""
    return "Verbose Logging"


def get_verbose_logging_switch_unique_id() -> str:
    """Return the unique_id for the verbose-logging switch."""
    return f"{DOMAIN}_{get_verbose_logging_switch_key()}_switch"


def get_verbose_logging_switch_entity_id() -> str:
    """Return the entity_id for the verbose-logging switch."""
    return f"switch.{s(get_verbose_logging_switch_unique_id())}"


def get_batteries_schedule_1_switch_key() -> str:
    """Return the config-entry key / unique_id basis for the batteries-schedule-1 switch."""
    return f"{DOMAIN}_batteries_enable_batteries_schedule_1"


def get_batteries_schedule_1_switch_name() -> str:
    """Return the display name for the batteries-schedule-1 switch."""
    return "Batteries Discharge Schedule 1"


def get_batteries_schedule_1_switch_unique_id() -> str:
    """Return the unique_id for the batteries-schedule-1 switch."""
    return f"{DOMAIN}_{get_batteries_schedule_1_switch_key()}_switch"


def get_batteries_schedule_1_switch_entity_id() -> str:
    """Return the entity_id for the batteries-schedule-1 switch."""
    return f"switch.{s(get_batteries_schedule_1_switch_unique_id())}"


def get_batteries_schedule_2_switch_key() -> str:
    """Return the config-entry key / unique_id basis for the batteries-schedule-2 switch."""
    return f"{DOMAIN}_batteries_enable_batteries_schedule_2"


def get_batteries_schedule_2_switch_name() -> str:
    """Return the display name for the batteries-schedule-2 switch."""
    return "Batteries Discharge Schedule 2"


def get_batteries_schedule_2_switch_unique_id() -> str:
    """Return the unique_id for the batteries-schedule-2 switch."""
    return f"{DOMAIN}_{get_batteries_schedule_2_switch_key()}_switch"


def get_batteries_schedule_2_switch_entity_id() -> str:
    """Return the entity_id for the batteries-schedule-2 switch."""
    return f"switch.{s(get_batteries_schedule_2_switch_unique_id())}"


def get_batteries_schedule_3_switch_key() -> str:
    """Return the config-entry key / unique_id basis for the batteries-schedule-3 switch."""
    return f"{DOMAIN}_batteries_enable_batteries_schedule_3"


def get_batteries_schedule_3_switch_name() -> str:
    """Return the display name for the batteries-schedule-3 switch."""
    return "Batteries Discharge Schedule 3"


def get_batteries_schedule_3_switch_unique_id() -> str:
    """Return the unique_id for the batteries-schedule-3 switch."""
    return f"{DOMAIN}_{get_batteries_schedule_3_switch_key()}_switch"


def get_batteries_schedule_3_switch_entity_id() -> str:
    """Return the entity_id for the batteries-schedule-3 switch."""
    return f"switch.{s(get_batteries_schedule_3_switch_unique_id())}"


def get_ev_force_discharge_switch_key() -> str:
    """Return the config-entry key / unique_id basis for the EV-force-discharge switch."""
    return f"{DOMAIN}_ev_charger_force_max_discharge_power"


def get_ev_force_discharge_switch_name() -> str:
    """Return the display name for the EV-force-discharge switch."""
    return "EV Charger Force Max Discharge Power"


def get_ev_force_discharge_switch_unique_id() -> str:
    """Return the unique_id for the EV-force-discharge switch."""
    return f"{DOMAIN}_{get_ev_force_discharge_switch_key()}_switch"


def get_ev_force_discharge_switch_entity_id() -> str:
    """Return the entity_id for the EV-force-discharge switch."""
    return f"switch.{s(get_ev_force_discharge_switch_unique_id())}"


# ---------------------------------------------------------------------------
# EV smart charging switches
# ---------------------------------------------------------------------------


def get_ev_smart_charging_switch_key() -> str:
    """Return the config-entry key for the primary EV smart charging switch."""
    return f"{DOMAIN}_ev_smart_charging"


def get_ev_smart_charging_switch_name() -> str:
    """Return the display name for the primary EV smart charging switch."""
    return "EV Smart Charging"


def get_ev_smart_charging_switch_unique_id() -> str:
    """Return the unique_id for the primary EV smart charging switch."""
    return f"{DOMAIN}_{get_ev_smart_charging_switch_key()}_switch"


def get_ev_smart_charging_switch_entity_id() -> str:
    """Return the entity_id for the primary EV smart charging switch."""
    return f"switch.{s(get_ev_smart_charging_switch_unique_id())}"


def get_ev_force_charge_now_switch_key() -> str:
    """Return the config-entry key for the primary EV force-charge-now switch."""
    return f"{DOMAIN}_ev_force_charge_now"


def get_ev_force_charge_now_switch_name() -> str:
    """Return the display name for the primary EV force-charge-now switch."""
    return "EV Force Charge Now"


def get_ev_force_charge_now_switch_unique_id() -> str:
    """Return the unique_id for the primary EV force-charge-now switch."""
    return f"{DOMAIN}_{get_ev_force_charge_now_switch_key()}_switch"


def get_ev_force_charge_now_switch_entity_id() -> str:
    """Return the entity_id for the primary EV force-charge-now switch."""
    return f"switch.{s(get_ev_force_charge_now_switch_unique_id())}"


def get_ev_second_smart_charging_switch_key() -> str:
    """Return the config-entry key for the second EV smart charging switch."""
    return f"{DOMAIN}_ev_second_smart_charging"


def get_ev_second_smart_charging_switch_name() -> str:
    """Return the display name for the second EV smart charging switch."""
    return "EV 2 Smart Charging"


def get_ev_second_smart_charging_switch_unique_id() -> str:
    """Return the unique_id for the second EV smart charging switch."""
    return f"{DOMAIN}_{get_ev_second_smart_charging_switch_key()}_switch"


def get_ev_second_smart_charging_switch_entity_id() -> str:
    """Return the entity_id for the second EV smart charging switch."""
    return f"switch.{s(get_ev_second_smart_charging_switch_unique_id())}"


def get_ev_second_force_charge_now_switch_key() -> str:
    """Return the config-entry key for the second EV force-charge-now switch."""
    return f"{DOMAIN}_ev_second_force_charge_now"


def get_ev_second_force_charge_now_switch_name() -> str:
    """Return the display name for the second EV force-charge-now switch."""
    return "EV 2 Force Charge Now"


def get_ev_second_force_charge_now_switch_unique_id() -> str:
    """Return the unique_id for the second EV force-charge-now switch."""
    return f"{DOMAIN}_{get_ev_second_force_charge_now_switch_key()}_switch"


def get_ev_second_force_charge_now_switch_entity_id() -> str:
    """Return the entity_id for the second EV force-charge-now switch."""
    return f"switch.{s(get_ev_second_force_charge_now_switch_unique_id())}"


# ---------------------------------------------------------------------------
# EV deadline time entities
# ---------------------------------------------------------------------------


def get_ev_deadline_time_key() -> str:
    """Return the config key for the EV charge deadline time entity."""
    return f"{DOMAIN}_ev_deadline_time"


def get_ev_deadline_time_name() -> str:
    """Return the display name for the EV charge deadline time entity."""
    return "EV Charge Deadline"


def get_ev_deadline_time_unique_id() -> str:
    """Return the unique ID for the EV charge deadline time entity."""
    return f"{DOMAIN}_{get_ev_deadline_time_key()}_time"


def get_ev_deadline_time_entity_id() -> str:
    """Return the Home Assistant entity ID for the EV charge deadline time entity."""
    return f"time.{s(get_ev_deadline_time_unique_id())}"


def get_ev_second_deadline_time_key() -> str:
    """Return the config key for the second EV charge deadline time entity."""
    return f"{DOMAIN}_ev_second_deadline_time"


def get_ev_second_deadline_time_name() -> str:
    """Return the display name for the second EV charge deadline time entity."""
    return "EV 2 Charge Deadline"


def get_ev_second_deadline_time_unique_id() -> str:
    """Return the unique ID for the second EV charge deadline time entity."""
    return f"{DOMAIN}_{get_ev_second_deadline_time_key()}_time"


def get_ev_second_deadline_time_entity_id() -> str:
    """Return the Home Assistant entity ID for the second EV charge deadline time entity."""
    return f"time.{s(get_ev_second_deadline_time_unique_id())}"


# ---------------------------------------------------------------------------
# Time entities
# ---------------------------------------------------------------------------


def get_schedule_1_start_time_key() -> str:
    """Return the config-entry key / unique_id basis for schedule-1-start time."""
    return f"{DOMAIN}_batteries_enable_batteries_schedule_1_start"


def get_schedule_1_start_time_name() -> str:
    """Return the display name for the schedule-1-start time entity."""
    return "Batteries Discharge Schedule 1 Start"


def get_schedule_1_start_time_unique_id() -> str:
    """Return the unique_id for the schedule-1-start time entity."""
    return f"{DOMAIN}_{get_schedule_1_start_time_key()}_time"


def get_schedule_1_start_time_entity_id() -> str:
    """Return the entity_id for the schedule-1-start time entity."""
    return f"time.{s(get_schedule_1_start_time_unique_id())}"


def get_schedule_1_end_time_key() -> str:
    """Return the config-entry key / unique_id basis for schedule-1-end time."""
    return f"{DOMAIN}_batteries_enable_batteries_schedule_1_end"


def get_schedule_1_end_time_name() -> str:
    """Return the display name for the schedule-1-end time entity."""
    return "Batteries Discharge Schedule 1 End"


def get_schedule_1_end_time_unique_id() -> str:
    """Return the unique_id for the schedule-1-end time entity."""
    return f"{DOMAIN}_{get_schedule_1_end_time_key()}_time"


def get_schedule_1_end_time_entity_id() -> str:
    """Return the entity_id for the schedule-1-end time entity."""
    return f"time.{s(get_schedule_1_end_time_unique_id())}"


def get_schedule_2_start_time_key() -> str:
    """Return the config-entry key / unique_id basis for schedule-2-start time."""
    return f"{DOMAIN}_batteries_enable_batteries_schedule_2_start"


def get_schedule_2_start_time_name() -> str:
    """Return the display name for the schedule-2-start time entity."""
    return "Batteries Discharge Schedule 2 Start"


def get_schedule_2_start_time_unique_id() -> str:
    """Return the unique_id for the schedule-2-start time entity."""
    return f"{DOMAIN}_{get_schedule_2_start_time_key()}_time"


def get_schedule_2_start_time_entity_id() -> str:
    """Return the entity_id for the schedule-2-start time entity."""
    return f"time.{s(get_schedule_2_start_time_unique_id())}"


def get_schedule_2_end_time_key() -> str:
    """Return the config-entry key / unique_id basis for schedule-2-end time."""
    return f"{DOMAIN}_batteries_enable_batteries_schedule_2_end"


def get_schedule_2_end_time_name() -> str:
    """Return the display name for the schedule-2-end time entity."""
    return "Batteries Discharge Schedule 2 End"


def get_schedule_2_end_time_unique_id() -> str:
    """Return the unique_id for the schedule-2-end time entity."""
    return f"{DOMAIN}_{get_schedule_2_end_time_key()}_time"


def get_schedule_2_end_time_entity_id() -> str:
    """Return the entity_id for the schedule-2-end time entity."""
    return f"time.{s(get_schedule_2_end_time_unique_id())}"


def get_schedule_3_start_time_key() -> str:
    """Return the config-entry key / unique_id basis for schedule-3-start time."""
    return f"{DOMAIN}_batteries_enable_batteries_schedule_3_start"


def get_schedule_3_start_time_name() -> str:
    """Return the display name for the schedule-3-start time entity."""
    return "Batteries Discharge Schedule 3 Start"


def get_schedule_3_start_time_unique_id() -> str:
    """Return the unique_id for the schedule-3-start time entity."""
    return f"{DOMAIN}_{get_schedule_3_start_time_key()}_time"


def get_schedule_3_start_time_entity_id() -> str:
    """Return the entity_id for the schedule-3-start time entity."""
    return f"time.{s(get_schedule_3_start_time_unique_id())}"


def get_schedule_3_end_time_key() -> str:
    """Return the config-entry key / unique_id basis for schedule-3-end time."""
    return f"{DOMAIN}_batteries_enable_batteries_schedule_3_end"


def get_schedule_3_end_time_name() -> str:
    """Return the display name for the schedule-3-end time entity."""
    return "Batteries Discharge Schedule 3 End"


def get_schedule_3_end_time_unique_id() -> str:
    """Return the unique_id for the schedule-3-end time entity."""
    return f"{DOMAIN}_{get_schedule_3_end_time_key()}_time"


def get_schedule_3_end_time_entity_id() -> str:
    """Return the entity_id for the schedule-3-end time entity."""
    return f"time.{s(get_schedule_3_end_time_unique_id())}"
