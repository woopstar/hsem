"""Diagnostic and meta sensor name generators.

Provides getter functions for working mode, degraded mode, read-only,
next update, missing entities, hardware writes, net consumption, force mode,
update interval, last updated, battery SoC, recommendation interval,
plan explanation, applier status, forecast accuracy, daily plan-vs-actual,
force working mode selector, and solcast likelihood selector names,
unique IDs, and entity IDs.
"""

from homeassistant.util import slugify as s

from custom_components.hsem.const import DOMAIN


# Working Mode Sensor
def get_working_mode_sensor_name() -> str:
    """Generate the display name for the working mode sensor.

    Returns:
        str: Display name of the working mode sensor.

    """
    return "Working Mode Sensor"


def get_working_mode_sensor_unique_id(entry_id: str) -> str:
    """Generate a unique ID for the working mode sensor.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.

    Returns:
        str: Unique ID of the working mode sensor.

    """
    return f"{DOMAIN}_{entry_id}_workingmode_sensor"


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


def get_degraded_mode_sensor_unique_id(entry_id: str) -> str:
    """Return a unique ID for the degraded-mode sensor.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.

    Returns:
        str: Unique ID.

    """
    return f"{DOMAIN}_{entry_id}_degraded_mode_sensor"


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


def get_read_only_sensor_unique_id(entry_id: str) -> str:
    """Return a unique ID for the read-only mode sensor.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.

    Returns:
        str: Unique ID.

    """
    return f"{DOMAIN}_{entry_id}_read_only_sensor"


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


def get_next_update_sensor_unique_id(entry_id: str) -> str:
    """Return a unique ID for the next-update sensor.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_next_update_sensor"


def get_next_update_sensor_entity_id() -> str:
    """Return the entity_id for the next-update sensor."""
    return f"sensor.{s(f'{DOMAIN}_next_update_sensor')}"


# Missing Entities Sensor
def get_missing_entities_sensor_name() -> str:
    """Return the display name for the missing-entities count diagnostic sensor."""
    return "Missing Input Entities"


def get_missing_entities_sensor_unique_id(entry_id: str) -> str:
    """Return a unique ID for the missing-entities sensor.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_missing_entities_sensor"


def get_missing_entities_sensor_entity_id() -> str:
    """Return the entity_id for the missing-entities sensor."""
    return f"sensor.{s(f'{DOMAIN}_missing_entities_sensor')}"


# Hardware Writes Blocked Sensor
def get_hardware_writes_sensor_name() -> str:
    """Return the display name for the hardware-writes-blocked diagnostic sensor."""
    return "Hardware Writes"


def get_hardware_writes_sensor_unique_id(entry_id: str) -> str:
    """Return a unique ID for the hardware-writes-blocked sensor.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_hardware_writes_sensor"


def get_hardware_writes_sensor_entity_id() -> str:
    """Return the entity_id for the hardware-writes-blocked sensor."""
    return f"sensor.{s(f'{DOMAIN}_hardware_writes_sensor')}"


# Net Consumption Sensor
def get_net_consumption_sensor_name() -> str:
    """Return the display name for the net-consumption diagnostic sensor."""
    return "Net Consumption"


def get_net_consumption_sensor_unique_id(entry_id: str) -> str:
    """Return a unique ID for the net-consumption sensor.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_net_consumption_sensor"


def get_net_consumption_sensor_entity_id() -> str:
    """Return the entity_id for the net-consumption sensor."""
    return f"sensor.{s(f'{DOMAIN}_net_consumption_sensor')}"


# Force Working Mode Sensor
def get_force_mode_sensor_name() -> str:
    """Return the display name for the force-working-mode diagnostic sensor."""
    return "Force Working Mode"


def get_force_mode_sensor_unique_id(entry_id: str) -> str:
    """Return a unique ID for the force-working-mode sensor.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_force_mode_sensor"


def get_force_mode_sensor_entity_id() -> str:
    """Return the entity_id for the force-working-mode sensor."""
    return f"sensor.{s(f'{DOMAIN}_force_mode_sensor')}"


# Update Interval Sensor
def get_update_interval_sensor_name() -> str:
    """Return the display name for the update-interval diagnostic sensor."""
    return "Update Interval"


def get_update_interval_sensor_unique_id(entry_id: str) -> str:
    """Return a unique ID for the update-interval sensor.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_update_interval_sensor"


def get_update_interval_sensor_entity_id() -> str:
    """Return the entity_id for the update-interval sensor."""
    return f"sensor.{s(f'{DOMAIN}_update_interval_sensor')}"


# Last Updated Sensor
def get_last_updated_sensor_name() -> str:
    """Return the display name for the last-updated diagnostic sensor."""
    return "Last Updated"


def get_last_updated_sensor_unique_id(entry_id: str) -> str:
    """Return a unique ID for the last-updated sensor.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_last_updated_sensor"


def get_last_updated_sensor_entity_id() -> str:
    """Return the entity_id for the last-updated sensor."""
    return f"sensor.{s(f'{DOMAIN}_last_updated_sensor')}"


# Battery SoC Sensor
def get_battery_soc_sensor_name() -> str:
    """Return the display name for the battery-SoC diagnostic sensor."""
    return "Battery State of Charge"


def get_battery_soc_sensor_unique_id(entry_id: str) -> str:
    """Return a unique ID for the battery-SoC sensor.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_battery_soc_sensor"


def get_battery_soc_sensor_entity_id() -> str:
    """Return the entity_id for the battery-SoC sensor."""
    return f"sensor.{s(f'{DOMAIN}_battery_soc_sensor')}"


# Recommendation Interval Sensor
def get_recommendation_interval_sensor_name() -> str:
    """Return the display name for the recommendation-interval diagnostic sensor."""
    return "Recommendation Interval"


def get_recommendation_interval_sensor_unique_id(entry_id: str) -> str:
    """Return a unique ID for the recommendation-interval sensor.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_recommendation_interval_sensor"


def get_recommendation_interval_sensor_entity_id() -> str:
    """Return the entity_id for the recommendation-interval sensor."""
    return f"sensor.{s(f'{DOMAIN}_recommendation_interval_sensor')}"


# Plan Explanation Sensor
def get_plan_explanation_sensor_name() -> str:
    """Return the display name for the plan-explanation diagnostic sensor."""
    return "Plan Strategy"


def get_plan_explanation_sensor_unique_id(entry_id: str) -> str:
    """Return a unique ID for the plan-explanation sensor.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_plan_explanation_sensor"


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


def get_force_working_mode_selector_unique_id(entry_id: str) -> str:
    """Return the unique_id for the force-working-mode select entity.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{get_force_working_mode_selector_key()}_{entry_id}"


def get_force_working_mode_selector_entity_id() -> str:
    """Return the entity_id for the force-working-mode select entity."""
    return f"select.{s(get_force_working_mode_selector_key())}"


# Solcast PV Forecast Likelihood Selector
def get_solcast_likelihood_selector_key() -> str:
    """Return the entity description key for the solcast likelihood select entity."""
    return f"{DOMAIN}_solcast_likelihood"


def get_solcast_likelihood_selector_name() -> str:
    """Return the display name for the solcast likelihood select entity."""
    return "Solcast PV Forecast Likelihood"


def get_solcast_likelihood_selector_unique_id(entry_id: str) -> str:
    """Return the unique_id for the solcast likelihood select entity.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{get_solcast_likelihood_selector_key()}_{entry_id}"


def get_solcast_likelihood_selector_entity_id() -> str:
    """Return the entity_id for the solcast likelihood select entity."""
    return f"select.{s(get_solcast_likelihood_selector_key())}"


# Applier Status Sensor
def get_applier_status_sensor_name() -> str:
    """Return the display name for the applier-status diagnostic sensor."""
    return "Inverter Apply Status"


def get_applier_status_sensor_unique_id(entry_id: str) -> str:
    """Return a unique ID for the applier-status sensor.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_applier_status_sensor"


def get_applier_status_sensor_entity_id() -> str:
    """Return the entity_id for the applier-status sensor."""
    return f"sensor.{s(f'{DOMAIN}_applier_status_sensor')}"


# Forecast Accuracy Sensor
def get_forecast_accuracy_sensor_name() -> str:
    """Return the display name for the forecast accuracy diagnostic sensor."""
    return "Forecast Accuracy"


def get_forecast_accuracy_sensor_unique_id(entry_id: str) -> str:
    """Return a unique ID for the forecast accuracy sensor.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_forecast_accuracy_sensor"


def get_forecast_accuracy_sensor_entity_id() -> str:
    """Return the entity_id for the forecast accuracy sensor."""
    return f"sensor.{s(f'{DOMAIN}_forecast_accuracy_sensor')}"


# Daily Plan-vs-Actual Diagnostic Sensor
def get_daily_plan_vs_actual_sensor_key() -> str:
    """Return the config key for the daily plan-vs-actual sensor."""
    return "daily_plan_vs_actual"


def get_daily_plan_vs_actual_sensor_name() -> str:
    """Return the display name for the daily plan-vs-actual sensor."""
    return "Daily Plan vs Actual"


def get_daily_plan_vs_actual_sensor_unique_id(entry_id: str) -> str:
    """Return the unique_id for the daily plan-vs-actual sensor.

    Args:
        entry_id: The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_{get_daily_plan_vs_actual_sensor_key()}_sensor"


def get_daily_plan_vs_actual_sensor_entity_id() -> str:
    """Return the entity_id for the daily plan-vs-actual sensor."""
    return f"sensor.{s(get_daily_plan_vs_actual_sensor_key())}"


# Effective Discharge Floor Sensor
def get_effective_discharge_floor_sensor_name() -> str:
    """Return the display name for the effective discharge floor sensor."""
    return "Effective Discharge Floor"


def get_effective_discharge_floor_sensor_unique_id(entry_id: str) -> str:
    """Return a unique ID for the effective discharge floor sensor.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_effective_discharge_floor_sensor"


def get_effective_discharge_floor_sensor_entity_id() -> str:
    """Return the entity_id for the effective discharge floor sensor."""
    return f"sensor.{s(f'{DOMAIN}_effective_discharge_floor_sensor')}"


# Solar Confidence Sensor


def get_solar_confidence_sensor_name() -> str:
    """Return the display name for the solar confidence diagnostic sensor."""
    return "Solar Forecast Confidence"


def get_solar_confidence_sensor_unique_id(entry_id: str) -> str:
    """Return a unique ID for the solar confidence sensor.

    Args:
        entry_id (str): The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_solar_confidence_sensor"


def get_solar_confidence_sensor_entity_id() -> str:
    """Return the entity_id for the solar confidence sensor."""
    return f"sensor.{s(f'{DOMAIN}_solar_confidence_sensor')}"


# Savings Tracker Sensor
def get_savings_tracker_sensor_name() -> str:
    """Return the display name for the savings tracker sensor."""
    return "Savings Tracker"


def get_savings_tracker_sensor_unique_id(entry_id: str) -> str:
    """Return a unique ID for the savings tracker sensor.

    Args:
        entry_id: The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_savings_tracker_sensor"


def get_savings_tracker_sensor_entity_id() -> str:
    """Return the entity_id for the savings tracker sensor."""
    return f"sensor.{s(f'{DOMAIN}_savings_tracker_sensor')}"


# Prediction Accuracy Sensor
def get_prediction_accuracy_sensor_name() -> str:
    """Return the display name for the prediction accuracy sensor."""
    return "Prediction Accuracy"


def get_prediction_accuracy_sensor_unique_id(entry_id: str) -> str:
    """Return a unique ID for the prediction accuracy sensor.

    Args:
        entry_id: The config entry ID for uniqueness across entries.
    """
    return f"{DOMAIN}_{entry_id}_prediction_accuracy_sensor"


def get_prediction_accuracy_sensor_entity_id() -> str:
    """Return the entity_id for the prediction accuracy sensor."""
    return f"sensor.{s(f'{DOMAIN}_prediction_accuracy_sensor')}"


# PV Curtailment Sensor (issue #611)
def get_pv_curtailment_sensor_name() -> str:
    """Return the display name for the PV curtailment sensor."""
    return "PV Curtailment"


def get_pv_curtailment_sensor_unique_id(entry_id: str) -> str:
    """Return a unique ID for the PV curtailment sensor."""
    return f"{DOMAIN}_{entry_id}_pv_curtailment_sensor"


def get_pv_curtailment_sensor_entity_id() -> str:
    """Return the entity_id for the PV curtailment sensor."""
    return f"sensor.{s(f'{DOMAIN}_pv_curtailment_sensor')}"
