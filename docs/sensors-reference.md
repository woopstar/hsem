# HSEM Sensors Reference

Comprehensive reference for all entities exposed by the HSEM integration, including
attributes, states, and dashboard examples.

---

## Entity overview

HSEM exposes these entity types:

| Type       | Count | Description                                                   |
| ---------- | ----- | ------------------------------------------------------------- |
| **Sensor** | ~40   | Read-only state, plan, diagnostic, financial, and EV entities |
| **Select** | 2     | Force working mode override and Solcast likelihood selector   |
| **Switch** | ~12   | Toggle entities for EV settings, features, and ML options     |
| **Time**   | 2     | EV charge deadlines                                           |
| **Number** | 4     | Charge/discharge efficiency and EV target SoC controls        |

---

## Working mode sensor

The primary HSEM sensor. Exposes the active battery recommendation and carries
all planner output as attributes.

**Entity:** `sensor.hsem_working_mode`

| State                       | Meaning                                                                                                 |
| --------------------------- | ------------------------------------------------------------------------------------------------------- |
| `batteries_charge_grid`     | Battery charging from grid (forced by schedule or price)                                                |
| `batteries_charge_solar`    | Battery charging from PV surplus                                                                        |
| `batteries_discharge_mode`  | Battery discharging to cover house load                                                                 |
| `force_batteries_discharge` | Forced discharge to grid (excess export)                                                                |
| `force_export`              | Negative import price — all energy exported                                                             |
| `ev_smart_charging`         | EV charging load allocated                                                                              |
| `batteries_wait_mode`       | Battery idle; may allow self-consumption above the planner reserve depending on **Wait mode behaviour** |
| `time_passed`               | Slot is in the past                                                                                     |
| `missing_input_entities`    | Required HA entities unavailable                                                                        |

### Standard attributes

| Attribute                                   | Type                                               | Description                                                                                                                                                               |
| ------------------------------------------- | -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `batteries_current_capacity`                | float (kWh)                                        | Current usable battery capacity above discharge floor                                                                                                                     |
| `batteries_usable_capacity`                 | float (kWh)                                        | Usable battery capacity (rated − floor)                                                                                                                                   |
| `batteries_recommended_min_price_threshold` | float                                              | Discharge threshold from `calculate_recommended_threshold()`                                                                                                              |
| `batteries_capacity_loss_pct`               | float                                              | Configured capacity loss percentage                                                                                                                                       |
| `import_electricity_price_state`            | float                                              | Live import spot price (currency/kWh)                                                                                                                                     |
| `export_electricity_price_state`            | float                                              | Live export spot price (currency/kWh)                                                                                                                                     |
| `export_electricity_min_price`              | float                                              | Minimum export price for export gating                                                                                                                                    |
| `electricity_price_update_interval`         | int                                                | Price refresh interval (minutes)                                                                                                                                          |
| `house_consumption_power_state`             | float (W)                                          | Instantaneous house load                                                                                                                                                  |
| `house_power_includes_ev_charger_power`     | bool                                               | Whether EV load is baked into house power                                                                                                                                 |
| `net_consumption`                           | float (W)                                          | Net load (house − solar)                                                                                                                                                  |
| `net_consumption_with_ev`                   | float (W)                                          | Net load including EV                                                                                                                                                     |
| `solar_production_power_state`              | float (W)                                          | Instantaneous solar production                                                                                                                                            |
| `months_winter` / `months_summer`           | list[int]                                          | Configured winter/summer month ranges                                                                                                                                     |
| `batteries_enable_excess_export`            | bool                                               | Excess export gating enabled                                                                                                                                              |
| `batteries_excess_export_discharge_buffer`  | float                                              | Discharge buffer for excess export                                                                                                                                        |
| `batteries_forecast_reserve_pct`            | float                                              | Extra SoC points above the hardware floor reserved immediately after intentional battery export (issue #807, 0 = disabled)                                                |
| `phase_aware_charging_enabled`              | bool                                               | Live phase-aware grid-charge safety limiter enabled (issue #831, default `False`)                                                                                         |
| `grid_phase_power_w`                        | tuple[float \| None, float \| None, float \| None] | Live per-phase grid power `(a, b, c)` in Watts; `None` per phase when unavailable                                                                                         |
| `huawei_batteries_grid_charge_max_power_w`  | float \| None                                      | Live Huawei grid-charge maximum-power ceiling in Watts, or `None` if unconfigured/unavailable                                                                             |
| `primary_grid_charge_owned`                 | bool                                               | Whether HSEM currently believes it owns an armed Huawei grid-charge cap (issue #840). Drives the Error-mode emergency stop; never true for a charge armed outside of HSEM |
| `batteries_wait_mode_behavior`              | string                                             | Wait mode behaviour: `strict` or `self_consumption_with_reserve`                                                                                                          |
| `house_consumption_energy_weight_1d`        | float                                              | 1-day consumption prediction weight                                                                                                                                       |
| `house_consumption_energy_weight_3d`        | float                                              | 3-day weight                                                                                                                                                              |
| `house_consumption_energy_weight_7d`        | float                                              | 7-day weight                                                                                                                                                              |
| `house_consumption_energy_weight_14d`       | float                                              | 14-day weight                                                                                                                                                             |
| `last_updated`                              | string (ISO-8601)                                  | Last coordinator cycle timestamp                                                                                                                                          |
| `status`                                    | string                                             | `ok`, `read_only`, `wait`, or `error`                                                                                                                                     |
| `degraded_mode`                             | string                                             | `ok`, `degraded`, or `error`                                                                                                                                              |
| `hardware_writes_blocked`                   | bool                                               | Safety gate preventing hardware writes                                                                                                                                    |
| `apply_status`                              | string                                             | Last apply result: `ok`, `unverified`, `failed`, `skipped`                                                                                                                |
| `apply_failed_entities`                     | list[string]                                       | Entities that failed the last hardware write                                                                                                                              |
| `data_quality`                              | dict                                               | Structured price/PV and load-forecast completeness report                                                                                                                 |
| `force_working_mode_state`                  | string                                             | Active override mode or `auto`                                                                                                                                            |

### Plan output attributes

| Attribute                | Type         | Description                                  |
| ------------------------ | ------------ | -------------------------------------------- |
| `hourly_recommendation`  | dict \| null | The recommendation slot active **right now** |
| `hourly_recommendations` | list[dict]   | Full list of planner slots for the horizon   |

### `hourly_recommendations` slot structure

Each entry in the `hourly_recommendations` list is a dictionary with these keys:

| Key                                  | Type              | Description                                           |
| ------------------------------------ | ----------------- | ----------------------------------------------------- |
| `start`                              | string (ISO-8601) | Slot start timestamp                                  |
| `end`                                | string (ISO-8601) | Slot end timestamp                                    |
| `recommendation`                     | string \| null    | Working-mode value (see state table above)            |
| `import_price`                       | float             | Spot import price (local currency/kWh)                |
| `export_price`                       | float             | Spot export price (local currency/kWh)                |
| `avg_house_consumption_kwh`          | float             | Weighted spike-aware consumption estimate (kWh)       |
| `avg_house_consumption_1d_kwh`       | float             | 1-day window contribution (kWh)                       |
| `avg_house_consumption_3d_kwh`       | float             | 3-day window contribution (kWh)                       |
| `avg_house_consumption_7d_kwh`       | float             | 7-day window contribution (kWh)                       |
| `avg_house_consumption_14d_kwh`      | float             | 14-day window contribution (kWh)                      |
| `solcast_pv_estimate_kwh`            | float             | Forecast PV production for the slot (kWh)             |
| `estimated_net_consumption_kwh`      | float             | avg_consumption + ev_planned_load − pv_estimate (kWh) |
| `ev_planned_load_kwh`                | float             | Extra EV AC load added to net consumption (kWh, ≥ 0)  |
| `ev_accounted_load_kwh`              | float             | EV AC load already in house consumption (kWh, ≥ 0)    |
| `ev_total_planned_load_kwh`          | float             | Total EV AC load (planned + accounted, kWh, ≥ 0)      |
| `ev_charger_calculated_power`        | float             | Primary EV charger target AC power (W)                |
| `ev_second_charger_calculated_power` | float             | Second EV charger target AC power (W)                 |
| `estimated_cost_currency`            | float             | Estimated grid cost for the slot (local currency)     |
| `batteries_charged_kwh`              | float             | Energy scheduled to charge into battery (kWh)         |
| `batteries_discharged_kwh`           | float             | Energy drawn from battery by SoC simulation (kWh)     |
| `estimated_battery_capacity_kwh`     | float             | Remaining usable battery energy at slot end (kWh)     |
| `estimated_battery_soc_pct`          | float             | Simulated absolute SoC at slot end (0–100 %)          |
| `grid_import_kwh`                    | float             | Energy imported from grid (kWh)                       |
| `grid_export_kwh`                    | float             | Energy exported to grid (kWh)                         |
| `is_ev_surplus_only_slot`            | bool              | Slot restricted to EV surplus-only charging           |

### Extended attributes (when enabled)

When the `switch.hsem_extended_attributes` switch is on, additional entity-ID
attributes are exposed. These reference the raw HA entity IDs for troubleshooting:

| Attribute                                      | Description                            |
| ---------------------------------------------- | -------------------------------------- |
| `import_electricity_price_sensor_entity`       | Import price sensor entity ID          |
| `export_electricity_price_sensor_entity`       | Export price sensor entity ID          |
| `ev_charger_power_entity`                      | Primary EV charger power entity ID     |
| `ev_charger_status_entity`                     | Primary EV charger status entity ID    |
| `ev_soc_entity`                                | Primary EV SoC entity ID               |
| `ev_connected_entity`                          | Primary EV connected entity ID         |
| `ev_second_charger_power_entity`               | Second EV charger power entity ID      |
| `ev_second_charger_status_entity`              | Second EV charger status entity ID     |
| `ev_second_soc_entity`                         | Second EV SoC entity ID                |
| `ev_second_connected_entity`                   | Second EV connected entity ID          |
| `force_working_mode_entity`                    | Force working mode entity ID           |
| `house_consumption_power_entity`               | House consumption power entity ID      |
| `solar_production_power_entity`                | Solar production power entity ID       |
| `solcast_pv_forecast_forecast_today_entity`    | Solcast today forecast entity ID       |
| `solcast_pv_forecast_forecast_tomorrow_entity` | Solcast tomorrow forecast entity ID    |
| (plus all `huawei_solar_*` entity IDs)         | Huawei battery and inverter entity IDs |
| `recommendation_interval_minutes`              | Slot width in minutes                  |
| `recommendation_interval_length`               | Number of slots in the horizon         |
| `unique_id`                                    | Integration unique ID                  |
| `update_interval`                              | Polling interval in minutes            |
| `read_only`                                    | Whether read-only mode is active       |

### EV attributes

| Attribute                                     | Type      | Description                                                                                                                  |
| --------------------------------------------- | --------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `ev_charger_power_state`                      | float (W) | Primary EV charger instantaneous power                                                                                       |
| `ev_charger_status_state`                     | bool      | Primary EV currently charging                                                                                                |
| `ev_soc_state`                                | float (%) | Primary EV current SoC                                                                                                       |
| `ev_soc_target_state`                         | float (%) | Primary EV target SoC                                                                                                        |
| `ev_connected_state`                          | bool      | Primary EV plugged in                                                                                                        |
| `ev_allow_charge_past_target_soc`             | bool      | Allow charging past target                                                                                                   |
| `ev_past_target_confidence_factor`            | float     | Confidence factor (0.0–1.0) applied to the avoided-future-import valuation of past-target charging                           |
| `ev_charger_max_discharge_power_state`        | float (W) | Battery discharge ceiling while this EV charges (once permission below is granted)                                           |
| `ev_charger_force_max_discharge_power`        | bool      | Permission for the Huawei battery to discharge while this EV charges; without it, discharge is 0 W whenever the EV is active |
| `ev_second_enabled`                           | bool      | Second EV integration enabled                                                                                                |
| `ev_second_charger_power_state`               | float (W) | Second EV charger power                                                                                                      |
| `ev_second_charger_status_state`              | bool      | Second EV charging                                                                                                           |
| `ev_second_soc_state`                         | float (%) | Second EV SoC                                                                                                                |
| `ev_second_soc_target_state`                  | float (%) | Second EV target SoC                                                                                                         |
| `ev_second_connected_state`                   | bool      | Second EV plugged in                                                                                                         |
| `ev_second_allow_charge_past_target_soc`      | bool      | Second EV past-target flag                                                                                                   |
| `ev_second_past_target_confidence_factor`     | float     | Second EV past-target confidence factor (0.0–1.0)                                                                            |
| `ev_second_charger_max_discharge_power_state` | float (W) | Second EV: same discharge ceiling semantics as the primary EV                                                                |
| `ev_second_charger_force_max_discharge_power` | bool      | Second EV: same discharge permission semantics as the primary EV                                                             |

### Huawei battery attributes

| Attribute                                                             | Type        | Description                  |
| --------------------------------------------------------------------- | ----------- | ---------------------------- |
| `huawei_solar_batteries_charging_cutoff_capacity_state`               | float (%)   | Inverter charging cutoff SoC |
| `huawei_solar_batteries_grid_charge_cutoff_soc_state`                 | float (%)   | Grid charge cutoff SoC       |
| `huawei_solar_batteries_maximum_charging_power_state`                 | float (W)   | Maximum charge power         |
| `huawei_solar_batteries_maximum_discharging_power_state`              | float (W)   | Maximum discharge power      |
| `huawei_solar_batteries_rated_capacity_max_state`                     | float (Wh)  | Rated battery capacity       |
| `huawei_solar_batteries_rated_capacity_min_state`                     | float (kWh) | Discharge floor capacity     |
| `huawei_solar_batteries_state_of_capacity_state`                      | float (%)   | Battery SoC                  |
| `huawei_solar_batteries_tou_charging_and_discharging_periods_periods` | list        | Parsed TOU periods           |
| `huawei_solar_batteries_tou_charging_and_discharging_periods_state`   | string      | Raw TOU entity state         |
| `huawei_solar_batteries_working_mode_state`                           | string      | Inverter working mode        |
| `huawei_solar_inverter_active_power_control_state_state`              | string      | APC mode                     |
| `huawei_solar_batteries_excess_pv_energy_use_in_tou_state`            | string      | Excess PV in TOU setting     |

---

## Plan explanation sensor

Displays the planner's strategy rationale and per-candidate cost breakdown.

**Entity:** `sensor.hsem_plan_explanation`

| Key attribute                                 | Description                                                      |
| --------------------------------------------- | ---------------------------------------------------------------- |
| **State**                                     | Winning candidate name: `"milp"`, `"passive"`, `"no_action"`     |
| `selected_strategy`                           | Human-readable description (e.g. `"charge_grid_discharge_peak"`) |
| `winner_name`                                 | Winning candidate name (same as state)                           |
| `summary`                                     | One-sentence human-readable reason                               |
| `score`                                       | Estimated savings vs doing nothing (currency)                    |
| `estimated_total_cost`                        | Net grid cost for the horizon                                    |
| `price_spread`                                | Max minus min import price (arbitrage potential)                 |
| `peak_import_price` / `off_peak_import_price` | Price extremes                                                   |
| `forecast_pv_kwh`                             | Total PV forecast for the horizon                                |
| `forecast_net_consumption_kwh`                | Total load minus PV                                              |
| `battery_soc_pct` / `battery_soc_at_end_pct`  | Starting and ending SoC                                          |
| `constraints`                                 | Active flags (`winter_month`, `excess_export_enabled`, etc.)     |
| `rejected_plans`                              | Alternatives with name, reason, and full cost breakdown          |
| `hysteresis_active`                           | Whether plan-level hysteresis was applied                        |
| `hysteresis_reason`                           | Explanation of hysteresis decision                               |

---

## Financial sensors

Cumulative monetary sensors that track grid import cost and export revenue.
All three use `total` because signed prices may make their values decrease.

**Entities:**

- `sensor.hsem_export_income` — Cumulative export revenue
- `sensor.hsem_import_cost` — Cumulative import cost
- `sensor.hsem_net_grid_balance` — Export income minus import cost

### `sensor.hsem_export_income`

| Property         | Value                                      |
| ---------------- | ------------------------------------------ |
| **Type**         | `sensor`                                   |
| **State class**  | `total`                                    |
| **State**        | Cumulative export revenue (local currency) |
| **Device class** | `monetary`                                 |

### `sensor.hsem_import_cost`

| Property         | Value                                   |
| ---------------- | --------------------------------------- |
| **Type**         | `sensor`                                |
| **State class**  | `total`                                 |
| **State**        | Cumulative import cost (local currency) |
| **Device class** | `monetary`                              |

### `sensor.hsem_net_grid_balance`

| Property         | Value                                                            |
| ---------------- | ---------------------------------------------------------------- |
| **Type**         | `sensor`                                                         |
| **State class**  | `total`                                                          |
| **State**        | Net grid balance (`export_income − import_cost`, local currency) |
| **Device class** | `monetary`                                                       |

**Template example:**

```jinja2
{{ states('sensor.hsem_net_grid_balance') | float | round(2) }}
```

---

## Prediction accuracy sensor

Tracks prediction accuracy across multiple horizons — solar, load, and battery SoC — up to 30 days.

**Entity:** `sensor.hsem_prediction_accuracy`

| State        | Meaning                                   |
| ------------ | ----------------------------------------- |
| `soc_mae_7d` | 7-day battery SoC MAE (percentage points) |

| Attribute      | Unit | Description                                     |
| -------------- | ---- | ----------------------------------------------- |
| `soc_mae_7d`   | pp   | 7-day SoC Mean Absolute Error                   |
| `soc_mae_30d`  | pp   | 30-day SoC Mean Absolute Error                  |
| `solar_mape`   | %    | Solar forecast MAPE                             |
| `load_mae_kwh` | kWh  | Load Mean Absolute Error                        |
| `action_mix`   | dict | Distribution of planner actions over the window |

---

## Solar confidence sensor

Per-hour PV forecast accuracy factors and confidence percentile.

**Entity:** `sensor.hsem_solar_confidence`

| Attribute        | Description                                      |
| ---------------- | ------------------------------------------------ |
| **State**        | Mean accuracy factor across the horizon (ratio)  |
| `factors`        | dict — per-hour correction factors for each slot |
| `confidence_pct` | float — confidence percentile (e.g. 50 = median) |

**Template example:**

```jinja2
{{ states('sensor.hsem_solar_confidence') | float | round(3) }}
Confidence: {{ state_attr('sensor.hsem_solar_confidence', 'confidence_pct') }}%
```

---

## EV charging plan sensors

Diagnostic sensors displaying the EV charging plan details.

**Entities:**

- `sensor.hsem_ev_optimal_charging_plan` — Primary EV, created only when `hsem_ev_planned_load_enabled` is set
- `sensor.hsem_ev_second_optimal_charging_plan` — Second EV, created only when `hsem_ev_second_planned_load_enabled` is set (issue #859)

| State                     | Meaning                                  |
| ------------------------- | ---------------------------------------- |
| `not_connected`           | EV is not plugged in                     |
| `smart_charging_disabled` | Smart charging turned off                |
| `fully_charged`           | Already at or above target SoC           |
| `charging`                | EV scheduled to charge in current slot   |
| `waiting`                 | Connected but no active charging slot    |
| `unavailable`             | Not configured or capacity/power is zero |

**Key attributes:**

| Attribute                    | Description                                   |
| ---------------------------- | --------------------------------------------- |
| `battery_capacity_kwh`       | EV battery nameplate capacity                 |
| `charge_power_kw`            | Charger AC output power                       |
| `current_soc` / `target_soc` | EV SoC values                                 |
| `ev_connected`               | Whether vehicle is plugged in                 |
| `total_kwh_needed`           | Energy needed to reach target                 |
| `deadline`                   | ISO-8601 charging deadline                    |
| `charging_slots`             | List of allocated charging slots with details |
| `planned_load_by_slot`       | Dict of slot → kWh load                       |
| `data_quality`               | Diagnostic warnings                           |

---

## EV charger calculated power sensors

Diagnostic sensors exposing the planner's calculated EV charger power for the
**current slot** as their state, so it can be graphed and used in automations
without parsing the working-mode sensor attributes.

**Entities:**

- `sensor.hsem_ev_charger_calculated_power` — Primary EV, created only when `hsem_ev_planned_load_enabled` is set
- `sensor.hsem_ev_second_charger_calculated_power` — Second EV, created only when `hsem_ev_second_planned_load_enabled` is set (issue #859)

| State | Unit | Description                                                                          |
| ----- | ---- | ------------------------------------------------------------------------------------ |
| ≥ 0   | W    | Target AC power for the charger in the active slot (`0` when no charging is planned) |

**Key attributes:**

| Attribute                   | Description                                 |
| --------------------------- | ------------------------------------------- |
| `slot_start` / `slot_end`   | ISO-8601 window of the active planning slot |
| `ev_total_planned_load_kwh` | Total EV AC load planned for the slot (kWh) |

These mirror `ev_charger_calculated_power` and
`ev_second_charger_calculated_power` from the `hourly_recommendation` attribute
of the working-mode sensor.

---

## EV charger current limit sensors

Diagnostic sensors publishing the planner's per-slot EV charging ceiling in
**whole amps**, for an external current controller to consume. HSEM owns the
economics (how many amps are worth drawing this slot); the external
controller keeps final authority for fuse safety and may only ramp _within_
the published ceiling. Conversion always rounds down and is phase-aware (see
[planner-spec.md](planner-spec.md) _Published charging ceiling and
stranded-residue re-portioning_).

**Entities:**

- `sensor.hsem_ev_charger_current_limit` — Primary EV, created only when `hsem_ev_planned_load_enabled` is set
- `sensor.hsem_ev_second_charger_current_limit` — Second EV, created only when `hsem_ev_second_planned_load_enabled` is set (issue #859)

| State | Unit | Description                                                                          |
| ----- | ---- | ------------------------------------------------------------------------------------ |
| ≥ 0   | A    | Ceiling for the active slot, floored to whole amps (`0` when no charging is planned) |

**Key attributes:**

| Attribute        | Description                                                     |
| ---------------- | --------------------------------------------------------------- |
| `phase_topology` | The charger's configured phase topology used for the conversion |
| `schedule`       | Up to 24 future slots, each `{start, current_a, power_w}`       |

Startup and a failed coordinator cycle are fail-closed: the entity is
unavailable and its native value is `0`. A previously restored positive
state is never accepted as a live actuator command (issue #789) — only a
successful, live coordinator recommendation can publish a positive ceiling.

---

## Daily plan-vs-actual sensor

Diagnostic sensor tracking daily cumulative plan-vs-actual energy deviations.

**Entity:** `sensor.hsem_daily_plan_vs_actual`

Tracks planned kWh vs actual kWh for import, export, PV, consumption, and battery
throughput on a per-calendar-day basis using cumulative energy meter readings.

---

## EV charging active sensor

Boolean sensor indicating whether any EV is actively drawing power.

**Entity:** `sensor.hsem_ev_charging_sensor` — created only when `hsem_ev_planned_load_enabled` is set (issue #859)

| State | Meaning                     |
| ----- | --------------------------- |
| `on`  | At least one EV is charging |
| `off` | No EV is charging           |

**Key attributes:**

| Attribute                                                                  | Meaning                                                                                       |
| -------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| `ev_soc_pct` / `ev_second_soc_pct`                                         | Vehicle integration's reported SoC                                                            |
| `ev_effective_soc_pct` / `ev_second_effective_soc_pct`                     | Reported SoC plus bounded delivered-energy credit when session identity is valid, else `None` |
| `ev_delivered_energy_credit_kwh` / `ev_second_delivered_energy_credit_kwh` | Battery-side measured energy not yet reflected by vehicle telemetry                           |
| `ev_power_w` / `ev_second_power_w`                                         | Live measured charger power                                                                   |

Delivered-energy credit is in memory only (issue #789). It clears on
disconnect, restart, unsafe session/SoC identity changes, and battery
capacity changes. Invalid power readings and excessive sample gaps add no
energy and break the integration interval without erasing credit already
validated in the same identified session. See
[planner-spec.md](planner-spec.md) _Session EV invariant_ and
`utils/ev_delivered_energy.py`.

---

## Battery SoC sensor

Snapshot of the battery state of charge with optional learned capacity tracking.

**Entity:** `sensor.hsem_battery_soc_sensor`

| State | Unit | Description            |
| ----- | ---- | ---------------------- |
| 0–100 | %    | Battery SoC percentage |

**Key attributes:**

| Attribute              | Unit | Description                                                             |
| ---------------------- | ---- | ----------------------------------------------------------------------- |
| `learned_capacity_kwh` | kWh  | Learned usable battery capacity from charge/discharge cycles            |
| `capacity_samples`     | int  | Number of charge/discharge samples contributing to the learned capacity |

---

## Dynamic discharge floor

Controls and reports the effective discharge floor SoC, which the planner uses as a minimum battery SoC when the dynamic floor feature is enabled.

**Entities:**

- `sensor.hsem_effective_discharge_floor` — Current effective floor SoC (%)
- `switch.hsem_dynamic_discharge_floor` — Enable/disable the dynamic floor feature

### `sensor.hsem_effective_discharge_floor`

| Property  | Value                                            |
| --------- | ------------------------------------------------ |
| **Type**  | `sensor`                                         |
| **State** | Current effective discharge floor SoC percentage |
| **Unit**  | %                                                |

### `switch.hsem_dynamic_discharge_floor`

| Property  | Value                                               |
| --------- | --------------------------------------------------- |
| **Type**  | `switch`                                            |
| **State** | `on` (dynamic floor active) or `off` (static floor) |

**Template example:**

```jinja2
{% if is_state('switch.hsem_dynamic_discharge_floor', 'on') %}
  Dynamic floor: {{ states('sensor.hsem_effective_discharge_floor') }}%
{% endif %}
```

---

## OCPP charger sensors

Sensors providing live status and diagnostics for an OCPP-compliant EV charger connected via the integrated OCPP server. HSEM runs **one OCPP server per EV** (primary EV on port 9000 by default, second EV on port 9001). The primary sensors below are only created when `hsem_ocpp_enabled` is set; the `_second` variants exist only when the second EV is configured and the second OCPP server is enabled (issue #859).

**Configuration keys** (set in config flow):

| Key                        | Description                                                     |
| -------------------------- | --------------------------------------------------------------- |
| `hsem_ocpp_enabled`        | Enable the OCPP server                                          |
| `hsem_ocpp_port`           | TCP port for the primary EV's OCPP WebSocket connections        |
| `hsem_ocpp_cpid`           | OCPP charge point identifier                                    |
| `hsem_ocpp_start_window_s` | Seconds before charge deadline to start charging                |
| `hsem_ocpp_stop_window_s`  | Seconds after charge deadline to stop charging                  |
| `hsem_ocpp_second_enabled` | Enable the dedicated second-EV OCPP server (requires second EV) |
| `hsem_ocpp_second_port`    | TCP port for the second EV's OCPP WebSocket connections         |
| `hsem_ocpp_second_cpid`    | Second EV charger's OCPP charge point identifier                |

### `sensor.hsem_ocpp_charger_status`

| Property       | Value                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| -------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Type**       | `sensor`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| **State**      | `not_configured` (this EV's OCPP server isn't enabled), `disconnected` (enabled, no charger connected), or the live connection/charging state (`Available`, `Preparing`, `Charging`, `Finishing`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| **Attributes** | `listening` (bool — server socket bound), `port`, `requested_current_a` (whole amps in the last `SetChargingProfile` sent to the charger, `None` until the first profile has been sent — issue #886), `anti_flap_state` (`"idle"`/`"starting"`/`"charging"`/`"stopping"` — HSEM's own anti-flap state machine, issue #892), `stalled` (bool — `True` when the active session has been stuck reporting `"SuspendedEVSE"`/`"Faulted"`/`"Unavailable"` for over 5 minutes despite an open transaction; `"SuspendedEV"`, an EV-decided pause, is never flagged — issue #894), `url` (best-effort `ws://<host>:<port>/<cpid>` for the EVSE's OCPP config, including the configured charge-point ID, when HA can resolve a reachable host) — present only when this EV's OCPP server is enabled; plus one entry per connected charger CPID with `status`, `power_w`, `transaction_id`, `connected_at`, `status_changed_at` (issue #894) |

### `sensor.hsem_ocpp_charger_power`

| Property         | Value                    |
| ---------------- | ------------------------ |
| **Type**         | `sensor`                 |
| **State**        | Live charging power (kW) |
| **Device class** | `power`                  |
| **Unit**         | kW                       |

### `sensor.hsem_ocpp_charger_info`

| Property       | Value                                                  |
| -------------- | ------------------------------------------------------ |
| **Type**       | `sensor`                                               |
| **State**      | Charger summary string                                 |
| **Attributes** | `vendor`, `model`, `firmware_version`, `serial_number` |

### `sensor.hsem_ocpp_charger_sessions`

| Property       | Value                                                                      |
| -------------- | -------------------------------------------------------------------------- |
| **Type**       | `sensor`                                                                   |
| **State**      | Number of completed sessions                                               |
| **Attributes** | `sessions` — list of completed session logs (start time, energy, duration) |

### Second-EV variants

When the second EV is configured with its own OCPP server, four additional
diagnostic sensors are created with `_second` suffixed entity IDs:

- `sensor.hsem_ocpp_charger_status_sensor_second`
- `sensor.hsem_ocpp_charger_power_sensor_second`
- `sensor.hsem_ocpp_charger_info_sensor_second`
- `sensor.hsem_ocpp_charger_sessions_sensor_second`

These mirror the primary sensors but read from the second EV's dedicated
OCPP server.

**Template example:**

```jinja2
{{ states('sensor.hsem_ocpp_charger_status') }}
{{ states('sensor.hsem_ocpp_charger_power') | float | round(2) }} kW
```

---

## Savings tracker sensor

Tracks actual vs missed savings over a rolling 90-day window.

**Entity:** `sensor.hsem_savings_tracker`

| Property       | Value                                                                            |
| -------------- | -------------------------------------------------------------------------------- |
| **Type**       | `sensor`                                                                         |
| **State**      | Current day savings (local currency)                                             |
| **Attributes** | `actual_savings`, `missed_savings`, `total_savings`, `log` (90-day rolling list) |

**Template example:**

```jinja2
Actual: {{ state_attr('sensor.hsem_savings_tracker', 'actual_savings') }}
Missed: {{ state_attr('sensor.hsem_savings_tracker', 'missed_savings') }}
```

---

## PV curtailment sensor

Detects when the inverter is actively curtailing PV production.

**Entity:** `sensor.hsem_pv_curtailment_sensor`

| Property            | Value                                                       |
| ------------------- | ----------------------------------------------------------- |
| **Type**            | `sensor`                                                    |
| **State**           | `curtailed` (PV being limited) or `normal` (no curtailment) |
| **Entity category** | `diagnostic`                                                |

**Template example:**

```jinja2
{% if is_state('sensor.hsem_pv_curtailment_sensor', 'curtailed') %}
  PV is being curtailed
{% endif %}
```

---

## Diagnostic sensors

| Entity                                           | Display Name                    | Purpose                                         | State / Value                           |
| ------------------------------------------------ | ------------------------------- | ----------------------------------------------- | --------------------------------------- |
| `sensor.hsem_applier_status_sensor`              | Inverter Apply Status           | Hardware write success/failure                  | `ok`, `unverified`, `failed`, `skipped` |
| `sensor.hsem_battery_soc_sensor`                 | Battery State of Charge         | Battery SoC snapshot                            | Percentage (0–100)                      |
| `sensor.hsem_daily_plan_vs_actual`               | Daily Plan vs Actual            | Daily energy plan-vs-actual tracking            | Dict with cumulative metrics            |
| `sensor.hsem_degraded_mode_sensor`               | System Health                   | Overall system health                           | `ok`, `degraded`, `error`               |
| `sensor.hsem_ev_charger_calculated_power`        | EV Charger Calculated Power     | Planner target power for primary EV charger     | Watts (W)                               |
| `sensor.hsem_ev_second_charger_calculated_power` | EV 2 Charger Calculated Power   | Planner target power for second EV charger      | Watts (W)                               |
| `sensor.hsem_ev_charger_current_limit`           | EV Charger Current Limit        | Planner charging ceiling for primary EV charger | Amps (A)                                |
| `sensor.hsem_ev_second_charger_current_limit`    | EV 2 Charger Current Limit      | Planner charging ceiling for second EV charger  | Amps (A)                                |
| `sensor.hsem_ev_charging_sensor`                 | EV Charging Active              | Any EV actively charging                        | `on`, `off`                             |
| `sensor.hsem_ev_optimal_charging_plan`           | EV Optimal Charging Plan        | Primary EV plan state                           | `charging`, `waiting`, etc.             |
| `sensor.hsem_ev_second_optimal_charging_plan`    | EV Second Optimal Charging Plan | Second EV plan state                            | `charging`, `waiting`, etc.             |
| `sensor.hsem_force_mode_sensor`                  | Force Working Mode              | Override active indicator                       | `auto` or override mode name            |
| `sensor.hsem_solar_confidence_sensor`            | Solar Forecast Confidence       | Per-hour PV forecast accuracy factors           | Mean factor (ratio)                     |
| `sensor.hsem_hardware_writes_sensor`             | Hardware Writes                 | Writes allowed/blocked by safety gate           | `allowed`, `blocked`                    |
| `sensor.hsem_read_only_sensor`                   | Read-Only Mode                  | Read-only mode indicator                        | `on`, `off`                             |
| `sensor.hsem_net_consumption_sensor`             | Net Consumption                 | Net load (house minus solar)                    | Watts (W)                               |
| `sensor.hsem_last_updated_sensor`                | Last Updated                    | Last coordinator cycle timestamp                | ISO-8601 timestamp                      |
| `sensor.hsem_next_update_sensor`                 | Next Update                     | Next scheduled coordinator cycle                | ISO-8601 timestamp                      |
| `sensor.hsem_missing_entities_sensor`            | Missing Input Entities          | Count of missing input entities                 | Integer                                 |
| `sensor.hsem_plan_explanation_sensor`            | Plan Explanation                | Planner strategy and cost breakdown             | Winning candidate name                  |
| `sensor.hsem_prediction_accuracy`                | Prediction Accuracy             | Multi-horizon forecast accuracy                 | `soc_mae_7d`                            |
| `sensor.hsem_forecast_accuracy_sensor`           | Forecast Accuracy               | PV and load forecast MAE                        | kWh                                     |
| `sensor.hsem_recommendation_interval_sensor`     | Recommendation Interval         | Slot width and horizon info                     | Minutes                                 |
| `sensor.hsem_update_interval_sensor`             | Update Interval                 | Current polling interval                        | Minutes                                 |
| `sensor.hsem_working_mode`                       | Working Mode                    | Active battery recommendation                   | Working mode state                      |
| `sensor.hsem_export_income`                      | Export Income                   | Cumulative export revenue                       | Monetary (total)                        |
| `sensor.hsem_import_cost`                        | Import Cost                     | Cumulative import cost                          | Monetary (total)                        |
| `sensor.hsem_net_grid_balance`                   | Net Grid Balance                | Export income minus import cost                 | Monetary (total)                        |
| `sensor.hsem_effective_discharge_floor`          | Effective Discharge Floor       | Current effective floor SoC                     | Percentage                              |
| `sensor.hsem_ocpp_charger_status`                | OCPP Charger Status             | Charger connection/charging state               | String                                  |
| `sensor.hsem_ocpp_charger_power`                 | OCPP Charger Power              | Live charging power                             | kW                                      |
| `sensor.hsem_ocpp_charger_info`                  | OCPP Charger Info               | Vendor, model, firmware, serial                 | String                                  |
| `sensor.hsem_ocpp_charger_sessions`              | OCPP Charger Sessions           | Completed session log                           | Integer                                 |
| `sensor.hsem_savings_tracker`                    | Savings Tracker                 | Actual vs missed savings (90-day)               | Monetary                                |
| `sensor.hsem_pv_curtailment_sensor`              | PV Curtailment                  | PV curtailment detection                        | `curtailed` / `normal`                  |

---

## Select entities

### Force working mode

**Entity:** `select.hsem_force_working_mode`

| Option                      | Description                                 |
| --------------------------- | ------------------------------------------- |
| `auto`                      | Normal operation — planner controls battery |
| `batteries_charge_grid`     | Force grid charge                           |
| `batteries_charge_solar`    | Force solar charge                          |
| `batteries_discharge_mode`  | Force discharge to house                    |
| `batteries_wait_mode`       | Force idle                                  |
| `ev_smart_charging`         | Force EV charging                           |
| `force_batteries_discharge` | Force discharge to grid                     |
| `force_export`              | Force all energy to export                  |

### Solcast PV forecast likelihood

**Entity:** `select.hsem_solcast_likelihood`

Selects which Solcast likelihood scenario to use for PV forecasts (e.g. `p10`, `p50`, `p90`).
This setting is also configurable in the options flow.

---

## Switch entities

EV switches are only created when the corresponding EV's planned load
(managed charging) is enabled — `hsem_ev_planned_load_enabled` for the
primary EV, `hsem_ev_second_planned_load_enabled` for the second EV
(issue #859). All other switches are always created.

| Entity                                    | Purpose                                        |
| ----------------------------------------- | ---------------------------------------------- |
| `switch.hsem_read_only`                   | Block all hardware writes                      |
| `switch.hsem_extended_attributes`         | Enable extended diagnostic attributes          |
| `switch.hsem_verbose_logging`             | Enable verbose logging                         |
| `switch.hsem_ev_force_discharge`          | Force EV maximum discharge power               |
| `switch.hsem_ev_smart_charging`           | Enable smart EV charging scheduling            |
| `switch.hsem_ev_force_charge_now`         | Force immediate EV charging                    |
| `switch.hsem_ev_second_smart_charging`    | Enable smart charging for second EV            |
| `switch.hsem_ev_second_force_charge_now`  | Force immediate second EV charging             |
| `switch.hsem_ml_consumption`              | Enable ML-based consumption prediction         |
| `switch.hsem_ml_sequential`               | Enable sequential (intra-day momentum) ML mode |
| `switch.hsem_dynamic_discharge_floor`     | Enable dynamic discharge floor                 |
| `switch.hsem_ev_auto_full_negative_price` | Auto-Full EV on negative price                 |

---

## Number entities

`number.hsem_ev_target_soc` is only created when `hsem_ev_planned_load_enabled`
is set; `number.hsem_ev_second_target_soc` only when
`hsem_ev_second_planned_load_enabled` is set (issue #859). The battery
efficiency numbers are always created.

| Entity                                     | Purpose                      | Range   |
| ------------------------------------------ | ---------------------------- | ------- |
| `number.hsem_battery_charge_efficiency`    | Battery charge efficiency    | 1–100 % |
| `number.hsem_battery_discharge_efficiency` | Battery discharge efficiency | 1–100 % |
| `number.hsem_ev_target_soc`                | Primary EV target SoC        | 0–100 % |
| `number.hsem_ev_second_target_soc`         | Second EV target SoC         | 0–100 % |

---

## Time entities

`time.hsem_ev_deadline` is only created when `hsem_ev_planned_load_enabled`
is set; `time.hsem_ev_second_deadline` only when
`hsem_ev_second_planned_load_enabled` is set (issue #859).

| Entity                         | Purpose                    |
| ------------------------------ | -------------------------- |
| `time.hsem_ev_deadline`        | Primary EV charge deadline |
| `time.hsem_ev_second_deadline` | Second EV charge deadline  |

---

## Config flow additions

### Quick setup (#610)

The config flow includes a `quick_setup` step that auto-detects HA entities
(Huawei inverter, EV charger, Solcast forecasts, price sensors) to reduce
manual configuration.

### OCPP server

OCPP configuration is exposed through the config flow with these keys:
`hsem_ocpp_enabled`, `hsem_ocpp_port`, `hsem_ocpp_cpid`,
`hsem_ocpp_start_window_s`, `hsem_ocpp_stop_window_s`. See
[OCPP charger sensors](#ocpp-charger-sensors) above.

---

## Services

### `hsem.create_dashboard`

Logs the path to the bundled HSEM dashboard YAML and provides import
instructions. The dashboard includes cards for working mode, battery SoC,
financial sensors, EV status, and plan explanation. Import the YAML manually
via **Developer Tools → Services** or **Settings → Dashboards**.

> The service does **not** create or modify Home Assistant dashboards
> automatically; it only surfaces the bundled YAML path for manual import.

---

## Internal additions (no new sensors)

These changes are internal to the planner and do not expose new entities:

- **Weekday/weekend profiling (#612):** `WeekdayProfile` module-level
  singleton distinguishes weekday vs weekend consumption patterns for
  more accurate load forecasting.
- **Session EV charging (#615):** `EVConfig.session_charge_kw` field
  allows per-session charge power configuration for EV co-optimisation.

---

## Data quality attribute

The `data_quality` dict on the working mode sensor is the serialized planner
input-completeness report. Example structure:

```json
{
  "is_complete": false,
  "load_forecast_ready": false,
  "load_forecast_reason": "zero_forecast_with_live_demand",
  "horizon_has_tomorrow": true,
  "horizon_days": 2,
  "tomorrow_price_missing_hours": [],
  "tomorrow_pv_missing_hours": [],
  "day2_price_missing_hours": [],
  "day2_pv_missing_hours": [],
  "today_price_missing_hours": [],
  "today_pv_missing_hours": []
}
```

`load_forecast_ready` is false when average-sensor provenance or a populated
future value is missing, non-finite, or negative. A complete zero profile is
valid while live house demand is at most 50 W; above that threshold the reason
is `zero_forecast_with_live_demand`. `is_complete` requires both load readiness
and complete price/PV inputs.
