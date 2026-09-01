"""This module defines constants used in the Huawei Solar Energy Management (HSEM) integration."""

import voluptuous as vol

DOMAIN = "hsem"  # Domain name for the integration.
NAME = "Huawei Solar Energy Management"  # Display name for the integration.

# Default TOU modes for letting the battery wait.
DEFAULT_HSEM_BATTERIES_WAIT_MODE = ["00:00-00:01/1234567/+"]

# TOU mode for force charging the battery.
DEFAULT_HSEM_TOU_MODES_FORCE_CHARGE = ["00:00-23:59/1234567/+"]

# Minimum required version of Huawei Solar.
MIN_HUAWEI_SOLAR_VERSION = "1.5.0a1"

DEFAULT_CONFIG_VALUES = {
    "device_name": NAME,
    "hsem_batteries_charge_efficiency": 98,
    "hsem_batteries_discharge_efficiency": 98,
    "hsem_batteries_enable_excess_export": False,
    "hsem_batteries_excess_export_discharge_buffer": 10,
    "hsem_batteries_forecast_reserve_pct": 0.0,
    # Per-slot hard floor for intentional battery-to-grid export (issue #752).
    # 0.0 = disabled (default) — fully backward compatible.
    "hsem_batteries_export_min_price": 0.0,
    # Wait mode behaviour: "strict" keeps the battery idle, "self_consumption_with_reserve"
    # allows normal household self-consumption while protecting the planner reserve.
    "hsem_batteries_wait_mode_behavior": "strict",
    "hsem_batteries_purchase_price": 0.0,
    "hsem_batteries_expected_cycles": 6000,
    "hsem_batteries_cycle_cost": 0.0,
    "hsem_batteries_capacity_loss_pct": 30,
    "hsem_batteries_enable_batteries_schedule_1_end": "09:00:00",
    "hsem_batteries_enable_batteries_schedule_1_start": "07:00:00",
    "hsem_batteries_enable_batteries_schedule_1": True,
    "hsem_batteries_enable_batteries_schedule_2_end": "21:00:00",
    "hsem_batteries_enable_batteries_schedule_2_start": "17:00:00",
    "hsem_batteries_enable_batteries_schedule_2": True,
    "hsem_batteries_enable_batteries_schedule_3_end": "02:00:00",
    "hsem_batteries_enable_batteries_schedule_3_start": "23:00:00",
    "hsem_batteries_enable_batteries_schedule_3": False,
    "hsem_ev_target_soc": 80,
    "hsem_ev_second_target_soc": 80,
    "hsem_ev_deadline_time": "07:00",
    "hsem_ev_second_deadline_time": "07:00",
    "hsem_export_electricity_min_price": -0.00,
    "hsem_electricity_price_update_interval": 15,
    "hsem_export_electricity_price_sensor": "sensor.energi_data_service_produktion",
    "hsem_import_electricity_price_sensor": "sensor.energi_data_service",
    "hsem_import_electricity_price_forecast_sensor": vol.UNDEFINED,
    "hsem_export_electricity_price_forecast_sensor": vol.UNDEFINED,
    "hsem_ev_allow_charge_past_target_soc": False,
    "hsem_ev_past_target_confidence_factor": 0.9,
    "hsem_ev_charger_force_max_discharge_power": False,
    "hsem_ev_charger_max_discharge_power": 0,
    "hsem_ev_charger_power": vol.UNDEFINED,
    "hsem_ev_charger_status": vol.UNDEFINED,
    "hsem_ev_connected": vol.UNDEFINED,
    "hsem_ev_smart_charging": False,
    "hsem_ev_force_charge_now": False,
    "hsem_ev_second_smart_charging": False,
    "hsem_ev_second_force_charge_now": False,
    # EV planned load integration — primary EV (optional, disabled by default)
    "hsem_ev_planned_load_enabled": False,
    "hsem_ev_planned_load_battery_capacity_kwh": 0.0,
    "hsem_ev_planned_load_charger_power_kw": 0.0,
    "hsem_ev_planned_load_charger_efficiency": 100,
    "hsem_ev_planned_load_charger_min_power_w": 1380,
    "hsem_ev_planned_load_charger_phase_topology": "single_phase",
    "hsem_ev_planned_load_deadline_safety_margin_pct": 0,
    "hsem_ev_planned_load_command_deadband_a": 3,
    "hsem_ev_planned_load_stub_floor_minutes": 2,
    # EV planned load integration — second EV (optional, disabled by default)
    "hsem_ev_second_planned_load_enabled": False,
    "hsem_ev_second_planned_load_battery_capacity_kwh": 0.0,
    "hsem_ev_second_planned_load_charger_power_kw": 0.0,
    "hsem_ev_second_planned_load_charger_efficiency": 100,
    "hsem_ev_second_planned_load_charger_min_power_w": 1380,
    "hsem_ev_second_planned_load_charger_phase_topology": "single_phase",
    "hsem_ev_second_planned_load_deadline_safety_margin_pct": 0,
    "hsem_ev_second_planned_load_command_deadband_a": 3,
    "hsem_ev_second_planned_load_stub_floor_minutes": 2,
    "hsem_ev_second_allow_charge_past_target_soc": False,
    "hsem_ev_second_past_target_confidence_factor": 0.9,
    "hsem_ev_second_charger_force_max_discharge_power": False,
    "hsem_ev_second_charger_max_discharge_power": 0,
    "hsem_ev_second_charger_power": vol.UNDEFINED,
    "hsem_ev_second_charger_status": vol.UNDEFINED,
    "hsem_ev_second_connected": vol.UNDEFINED,
    "hsem_ev_second_enabled": False,
    "hsem_ev_second_soc": vol.UNDEFINED,
    "hsem_ev_soc": vol.UNDEFINED,
    "hsem_extended_attributes": False,
    # Planner hysteresis — keep the active plan unless a new plan is
    # materially better (anti-flapping, issue #372).
    "hsem_planner_hysteresis_enabled": True,
    "hsem_planner_hysteresis_absolute": 0.0,
    "hsem_planner_hysteresis_percentage": 5.0,
    # Window-level hysteresis — prevent rapid recommendation toggles
    # (e.g. ev_smart_charging ↔ batteries_charge_solar) by enforcing a
    # minimum hold time (minutes).  0 disables the feature.
    "hsem_planner_window_hysteresis_minutes": 10,
    "hsem_house_consumption_energy_weight_14d": 15,
    "hsem_house_consumption_energy_weight_1d": 25,
    "hsem_house_consumption_energy_weight_3d": 30,
    "hsem_house_consumption_energy_weight_7d": 30,
    "hsem_house_consumption_power": "sensor.power_house_load",
    "hsem_house_power_includes_ev_charger_power": True,
    "hsem_main_fuse_amps": 25,
    "hsem_main_fuse_phases": 3,
    "hsem_max_grid_export_power_kw": 0,
    # Live per-phase Huawei grid-charge safety limiter (issue #831).
    # Disabled by default — fully backward compatible.  Requires
    # main_fuse_phases == 3 and the grid-charge-maximum-power +
    # power-meter-phase entities below to be configured.
    "hsem_phase_aware_charging_enabled": False,
    "hsem_huawei_solar_batteries_charging_cutoff_capacity": "number.batteries_end_of_charge_soc",
    "hsem_huawei_solar_batteries_end_of_discharge_soc": "number.batteries_end_of_discharge_soc",
    "hsem_huawei_solar_batteries_excess_pv_energy_use_in_tou": "select.batteries_excess_pv_energy_use_in_tou",
    "hsem_huawei_solar_batteries_forcible_charge": "sensor.batteries_forcible_charge",
    "hsem_huawei_solar_batteries_grid_charge_cutoff_soc": "number.batteries_grid_charge_cutoff_soc",
    "hsem_huawei_solar_batteries_charge_discharge_power": vol.UNDEFINED,
    "hsem_huawei_solar_batteries_grid_charge_maximum_power": vol.UNDEFINED,
    "hsem_huawei_solar_batteries_maximum_charging_power": "number.batteries_maximum_charging_power",
    "hsem_huawei_solar_batteries_maximum_discharging_power": "number.batteries_maximum_discharging_power",
    "hsem_huawei_solar_power_meter_phase_a_active_power": vol.UNDEFINED,
    "hsem_huawei_solar_power_meter_phase_b_active_power": vol.UNDEFINED,
    "hsem_huawei_solar_power_meter_phase_c_active_power": vol.UNDEFINED,
    "hsem_huawei_solar_batteries_rated_capacity": "sensor.batteries_rated_capacity",
    "hsem_huawei_solar_batteries_state_of_capacity": "sensor.batteries_state_of_capacity",
    "hsem_huawei_solar_batteries_tou_charging_and_discharging_periods": "sensor.batteries_tou_charging_and_discharging_periods",
    "hsem_huawei_solar_batteries_working_mode": "select.batteries_working_mode",
    "hsem_huawei_solar_device_id_batteries": vol.UNDEFINED,
    "hsem_huawei_solar_device_id_batteries_2": vol.UNDEFINED,
    "hsem_huawei_solar_device_id_inverter_1": vol.UNDEFINED,
    "hsem_huawei_solar_device_id_inverter_2": vol.UNDEFINED,
    "hsem_huawei_solar_inverter_active_power_control": "sensor.inverter_active_power_control",
    "hsem_months_summer": [4, 5, 6, 7, 8, 9],
    "hsem_months_winter": [1, 2, 3, 10, 11, 12],
    "hsem_read_only": False,
    "hsem_recommendation_interval_length": 48,
    "hsem_recommendation_interval_minutes": 15,
    "hsem_solar_production_power": "sensor.power_inverter_input_total",
    "hsem_solcast_pv_forecast_forecast_likelihood": "pv_estimate",
    "hsem_solcast_pv_forecast_forecast_today": "sensor.solcast_pv_forecast_forecast_today",
    "hsem_solcast_pv_forecast_forecast_tomorrow": "sensor.solcast_pv_forecast_forecast_tomorrow",
    "hsem_update_interval": 5,
    "hsem_verbose_logging": False,
    "hsem_dynamic_discharge_floor": False,
    # Embedded OCPP 1.6 server for EV charger control (issue #603).
    "hsem_ocpp_enabled": False,
    "hsem_ocpp_port": 9000,
    "hsem_ocpp_cpid": "",
    "hsem_ocpp_start_window_s": 60,
    "hsem_ocpp_stop_window_s": 180,
    # Second OCPP server for the optional second EV (separate port).
    "hsem_ocpp_second_enabled": False,
    "hsem_ocpp_second_port": 9001,
    "hsem_ocpp_second_cpid": "",
    # Daily plan-vs-actual tracking — optional energy meter entities.
    # When not configured, the sensor falls back to Riemann-sum estimates
    # from instantaneous power sensors.
    "hsem_grid_import_energy_entity": vol.UNDEFINED,
    "hsem_grid_export_energy_entity": vol.UNDEFINED,
    "hsem_pv_energy_entity": vol.UNDEFINED,
    # ML consumption prediction — toggle and settings
    "hsem_ml_consumption_enabled": False,
    "hsem_ml_consumption_energy_entity": vol.UNDEFINED,
    "hsem_ml_consumption_history_days": 14,
    "hsem_ml_consumption_net_consumption": False,
    "hsem_ml_consumption_sequential": False,
    "hsem_ml_consumption_temperature_entity": vol.UNDEFINED,
    # EV charging — auto-Full on negative price (issue #609)
    "hsem_ev_auto_full_negative_price": False,
}

# ---------------------------------------------------------------------------
# IQR outlier detection (replaces ratio-based spike detection, issue #301)
# ---------------------------------------------------------------------------

# IQR multiplier for outlier fence: values outside [Q1 - k * IQR, Q3 + k * IQR]
# are flagged as outliers.  1.5 is the standard Tukey fence.
IQR_OUTLIER_MULTIPLIER = 1.5

# When a window is flagged as an outlier, its weight is redistributed to the
# remaining non-outlier windows proportionally.  If ALL windows are outliers
# (degenerate case), no redistribution occurs.

# --- Caps between 7d and 14d (very mild, fail-safe) ---
CAP7_DOWN = 0.85  # 7d cannot go below 85% of 14d
CAP7_UP = 1.15  # 7d cannot go above 115% of 14d
CAP14_DOWN = 0.90  # 14d cannot go below 90% of 7d_eff
CAP14_UP = 1.10  # 14d cannot go above 110% of 7d_eff

# --- Capping of short windows vs calm baseline (7d/14d) ---
BASELINE_7D_SHARE = 0.70  # baseline = 0.70*7d + 0.30*14d
BASELINE_14D_SHARE = 0.30
CHANGE_LIMIT_DOWN_FACTOR = (
    0.80  # cap(yesterday, lower=0.8*baseline, upper=1.2*baseline)
)
CHANGE_LIMIT_UP_FACTOR = 1.20
CHANGE3_LIMIT_DOWN_FACTOR = 0.85  # slightly looser capping for 3d
CHANGE3_LIMIT_UP_FACTOR = 1.15

# --- Reliability weighting (down-weight disagreement) ---
# weights are scaled by 1 / (EPS + absolute deviation)
RELIABILITY_EPS = 0.05  # kWh; prevents division by zero and over-sensitivity
RELIABILITY_SCALE_STRENGTH = 1.00  # 1.0 = full effect; lower to soften

# ---------------------------------------------------------------------------
# Grid export power limit (watts)
# ---------------------------------------------------------------------------

# Grid export limit in watts when export should be blocked (instead of 0%).
# Setting 0% export via the percentage-based service is not well handled by
# the inverter; a 100 W floor is used instead.
GRID_EXPORT_LIMIT_WATT = 100

# ---------------------------------------------------------------------------
# Planner power thresholds (kWh per slot)
# ---------------------------------------------------------------------------

# Minimum solar surplus (negative net consumption) required to classify a
# slot as having usable solar generation when scheduling battery charge.
# A slot must export at least this much excess before it is considered a
# "solar surplus" charging opportunity.  Default matches v5.1.0 behaviour.
SOLAR_SURPLUS_CHARGE_THRESHOLD_KWH = -0.2

# Maximum net consumption for a slot to be treated as "near-zero" or solar-
# charged during seasonal optimisation.  Slots at or below this level are
# charged from solar rather than from the grid.  Default matches v5.1.0.
NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH = 0.1

# EMA smoothing factor for live net consumption used in EV charger power
# smoothing.  Alpha=0.3 means each new reading contributes 30 % to the
# smoothed value — this damps transient loads and
# short cloud shadows so they don't kill the EV charging setpoint for the
# rest of the 15-minute slot.
EMA_ALPHA_NET_CONSUMPTION = 0.3

# EV charger command stability.
#
# The MILP re-solves every cycle and re-derives the live slot's charger
# command from scratch.  Because the amp lattice is integer (issue #797) and
# the current slot's amp step shrinks as the slot elapses, competing integer
# splits are frequently within a rounding error of each other on cost — so a
# 0.3 % SoC update can flip the published command by 2-3 A for a fraction of
# an ore.  The deadband below holds the previous command through that noise.
#
# Escape hatch: a change that improves the live slot's own EV cost by more
# than this fraction is always allowed through, so the deadband can never
# trap the charger in a materially worse command.  5 % mirrors the default
# ``hsem_planner_hysteresis_percentage`` used for plan-level hysteresis.
EV_COMMAND_DEADBAND_COST_BYPASS_FRACTION = 0.05
