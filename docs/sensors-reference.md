# HSEM Sensors Reference

Comprehensive reference for all entities exposed by the HSEM integration, including
attributes, states, and dashboard examples.

---

## Entity overview

HSEM exposes these entity types:

| Type | Count | Description |
|---|---|---|
| **Sensor** | ~20+ | Read-only state, plan, and diagnostic entities |
| **Select** | 2 | Working-mode override and Solcast likelihood selector |
| **Switch** | 6+ | Toggle entities for schedules and configuration |
| **Time** | 6 | Start/end time inputs for battery schedules |
| **Number** | 4 | Charge/discharge efficiency and EV target SoC inputs |

---

## Working mode sensor

The primary HSEM sensor. Exposes the active battery recommendation and carries
all planner output as attributes.

**Entity:** `sensor.hsem_working_mode`

| State | Meaning |
|---|---|
| `batteries_charge_grid` | Battery charging from grid (forced by schedule or price) |
| `batteries_charge_solar` | Battery charging from PV surplus |
| `batteries_discharge_mode` | Battery discharging to cover house load |
| `force_batteries_discharge` | Forced discharge to grid (excess export) |
| `force_export` | Negative import price — all energy exported |
| `ev_smart_charging` | EV charging load allocated |
| `batteries_wait_mode` | Battery idle |
| `time_passed` | Slot is in the past |
| `missing_input_entities` | Required HA entities unavailable |

### Standard attributes

| Attribute | Type | Description |
|---|---|---|
| `batteries_current_capacity` | float (kWh) | Current usable battery capacity above discharge floor |
| `batteries_usable_capacity` | float (kWh) | Usable battery capacity (rated − floor) |
| `batteries_recommended_min_price_threshold` | float | Discharge threshold from `calculate_recommended_threshold()` |
| `batteries_capacity_loss_pct` | float | Configured capacity loss percentage |
| `import_electricity_price_state` | float | Live import spot price (currency/kWh) |
| `export_electricity_price_state` | float | Live export spot price (currency/kWh) |
| `export_electricity_min_price` | float | Minimum export price for export gating |
| `electricity_price_update_interval` | int | Price refresh interval (minutes) |
| `house_consumption_power_state` | float (W) | Instantaneous house load |
| `house_power_includes_ev_charger_power` | bool | Whether EV load is baked into house power |
| `net_consumption` | float (W) | Net load (house − solar) |
| `net_consumption_with_ev` | float (W) | Net load including EV |
| `solar_production_power_state` | float (W) | Instantaneous solar production |
| `months_winter` / `months_summer` | list[int] | Configured winter/summer month ranges |
| `batteries_enable_excess_export` | bool | Excess export gating enabled |
| `batteries_excess_export_discharge_buffer` | float | Discharge buffer for excess export |
| `house_consumption_energy_weight_1d` | float | 1-day consumption prediction weight |
| `house_consumption_energy_weight_3d` | float | 3-day weight |
| `house_consumption_energy_weight_7d` | float | 7-day weight |
| `house_consumption_energy_weight_14d` | float | 14-day weight |
| `last_updated` | string (ISO-8601) | Last coordinator cycle timestamp |
| `status` | string | `ok`, `read_only`, `wait`, or `error` |
| `degraded_mode` | string | `ok`, `degraded`, or `error` |
| `hardware_writes_blocked` | bool | Safety gate preventing hardware writes |
| `apply_status` | string | Last apply result: `ok`, `unverified`, `failed`, `skipped` |
| `apply_failed_entities` | list[string] | Entities that failed the last hardware write |
| `data_quality` | dict | Structured input completeness report |
| `force_working_mode_state` | string | Active override mode or `auto` |

### Plan output attributes

| Attribute | Type | Description |
|---|---|---|
| `hourly_recommendation` | dict \| null | The recommendation slot active **right now** |
| `hourly_recommendations` | list[dict] | Full list of planner slots for the horizon |
| `batteries_schedules` | list | Active battery discharge schedule definitions |
| `batteries_schedules_remaining_capacity_needed` | float (kWh) | Remaining discharge budget across schedules |

### `hourly_recommendations` slot structure

Each entry in the `hourly_recommendations` list is a dictionary with these keys:

| Key | Type | Description |
|---|---|---|
| `start` | string (ISO-8601) | Slot start timestamp |
| `end` | string (ISO-8601) | Slot end timestamp |
| `recommendation` | string \| null | Working-mode value (see state table above) |
| `import_price` | float | Spot import price (local currency/kWh) |
| `export_price` | float | Spot export price (local currency/kWh) |
| `avg_house_consumption_kwh` | float | Weighted spike-aware consumption estimate (kWh) |
| `avg_house_consumption_1d_kwh` | float | 1-day window contribution (kWh) |
| `avg_house_consumption_3d_kwh` | float | 3-day window contribution (kWh) |
| `avg_house_consumption_7d_kwh` | float | 7-day window contribution (kWh) |
| `avg_house_consumption_14d_kwh` | float | 14-day window contribution (kWh) |
| `solcast_pv_estimate_kwh` | float | Forecast PV production for the slot (kWh) |
| `estimated_net_consumption_kwh` | float | avg_consumption + ev_planned_load − pv_estimate (kWh) |
| `ev_planned_load_kwh` | float | Extra EV AC load added to net consumption (kWh, ≥ 0) |
| `ev_accounted_load_kwh` | float | EV AC load already in house consumption (kWh, ≥ 0) |
| `ev_total_planned_load_kwh` | float | Total EV AC load (planned + accounted, kWh, ≥ 0) |
| `ev_charger_calculated_power` | float | Primary EV charger target AC power (W) |
| `ev_second_charger_calculated_power` | float | Second EV charger target AC power (W) |
| `estimated_cost_currency` | float | Estimated grid cost for the slot (local currency) |
| `batteries_charged_kwh` | float | Energy scheduled to charge into battery (kWh) |
| `batteries_discharged_kwh` | float | Energy drawn from battery by SoC simulation (kWh) |
| `estimated_battery_capacity_kwh` | float | Remaining usable battery energy at slot end (kWh) |
| `estimated_battery_soc_pct` | float | Simulated absolute SoC at slot end (0–100 %) |
| `grid_import_kwh` | float | Energy imported from grid (kWh) |
| `grid_export_kwh` | float | Energy exported to grid (kWh) |

### Extended attributes (when enabled)

When the `switch.hsem_extended_attributes` switch is on, additional entity-ID
attributes are exposed. These reference the raw HA entity IDs for troubleshooting:

| Attribute | Description |
|---|---|
| `import_electricity_price_sensor_entity` | Import price sensor entity ID |
| `export_electricity_price_sensor_entity` | Export price sensor entity ID |
| `ev_charger_power_entity` | Primary EV charger power entity ID |
| `ev_charger_status_entity` | Primary EV charger status entity ID |
| `ev_soc_entity` | Primary EV SoC entity ID |
| `ev_connected_entity` | Primary EV connected entity ID |
| `ev_second_charger_power_entity` | Second EV charger power entity ID |
| `ev_second_charger_status_entity` | Second EV charger status entity ID |
| `ev_second_soc_entity` | Second EV SoC entity ID |
| `ev_second_connected_entity` | Second EV connected entity ID |
| `force_working_mode_entity` | Force working mode entity ID |
| `house_consumption_power_entity` | House consumption power entity ID |
| `solar_production_power_entity` | Solar production power entity ID |
| `solcast_pv_forecast_forecast_today_entity` | Solcast today forecast entity ID |
| `solcast_pv_forecast_forecast_tomorrow_entity` | Solcast tomorrow forecast entity ID |
| (plus all `huawei_solar_*` entity IDs) | Huawei battery and inverter entity IDs |
| `recommendation_interval_minutes` | Slot width in minutes |
| `recommendation_interval_length` | Number of slots in the horizon |
| `unique_id` | Integration unique ID |
| `update_interval` | Polling interval in minutes |
| `read_only` | Whether read-only mode is active |

### EV attributes

| Attribute | Type | Description |
|---|---|---|
| `ev_charger_power_state` | float (W) | Primary EV charger instantaneous power |
| `ev_charger_status_state` | bool | Primary EV currently charging |
| `ev_soc_state` | float (%) | Primary EV current SoC |
| `ev_soc_target_state` | float (%) | Primary EV target SoC |
| `ev_connected_state` | bool | Primary EV plugged in |
| `ev_allow_charge_past_target_soc` | bool | Allow charging past target |
| `ev_charger_max_discharge_power_state` | float (W) | Max discharge power cap |
| `ev_charger_force_max_discharge_power` | bool | Force max discharge power flag |
| `ev_second_enabled` | bool | Second EV integration enabled |
| `ev_second_charger_power_state` | float (W) | Second EV charger power |
| `ev_second_charger_status_state` | bool | Second EV charging |
| `ev_second_soc_state` | float (%) | Second EV SoC |
| `ev_second_soc_target_state` | float (%) | Second EV target SoC |
| `ev_second_connected_state` | bool | Second EV plugged in |
| `ev_second_allow_charge_past_target_soc` | bool | Second EV past-target flag |
| `ev_second_charger_max_discharge_power_state` | float (W) | Second EV max discharge cap |
| `ev_second_charger_force_max_discharge_power` | bool | Second EV force max discharge flag |

### Huawei battery attributes

| Attribute | Type | Description |
|---|---|---|
| `huawei_solar_batteries_charging_cutoff_capacity_state` | float (%) | Inverter charging cutoff SoC |
| `huawei_solar_batteries_grid_charge_cutoff_soc_state` | float (%) | Grid charge cutoff SoC |
| `huawei_solar_batteries_maximum_charging_power_state` | float (W) | Maximum charge power |
| `huawei_solar_batteries_maximum_discharging_power_state` | float (W) | Maximum discharge power |
| `huawei_solar_batteries_rated_capacity_max_state` | float (Wh) | Rated battery capacity |
| `huawei_solar_batteries_rated_capacity_min_state` | float (kWh) | Discharge floor capacity |
| `huawei_solar_batteries_state_of_capacity_state` | float (%) | Battery SoC |
| `huawei_solar_batteries_tou_charging_and_discharging_periods_periods` | list | Parsed TOU periods |
| `huawei_solar_batteries_tou_charging_and_discharging_periods_state` | string | Raw TOU entity state |
| `huawei_solar_batteries_working_mode_state` | string | Inverter working mode |
| `huawei_solar_inverter_active_power_control_state_state` | string | APC mode |
| `huawei_solar_batteries_excess_pv_energy_use_in_tou_state` | string | Excess PV in TOU setting |

---

## Plan explanation sensor

Displays the planner's strategy rationale and per-candidate cost breakdown.

**Entity:** `sensor.hsem_plan_explanation`

| Key attribute | Description |
|---|---|
| **State** | Winning candidate name: `"milp"`, `"passive"`, `"no_action"` |
| `selected_strategy` | Human-readable description (e.g. `"charge_grid_discharge_peak"`) |
| `winner_name` | Winning candidate name (same as state) |
| `summary` | One-sentence human-readable reason |
| `score` | Estimated savings vs doing nothing (currency) |
| `estimated_total_cost` | Net grid cost for the horizon |
| `price_spread` | Max minus min import price (arbitrage potential) |
| `peak_import_price` / `off_peak_import_price` | Price extremes |
| `forecast_pv_kwh` | Total PV forecast for the horizon |
| `forecast_net_consumption_kwh` | Total load minus PV |
| `battery_soc_pct` / `battery_soc_at_end_pct` | Starting and ending SoC |
| `constraints` | Active flags (`winter_month`, `excess_export_enabled`, etc.) |
| `rejected_plans` | Alternatives with name, reason, and full cost breakdown |
| `hysteresis_active` | Whether plan-level hysteresis was applied |
| `hysteresis_reason` | Explanation of hysteresis decision |

---

## Forecast accuracy sensor

Diagnostic sensor tracking forecast vs actual PV and load accuracy.

**Entity:** `sensor.hsem_forecast_accuracy`

| Attribute | Unit | Description |
|---|---|---|
| `native_value` | kWh | PV MAE (Mean Absolute Error) |
| `window_slots` | — | Total slots in ring buffer |
| `finalised_slots` | — | Slots contributing to metrics |
| `mae_pv_kwh` | kWh | PV Mean Absolute Error |
| `mae_load_kwh` | kWh | Load Mean Absolute Error |
| `bias_pv_kwh` | kWh | PV signed bias (positive = over-forecast) |
| `bias_load_kwh` | kWh | Load signed bias |
| `rmse_pv_kwh` | kWh | PV Root Mean Squared Error |
| `rmse_load_kwh` | kWh | Load RMSE |
| `mape_pv_pct` | % | PV MAPE |
| `mape_load_pct` | % | Load MAPE |
| `latest_pv_forecast_kwh` | kWh | Latest finalised slot PV forecast |
| `latest_pv_actual_kwh` | kWh | Latest finalised slot PV actual |
| `latest_load_forecast_kwh` | kWh | Latest finalised slot load forecast |
| `latest_load_actual_kwh` | kWh | Latest finalised slot load actual |

---

## EV charging plan sensors

Diagnostic sensors displaying the EV charging plan details.

**Entities:**
- `sensor.hsem_ev_optimal_charging_plan` — Primary EV
- `sensor.hsem_ev_second_optimal_charging_plan` — Second EV

| State | Meaning |
|---|---|
| `not_connected` | EV is not plugged in |
| `smart_charging_disabled` | Smart charging turned off |
| `fully_charged` | Already at or above target SoC |
| `charging` | EV scheduled to charge in current slot |
| `waiting` | Connected but no active charging slot |
| `unavailable` | Not configured or capacity/power is zero |

**Key attributes:**

| Attribute | Description |
|---|---|
| `battery_capacity_kwh` | EV battery nameplate capacity |
| `charge_power_kw` | Charger AC output power |
| `current_soc` / `target_soc` | EV SoC values |
| `ev_connected` | Whether vehicle is plugged in |
| `total_kwh_needed` | Energy needed to reach target |
| `deadline` | ISO-8601 charging deadline |
| `charging_slots` | List of allocated charging slots with details |
| `planned_load_by_slot` | Dict of slot → kWh load |
| `data_quality` | Diagnostic warnings |

---

## EV charging active sensor

Boolean sensor indicating whether any EV is actively drawing power.

**Entity:** `sensor.hsem_ev_charging_sensor`

| State | Meaning |
|---|---|
| `on` | At least one EV is charging |
| `off` | No EV is charging |

---

## Battery SoC sensor

Snapshot of the battery state of charge.

**Entity:** `sensor.hsem_battery_soc_sensor`

| State | Unit | Description |
|---|---|---|
| 0–100 | % | Battery SoC percentage |

---

## Diagnostic sensors

| Entity | Display Name | Purpose | State / Value |
|---|---|---|---|
| `sensor.hsem_applier_status_sensor` | Inverter Apply Status | Hardware write success/failure | `ok`, `unverified`, `failed`, `skipped` |
| `sensor.hsem_battery_soc_sensor` | Battery State of Charge | Battery SoC snapshot | Percentage (0–100) |
| `sensor.hsem_degraded_mode_sensor` | System Health | Overall system health | `ok`, `degraded`, `error` |
| `sensor.hsem_ev_charging_sensor` | EV Charging Active | Any EV actively charging | `on`, `off` |
| `sensor.hsem_force_mode_sensor` | Force Working Mode | Override active indicator | `auto` or override mode name |
| `sensor.hsem_hardware_writes_sensor` | Hardware Writes | Writes allowed/blocked by safety gate | `allowed`, `blocked` |
| `sensor.hsem_read_only_sensor` | Read-Only Mode | Read-only mode indicator | `on`, `off` |
| `sensor.hsem_house_consumption_power_sensor_*_*` | House Consumption Power | House load for a time range | Watts (W) |
| `sensor.hsem_net_consumption_sensor` | Net Consumption | Net load (house minus solar) | Watts (W) |
| `sensor.hsem_last_updated_sensor` | Last Updated | Last coordinator cycle timestamp | ISO-8601 timestamp |
| `sensor.hsem_next_update_sensor` | Next Update | Next scheduled coordinator cycle | ISO-8601 timestamp |
| `sensor.hsem_missing_entities_sensor` | Missing Input Entities | Count of missing input entities | Integer |
| `sensor.hsem_recommendation_interval_sensor` | Recommendation Interval | Slot width and horizon info | Minutes |
| `sensor.hsem_update_interval_sensor` | Update Interval | Current polling interval | Minutes |

---

## Select entities

### Force working mode

**Entity:** `select.hsem_force_working_mode`

| Option | Description |
|---|---|
| `auto` | Normal operation — planner controls battery |
| `batteries_charge_grid` | Force grid charge |
| `batteries_charge_solar` | Force solar charge |
| `batteries_discharge_mode` | Force discharge to house |
| `batteries_wait_mode` | Force idle |
| `ev_smart_charging` | Force EV charging |
| `force_batteries_discharge` | Force discharge to grid |
| `force_export` | Force all energy to export |

### Solcast PV forecast likelihood

**Entity:** `select.hsem_solcast_likelihood`

Selects which Solcast likelihood scenario to use for PV forecasts (e.g. `p10`, `p50`, `p90`).
This setting is also configurable in the options flow.

---

## Switch entities

| Entity | Purpose |
|---|---|
| `switch.hsem_read_only` | Block all hardware writes |
| `switch.hsem_extended_attributes` | Enable extended diagnostic attributes |
| `switch.hsem_verbose_logging` | Enable verbose logging |
| `switch.hsem_batteries_schedule_1` | Toggle battery schedule 1 |
| `switch.hsem_batteries_schedule_2` | Toggle battery schedule 2 |
| `switch.hsem_batteries_schedule_3` | Toggle battery schedule 3 |

---

## Number entities

| Entity | Purpose | Range |
|---|---|---|
| `number.hsem_battery_charge_efficiency` | Battery charge efficiency | 1–100 % |
| `number.hsem_battery_discharge_efficiency` | Battery discharge efficiency | 1–100 % |
| `number.hsem_ev_target_soc` | Primary EV target SoC | 0–100 % |
| `number.hsem_ev_second_target_soc` | Second EV target SoC | 0–100 % |

---

## Time entities

| Entity | Purpose |
|---|---|
| `time.hsem_batteries_schedule_1_start` | Schedule 1 start time |
| `time.hsem_batteries_schedule_1_end` | Schedule 1 end time |
| `time.hsem_batteries_schedule_2_start` | Schedule 2 start time |
| `time.hsem_batteries_schedule_2_end` | Schedule 2 end time |
| `time.hsem_batteries_schedule_3_start` | Schedule 3 start time |
| `time.hsem_batteries_schedule_3_end` | Schedule 3 end time |

---

## Data quality attribute

The `data_quality` dict on the working mode sensor provides a structured
input completeness report. Example structure:

```json
{
  "import_price": true,
  "export_price": true,
  "house_consumption": true,
  "solar_production": true,
  "battery_soc": true,
  "battery_capacity": true,
  "solcast_today": true,
  "solcast_tomorrow": false,
  "ev_connected": true,
  "ev_soc": true,
  "ev_second_connected": null,
  "ev_second_soc": null
}
```

Each key is a sensor category; the value is `true` (available), `false`
(missing), or `null` (not configured / not applicable).
