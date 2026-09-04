# HSEM Config Flow Reference

This document describes every step in the HSEM configuration and options flows.

---

## Config flow steps

The config flow is a multi-step wizard. Steps appear in this order:

```
quick_setup → init → prices → months → solcast → huawei_solar
    → battery_economics → power → ev → [ev_second] → ev_planned_load
    → [ev_second_planned_load] → ocpp → batteries_schedules
    → batteries_wait_mode → batteries_excess_export → weighted_values
    → energy_and_ml
```

### Step: `quick_setup`

Initial entity auto-detection step. Scans available HA entities and
pre-populates the config flow with discovered sensors and devices.

| Field              | Key | Default | Description                                            |
| ------------------ | --- | ------- | ------------------------------------------------------ |
| Confirm & Continue | —   | —       | Accept auto-detected entities and skip to final review |
| Advanced Setup     | —   | —       | Proceed through the full step-by-step wizard           |

When the user selects "Confirm & Continue", all auto-detected entities
are saved and the config flow jumps directly to `energy_and_ml` for
review and confirmation. Selecting "Advanced Setup" walks through
every step in order so individual entities can be customised.

### Step: `init`

| Field                   | Key                                    | Default                            | Description                                   |
| ----------------------- | -------------------------------------- | ---------------------------------- | --------------------------------------------- |
| Device name             | `device_name`                          | `"Huawei Solar Energy Management"` | Friendly name for the integration             |
| Update interval         | `hsem_update_interval`                 | 5 minutes                          | Coordinator polling interval                  |
| Recommendation interval | `hsem_recommendation_interval_minutes` | 15 minutes                         | Planner slot resolution (15 or 60 minutes)    |
| Planning horizon        | `hsem_recommendation_interval_length`  | 48 hours                           | Physical horizon: 12, 24, 36, 48, or 72 hours |
| Read-only mode          | `hsem_read_only`                       | `False`                            | Block all hardware writes when enabled        |
| Verbose logging         | `hsem_verbose_logging`                 | `False`                            | Enable debug-level planner logging            |

### Step: `prices`

Generic electricity price sensor configuration. Provider-agnostic — supports
Energi Data Service, Nordpool, Amber Electric, and any other price source.

| Field                        | Key                                             | Default                                 | Description                                                                                                                                                                                                                                                                              |
| ---------------------------- | ----------------------------------------------- | --------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Import price sensor          | `hsem_import_electricity_price_sensor`          | `sensor.energi_data_service`            | HA entity for import price                                                                                                                                                                                                                                                               |
| Export price sensor          | `hsem_export_electricity_price_sensor`          | `sensor.energi_data_service_produktion` | HA entity for export price                                                                                                                                                                                                                                                               |
| Import price forecast sensor | `hsem_import_electricity_price_forecast_sensor` | —                                       | Optional dedicated import forecast sensor                                                                                                                                                                                                                                                |
| Export price forecast sensor | `hsem_export_electricity_price_forecast_sensor` | —                                       | Optional dedicated export forecast sensor                                                                                                                                                                                                                                                |
| Export min price             | `hsem_export_electricity_min_price`             | 0.0                                     | Minimum export price for intentional battery-to-grid discharge. The inverter no longer throttles the grid feed-in limit for positive prices; surplus PV export is always allowed (issue #767). Negative export prices still trigger a physical block because exporting then costs money. |
| Price update interval        | `hsem_electricity_price_update_interval`        | 15 minutes                              | How often the price source publishes (15, 30, or 60)                                                                                                                                                                                                                                     |

### Step: `months`

Seasonal month classification.

| Field         | Key                  | Default                 | Description                                                                                                                                                           |
| ------------- | -------------------- | ----------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Summer months | `hsem_months_summer` | `[4, 5, 6, 7, 8, 9]`    | Months classified as summer (derived as the complement of winter)                                                                                                     |
| Winter months | `hsem_months_winter` | `[1, 2, 3, 10, 11, 12]` | Months classified as winter. Selecting **all 12 months** is allowed (issue #725) — this keeps Winter/Spring (TOU) mode active year-round and the summer set is empty. |

### Step: `solcast`

PV forecast sensor configuration.

| Field               | Key                                            | Default                                        | Description                          |
| ------------------- | ---------------------------------------------- | ---------------------------------------------- | ------------------------------------ |
| Forecast today      | `hsem_solcast_pv_forecast_forecast_today`      | `sensor.solcast_pv_forecast_forecast_today`    | Today's Solcast forecast             |
| Forecast tomorrow   | `hsem_solcast_pv_forecast_forecast_tomorrow`   | `sensor.solcast_pv_forecast_forecast_tomorrow` | Tomorrow's Solcast forecast          |
| Forecast likelihood | `hsem_solcast_pv_forecast_forecast_likelihood` | `pv_estimate`                                  | Attribute key for the estimate field |

### Step: `huawei_solar`

Huawei Solar inverter and battery entity configuration (device selectors and entity sensors only).

| Field                     | Key                                                                | Default                                                 | Description                                                                                                                                                                                                                                           |
| ------------------------- | ------------------------------------------------------------------ | ------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Inverter 1 device ID      | `hsem_huawei_solar_device_id_inverter_1`                           | —                                                       | Device registry ID for inverter 1                                                                                                                                                                                                                     |
| Inverter 2 device ID      | `hsem_huawei_solar_device_id_inverter_2`                           | —                                                       | Device registry ID for inverter 2 (optional)                                                                                                                                                                                                          |
| Batteries device ID       | `hsem_huawei_solar_device_id_batteries`                            | —                                                       | Device registry ID for battery                                                                                                                                                                                                                        |
| Batteries 2 device ID     | `hsem_huawei_solar_device_id_batteries_2`                          | —                                                       | Device registry ID for second battery (optional)                                                                                                                                                                                                      |
| Working mode              | `hsem_huawei_solar_batteries_working_mode`                         | `select.batteries_working_mode`                         | Battery working mode select                                                                                                                                                                                                                           |
| End of discharge SoC      | `hsem_huawei_solar_batteries_end_of_discharge_soc`                 | `number.batteries_end_of_discharge_soc`                 | Min SoC floor entity                                                                                                                                                                                                                                  |
| State of capacity         | `hsem_huawei_solar_batteries_state_of_capacity`                    | `sensor.batteries_state_of_capacity`                    | SoC sensor                                                                                                                                                                                                                                            |
| Charging cutoff capacity  | `hsem_huawei_solar_batteries_charging_cutoff_capacity`             | `number.batteries_end_of_charge_soc`                    | Max SoC during charging                                                                                                                                                                                                                               |
| Grid charge cutoff SoC    | `hsem_huawei_solar_batteries_grid_charge_cutoff_soc`               | `number.batteries_grid_charge_cutoff_soc`               | Max SoC when charging from grid                                                                                                                                                                                                                       |
| Max charging power        | `hsem_huawei_solar_batteries_maximum_charging_power`               | `number.batteries_maximum_charging_power`               | Max charge power                                                                                                                                                                                                                                      |
| Max discharging power     | `hsem_huawei_solar_batteries_maximum_discharging_power`            | `number.batteries_maximum_discharging_power`            | Max discharge power                                                                                                                                                                                                                                   |
| Grid charge maximum power | `hsem_huawei_solar_batteries_grid_charge_maximum_power`            | —                                                       | Optional. Written by the live phase-aware charging safety limiter (issue #831, `hsem_phase_aware_charging_enabled` in the `power` step) to cap grid-funded charging below the main fuse's per-phase limit. Required only when that limiter is enabled |
| Charge/discharge power    | `hsem_huawei_solar_batteries_charge_discharge_power`               | —                                                       | Optional. Signed instantaneous battery power sensor (positive = charging, negative = discharging). Required by the phase-aware charging safety limiter to remove Huawei's own contribution from the live phase snapshot before computing headroom     |
| Rated capacity            | `hsem_huawei_solar_batteries_rated_capacity`                       | `sensor.batteries_rated_capacity`                       | Nameplate capacity sensor                                                                                                                                                                                                                             |
| TOU periods               | `hsem_huawei_solar_batteries_tou_charging_and_discharging_periods` | `sensor.batteries_tou_charging_and_discharging_periods` | TOU period schedule                                                                                                                                                                                                                                   |
| Excess PV use             | `hsem_huawei_solar_batteries_excess_pv_energy_use_in_tou`          | `select.batteries_excess_pv_energy_use_in_tou`          | Excess PV mode in TOU                                                                                                                                                                                                                                 |
| Active power control      | `hsem_huawei_solar_inverter_active_power_control`                  | `sensor.inverter_active_power_control`                  | Export power control mode                                                                                                                                                                                                                             |

### Step: `battery_economics`

Battery depreciation and efficiency parameters.

| Field                | Key                                   | Default | Description                               |
| -------------------- | ------------------------------------- | ------- | ----------------------------------------- |
| Purchase price       | `hsem_batteries_purchase_price`       | 0       | Battery system cost                       |
| Expected cycles      | `hsem_batteries_expected_cycles`      | 6000    | Total expected lifetime cycles            |
| Cycle cost           | `hsem_batteries_cycle_cost`           | 0       | Extra per-kWh wear margin                 |
| Capacity loss at EOL | `hsem_batteries_capacity_loss_pct`    | 30 %    | Expected capacity loss at end-of-life (%) |
| Charge efficiency    | `hsem_batteries_charge_efficiency`    | 98 %    | Charge-side efficiency                    |
| Discharge efficiency | `hsem_batteries_discharge_efficiency` | 98 %    | Discharge-side efficiency                 |

### Step: `power`

Power sensor configuration.

| Field                       | Key                                                        | Default                             | Description                                                                                                                                                                                                                                                                                                                                                                                                          |
| --------------------------- | ---------------------------------------------------------- | ----------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| House consumption power     | `hsem_house_consumption_power`                             | `sensor.power_house_load`           | House load power sensor                                                                                                                                                                                                                                                                                                                                                                                              |
| Solar production power      | `hsem_solar_production_power`                              | `sensor.power_inverter_input_total` | PV production sensor                                                                                                                                                                                                                                                                                                                                                                                                 |
| House includes EV           | `hsem_house_power_includes_ev_charger_power`               | `True`                              | Whether house sensor already includes EV charger                                                                                                                                                                                                                                                                                                                                                                     |
| Main fuse amps              | `hsem_main_fuse_amps`                                      | 25                                  | Main fuse/breaker rating in amps. Set to 0 to disable. The MILP optimizer will respect this limit when scheduling battery and EV charging                                                                                                                                                                                                                                                                            |
| Main fuse phases            | `hsem_main_fuse_phases`                                    | 3                                   | Electrical phase count (1 or 3). Single-phase installations MUST set this to 1 — setting 3 on a single-phase install makes the fuse constraint 3× too permissive                                                                                                                                                                                                                                                     |
| Max grid export power       | `hsem_max_grid_export_power_kw`                            | 0                                   | DNO/inverter grid export cap in kW for export-limited connections (issue #726). The MILP planner never schedules export above this limit, and the applier writes this value in watts to the inverter when export is allowed (issue #770). Set to 0 to disable                                                                                                                                                        |
| Power meter phase A/B/C     | `hsem_huawei_solar_power_meter_phase_{a,b,c}_active_power` | —                                   | Optional. Live per-phase active-power sensors from the Huawei power meter. Required only when the phase-aware charging safety limiter below is enabled                                                                                                                                                                                                                                                               |
| Enable phase-aware charging | `hsem_phase_aware_charging_enabled`                        | `False`                             | Enable a live safety check immediately before each Huawei grid-charge hardware write (issue #831). Uses the three phase power-meter sensors above plus the grid-charge maximum-power entity (`huawei_solar` step) to cap grid-funded charging so it never pushes a phase over the fuse rating, even if load changed since the plan was solved. Requires `hsem_main_fuse_phases = 3` and all four entities configured |

### Step: `ev`

Primary EV charger configuration.

| Field                                 | Key                                         | Default | Description                                                                                                                                                                                                                                                             |
| ------------------------------------- | ------------------------------------------- | ------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| EV charger status                     | `hsem_ev_charger_status`                    | —       | Charger status sensor entity                                                                                                                                                                                                                                            |
| EV charger power                      | `hsem_ev_charger_power`                     | —       | Charger power sensor entity. **Watts expected** — a sensor reporting kW must have `unit_of_measurement: kW` so HSEM can convert it automatically                                                                                                                        |
| EV SoC sensor                         | `hsem_ev_soc`                               | —       | EV battery SoC sensor                                                                                                                                                                                                                                                   |
| EV SoC target                         | `hsem_ev_soc_target`                        | 80 %    | EV target SoC                                                                                                                                                                                                                                                           |
| EV connected sensor                   | `hsem_ev_connected`                         | —       | Binary sensor for EV plugged in                                                                                                                                                                                                                                         |
| Allow charge past target              | `hsem_ev_allow_charge_past_target_soc`      | `False` | Allow charging beyond target SoC from surplus PV, valued against export by avoided future import cost                                                                                                                                                                   |
| Past-target confidence factor         | `hsem_ev_past_target_confidence_factor`     | `0.9`   | Discount (0.0–1.0) applied to the avoided-future-import valuation used for past-target charging                                                                                                                                                                         |
| Auto-Full on negative price           | `hsem_ev_auto_full_negative_price`          | `False` | Charge EV to 100 % when electricity price is negative                                                                                                                                                                                                                   |
| Allow Huawei discharge while charging | `hsem_ev_charger_force_max_discharge_power` | `False` | Permission (not a command): allows the Huawei house battery to discharge while this EV is charging. Huawei exposes one global discharge limit shared by the battery and every EV, so leaving this off forces battery discharge to 0 W whenever the EV is active/planned |
| Max discharge power                   | `hsem_ev_charger_max_discharge_power`       | 0       | Discharge ceiling (W) while this EV charges, once permission above is granted. The applier's actual cap is the planner's own solved discharge rate for the slot, clamped to this ceiling — never a command on its own                                                   |

### Step: `ev_second`

Second EV charger configuration (identical fields to primary EV step; only shown when second EV enabled).

### Step: `ev_planned_load`

Primary EV planned load integration (optional, default disabled).

| Field                      | Key                                               | Default        | Description                                                                                                                                                                                                                                                                                                           |
| -------------------------- | ------------------------------------------------- | -------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Enable                     | `hsem_ev_planned_load_enabled`                    | `False`        | Master switch                                                                                                                                                                                                                                                                                                         |
| Battery capacity           | `hsem_ev_planned_load_battery_capacity_kwh`       | 0.0            | EV battery nameplate capacity (kWh)                                                                                                                                                                                                                                                                                   |
| Charger power              | `hsem_ev_planned_load_charger_power_kw`           | 0.0            | Charger AC output (kW)                                                                                                                                                                                                                                                                                                |
| Charger efficiency         | `hsem_ev_planned_load_charger_efficiency`         | 100 %          | Charger efficiency                                                                                                                                                                                                                                                                                                    |
| Charger min power          | `hsem_ev_planned_load_charger_min_power_w`        | 1380 W         | Minimum charger power for physical operation                                                                                                                                                                                                                                                                          |
| Charger phase topology     | `hsem_ev_planned_load_charger_phase_topology`     | `single_phase` | How the charger spreads load across mains phases. `single_phase` (safe default) makes the hard per-phase fuse rows assume the whole command can land on one phase; `three_phase_balanced` charges only one third to each phase. See [planner-spec.md](planner-spec.md) _Optional hard per-phase charging protection_. |
| Deadline safety margin     | `hsem_ev_planned_load_deadline_safety_margin_pct` | 0 %            | Extra energy budgeted above the target SoC so charging friction doesn't miss the deadline. Opt-in.                                                                                                                                                                                                                    |
| Ceiling deadband           | `hsem_ev_planned_load_command_deadband_a`         | 3 A            | Smallest _reduction_ in the published charging ceiling that is actually applied (0–5 A). Raising the ceiling is never delayed. Damps integer-lattice churn that costs nothing to ignore. 0 disables. See [planner-spec.md](planner-spec.md) _EV charger command stability_.                                           |
| Slot-tail stop suppression | `hsem_ev_planned_load_stub_floor_minutes`         | 2 min          | In the last N minutes of a slot, suppress a 0 W command while the EV still has unmet need before its deadline (0–10 min). Prevents a stop/restart handshake over a few seconds of leftover slot. 0 disables.                                                                                                          |

The planner's per-slot charging decision for this charger is published as a
whole-amp ceiling on the diagnostic sensor `sensor.hsem_ev_charger_current_limit`
(and `sensor.hsem_ev_second_charger_current_limit` for the second EV) for an
external current controller to consume — no separate config field. Conversion
respects the charger's `..._charger_phase_topology` above. See
[planner-spec.md](planner-spec.md) _Published charging ceiling and
stranded-residue re-portioning_.

Target SoC and deadline are configured outside this step:

- **Target SoC**: via the number entity `number.hsem_ev_target_soc`
- **Deadline**: via the HSEM time entity `time.hsem_ev_deadline_time`
- **Smart charging**: via the HSEM switch `switch.hsem_ev_smart_charging`
- **Force charge now**: via the HSEM switch `switch.hsem_ev_force_charge_now`
- **Allow charge past target**: via `hsem_ev_allow_charge_past_target_soc` in the EV charger step
- **Past-target confidence factor**: via `hsem_ev_past_target_confidence_factor` in the EV charger step

### Step: `ev_second_planned_load`

Second EV planned load integration (identical fields; only shown when second EV enabled).
Each charger carries its own `..._charger_phase_topology`, so a three-phase
primary charger and a single-phase second charger are modelled independently.

### Step: `ocpp`

OCPP (Open Charge Point Protocol) integration for EV charger remote control.
HSEM runs **one OCPP server per EV**: the primary EV's charger connects to the
primary server, and — when the second EV is enabled — the second EV's charger
can connect to a dedicated second server on its own port. The second-server
fields are only shown when the second EV is configured.

| Field                  | Key                        | Default | Description                                                                            |
| ---------------------- | -------------------------- | ------- | -------------------------------------------------------------------------------------- |
| OCPP enabled           | `hsem_ocpp_enabled`        | `False` | Master switch for OCPP integration                                                     |
| OCPP port              | `hsem_ocpp_port`           | `9000`  | TCP port for the primary EV's OCPP WebSocket server                                    |
| OCPP charge point ID   | `hsem_ocpp_cpid`           | —       | Charge point identifier (as configured in the charger)                                 |
| Start window           | `hsem_ocpp_start_window_s` | `300`   | Seconds before a scheduled charge slot to send `RemoteStartTransaction`                |
| Stop window            | `hsem_ocpp_stop_window_s`  | `300`   | Seconds before a non-charge slot to send `RemoteStopTransaction`                       |
| Second OCPP enabled    | `hsem_ocpp_second_enabled` | `False` | Enable the dedicated second-EV server (only shown with second EV)                      |
| Second OCPP port       | `hsem_ocpp_second_port`    | `9001`  | TCP port for the second EV's OCPP WebSocket server (must differ from the primary port) |
| Second charge point ID | `hsem_ocpp_second_cpid`    | —       | Charge point identifier of the second EV charger                                       |

### Step: `batteries_schedule_1/2/3`

Battery charge/discharge schedule windows (up to three).

| Field      | Key                                                | Default | Description                 |
| ---------- | -------------------------------------------------- | ------- | --------------------------- |
| Enabled    | `hsem_batteries_enable_batteries_schedule_N`       | Varies  | Toggle this schedule window |
| Start time | `hsem_batteries_enable_batteries_schedule_N_start` | Varies  | Window start (HH:MM:SS)     |
| End time   | `hsem_batteries_enable_batteries_schedule_N_end`   | Varies  | Window end (HH:MM:SS)       |

Schedule 1 and 2 are enabled by default; schedule 3 is disabled by default.

### Step: `batteries_wait_mode`

Battery wait-mode behaviour.

| Field               | Key                                 | Default  | Description                                                                                                                                                                    |
| ------------------- | ----------------------------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Wait mode behaviour | `hsem_batteries_wait_mode_behavior` | `strict` | `strict` keeps the battery idle in Wait mode; `self_consumption_with_reserve` allows normal household self-consumption while protecting the planner's required battery reserve |

### Step: `batteries_excess_export`

Excess battery export configuration.

| Field                    | Key                                             | Default         | Description                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| ------------------------ | ----------------------------------------------- | --------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Enable excess export     | `hsem_batteries_enable_excess_export`           | `False`         | Master switch                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| Discharge buffer         | `hsem_batteries_excess_export_discharge_buffer` | 10 %            | Conditional SoC reserve retained through the demand window after intentional battery export. Every slot in one contiguous PV-surplus run shares the same later checkpoint; direct PV export is unaffected.                                                                                                                                                                                                                                                                                                                 |
| Forecast reserve         | `hsem_batteries_forecast_reserve_pct`           | 0 % (disabled)  | Extra absolute SoC percentage points above Huawei's hardware end-of-discharge limit that intentional battery export must retain **immediately after** each export slot (issue #807) — unlike the discharge buffer above, a later forecast PV/grid refill cannot justify spending it first. Ordinary household self-consumption may still use the energy when the forecast is wrong; direct PV export is unaffected. Range 0–50 %.                                                                                          |
| Battery export min price | `hsem_batteries_export_min_price`               | `0.0`           | Per-slot hard floor for intentional battery-to-grid export (issue #752). When > 0, the MILP forbids marking a slot as `force_batteries_discharge` when the export price is strictly below this floor — the battery can still serve house load in those slots. Reaching the threshold does NOT automatically trigger export; the optimizer still decides whether selling is worthwhile. Applies only to intentional battery-to-grid export, not to normal self-consumption, PV export, or PV charging. Set to 0 to disable. |
| Price threshold          | —                                               | Auto-calculated | Computed from battery depreciation settings at runtime                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |

### Step: `weighted_values`

Consumption prediction weight configuration.

| Field         | Key                                        | Default | Description               |
| ------------- | ------------------------------------------ | ------- | ------------------------- |
| Weight 1-day  | `hsem_house_consumption_energy_weight_1d`  | 25 %    | Weight for 1-day average  |
| Weight 3-day  | `hsem_house_consumption_energy_weight_3d`  | 30 %    | Weight for 3-day average  |
| Weight 7-day  | `hsem_house_consumption_energy_weight_7d`  | 30 %    | Weight for 7-day average  |
| Weight 14-day | `hsem_house_consumption_energy_weight_14d` | 15 %    | Weight for 14-day average |

Battery parameters and planner settings in this step duplicate their primary-step
counterparts and are kept for backward compatibility during migration.

### Step: `energy_and_ml`

Energy meter entities and ML consumption prediction (last step, creates entry).

| Field                   | Key                                           | Default | Description                                                                                                                                                                                                  |
| ----------------------- | --------------------------------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Grid Import Energy      | `hsem_grid_import_energy_entity`              | —       | Cumulative grid import meter (kWh). Also used as ML data source.                                                                                                                                             |
| Grid Export Energy      | `hsem_grid_export_energy_entity`              | —       | Cumulative grid export meter (kWh). Used for net consumption.                                                                                                                                                |
| PV Energy               | `hsem_pv_energy_entity`                       | —       | Cumulative PV production meter (kWh).                                                                                                                                                                        |
| ML enabled              | `hsem_ml_consumption_enabled`                 | `False` | Enable ridge regression predictor instead of rolling averages.                                                                                                                                               |
| ML history days         | `hsem_ml_consumption_history_days`            | 14      | Days of recorder history for ML training (7–90).                                                                                                                                                             |
| Net consumption         | `hsem_ml_consumption_net_consumption`         | `False` | Subtract export from import for net house consumption.                                                                                                                                                       |
| Sequential prediction   | `hsem_ml_consumption_sequential`              | `False` | Feed each slot's prediction as lag input to the next (captures intra-day momentum).                                                                                                                          |
| Temperature sensor      | `hsem_ml_consumption_temperature_entity`      | —       | Outdoor (ambient) temperature in °C for weather-driven predictions.                                                                                                                                          |
| Weather forecast entity | `hsem_ml_consumption_weather_forecast_entity` | —       | Optional `weather` entity supplying forecast temperatures for future inference slots (issue #918). Requires the temperature sensor above; falls back to it per-slot when forecast coverage is missing/stale. |
