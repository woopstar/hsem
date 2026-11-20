# HSEM Services Reference

HSEM exposes thirteen Home Assistant services that allow automation, script, and
manual control over the planner and hardware writes.

These services are **integration-level actions**: they operate on the single
configured HSEM instance and do not require a `target` (entity, device, or
area). The UI and YAML calls should only provide the `data` fields shown
below.

---

## Service listing

| Service                                 | Description                                                        | Response |
| --------------------------------------- | ------------------------------------------------------------------ | -------- |
| `hsem.force_recalculation`              | Trigger an immediate full planner re-run                           | None     |
| `hsem.set_temporary_override`           | Force a specific battery working mode                              | None     |
| `hsem.clear_override`                   | Return to automatic planner control                                | None     |
| `hsem.create_dashboard`                 | Create or update the bundled Lovelace dashboard                    | Dict     |
| `hsem.export_diagnostics`               | Export structured diagnostic data                                  | Dict     |
| `hsem.ocpp_debug_start_charging`        | Diagnostics-only: manually start an OCPP charger                   | None     |
| `hsem.ocpp_debug_stop_charging`         | Diagnostics-only: manually stop an OCPP charger                    | None     |
| `hsem.ocpp_debug_diagnostics`           | Diagnostics-only: query the charger's config and computed limit    | None     |
| `hsem.ocpp_debug_set_current`           | Diagnostics-only: send only a charging profile, at a given current | None     |
| `hsem.ocpp_debug_set_availability`      | Diagnostics-only: set a connector Operative/Inoperative            | None     |
| `hsem.ocpp_debug_set_configuration`     | Diagnostics-only: write one OCPP configuration key                 | None     |
| `hsem.ocpp_generate_client_certificate` | Sign a client key and certificate for mutual TLS                   | Dict     |
| `hsem.what_if`                          | Replay recorded days with another battery, read-only               | Dict     |

---

## 1. `hsem.force_recalculation`

Forces the HSEM coordinator to run a full recalculation cycle immediately.
All entity states are re-read and the planner is re-run.

**Use cases:**

- Testing and debugging
- Forcing a plan update faster than the normal polling interval
- After changing a configuration value that affects the current plan

**Schema:** No fields.

**Example:**

```yaml
service: hsem.force_recalculation
```

---

## 2. `hsem.set_temporary_override`

Temporarily bypasses the automatic planner by writing a specific working mode
directly to the inverter. While the override is active, the planner output is
ignored.

**Schema:**

| Field              | Required | Type             | Description                                                       |
| ------------------ | -------- | ---------------- | ----------------------------------------------------------------- |
| `working_mode`     | Yes      | Select           | One of the supported override modes                               |
| `duration_minutes` | No       | Integer (1–1440) | Minutes until override auto-expires; planner resumes after expiry |

**Supported override modes:**

| Mode                              | Behaviour                                                                                            |
| --------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `batteries_charge_grid`           | Force-charge the battery from the grid                                                               |
| `batteries_charge_solar`          | Charge the battery from PV only                                                                      |
| `batteries_discharge_mode`        | Discharge the battery to cover house load                                                            |
| `batteries_discharge_window_mode` | Same execution as `batteries_discharge_mode` (self-consumption with the discharge cap at rated max)  |
| `batteries_wait_mode`             | Battery idle by default; follows the configured **Wait mode behaviour** when selected by the planner |
| `ev_smart_charging`               | Prioritise EV charging                                                                               |
| `force_batteries_discharge`       | Force-discharge the battery to the grid (export)                                                     |
| `force_export`                    | Export all available energy to the grid                                                              |

**Implementation notes:**

- Writes the mode to the `select.hsem_force_working_mode` entity
- Triggers an immediate recalculation after setting
- When `duration_minutes` is omitted, the override persists until `hsem.clear_override` is called or the select is manually set to `"auto"`
- When `duration_minutes` is provided, the override auto-expires after the specified duration and the planner resumes control automatically

**Examples:**

```yaml
# Override without expiry — persists until cleared
service: hsem.set_temporary_override
data:
  working_mode: batteries_discharge_mode

# Timed override — auto-expires after 30 minutes
service: hsem.set_temporary_override
data:
  working_mode: batteries_charge_grid
  duration_minutes: 30

# One-hour idle override
service: hsem.set_temporary_override
data:
  working_mode: batteries_wait_mode
  duration_minutes: 60
```

---

## 3. `hsem.clear_override`

Clears any active temporary working-mode override and returns to automatic
planner control. Has no effect when no override is currently active.

**Schema:** No fields.

**Implementation notes:**

- Resets the force-mode select entity to `"auto"`
- Triggers an immediate recalculation so the planner output takes effect

**Example:**

```yaml
service: hsem.clear_override
```

---

## 4. `hsem.create_dashboard`

Creates or updates the bundled HSEM Lovelace dashboard. The dashboard YAML is
copied from `custom_components/hsem/dashboards/dashboard_en.yaml` to
`<config>/hsem_dashboard.yaml` and a storage-mode Lovelace dashboard is
registered in Home Assistant so it appears in the sidebar.

**Schema:**

| Field            | Required | Type   | Description                                                                                                                         |
| ---------------- | -------- | ------ | ----------------------------------------------------------------------------------------------------------------------------------- |
| `dashboard_path` | No       | String | Absolute file path for the dashboard YAML, must resolve inside the HA config directory. Defaults to `<config>/hsem_dashboard.yaml`. |

**Response:**

| Key              | Type          | Description                                                                                                |
| ---------------- | ------------- | ---------------------------------------------------------------------------------------------------------- |
| `dashboard_path` | `str`         | Absolute path to the written dashboard YAML file                                                           |
| `dashboard_url`  | `str \| None` | Dashboard URL path (e.g. `/hsem-dashboard`), or `None` if the dashboard was previously deleted by the user |

**Use cases:**

- Set up the HSEM dashboard during initial configuration
- Re-create the dashboard after deleting it by accident
- Write the dashboard YAML to a custom location

**Implementation notes:**

- The bundled YAML is written to disk every time the service is called, but the
  Lovelace dashboard entry is only created once.
- If the user deletes the dashboard via the HA UI, the service remembers that
  choice and will not recreate it automatically.
- You can still edit the generated YAML manually after creation.
- `dashboard_path` must resolve inside the HA config directory; paths that
  escape it (via `..` segments or a symlinked parent) are rejected before any
  file is written.

**Examples:**

```yaml
service: hsem.create_dashboard
response_variable: dashboard_result
```

```yaml
service: hsem.create_dashboard
data:
  dashboard_path: /config/ui_lovelace_minimalist/hsem.yaml
response_variable: dashboard_result
```

---

## 5. `hsem.export_diagnostics`

Exports a structured diagnostics dump containing the most recent planner input,
planner output, hardware write status, and integration version. All entity IDs
are redacted for safe sharing in issue reports.

**Schema:** No fields.

**Response:** A dict with the following structure:

| Key              | Type   | Description                                                                                                                        |
| ---------------- | ------ | ---------------------------------------------------------------------------------------------------------------------------------- |
| `hsem_version`   | `str`  | HSEM version from `manifest.json`                                                                                                  |
| `dump_timestamp` | `str`  | ISO-8601 timestamp of the dump                                                                                                     |
| `planner_input`  | `dict` | Latest `PlannerInput` (redacted)                                                                                                   |
| `planner_output` | `dict` | Latest `PlannerOutput` summary (redacted)                                                                                          |
| `apply_result`   | `dict` | Latest hardware write status summary                                                                                               |
| `ocpp_tls`       | `dict` | OCPP TLS mode, server certificate expiry, SANs, CA fingerprint and the CN-mismatch setting, or the setup error. Never key material |

**Example:**

```yaml
service: hsem.export_diagnostics
response_variable: diagnostics_result
```

**Replaying a dump.** `planner_input` is a complete, lossless serialisation of
the planner's input, so a dump can be re-run offline against any checkout:

```bash
python3 scripts/replay_planner_input.py path/to/diagnostics.json
```

This reports whether the dump still round-trips onto the current
`PlannerInput`, and checks the resulting plan against the invariants in
[the planner specification](planner-spec.md). See
[Planner Backtest Harness](backtest-harness.md).

---

## 6. `hsem.ocpp_debug_start_charging`

**Diagnostics-only.** Manually sends `RemoteStartTransaction` followed by
`SetChargingProfile` directly to the currently connected OCPP charger,
**bypassing the anti-flap state machine entirely**. Use this to check whether
a charger accepts a bare OCPP start command at all when the normal
planner-driven path doesn't seem to start it — it isolates a charger/protocol
problem from a planner-logic problem.

Because the anti-flap window is bypassed, the planner's own target still
applies on the next coordinator cycle and may immediately stop the charger
again if the plan calls for zero power. This service is not a substitute for
normal operation.

**Schema:**

| Field           | Required | Type           | Description                                                                     |
| --------------- | -------- | -------------- | ------------------------------------------------------------------------------- |
| `charger`       | No       | Select         | `"primary"` (default) or `"second"` — which EV's embedded OCPP server to target |
| `max_current_a` | No       | Integer (6–32) | Maximum charging current to request, in amperes. Default 16.                    |

**Raises:**

- `ServiceValidationError` when OCPP isn't enabled/configured for the selected EV, or no charger is currently connected.
- `HomeAssistantError` when the commands fail to reach the charger.

**Example:**

```yaml
service: hsem.ocpp_debug_start_charging
data:
  charger: primary
  max_current_a: 10
```

---

## 7. `hsem.ocpp_debug_stop_charging`

**Diagnostics-only.** Manually sends `RemoteStopTransaction` directly to the
currently connected OCPP charger, **bypassing the anti-flap state machine
entirely**. Use this to check whether a charger accepts a bare OCPP stop
command at all when the normal planner-driven path doesn't seem to stop it.

**Schema:**

| Field     | Required | Type   | Description                                                                     |
| --------- | -------- | ------ | ------------------------------------------------------------------------------- |
| `charger` | No       | Select | `"primary"` (default) or `"second"` — which EV's embedded OCPP server to target |

**Raises:**

- `ServiceValidationError` when OCPP isn't enabled/configured for the selected EV, or no charger is currently connected.
- `HomeAssistantError` when the command fails to reach the charger.

**Example:**

```yaml
service: hsem.ocpp_debug_stop_charging
data:
  charger: primary
```

---

## 8. `hsem.ocpp_debug_diagnostics`

**Diagnostics-only.** Asks the connected OCPP charger two questions and logs
its replies, for the case where the charger accepts every command HSEM sends
yet still delivers no power:

- **`GetConfiguration`** — the charger's own settings. Most useful keys:
  `SupportedFeatureProfiles` (does it implement SmartCharging at all?) and
  `ChargingScheduleAllowedChargingRateUnit` (does it expect amps or watts?
  HSEM always sends amps, which a watt-only charger can accept as
  schema-valid and then apply as nothing).
- **`GetCompositeSchedule`** — the limit the charger has _actually_ computed
  from every charging profile installed on the connector. This is the one
  question a `"status": "Accepted"` on `SetChargingProfile` cannot answer: a
  profile that was accepted _and applied_ reports the requested amps, while
  one accepted and silently ignored reports 0 (or the call is rejected).

Especially relevant when the charger sits in `SuspendedEVSE`, which OCPP 1.6
defines as the _EVSE_ — not the EV — withholding energy, explicitly listing
"a smart charging restriction" as a cause.

**Schema:**

| Field     | Required | Type   | Description                                                                     |
| --------- | -------- | ------ | ------------------------------------------------------------------------------- |
| `charger` | No       | Select | `"primary"` (default) or `"second"` — which EV's embedded OCPP server to target |

**Raises:**

- `ServiceValidationError` when OCPP isn't enabled/configured for the selected
  EV, or no charger is currently connected.
- `HomeAssistantError` when the queries fail to reach the charger.

Replies arrive asynchronously and are written to the HSEM log at warning level
as they come in, so they are visible without enabling DEBUG logging.

**Example:**

```yaml
service: hsem.ocpp_debug_diagnostics
data:
  charger: primary
```

---

## 9. `hsem.ocpp_debug_set_availability`

**Diagnostics-only.** Sets a connector Operative or Inoperative via OCPP
`ChangeAvailability` — the standard way a central system takes a connector
into or out of service.

Be aware of what this does _not_ cover: `Inoperative` maps to connector status
`Unavailable`, which is a different thing from `SuspendedEVSE`. A charger that
is already Operative but locally refusing to deliver power will answer
`Accepted` and change nothing. That outcome is still informative — it rules
availability out and points at a charger-local setting instead.

**Schema:**

| Field          | Required | Type          | Description                                                                     |
| -------------- | -------- | ------------- | ------------------------------------------------------------------------------- |
| `charger`      | No       | Select        | `"primary"` (default) or `"second"` — which EV's embedded OCPP server to target |
| `operative`    | No       | Boolean       | `true` (default) for Operative, `false` for Inoperative                         |
| `connector_id` | No       | Integer (0–8) | Connector to change. Default 1; `0` addresses the whole charge point            |

**Example:**

```yaml
service: hsem.ocpp_debug_set_availability
data:
  operative: true
```

---

## 10. `hsem.ocpp_debug_set_configuration`

**Diagnostics-only.** Writes a single OCPP configuration key on the charger via
`ChangeConfiguration`.

Deliberately generic: rather than HSEM guessing which vendor-specific key
governs a charger that ignores remote control, run
`hsem.ocpp_debug_diagnostics` first to list the keys your charger actually
exposes — including vendor-specific ones — then set whichever one matters here,
with no code change needed per charger model.

The charger's reply (`Accepted`, `Rejected`, `NotSupported`, or
`RebootRequired`) is written to the HSEM log.

**Schema:**

| Field     | Required | Type   | Description                                                                     |
| --------- | -------- | ------ | ------------------------------------------------------------------------------- |
| `charger` | No       | Select | `"primary"` (default) or `"second"` — which EV's embedded OCPP server to target |
| `key`     | Yes      | String | Configuration key name, exactly as the charger reports it                       |
| `value`   | Yes      | String | New value. OCPP 1.6 carries all configuration values as strings                 |

**Example:**

```yaml
service: hsem.ocpp_debug_set_configuration
data:
  key: AuthorizeRemoteTxRequests
  value: "false"
```

---

## 11. `hsem.ocpp_debug_set_current`

**Diagnostics-only**, and the narrowest of these services: it sends a
`SetChargingProfile` and **nothing else** — no `RemoteStartTransaction`, no
vendor force-state write.

That isolation is the whole point. Every other action changes more than one
thing at once, so a visible change can't be attributed. This one answers a
single question: **does the charger honour charging profiles at all?**

`current_a: 0` is the interesting case — a 0 A profile is the generic,
standards-only way an energy-management system says "draw nothing". If it
stops a charge on its own, no vendor-specific handling is needed for that
charger.

**Schema:**

| Field       | Required | Type         | Description                                                                                                        |
| ----------- | -------- | ------------ | ------------------------------------------------------------------------------------------------------------------ |
| `charger`   | No       | Select       | `"primary"` (default) or `"second"` — which EV's embedded OCPP server to target                                    |
| `current_a` | Yes      | `0`, or 6–32 | Current limit. `0` means draw nothing; 1–5 A is rejected, since no charger delivers below its `MinChargingCurrent` |

A value above the charger's own `Station-MaxCurrent` is accepted but cannot
raise the limit, so it will look like nothing happened — HSEM logs a warning
naming both numbers when that is why.

**Example:**

```yaml
service: hsem.ocpp_debug_set_current
data:
  current_a: 8
```

---

## 12. `hsem.ocpp_generate_client_certificate`

Signs a client key and certificate for one charge point with the CA HSEM
generates for its OCPP servers, for chargers that need a certificate uploaded
before they can use mutual TLS (`hsem_ocpp_tls_mode: mtls`, issue #1341). The
CA is created first if it does not exist yet, so this works before TLS is
turned on and with no charger connected.

The files are written to `clients/` next to the CA: `/ssl/hsem/ocpp/clients/`,
or `<config>/ssl/hsem/ocpp/clients/` when `/ssl` does not exist. The key is
readable by the owner only.

**Schema:**

| Field     | Required | Type    | Description                                                                                                                            |
| --------- | -------- | ------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `cpid`    | Yes      | String  | Charge point ID the charger connects with (last segment of its URL path). Becomes the certificate's CN. Letters, digits, `.`, `_`, `-` |
| `replace` | No       | Boolean | Overwrite an existing certificate and key for this charge point (default `false`)                                                      |

**Response:**

| Key                   | Type  | Description                                           |
| --------------------- | ----- | ----------------------------------------------------- |
| `certificate_path`    | `str` | Client certificate (PEM)                              |
| `key_path`            | `str` | Client private key (PEM). Contents are never returned |
| `ca_certificate_path` | `str` | HSEM's CA certificate, for the charger's trust store  |

The call fails when a certificate for `cpid` already exists and `replace` is
off, and when `hsem_ocpp_tls_client_ca_path` is set: chargers would then be
checked against your own CA, which HSEM cannot sign with.

**Example:**

```yaml
service: hsem.ocpp_generate_client_certificate
data:
  cpid: "222819"
response_variable: client_cert
```

---

## 13. `hsem.what_if`

Answers "would 5 kWh more battery pay off?" and "what does my cycle cost
setting cost me?" from the days HSEM has recorded.

For each recorded day the service solves the cheapest possible run of the day
twice, over the same house load, PV and prices: once with the battery as
configured and once with the scenario battery. It returns the difference.

**It is read-only.** It never writes to the inverter, never changes the active
plan and never starts a planner cycle, in read-only mode or outside it. The
solves run in a worker thread, one what-if at a time. A second call while one
is running is refused. Stopping the script or automation that called it stops
the run before its next solve, and so does unloading the integration. A new
call is accepted once the worker thread has returned.

**Read the result as an estimate.** Both costs are perfect-foresight bounds:
the best any controller could have done with full knowledge of the day. HSEM
does not reach either of them. The difference between the two says what the
hardware change is worth to a perfect controller, which is an upper estimate
of what it is worth to you.

**Schema:**

| Field                      | Type    | Required | Description                                                                                |
| -------------------------- | ------- | -------- | ------------------------------------------------------------------------------------------ |
| `history_days`             | `int`   | No       | Completed days before today to replay, 1 to 90. Default 30                                 |
| `usable_capacity_kwh`      | `float` | No       | Usable capacity of the scenario battery, between end-of-discharge SoC and charging cut-off |
| `max_charge_power_w`       | `float` | No       | Maximum charge power of the scenario battery                                               |
| `max_discharge_power_w`    | `float` | No       | Maximum discharge power of the scenario battery                                            |
| `charge_efficiency_pct`    | `float` | No       | Charge efficiency of the scenario battery, 1 to 100                                        |
| `discharge_efficiency_pct` | `float` | No       | Discharge efficiency of the scenario battery, 1 to 100                                     |
| `cycle_cost_per_kwh`       | `float` | No       | Battery wear per kWh of throughput in the scenario                                         |
| `capacity_start_kwh`       | `float` | No       | First usable capacity of a range. Give start, end and step together, at most 10 sizes      |
| `capacity_end_kwh`         | `float` | No       | Last usable capacity of the range                                                          |
| `capacity_step_kwh`        | `float` | No       | Distance between two capacities of the range                                               |
| `capacity_price_per_kwh`   | `float` | No       | Price of one kWh of usable capacity. Adds investment and payback years to each row         |

A field left out keeps the configured value. Give at least one override or a
capacity range. With a range, every size also carries the other overrides.
`usable_capacity_kwh` and a range cannot be combined.

**Response:**

| Key                  | Type    | Description                                                                                                                     |
| -------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `history_days`       | `int`   | Days asked for                                                                                                                  |
| `days_compared`      | `int`   | Days that were replayed                                                                                                         |
| `first_day_compared` | `str`   | First replayed day, `null` when none was                                                                                        |
| `last_day_compared`  | `str`   | Last replayed day, `null` when none was                                                                                         |
| `days_skipped`       | `int`   | Days that were not                                                                                                              |
| `days_timed_out`     | `int`   | Of those, days lost to a time limit (`oracle_timeout` and `run_time_limit`)                                                     |
| `skipped_reasons`    | `dict`  | Count per reason, see below                                                                                                     |
| `current`            | `dict`  | The configured battery: capacity, power, efficiencies and cycle cost                                                            |
| `scenarios`          | `list`  | One row per scenario: its limits and the figures below                                                                          |
| `days`               | `list`  | Per compared day: `date`, `current_grid_cost`, `scenario_grid_cost`, `grid_savings`, `savings_after_wear`. Only without a range |
| `note`               | `str`   | States that both costs are perfect-foresight bounds and that yearly figures carry the season                                    |
| `duration_seconds`   | `float` | How long the run took                                                                                                           |

Each row of `scenarios` holds two savings figures, and they answer different
questions:

| Key                     | Meaning                                                                           |
| ----------------------- | --------------------------------------------------------------------------------- |
| `current_grid_cost`     | Grid bill of the configured battery: import paid minus export earned              |
| `scenario_grid_cost`    | Grid bill of the scenario battery                                                 |
| `grid_savings`          | `current_grid_cost − scenario_grid_cost`: money not paid to the grid              |
| `current_wear_cost`     | Battery wear of the configured side: its cycle cost times the energy cycled       |
| `scenario_wear_cost`    | Battery wear of the scenario side                                                 |
| `savings_after_wear`    | `grid_savings` minus the extra wear of the scenario                               |
| `grid_savings_per_year` | `grid_savings` scaled from the compared days to 365. `null` below 7 compared days |

Both sides are solved with their wear in the objective, so neither cycles the
battery for a price difference smaller than the wear.

With `capacity_price_per_kwh`, each row also has `extra_capacity_kwh`,
`investment` (extra capacity times the price) and `payback_years`
(`investment ÷ grid_savings_per_year`). `payback_years` is `null` when the
scenario is not larger, saves nothing, or fewer than 7 days were compared.

The payback is counted from the grid savings, not from `savings_after_wear`.
The cycle cost HSEM derives is the battery's purchase price spread over its
life. Subtracting it and then dividing the purchase price by what is left
would charge the battery twice.

**The yearly figure carries the season.** It is the mean of the compared days
times 365. 30 days in June say little about December. `days_compared`,
`first_day_compared` and `last_day_compared` say what the figure rests on.
Recording keeps 90 days, so it never rests on a full year.

**Skipped days.** Every day of the asked-for history is either compared or
counted under a reason:

| Reason                         | Meaning                                                        |
| ------------------------------ | -------------------------------------------------------------- |
| `not_recorded`                 | HSEM holds no complete record of the day                       |
| `insufficient_coverage`        | Part of the day was not observed, for example during a restart |
| `stored_energy_outside_limits` | The recorded SoC does not fit the configured capacity          |
| `energy_balance_residual`      | The meters contradict each other                               |
| `oracle_unsolved`              | The solver found no run of the day for one of the batteries    |
| `oracle_timeout`               | A solve did not finish in 10 seconds                           |
| `run_time_limit`               | The whole run reached 10 minutes before it got to the day      |
| `scipy_unavailable`            | The solver is not installed                                    |

A day counts only when it could be solved for every scenario of the call, so
all rows of a range cover the same days.

**What is replayed.** HSEM keeps the meter readings of each completed day
(grid import and export, SoC, both prices, PV) for 90 days in
`.storage/hsem_what_if_history.json`. Recording starts with the version that
added this service: days before that are `not_recorded`. A day is kept only
when every slot of it was observed. The battery's rated capacity and
efficiencies of that day are kept with it. They turn the day's SoC into
energy, so a battery that is changed later does not distort the days before
the change. Both sides of the comparison use the limits configured today.

**How a day is compared.** The house load is rebuilt from the meters the same
way the [performance sensors](sensors-reference.md#performance-sensors) do
it. Each run starts with the stored energy the battery really had and must
end with at least what it really ended with, counted from the
end-of-discharge floor and capped at what the battery in question can hold.
Extra capacity therefore starts each day empty: what it would carry from one
day to the next is not counted. The grid limits (fuse, export limit, battery
export settings) are the configured ones for both sides.

Where a meter read more battery power in a slot than the configured limit,
that excess is added to the limit of both sides in that slot. Entering the
configured power as an override therefore saves exactly nothing.

**Limits of the estimate:**

- The cycle cost of the configured side is the one the planner uses. The
  scenario keeps it unless `cycle_cost_per_kwh` is given, also when the
  capacity changes.
- Efficiencies are single numbers per direction, as in the planner.
- An EV is part of the fixed load: it is not rescheduled.

**Run time.** One solve per day for the configured battery plus one per
scenario. 90 days of 15-minute slots with one scenario take 15 to 20 seconds
on a desktop CPU. A range of 10 sizes takes about eight times as long. Held to
a fifth of one desktop core, one scenario over 90 days took 2.5 minutes:
expect that order on a Raspberry Pi.

A run stops after 10 minutes. The days it did not reach are counted as
`run_time_limit`, and the rows cover the days that were reached. On a slow
device, ask for fewer days or fewer sizes when that happens.

The planner keeps running meanwhile. The solver releases Python's interpreter
lock while it works, so the event loop is not held up: measured next to a
running replay, it was never more than 6 ms late.

**Examples:**

```yaml
# Would 5 kWh more pay off at 2500 per kWh?
service: hsem.what_if
data:
  history_days: 60
  usable_capacity_kwh: 15
  capacity_price_per_kwh: 2500
response_variable: what_if
```

```yaml
# Which size is worth it?
service: hsem.what_if
data:
  history_days: 30
  capacity_start_kwh: 5
  capacity_end_kwh: 25
  capacity_step_kwh: 5
  capacity_price_per_kwh: 2500
response_variable: what_if
```

```yaml
# What does my cycle cost setting cost me?
service: hsem.what_if
data:
  cycle_cost_per_kwh: 0
response_variable: what_if
```

---

## Automation examples

### Disable battery discharging during expensive evening hours (with auto-expiry)

```yaml
alias: "HSEM: Prevent discharge during peak"
trigger:
  - platform: time
    at: "16:00:00"
action:
  - service: hsem.set_temporary_override
    data:
      working_mode: batteries_wait_mode
      duration_minutes: 480 # auto-resume at midnight
```

### Force charge for the next hour ahead of a known price spike

```yaml
alias: "HSEM: Pre-charge before price spike"
trigger:
  - platform: time
    at: "06:00:00"
action:
  - service: hsem.set_temporary_override
    data:
      working_mode: batteries_charge_grid
      duration_minutes: 60
```

### Return to automatic control at midnight

```yaml
alias: "HSEM: Return to auto at midnight"
trigger:
  - platform: time
    at: "00:00:00"
action:
  - service: hsem.clear_override
```

### Force recalculation after price update

```yaml
alias: "HSEM: Re-plan after price update"
trigger:
  - platform: state
    entity_id: sensor.stromligning_current_price_vat
action:
  - service: hsem.force_recalculation
```

### Export diagnostics for troubleshooting

```yaml
alias: "HSEM: Export diagnostics on error"
trigger:
  - platform: state
    entity_id: sensor.hsem_degraded_mode_sensor
    to: "error"
action:
  - service: hsem.export_diagnostics
    response_variable: diag
  - service: persistent_notification.create
    data:
      title: "HSEM Error Diagnostics"
      message: "{{ diag }}"
```
