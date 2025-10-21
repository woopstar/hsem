"""
This module defines constants used in the Huawei Solar Energy Management (HSEM) integration.
"""

import voluptuous as vol

DOMAIN = "hsem"  # Domain name for the integration
NAME = "Huawei Solar Energy Management"  # Display name for the integration

# Default TOU modes for EV charger when charging
DEFAULT_HSEM_EV_CHARGER_TOU_MODES = ["00:00-00:01/1234567/+"]

# Default TOU modes for letting the battery wait
DEFAULT_HSEM_BATTERIES_WAIT_MODE = ["00:00-00:01/1234567/+"]

# TOU mode for force charging the battery
DEFAULT_HSEM_TOU_MODES_FORCE_CHARGE = ["00:00-23:59/1234567/+"]

# TOU mode for force dicharging the battery
DEFAULT_HSEM_TOU_MODES_FORCE_DISCHARGE = ["00:00-23:59/1234567/-"]

# Minimum required version of Huawei Solar
MIN_HUAWEI_SOLAR_VERSION = "1.5.0a1"

DEFAULT_CONFIG_VALUES = {
    "device_name": NAME,
    "hsem_months_winter": ["1", "2", "3", "4", "10", "11", "12"],
    "hsem_months_summer": ["5", "6", "7", "8", "9"],
    "hsem_batteries_conversion_loss": 10,
    "hsem_batteries_enable_batteries_schedule_1_end": "09:00:00",
    "hsem_batteries_enable_batteries_schedule_1_min_price_difference": 0.00,
    "hsem_batteries_enable_batteries_schedule_1_start": "07:00:00",
    "hsem_batteries_enable_batteries_schedule_1": True,
    "hsem_batteries_enable_batteries_schedule_2_end": "21:00:00",
    "hsem_batteries_enable_batteries_schedule_2_min_price_difference": 0.00,
    "hsem_batteries_enable_batteries_schedule_2_start": "17:00:00",
    "hsem_batteries_enable_batteries_schedule_2": True,
    "hsem_batteries_enable_batteries_schedule_3_end": "00:00:00",
    "hsem_batteries_enable_batteries_schedule_3_min_price_difference": 0.00,
    "hsem_batteries_enable_batteries_schedule_3_start": "00:00:00",
    "hsem_batteries_enable_batteries_schedule_3": False,
    "hsem_energi_data_service_export_min_price": -0.00,
    "hsem_energi_data_service_export": "sensor.energi_data_service_produktion",
    "hsem_energi_data_service_import": "sensor.energi_data_service",
    "hsem_ev_charger_force_max_discharge_power": False,
    "hsem_ev_charger_max_discharge_power": 0,
    "hsem_ev_charger_power": vol.UNDEFINED,
    "hsem_ev_charger_status": vol.UNDEFINED,
    "hsem_ev_soc": vol.UNDEFINED,
    "hsem_ev_soc_target": vol.UNDEFINED,
    "hsem_ev_connected": vol.UNDEFINED,
    "hsem_ev_allow_charge_past_target_soc": False,
    "hsem_extended_attributes": False,
    "hsem_house_consumption_energy_weight_14d": 15,
    "hsem_house_consumption_energy_weight_1d": 25,
    "hsem_house_consumption_energy_weight_3d": 30,
    "hsem_house_consumption_energy_weight_7d": 30,
    "hsem_house_consumption_power": "sensor.power_house_load",
    "hsem_house_power_includes_ev_charger_power": True,
    "hsem_huawei_solar_batteries_excess_pv_energy_use_in_tou": "select.batteries_excess_pv_energy_use_in_tou",
    "hsem_huawei_solar_batteries_grid_charge_cutoff_soc": "number.batteries_grid_charge_cutoff_soc",
    "hsem_huawei_solar_batteries_maximum_charging_power": "number.batteries_maximum_charging_power",
    "hsem_huawei_solar_batteries_maximum_discharging_power": "number.batteries_maximum_discharging_power",
    "hsem_huawei_solar_batteries_rated_capacity": "sensor.batteries_rated_capacity",
    "hsem_huawei_solar_batteries_state_of_capacity": "sensor.batteries_state_of_capacity",
    "hsem_huawei_solar_batteries_tou_charging_and_discharging_periods": "sensor.batteries_tou_charging_and_discharging_periods",
    "hsem_huawei_solar_batteries_working_mode": "select.batteries_working_mode",
    "hsem_huawei_solar_device_id_batteries": vol.UNDEFINED,
    "hsem_huawei_solar_device_id_inverter_1": vol.UNDEFINED,
    "hsem_huawei_solar_device_id_inverter_2": vol.UNDEFINED,
    "hsem_huawei_solar_inverter_active_power_control": "sensor.inverter_active_power_control",
    "hsem_read_only": False,
    "hsem_solar_production_power": "sensor.power_inverter_input_total",
    "hsem_solcast_pv_forecast_forecast_today": "sensor.solcast_pv_forecast_forecast_today",
    "hsem_solcast_pv_forecast_forecast_tomorrow": "sensor.solcast_pv_forecast_forecast_tomorrow",
    "hsem_solcast_pv_forecast_forecast_likelihood": "pv_estimate",
    "hsem_update_interval": 5,
    "hsem_verbose_logging": False,
}

# --- Caps between 7d and 14d (very mild, fail-safe) ---
CAP7_DOWN = 0.85  # 7d cannot go below 85% of 14d
CAP7_UP = 1.15  # 7d cannot go above 115% of 14d
CAP14_DOWN = 0.90  # 14d cannot go below 90% of 7d_eff
CAP14_UP = 1.10  # 14d cannot go above 110% of 7d_eff

# --- Spike detection thresholds (ratios) ---
# 1d vs 7d
SPIKE1_RATIO_MIN = 1.30  # ≤ → no spike
SPIKE1_RATIO_MAX = 2.00  # ≥ → max severity
SPIKE1_REDUCE_FRACTION_MAX = 0.50  # at max severity, reallocate up to 50% of 1d weight
SPIKE1_REDIST_TO_3D = 0.20
SPIKE1_REDIST_TO_7D = 0.55
SPIKE1_REDIST_TO_14D = 0.25

# 3d vs 7d (milder than 1d)
SPIKE3_RATIO_MIN = 1.20
SPIKE3_RATIO_MAX = 1.80
SPIKE3_REDUCE_FRACTION_MAX = 0.30
SPIKE3_REDIST_TO_7D = 0.60
SPIKE3_REDIST_TO_14D = 0.40

# 7d vs 14d (recent window vs longer history)
# If 7d >> 14d, nudge some weight from 7d to 14d to avoid overreacting to short bursts.
SPIKE7_RATIO_MIN = 1.20
SPIKE7_RATIO_MAX = 1.60
SPIKE7_REDUCE_FRACTION_MAX = 0.20
SPIKE7_REDIST_TO_14D = 1.00  # all freed 7d weight goes to 14d

# 14d vs 7d (long window dominating)
# If 14d >> 7d, nudge some weight from 14d to 7d to adapt to newer reality.
SPIKE14_RATIO_MIN = 1.15
SPIKE14_RATIO_MAX = 1.50
SPIKE14_REDUCE_FRACTION_MAX = 0.15
SPIKE14_REDIST_TO_7D = 1.00  # all freed 14d weight goes to 7d

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
