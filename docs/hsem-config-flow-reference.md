# HSEM Config Flow Reference

This document describes every step in the HSEM configuration and options flows.

---

## Config flow steps

The config flow is a multi-step wizard. Steps appear in this order:

```
init → energidataservice → months → solcast → huawei_solar
    → power → ev → [ev_second] → ev_planned_load
    → [ev_second_planned_load] → batteries_schedule_1
    → batteries_schedule_2 → batteries_schedule_3
    → batteries_excess_export → weighted_values
```

### Step: `init`

| Field | Key | Default | Description |
|---|---|---|---|
| Device name | `device_name` | `"Huawei Solar Energy Management"` | Friendly name for the integration |
| Update interval | `hsem_update_interval` | 5 minutes | Coordinator polling interval |
| Read-only mode | `hsem_read_only` | `False` | Block all hardware writes when enabled |
| Verbose logging | `hsem_verbose_logging` | `False` | Enable debug-level planner logging |

### Step: `energidataservice`

Electricity price sensor configuration.

| Field | Key | Default | Description |
|---|---|---|---|
| Import price sensor | `hsem_energi_data_service_import` | `sensor.energi_data_service` | HA entity for import price |
| Export price sensor | `hsem_energi_data_service_export` | `sensor.energi_data_service_produktion` | HA entity for export price |
| EDS update interval | `hsem_energi_data_service_update_interval` | 15 minutes | How often EDS publishes prices (15 or 60) |
| Export min price | `hsem_energi_data_service_export_min_price` | 0.0 | Below this, inverter throttles export to zero |

### Step: `months`

Seasonal month classification.

| Field | Key | Default | Description |
|---|---|---|---|
| Summer months | `hsem_months_summer` | `[5, 6, 7, 8, 9]` | Months classified as summer |
| Winter months | `hsem_months_winter` | `[1, 2, 3, 4, 10, 11, 12]` | Months classified as winter |

### Step: `solcast`

PV forecast sensor configuration.

| Field | Key | Default | Description |
|---|---|---|---|
| Forecast today | `hsem_solcast_pv_forecast_forecast_today` | `sensor.solcast_pv_forecast_forecast_today` | Today's Solcast forecast |
| Forecast tomorrow | `hsem_solcast_pv_forecast_forecast_tomorrow` | `sensor.solcast_pv_forecast_forecast_tomorrow` | Tomorrow's Solcast forecast |
| Forecast likelihood | `hsem_solcast_pv_forecast_forecast_likelihood` | `pv_estimate` | Attribute key for the estimate field |

### Step: `huawei_solar`

Huawei Solar inverter and battery entity configuration (device selectors and entity sensors only).

| Field | Key | Default | Description |
|---|---|---|---|
| Inverter 1 device ID | `hsem_huawei_solar_device_id_inverter_1` | — | Device registry ID for inverter 1 |
| Inverter 2 device ID | `hsem_huawei_solar_device_id_inverter_2` | — | Device registry ID for inverter 2 (optional) |
| Batteries device ID | `hsem_huawei_solar_device_id_batteries` | — | Device registry ID for battery |
| Working mode | `hsem_huawei_solar_batteries_working_mode` | `select.batteries_working_mode` | Battery working mode select |
| End of discharge SoC | `hsem_huawei_solar_batteries_end_of_discharge_soc` | `number.batteries_end_of_discharge_soc` | Min SoC floor entity |
| State of capacity | `hsem_huawei_solar_batteries_state_of_capacity` | `sensor.batteries_state_of_capacity` | SoC sensor |
| Charging cutoff capacity | `hsem_huawei_solar_batteries_charging_cutoff_capacity` | `number.batteries_end_of_charge_soc` | Max SoC during charging |
| Grid charge cutoff SoC | `hsem_huawei_solar_batteries_grid_charge_cutoff_soc` | `number.batteries_grid_charge_cutoff_soc` | Max SoC when charging from grid |
| Max charging power | `hsem_huawei_solar_batteries_maximum_charging_power` | `number.batteries_maximum_charging_power` | Max charge power |
| Max discharging power | `hsem_huawei_solar_batteries_maximum_discharging_power` | `number.batteries_maximum_discharging_power` | Max discharge power |
| Rated capacity | `hsem_huawei_solar_batteries_rated_capacity` | `sensor.batteries_rated_capacity` | Nameplate capacity sensor |
| TOU periods | `hsem_huawei_solar_batteries_tou_charging_and_discharging_periods` | `sensor.batteries_tou_charging_and_discharging_periods` | TOU period schedule |
| Excess PV use | `hsem_huawei_solar_batteries_excess_pv_energy_use_in_tou` | `select.batteries_excess_pv_energy_use_in_tou` | Excess PV mode in TOU |
| Active power control | `hsem_huawei_solar_inverter_active_power_control` | `sensor.inverter_active_power_control` | Export power control mode |

### Step: `battery_economics`

Battery depreciation and efficiency parameters.

| Field | Key | Default | Description |
|---|---|---|---|
| Purchase price | `hsem_batteries_purchase_price` | 0 | Battery system cost (EUR) |
| Expected cycles | `hsem_batteries_expected_cycles` | 6000 | Total expected lifetime cycles |
| Cycle cost | `hsem_batteries_cycle_cost` | 0 | Extra per-kWh wear margin (EUR/kWh) |
| Capacity loss at EOL | `hsem_batteries_capacity_loss_pct` | 30 % | Expected capacity loss at end-of-life (%) |
| Charge efficiency | `hsem_batteries_charge_efficiency` | 98 % | Charge-side efficiency |
| Discharge efficiency | `hsem_batteries_discharge_efficiency` | 98 % | Discharge-side efficiency |

### Step: `power`

Power sensor configuration.

| Field | Key | Default | Description |
|---|---|---|---|
| House consumption power | `hsem_house_consumption_power` | `sensor.power_house_load` | House load power sensor |
| Solar production power | `hsem_solar_production_power` | `sensor.power_inverter_input_total` | PV production sensor |
| House includes EV | `hsem_house_power_includes_ev_charger_power` | `True` | Whether house sensor already includes EV charger |

### Step: `ev`

Primary EV charger configuration.

| Field | Key | Default | Description |
|---|---|---|---|
| EV charger status | `hsem_ev_charger_status` | — | Charger status sensor entity |
| EV charger power | `hsem_ev_charger_power` | — | Charger power sensor entity |
| EV SoC sensor | `hsem_ev_soc` | — | EV battery SoC sensor |
| EV SoC target | `hsem_ev_soc_target` | — | EV target SoC entity |
| EV connected sensor | `hsem_ev_connected` | — | Binary sensor for EV plugged in |

### Step: `ev_planned_load`

Primary EV planned load integration (optional, default disabled).

| Field | Key | Default | Description |
|---|---|---|---|
| Enable | `hsem_ev_planned_load_enabled` | `False` | Master switch |
| Target SoC fixed | `hsem_ev_planned_load_target_soc_fixed` | 80 % | Fixed target when no entity |
| Deadline entity | `hsem_ev_planned_load_deadline_entity` | — | Override deadline entity |
| Deadline fixed | `hsem_ev_planned_load_deadline_fixed` | `"07:00"` | Fixed deadline |
| Smart charging entity | `hsem_ev_planned_load_smart_charging_entity` | — | Enable/disable smart charging at runtime |
| Battery capacity | `hsem_ev_planned_load_battery_capacity_kwh` | 0.0 | EV battery nameplate capacity (kWh) |
| Charger power | `hsem_ev_planned_load_charger_power_kw` | 0.0 | Charger AC output (kW) |
| Charger efficiency | `hsem_ev_planned_load_charger_efficiency` | 100 % | Charger efficiency |


### Step: `batteries_schedule_1/2/3`

Battery charge/discharge schedule windows (up to three).

| Field | Key | Default | Description |
|---|---|---|---|
| Enabled | `hsem_batteries_enable_batteries_schedule_N` | Varies | Toggle this schedule window |
| Start time | `hsem_batteries_enable_batteries_schedule_N_start` | Varies | Window start (HH:MM:SS) |
| End time | `hsem_batteries_enable_batteries_schedule_N_end` | Varies | Window end (HH:MM:SS) |

Schedule 1 and 2 are enabled by default; schedule 3 is disabled by default.

### Step: `batteries_excess_export`

Excess battery export configuration.

| Field | Key | Default | Description |
|---|---|---|---|
| Enable excess export | `hsem_batteries_enable_excess_export` | `False` | Master switch |
| Discharge buffer | `hsem_batteries_excess_export_discharge_buffer` | 10 % | Safety SoC buffer before forced export |
| Price threshold | — | Auto-calculated | Computed from battery depreciation settings at runtime |


### Step: `weighted_values`

Consumption prediction weights and battery configuration.

| Field | Key | Default | Description |
|---|---|---|---|
| Weight 1-day | `hsem_house_consumption_energy_weight_1d` | 25 % | Weight for 1-day average |
| Weight 3-day | `hsem_house_consumption_energy_weight_3d` | 30 % | Weight for 3-day average |
| Weight 7-day | `hsem_house_consumption_energy_weight_7d` | 30 % | Weight for 7-day average |
| Weight 14-day | `hsem_house_consumption_energy_weight_14d` | 15 % | Weight for 14-day average |
| Charge efficiency | `hsem_batteries_charge_efficiency` | 95 % | Battery charge efficiency |
| Discharge efficiency | `hsem_batteries_discharge_efficiency` | 95 % | Battery discharge efficiency |
| Purchase price | `hsem_batteries_purchase_price` | 0.0 | Battery purchase price for cycle cost |
| Expected cycles | `hsem_batteries_expected_cycles` | 6000 | Expected lifetime cycles |
| Cycle cost | `hsem_batteries_cycle_cost` | 0.0 | Explicit cycle cost (auto-derived when 0) |
| Planning horizon | `hsem_recommendation_interval_length` | 48 hours | How far to plan ahead |
| Slot width | `hsem_recommendation_interval_minutes` | 15 minutes | Width of each planning slot |
| Hysteresis enabled | `hsem_planner_hysteresis_enabled` | `True` | Enable plan-level hysteresis |
| Hysteresis % | `hsem_planner_hysteresis_percentage` | 5.0 % | Percentage threshold for plan switching |
| Window hysteresis | `hsem_planner_window_hysteresis_minutes` | 0 | Window-level hysteresis hold time (0 = disabled) |
| Extended attributes | `hsem_extended_attributes` | `False` | Expose extended sensor attributes |
