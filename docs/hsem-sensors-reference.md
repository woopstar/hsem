# HSEM Sensors Reference

Comprehensive reference for all sensor entities exposed by the HSEM integration.

---

## Sensor types

HSEM exposes three types of entities:

| Type | Count | Description |
|---|---|---|
| **Sensor** | ~20+ | Read-only state and diagnostic entities |
| **Select** | 1 | Working-mode override selector |
| **Switch** | 5+ | Toggle entities for configuration options |
| **Time** | 1+ | Time input entities |

---

## Working mode sensor

The primary HSEM sensor that displays the currently active battery recommendation
and serves as the entry point for hardware writes.

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

**Key attributes:**

| Attribute | Description |
|---|---|
| `planned_slots` | Full list of planned slots with recommendations |
| `missing_inputs` | List of missing entity diagnostics |
| `degraded_mode` | Current health state (`ok`, `degraded`, `error`) |
| `force_working_mode` | Whether an override is active |
| `is_read_only` | Whether hardware writes are blocked |
| `weight_1d/3d/7d/14d` | Current consumption prediction weights |
| `data_quality` | Structured input completeness report |

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
| `price_spread` | Max − min import price (arbitrage potential) |
| `peak_import_price` / `off_peak_import_price` | Price extremes |
| `forecast_pv_kwh` | Total PV forecast for the horizon |
| `forecast_net_consumption_kwh` | Total load − PV |
| `battery_soc_pct` / `battery_soc_at_end_pct` | Starting and ending SoC |
| `constraints` | Active flags (`winter_month`, `excess_export_enabled`, etc.) |
| `rejected_plans` | Alternatives with name, reason, and full cost breakdown (import_cost, export_revenue, conversion_loss, cycle_cost, score) |
| `hysteresis_active` | Whether plan-level hysteresis was applied |
| `hysteresis_reason` | Explanation of hysteresis decision |

---

## Forecast accuracy sensor

Diagnostic sensor tracking forecast vs actual PV and load accuracy.

**Entity:** `sensor.forecast_accuracy`

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

## Diagnostic sensors

| Entity | Purpose | State / Value |
|---|---|---|
| `sensor.hsem_applier_status` | Hardware write success/failure | `ok`, `unverified`, `failed`, `skipped` |
| `sensor.hsem_battery_soc` | Battery SoC snapshot | Percentage (0–100) |
| `sensor.hsem_degraded_mode` | System health | `ok`, `degraded`, `error` |
| `sensor.hsem_ev_is_charging` | Any EV actively charging | `on`, `off` |
| `sensor.hsem_force_mode` | Override active indicator | `auto` or override mode name |
| `sensor.hsem_hardware_writes` | Writes allowed/blocked by safety gate | `allowed`, `blocked` |
| `sensor.hsem_read_only` | Read-only mode indicator | `on`, `off` |
| `sensor.hsem_house_consumption_power` | Instantaneous house load | Watts (W) |
| `sensor.hsem_net_consumption` | Net load (house − solar) | Watts (W) |
| `sensor.hsem_last_updated` | Last coordinator cycle timestamp | ISO-8601 timestamp |
| `sensor.hsem_next_update` | Next scheduled coordinator cycle | ISO-8601 timestamp |
| `sensor.hsem_missing_entities` | Count of missing input entities | Integer |
| `sensor.hsem_recommendation_interval` | Slot width + horizon info | Minutes |
| `sensor.hsem_update_interval` | Current polling interval | Minutes |

---

## Select entity

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
| `switch.hsem_ev_charge_disabled` | Disable EV charging |
| `switch.hsem_ev_second_charge_disabled` | Disable second EV charging |
