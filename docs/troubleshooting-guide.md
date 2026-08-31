# HSEM Troubleshooting Guide

This guide helps you diagnose and fix common HSEM problems. Work through the
symptoms below in order — the first sections cover foundational issues
(missing data, wrong data) that must be fixed before later sections
(battery behaviour) can be addressed.

---

## Quick diagnostic checklist

Before diving into individual symptoms, check these four sensors in the
Home Assistant **Developer Tools → States** tab. They answer 90 % of questions.

| Sensor                        | Look for                                                                                                     |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------ |
| `sensor.hsem_degraded_mode`   | Must be `ok`. `error` means no hardware writes. `degraded` means some data is missing but writes still work. |
| `sensor.hsem_hardware_writes` | Must be `allowed`. `blocked` means the system cannot send commands to the inverter.                          |
| `sensor.hsem_read_only`       | Must be `off`. `on` means you intentionally disabled writes.                                                 |
| `sensor.hsem_applier_status`  | Must be `ok` or `skipped`. `failed` means the last hardware write did not take effect.                       |

If any of these show a non-ideal value, start with the matching section below.

---

## 1. Missing data

### Symptoms

- `sensor.hsem_degraded_mode` shows `degraded` or `error`
- `sensor.hsem_missing_entities` shows a number > 0
- `sensor.hsem_working_mode` shows `missing_input_entities`
- `sensor.hsem_plan_explanation` → `data_quality_complete` is `false`
- The `HSEM` device in HA shows unavailable entities

### Checks & likely causes

**1a. Huawei Solar entities missing after HA restart**

The Huawei Solar integration needs one poll cycle before its entities become
available. HSEM can read them only after that.

- **Check:** Look at `sensor.hsem_degraded_mode` → `missing_entities` attribute.
  Are battery/power entities listed?
- **Fix:** Wait 2–3 minutes after HA restart for the inverter to respond. HSEM
  automatically shortens its update interval to 1 minute while entities are
  missing and returns to the configured interval when data is available.

**1b. Wrong device IDs in the HSEM config flow**

The Huawei Solar step in the config flow requires device IDs for the inverter
and batteries. If these IDs don't match your physical devices, no battery or
inverter entities will be found.

- **Check:** HSEM → **Configure** → **Huawei Solar** step. Verify the
  _Inverter Device ID_ and _Batteries Device ID_ match the Device Info shown in
  the Huawei Solar integration's device list.
- **Fix:** Correct the device IDs in the config flow and save.

**1c. Price feed (Energi Data Service) not installed or broken**

HSEM reads electricity prices from the `energidataservice` integration. If it's
missing, not configured, or its entities are unavailable, prices default to
`0.0`.

- **Check:** Look at the `data_quality` attribute on `sensor.hsem_plan_explanation`.
  Are `today_price_missing_hours` or `tomorrow_price_missing_hours` high?
- **Fix:** Verify the EDS integration is installed, configured for your
  price area, and its entities show valid data in HA Developer Tools → States.

**1d. Critical sensors missing → Error mode**

If any of these five entities are missing, HSEM enters `error` mode and
**blocks all hardware writes**:

| Sensor label keyword                  | Entity description      |
| ------------------------------------- | ----------------------- |
| `batteries_state_of_capacity`         | Battery state of charge |
| `batteries_maximum_charging_power`    | Max charging power      |
| `batteries_maximum_discharging_power` | Max discharging power   |
| `batteries_rated_capacity`            | Battery rated capacity  |
| `house_consumption_power`             | House consumption power |

- **Check:** `sensor.hsem_degraded_mode` → `missing_entities` attribute.
- **Fix:** Correct device IDs or reinstall the Huawei Solar integration.

**1e. Consumption energy sensors not ready after restart**

The `sensor.hsem_house_consumption_energy_*` sensors use HA's statistics table.
After a restart they may exist while their restored state is still `unknown` or
`unavailable`. HSEM keeps that state missing; it does not turn it into a
zero-load forecast.

- **Check:** Inspect `sensor.hsem_working_mode` → `data_quality`.
  `load_forecast_ready` is false until the future profile is safe, and
  `load_forecast_reason` identifies the cause. A zero profile with live house
  demand above 50 W reports `zero_forecast_with_live_demand`.
- **Behaviour:** Automatic mode publishes a strict `batteries_wait_mode` hold
  and does not run or reuse an optimized plan. Manual force mode remains higher
  authority.
- **Fix:** Wait for the next statistics cycle (usually 5 minutes), or repair an
  average sensor that remains unavailable. HSEM retries every minute and runs a
  fresh plan when the profile recovers.

---

## 2. Wrong prices

### Symptoms

- The planner makes decisions that don't match your intuition about electricity
  prices
- `sensor.hsem_plan_explanation` → `selected_strategy` doesn't match price
  patterns you see in EDS
- Charge/discharge happens at unexpected times

### Checks & likely causes

**2a. Price cadence not detectable**

HSEM auto-detects the cadence of each price attribute from its timestamps.
If an attribute has fewer than two parseable timestamps, or the timestamps are
malformed, HSEM falls back to the configured EDS interval and may only match
part of the data.

- **Check:** HSEM → **Configure** → **Energi Data Service** step. Verify the
  sensor attributes contain valid timestamps and that the active attribute key
  matches the data actually published by your price source.
- **Fix:** Correct the timestamp format or attribute key so HSEM can detect
  the real cadence. If the source truly changes cadence (for example, `forecast`
  vs `prices_today`), HSEM now handles that automatically.

**2b. Grid fee or tax configuration**

HSEM adds grid fees and taxes to spot prices. Wrong values here distort the
cost function.

- **Check:** HSEM → **Configure** → **Energi Data Service** step. Review
  _Grid Fee_ (net-tariff), _Grid Tariff_ (transmissions-net), _El-afgift_,
  and _Reduktion_. Verify against your electricity bill.
- **Fix:** Correct any mismatched values.

**2c. Export minimum price blocks all export**

The `export_min_price` setting prevents grid export when the export price
is below that threshold. If set too high, HSEM never exports — the battery
stays full and the inverter physically blocks export.

- **Check:** HSEM → **Configure** → **Energi Data Service** step.
  _Export Minimum Price_. Compare against current export spot prices.
- **Fix:** Lower or set to 0 if you want to export at all positive prices.

**2d. Negative prices trigger force-export**

If the live import price is negative, the runtime recommendation resolver
forces `force_export` mode regardless of planner output. This is correct
behaviour — the battery should discharge to avoid paying to import.

- **Check:** `sensor.hsem_working_mode` shows `force_export` during a
  slot with negative prices. This is normal — verify the spot price in EDS.
- **Fix:** If you don't want force-export, disable it in your battery
  schedule configuration (set _Allow Forced Export_ to off).

---

## 3. Wrong PV forecast

### Symptoms

- The planner charges or discharges at unexpected times despite plenty of solar
- `sensor.hsem_plan_explanation` → `data_quality` shows `tomorrow_pv_missing_hours` > 0
- `sensor.forecast_accuracy` shows `mae_pv_kwh` consistently high
- Battery does not charge from solar when PV is available

### Checks & likely causes

**3a. Solcast integration not installed or wrong entity**

HSEM reads PV forecasts from the `solcast_solar` integration. If it's missing
or the entity ID is wrong, all PV estimates default to `0.0`.

- **Check:** HSEM → **Configure** → **Solcast** step. Verify the
  _Solcast Entity ID_ points to an existing, working entity.
- **Fix:** Install the `solcast_solar` integration and configure HSEM to
  use the correct entity. The entity should show forecast values > 0 during
  daylight hours.

**3b. Solcast forecast attribute key**

HSEM reads a specific attribute from the Solcast entity (configurable as
_Forecast Likelihood_). If the attribute key doesn't match what Solcast
exposes, PV forecast will be all zeros.

- **Check:** In HA Developer Tools → States, find your Solcast entity and
  inspect its attributes. Check which attribute key holds the forecast data
  (typically `pv_estimate`, `pv_estimate10`, or `pv_estimate90`).
- **Fix:** Set the _Forecast Likelihood_ field in the HSEM Solcast config
  step to match the attribute name that exists.

**3c. Seasonal mode classification wrong**

HSEM classifies each month as winter or summer based on the _Winter Months_
setting. In winter mode the planner uses `batteries_wait_mode` — it does not
actively charge from solar. In summer mode it uses solar charging strategies.
You can change **Wait mode behaviour** to _Self-consumption with reserve_ if
you want the battery to cover normal household load during wait periods while
still protecting the planner's required reserve.

- **Check:** `sensor.hsem_plan_explanation` → `forecast_mode`. Does it
  match your expectation for the current month?
- **Fix:** HSEM → **Configure** → **Months** step. Adjust the _Winter
  Months_ list so months are correctly classified for your climate.

**3d. Persistent forecast bias (Solcast consistently over/under)**

Use the `sensor.forecast_accuracy` sensor to track forecast quality over time.

- **Check:** `mae_pv_kwh` (mean absolute error in kWh) and `bias_pv_kwh`
  (systematic over/under prediction).
- **Fix:** If bias is consistently high in one direction, consider switching
  the _Forecast Likelihood_ to a different percentile (e.g. from
  `pv_estimate` to `pv_estimate90` for a conservative estimate, or to
  `pv_estimate10` for an optimistic one).

**3e. Current-slot live house reading ignored (or a stale reading trusted)**

The coordinator now marks the live house-power reading authoritative only
when it can prove the reading is genuine: the entity is configured, its
value is present, finite, and non-negative, and it is not on the live
state's missing-entities list (`coordinator_builder._resolve_live_house_measurement`,
issue #792). A real, measured **0 W** house reading is a valid value and
overwrites the current slot's forecast; an unavailable reading (missing
entity, negative/non-finite value) leaves the forecast untouched instead of
being masked by whatever number happens to sit in the raw watt field.

- **Check:** `sensor.hsem_plan_explanation` for the current slot's
  `avg_house_consumption_kwh` against your house power sensor's live value.
- **Behaviour:** If the house power sensor is temporarily unavailable, the
  slot keeps using the forecast until the sensor recovers — this is
  expected, not a bug.
- **Fix:** If the plan still doesn't reflect a genuine live reading, verify
  the configured _House Consumption Power_ entity is not appearing in
  HSEM's missing-entities diagnostics and reports a non-negative, numeric
  value in Watts.

---

## 4. Inverter write failures

### Symptoms

- `sensor.hsem_applier_status` shows `failed`
- `sensor.hsem_degraded_mode` shows `error` (writes blocked)
- `sensor.hsem_hardware_writes` shows `blocked`
- Battery does not change behaviour despite planner recommendations
- `sensor.hsem_plan_explanation` → `last_apply_status` is `failed`

### Checks & likely causes

**4a. Degraded mode = Error**

When critical entities are missing, all hardware writes are blocked. See
[Section 1d](#1d-critical-sensors-missing--error-mode).

- **Fix:** Resolve the missing critical entities first.

**4b. Read-only mode active**

`switch.hsem_read_only` being `on` blocks all writes intentionally. This is
a safety feature for when you want to monitor the planner without letting it
control hardware.

- **Check:** `sensor.hsem_read_only` state. Is it `on`?
- **Fix:** Set `switch.hsem_read_only` to `off`.

**4c. Write accepted but unverified (transient)**

`unverified` status means the write was sent but the read-back timed out or
returned `None`. The inverter may still have accepted the value.

- **Check:** `sensor.hsem_applier_status` → `last_apply_details` attribute.
  Look for entries with status `unverified`.
- **Fix:** Usually self-corrects on the next cycle (HSEM retries up to 3
  times per write). If persistent, the inverter entity may be slow to
  update — check Huawei Solar integration health.

**4d. Persistent write failures**

`failed` status means all 3 retry attempts were exhausted — the inverter did
not accept the value.

- **Check:** `sensor.hsem_applier_status` → `failed_entities` attribute for
  the specific entity IDs that failed.
- **Check:** Home Assistant logs for errors from the `huawei_solar`
  integration.
- **Fix:**
  1. Verify the inverter is online and reachable.
  2. Check that the working mode or settings being written are valid for
     your inverter model.
  3. Restart the Huawei Solar integration.
  4. If the failure is on `set_tou_periods`, check your battery schedule
     configuration for invalid values.

**4e. Force working mode override active**

When `select.hsem_force_working_mode` is set to anything other than `auto`,
the planner is bypassed and HSEM writes the forced mode directly. If that
mode is invalid for your inverter, writes may fail.

- **Check:** `select.hsem_force_working_mode` state.
- **Fix:** Set to `auto` to restore normal planner control, or set a valid
  mode for your inverter.

---

## 5. Plan not changing

### Symptoms

- `sensor.hsem_plan_explanation` → `selected_strategy` stays the same for
  many hours
- The working mode sensor never changes recommendation
- You expect the battery to switch between charge/discharge but it doesn't

### Checks & likely causes

**5a. Hysteresis holding previous plan**

The planner has hysteresis: it keeps the previous plan if the new plan's
improvement is less than 5 %. This prevents oscillation between similar
strategies.

- **Check:** `sensor.hsem_plan_explanation` → `hysteresis_active` (is it
  `true`?) and `hysteresis_reason`.
- **Fix:** This is normal behaviour. If you want more responsive switching,
  reduce the hysteresis threshold in HSEM → **Configure** → **Batteries
  Hysteresis** step.

**5b. Window hysteresis preventing slot-level changes**

`planner_window_hysteresis_minutes` prevents rapid recommendation toggling
(e.g. `ev_smart_charging` ↔ `batteries_charge_solar`) by enforcing a
minimum hold time. When enabled, a slot's recommendation is locked once
established.

- **Check:** HSEM → **Configure** → **Battery Economics & Hysteresis** step.
  _Window Hysteresis Hold Time_ value (0 = disabled).
- **Fix:** Set to 0 to disable window hysteresis, or lower it if slots are
  staying locked too long.

**5c. Only one schedule active**

If you only have one battery schedule configured and it covers only part of
the day, the battery will be in `batteries_wait_mode` outside that window.

- **Check:** HSEM → **Configure** → **Batteries Schedule** steps (1, 2, 3).
  Are schedules enabled? Do they cover the hours you expect?
- **Fix:** Enable additional schedules or widen the hours. Each schedule
  defines when a specific working mode is permitted.

**5d. Planner in winter wait mode**

In winter months, the planner uses `batteries_wait_mode` by default — it
doesn't actively charge or discharge. This is intentional, but you can enable
**Self-consumption with reserve** in the **Battery Wait Mode Behaviour** step if
you prefer the battery to cover household load using surplus capacity above the
planner reserve.

- **Check:** `sensor.hsem_plan_explanation` → `forecast_mode` is `winter`
  and `selected_strategy` is `winter_wait`.
- **Fix:** If this is unexpected, check the _Winter Months_ setting in the
  Months config step. Adjust if your climate has different seasonal patterns.

**5e. Consumption weights prevent plan selection**

If the consumption prediction weights don't sum to 100 %, the planner logs a
warning and may produce suboptimal plans.

- **Check:** HSEM → **Configure** → **Weighted Values** step. Do the
  weights sum to 100 %?
- **Check:** `hsem.log` for the warning "Consumption weights sum to X, not 100."
- **Fix:** Adjust weights to sum to exactly 100 %.

---

## 6. Battery not charging

### Symptoms

- Battery SoC stays low or doesn't increase
- `sensor.hsem_working_mode` never shows `batteries_charge` recommendations
- Battery draws no power from grid or solar despite low SoC

### Checks & likely causes

**6a. No charge schedule configured or active**

HSEM only commands charging when a schedule permits it and the planner
assigns charge recommendations to slots.

- **Check:** `sensor.hsem_plan_explanation` → `selected_strategy`. Does it
  include "charge"? Look at the `planned_slots` attribute — are any marked
  with a charge recommendation?
- **Fix:** Enable a battery schedule in the config flow and ensure it
  covers the hours when you want charging to happen.

**6b. SoC already at or above charge cutoff**

HSEM respects the configured charge cutoff. If the battery is already above
the cutoff, the planner won't schedule charging.

- **Check:** HSEM → **Configure** → **Huawei Solar** step.
  _Grid Charge Cutoff SoC_ value.
- **Check:** Current battery SoC vs the cutoff value.
- **Fix:** Adjust the cutoff value if desired. Note: this is a safety
  setting — setting it too high may reduce battery lifespan.

**6c. Winter season blocks solar charging**

In winter mode, the planner does not actively charge from solar. It waits
for prices to drop below the charge threshold.

- **Check:** `sensor.hsem_plan_explanation` → `forecast_mode` is `winter`.
- **Fix:** If you want solar charging in what HSEM considers winter, adjust
  the _Winter Months_ in the Months config step.

**6d. Charge efficiency configured incorrectly**

If `charge_efficiency` is set to 100 % but the actual round-trip efficiency
is lower, the planner overestimates how much energy reaches the battery.

- **Check:** HSEM → **Configure** → **Battery Economics** step.
  _Charge Efficiency_ value.
- **Fix:** Set to realistic values (e.g. 95–98 % for lithium-ion).

**6e. Cycle cost too high**

If the `purchase_price` is set very high and `battery_cycle_cost` is auto-derived
from it, the cycle cost can be high enough that the planner decides charging
is not economical.

- **Check:** HSEM → **Configure** → **Battery Economics** step.
  _Purchase Price_ and _Cycle Cost_ values.
- **Fix:** Set `purchase_price` to your actual battery purchase price per kWh
  of usable capacity for accurate cycle cost calculation.

**6f. Consumption exceeds available energy**

If house consumption is higher than available solar + low-price grid energy,
the planner may determine there's no surplus to charge the battery.

- **Check:** `sensor.hsem_net_consumption` — is it positive (house draws
  from battery/grid) during sunny hours?
- **Fix:** This is normal if consumption is high. The planner correctly
  prioritises serving house load over charging.

**6g. Non-critical configuration issues**

- `batteries_charging_cutoff_capacity` in the Huawei Solar step: sets the
  SoC at which the battery physically stops charging. Must coordinate with
  the planner's charge targets.
- `end_of_discharge_soc`: HSEM uses this for safety floors but incorrect
  values can confuse the planner.

---

## 7. Battery not discharging

### Symptoms

- Battery SoC stays high or doesn't decrease during peak price hours
- `sensor.hsem_working_mode` never shows `batteries_discharge` recommendations
- Battery does not export or serve house load when expected

### Checks & likely causes

**7a. No discharge schedule configured or active**

Discharge requires a battery schedule with a discharge-compatible mode
enabled.

- **Check:** `sensor.hsem_plan_explanation` → `selected_strategy`. Does it
  include "discharge"?
- **Fix:** Enable a battery schedule that permits discharge during the hours
  when you want to discharge.

**7b. SoC at or below end-of-discharge floor**

HSEM respects the battery's minimum SoC. If the battery is already near the
end-of-discharge threshold, no discharge is scheduled.

- **Check:** Current battery SoC vs the configured `end_of_discharge_soc`
  in the Huawei Solar config step.
- **Fix:** Lower the `end_of_discharge_soc` if safe for your battery
  chemistry. Never set below the manufacturer's recommended minimum.

**7c. Export minimum price blocks all export**

See [Section 2c](#2c-export-minimum-price-blocks-all-export). If
`export_min_price` is set above current export prices, the inverter blocks
grid export. The battery may still discharge to serve house load, but won't
export.

- **Check:** Current export spot price vs the `export_min_price` setting.
- **Fix:** Lower the threshold if you want to export during low-price
  periods.

**7d. Discharge efficiency configured incorrectly**

If `discharge_efficiency` is unrealistically high, the planner overestimates
usable energy from the battery.

- **Check:** HSEM → **Configure** → **Battery Economics** step.
  _Discharge Efficiency_ value.
- **Fix:** Set to realistic values (e.g. 95–98 %).

**7e. Capacity loss factor too high**

`capacity_loss_pct` (default 30 %) accounts for battery wear. Higher values
make cycling more expensive in the cost function, which may reduce discharge.

- **Check:** HSEM → **Configure** → **Battery Economics** step.
  _Capacity Loss %_.
- **Fix:** Adjust to match your battery's expected lifetime degradation.

**7f. Excess export feature misconfigured**

The _Excess Export_ feature (in the Batteries Excess Export config step)
discharges battery surplus above a buffer to the grid. If disabled or the
buffer is too high, excess energy stays in the battery.

- **Check:** Is the feature enabled? What is the `discharge_buffer` value?
- **Fix:** Enable the feature and set a reasonable buffer for your needs.

**7g. Forecast reserve holding back more export than expected**

`hsem_batteries_forecast_reserve_pct` (issue #807, default 0 = disabled)
reserves extra SoC points above the hardware floor immediately after each
export slot — unlike the discharge buffer above, a later PV/grid refill
cannot release it early. A high value can noticeably reduce export volume.

- **Check:** Batteries Excess Export config step → _Forecast reserve_.
- **Fix:** Lower the percentage, or set to 0 to disable if you don't need
  protection against a wrong PV forecast.

**7h. Export physically blocked at inverter level**

HSEM writes `set_maximum_feed_grid_power_percent` to 0 % when export is
not allowed for the current slot. If the inverter's own export limit is also
set to 0, the battery cannot export regardless of HSEM's decision.

- **Check:** The inverter's grid export settings in the Huawei Solar
  integration or FusionSolar app. Ensure export is permitted at the
  inverter level.

---

## When to check the logs

### HSEM log (`hsem.log`)

Located in your Home Assistant config directory. Enable verbose logging by
turning on `switch.hsem_verbose_logging` before collecting logs.

Search for these patterns in `hsem.log`:

| Pattern                                              | Meaning                                                    |
| ---------------------------------------------------- | ---------------------------------------------------------- |
| `[core] run_planner ABORTED — no slots generated`    | `interval_minutes` or `interval_length_hours` config error |
| `Consumption weights sum to`                         | Weight misconfiguration                                    |
| `MILP: SoC penalty violations`                       | Battery was overcharged at planning start                  |
| `Hardware writes BLOCKED`                            | Error mode or read-only active                             |
| `[selector] No eligible candidates`                  | All plans rejected during validation                       |
| `[selector] HYSTERESIS kept previous plan`           | Plan switch suppressed by hysteresis                       |
| `Sensor read failed for entity_id`                   | Specific entity reading error — check entity               |
| `EV is physically charging but no slot has load > 0` | EV charging without planned load                           |

### Home Assistant log (`home-assistant.log`)

HSEM messages do **not** propagate to `home-assistant.log` by default. To
see them there, add to `configuration.yaml`:

```yaml
logger:
  logs:
    custom_components.hsem: debug
```

### Huawei Solar integration logs

Check the Huawei Solar integration for network errors, timeouts, or
authentication failures. These affect HSEM's ability to read and write
hardware entities.

---

## System recovery steps

If you've checked everything and HSEM still doesn't work:

1. **Reload the integration:** HA Settings → Devices & Services → HSEM →
   ⋮ menu → Reload.

2. **Restart HA:** Full restart clears any stuck state.

3. **Re-run config flow:** HSEM → Configure → step through all config
   steps → save. This re-validates all entity IDs and device IDs.

4. **Enable extended attributes:** Turn on `switch.hsem_extended_attributes`
   to expose additional diagnostic attributes on the working mode and plan
   explanation sensors.

5. **Enable verbose logging:** Turn on `switch.hsem_verbose_logging`,
   wait for 2–3 coordinator cycles, then collect `hsem.log` from the
   HA config directory. The log shows every planning step in detail.

6. **Check for known issues:** Review open issues at
   [github.com/woopstar/hsem/issues](https://github.com/woopstar/hsem/issues).

---

## Key diagnostic entities reference

| Entity                            | Type   | Purpose                                                |
| --------------------------------- | ------ | ------------------------------------------------------ |
| `sensor.hsem_degraded_mode`       | Sensor | `ok` / `degraded` / `error`                            |
| `sensor.hsem_missing_entities`    | Sensor | Count of missing input entities                        |
| `sensor.hsem_hardware_writes`     | Sensor | `allowed` / `blocked`                                  |
| `sensor.hsem_read_only`           | Sensor | `on` / `off`                                           |
| `sensor.hsem_applier_status`      | Sensor | `ok` / `unverified` / `failed` / `skipped` / `pending` |
| `sensor.hsem_plan_explanation`    | Sensor | Active strategy + detailed attributes                  |
| `sensor.hsem_working_mode`        | Sensor | Current slot recommendation                            |
| `sensor.forecast_accuracy`        | Sensor | PV/load forecast accuracy metrics                      |
| `switch.hsem_read_only`           | Switch | Toggle read-only mode                                  |
| `switch.hsem_verbose_logging`     | Switch | Toggle verbose HSEM logging                            |
| `switch.hsem_extended_attributes` | Switch | Expose additional sensor attributes                    |
| `select.hsem_force_working_mode`  | Select | Manual working mode override                           |
