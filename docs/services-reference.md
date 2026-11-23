# HSEM Services Reference

HSEM exposes fifteen Home Assistant services that allow automation, script, and
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
| `hsem.export_data`                      | Back up the learned data and configuration to one file             | Dict     |
| `hsem.import_data`                      | Restore the learned data from a backup file                        | Dict     |
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

**Schema:**

| Field        | Type   | Required | Description                                                                                         |
| ------------ | ------ | -------- | --------------------------------------------------------------------------------------------------- |
| `write_file` | `bool` | No       | Also write the dump to `<config>/hsem_debug/` and create a notification. Default `false`. See below |

**Response:** A dict with the following structure:

| Key                  | Type   | Description                                                                                                                        |
| -------------------- | ------ | ---------------------------------------------------------------------------------------------------------------------------------- |
| `hsem_version`       | `str`  | HSEM version from `manifest.json`                                                                                                  |
| `dump_timestamp`     | `str`  | ISO-8601 timestamp of the dump                                                                                                     |
| `planner_input`      | `dict` | Latest `PlannerInput` (redacted)                                                                                                   |
| `planner_output`     | `dict` | Latest `PlannerOutput` summary (redacted)                                                                                          |
| `apply_result`       | `dict` | Latest hardware write status summary                                                                                               |
| `ocpp_tls`           | `dict` | OCPP TLS mode, server certificate expiry, SANs, CA fingerprint and the CN-mismatch setting, or the setup error. Never key material |
| `price_sensors`      | `dict` | What each price sensor published last cycle, per side: `published`, `predictions` or `none` (`docs/price-attributes.md`)           |
| `efficiency_learner` | `dict` | Battery efficiency learner: round-trip status, and status, sample count and power-step count per direction                         |
| `energy_tracker`     | `dict` | Energy tracker: number of days held, first and last day                                                                            |
| `price_history`      | `dict` | Price history: number of recorded slots, retention in days and the start of a running price outage                                 |

The last three hold counts and status only, never the recorded series.

With `write_file: true` the response also holds `debug_file`, the path of the
written file.

**Example:**

```yaml
service: hsem.export_diagnostics
response_variable: diagnostics_result
```

**Debug file for a bug report.** With `write_file: true` the same redacted dump
is written to `<config>/hsem_debug/hsem_debug_<date>_<time>.json`, and a
persistent notification shows the path and a link that opens a new issue on
GitHub. The link is prefilled with the HSEM and Home Assistant versions and
nothing else: no entity IDs, prices or configuration. Attach the file to the
issue yourself.

The file is redacted once more than the response: fields named like a secret
(passphrase, credentials, auth, charge point ID) and any PEM block are
replaced. The certificate facts under `ocpp_tls` stay readable.

```yaml
service: hsem.export_diagnostics
data:
  write_file: true
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

## 13. `hsem.export_data`

Writes one JSON file with everything HSEM has learned, so it can be moved to
another Home Assistant or kept before a reinstall.

**Schema:** No fields.

**File:** `<config>/hsem_backups/hsem_backup_<date>_<time>.json`

| Key                | Description                                                                                    |
| ------------------ | ---------------------------------------------------------------------------------------------- |
| `schema`           | Always `hsem_backup`                                                                           |
| `schema_version`   | Version of the file layout, currently `2`. Version `1` holds the first five history files only |
| `hsem_version`     | HSEM version that wrote the file                                                               |
| `created`          | ISO-8601 timestamp                                                                             |
| `reason`           | `manual`, `before_import` or `deleted`                                                         |
| `history`          | The nine history files from `.storage`, listed below                                           |
| `solar_corrector`  | The learned solar forecast correction, or `null`                                               |
| `forecast_tracker` | The forecast accuracy records, or `null`                                                       |
| `average_sensors`  | The daily samples of the hourly consumption averages, keyed `<hour_start>_<hour_end>_<days>`   |
| `configuration`    | `entry_version`, `entry_minor_version`, and the config entry `data` and `options`, redacted    |

The history files, by their key under `history`:

| File                            | Holds                                             |
| ------------------------------- | ------------------------------------------------- |
| `hsem_prediction_history.json`  | Consumption prediction accuracy                   |
| `hsem_financial_history.json`   | Daily cost and revenue                            |
| `hsem_savings_history.json`     | Savings against the baseline                      |
| `hsem_daily_history.json`       | Daily plan against actual                         |
| `hsem_performance_history.json` | Planner performance scores                        |
| `hsem_battery_efficiency.json`  | Learned battery efficiency (schema version 2)     |
| `hsem_energy_history.json`      | Daily energy for the monthly KPIs (version 2)     |
| `hsem_price_history.json`       | Recorded prices for the price profile (version 2) |
| `hsem_what_if_history.json`     | Recorded days for `hsem.what_if` (version 2)      |

A file is `null` when it does not exist yet.

A backup reads the files from disk. HSEM saves the energy history at most
every 120 seconds, so a backup can miss up to two minutes of it. The same
holds for other stores between two saves.

The solar corrector, the forecast tracker and the average sensors live in the
restore state of HSEM's sensors, not in a file. They are `null` only when HSEM
has not run since Home Assistant started.

A backup never holds OCPP certificates, private keys or tokens. HSEM does not
read the certificate files for a backup. Redacted in `configuration`: secrets,
charge point IDs (`hsem_ocpp_cpid`) and certificate paths.

Entity IDs are kept, so a backup can restore the full configuration. They show
which devices you have. Do not post a backup publicly; for a bug report use
the debug file of `hsem.export_diagnostics`, which redacts them.

**Not in a backup.** The battery capacity learner keeps no state. Other
sensors that restore their last value after a restart (EV state of charge,
PV curtailment, net consumption and similar) are not included: they hold a
last reading, not learned data, and fill again within a cycle.

**Response:** `backup_path`, the path of the written file.

**Automatic backups.** HSEM writes the same file on its own in two cases:

- `hsem_backup_<date>_<time>_before_import.json` before `hsem.import_data`
  replaces anything, and before a restore in the setup flow replaces history
  files that are already on disk.
- `hsem_backup_<date>_<time>_deleted.json` when the HSEM config entry is
  deleted.

HSEM never deletes backup files. Remove old ones by hand.

**Example:**

```yaml
service: hsem.export_data
response_variable: backup
```

---

## 14. `hsem.import_data`

Replaces the learned data with the contents of a backup file.

**Schema:**

| Field                   | Type   | Required | Description                                                                |
| ----------------------- | ------ | -------- | -------------------------------------------------------------------------- |
| `filename`              | `str`  | Yes      | Name of a file in `<config>/hsem_backups/`. A file name only, without path |
| `restore_configuration` | `bool` | No       | Also restore the configuration options. Default `false`                    |

**What happens, in order:**

1. The file is read and checked. It is rejected, and nothing changes, when the
   `schema` is not `hsem_backup`, when `schema_version` is newer than this HSEM
   reads, or when the backup was written by a newer HSEM version than the one
   installed.
2. The import waits for a planning cycle that is still running, so the cycle
   cannot write its own data over the import.
3. The current data is saved as `hsem_backup_<date>_<time>_before_import.json`.
4. HSEM is unloaded and the history files are replaced, all or nothing: if one
   file cannot be written, the others are put back. A history file that is
   `null` in the backup is left as it is. So is a file the backup does not
   name: a backup with `schema_version` `1` restores its five files and leaves
   the battery efficiency, energy, price and what-if history untouched.
5. HSEM starts again and loads the solar correction, the forecast accuracy
   records and the hourly averages from the backup. If the start fails and
   Home Assistant retries it later, they are loaded then.

**Price history.** The price history belongs to the price sensors it was
recorded from. When the backup's import or export price sensor, or its unit,
differs from the one HSEM uses, HSEM would discard the imported prices on its
next cycle. The import therefore leaves the current price history in place and
lists the file under `history_files_skipped` with the reason. With
`restore_configuration` the comparison uses the price sensors from the backup.
All other files are imported as usual.

Only one import runs at a time. A second call is rejected while one is
running. If Home Assistant stops while the files are being replaced, HSEM puts
the history from before the import back on its next start.

The import is rejected when HSEM is retrying or failed its setup, or failed to
unload. Fix that first. A disabled HSEM can be imported into: it stays
disabled and loads the learned state when it is enabled.

**Restoring the configuration.** With `restore_configuration: true` the options
from the backup are merged into the current options, entity IDs included.
Redacted values cannot be restored: secrets, charge point IDs and certificate
paths keep their current value and are listed in the response. Entity IDs
that do not exist there show up as missing entities until you correct them in
the options.

**On a new Home Assistant.** Copy the backup file into
`<config>/hsem_backups/` and add the HSEM integration. The setup flow then
offers **Restore from a backup**, which creates HSEM with the configuration
and the learned data from the file. It runs the checks and the file replace of
this action. See
[Restore from a backup](config-flow-reference.md#restore-from-a-backup).

The options are only restored when the backup was written with the same config
entry version as the installed HSEM, and a minor version that is not newer.
Otherwise the call is rejected and nothing changes. Import again without
`restore_configuration` to restore the learned data only.

**Response:**

| Key                          | Description                                           |
| ---------------------------- | ----------------------------------------------------- |
| `imported_from`              | The file name that was imported                       |
| `before_import_path`         | Path of the backup taken before the import            |
| `history_files_restored`     | The history files that were replaced                  |
| `history_files_skipped`      | History files left as they were, each with the reason |
| `learned_state_restored`     | Learners loaded after the restart                     |
| `learned_state_pending`      | Learners that load when HSEM next starts              |
| `configuration_restored`     | Whether the options were restored                     |
| `configuration_keys_skipped` | Options left unchanged because of redaction           |

**Example:**

```yaml
service: hsem.import_data
data:
  filename: hsem_backup_20261120_143000.json
response_variable: import_result
```

---

## 15. `hsem.what_if`

Answers "would 5 kWh more battery pay off?" and "what does my cycle cost
setting cost me?" from the days HSEM has recorded.

For each recorded day the service solves the cheapest possible run of the day
twice, over the same house load, PV and prices: once with the battery as
configured and once with the scenario battery. It returns the difference.

HSEM keeps 90 measured days. A day of the asked-for history without a
measured record is rebuilt from Home Assistant's hourly long-term statistics,
so the history can reach back up to 365 days. Such days are marked and their
totals are listed apart: see _Days rebuilt from statistics_ below.

**It is read-only.** It never writes to the inverter, never changes the active
plan and never starts a planner cycle, in read-only mode or outside it.
Rebuilding a day only reads the recorder's statistics and the kept prices: a
rebuilt day is not stored. The solves run in a worker thread of their own, one
what-if at a time. The thread is not one of Home Assistant's shared pool, so a
long run takes no worker from the planner or from other integrations. It is
made on the first call and ends when the integration is unloaded or Home
Assistant stops. A second call while one is running is refused. Stopping the script or automation that called it
stops the run before its next solve, and so does unloading the integration. A
new call is accepted once the worker thread has returned.

**Read the result as an estimate.** Both costs are perfect-foresight bounds:
the best any controller could have done with full knowledge of the day. HSEM
does not reach either of them. The difference between the two says what the
hardware change is worth to a perfect controller, which is an upper estimate
of what it is worth to you.

**Schema:**

| Field                      | Type    | Required | Description                                                                                |
| -------------------------- | ------- | -------- | ------------------------------------------------------------------------------------------ |
| `history_days`             | `int`   | No       | Completed days before today to replay, 1 to 365. Default 30                                |
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

| Key                     | Type    | Description                                                                                                                                         |
| ----------------------- | ------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `history_days`          | `int`   | Days asked for                                                                                                                                      |
| `days_compared`         | `int`   | Days that were replayed                                                                                                                             |
| `days_measured`         | `int`   | Of those, days HSEM measured itself                                                                                                                 |
| `days_from_statistics`  | `int`   | Of those, days rebuilt from hourly statistics                                                                                                       |
| `days_on_todays_limits` | `int`   | Of those, days solved with today's SoC and power limits: see _Limits of each day_                                                                   |
| `first_day_compared`    | `str`   | First replayed day, `null` when none was                                                                                                            |
| `last_day_compared`     | `str`   | Last replayed day, `null` when none was                                                                                                             |
| `months_compared`       | `list`  | The months the replayed days fall in, each as `month` (`2026-06`) and `days`                                                                        |
| `days_skipped`          | `int`   | Days that were not                                                                                                                                  |
| `days_timed_out`        | `int`   | Of those, days lost to a time limit (`oracle_timeout` and `run_time_limit`)                                                                         |
| `skipped_reasons`       | `dict`  | Count per reason, see below                                                                                                                         |
| `current`               | `dict`  | The battery as configured today: capacity, power, efficiencies and cycle cost                                                                       |
| `scenarios`             | `list`  | One row per scenario: its limits and the figures below                                                                                              |
| `days`                  | `list`  | Per compared day: `date`, `source`, `limits`, `current_grid_cost`, `scenario_grid_cost`, `grid_savings`, `savings_after_wear`. Only without a range |
| `note`                  | `str`   | States that both costs are perfect-foresight bounds and that yearly figures carry the season                                                        |
| `duration_seconds`      | `float` | How long the run took                                                                                                                               |

Each row of `scenarios` holds two savings figures, and they answer different
questions:

| Key                     | Meaning                                                                            |
| ----------------------- | ---------------------------------------------------------------------------------- |
| `current_grid_cost`     | Grid bill of the configured battery: import paid minus export earned               |
| `scenario_grid_cost`    | Grid bill of the scenario battery                                                  |
| `grid_savings`          | `current_grid_cost − scenario_grid_cost`: money not paid to the grid               |
| `current_wear_cost`     | Battery wear of the configured side: its cycle cost times the energy cycled        |
| `scenario_wear_cost`    | Battery wear of the scenario side                                                  |
| `savings_after_wear`    | `grid_savings` minus the extra wear of the scenario                                |
| `grid_savings_per_year` | `grid_savings` scaled from the compared days to 365. `null` below 7 compared days  |
| `measured`              | The six cost figures above over the measured days only, with their count as `days` |
| `from_statistics`       | The same over the days rebuilt from statistics                                     |

The figures of a row cover all compared days, and so do the yearly figure and
the payback. `measured` and `from_statistics` split them by where the day
came from, so the coarser rebuilt days can be judged apart.

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
`first_day_compared`, `last_day_compared` and `months_compared` say what the
figure rests on. A payback counted from summer months only is a summer
payback. Measured days cover 90 days at most. Days rebuilt from statistics can
fill a year, as far back as HSEM has kept hourly prices.

**Skipped days.** Every day of the asked-for history is either compared or
counted under a reason:

| Reason                         | Meaning                                                                        |
| ------------------------------ | ------------------------------------------------------------------------------ |
| `not_recorded`                 | HSEM holds no complete record of the day, and the recorder is not loaded       |
| `price_incomplete`             | No measured record, and no real price for at least one hour of the day         |
| `statistics_incomplete`        | No measured record, and the statistics lack a house load hour or have a PV gap |
| `insufficient_coverage`        | Part of the day was not observed, for example during a restart                 |
| `stored_energy_outside_limits` | The recorded SoC does not fit the capacity the day is solved with              |
| `energy_balance_residual`      | The meters contradict each other                                               |
| `oracle_unsolved`              | The solver found no run of the day for one of the batteries                    |
| `oracle_timeout`               | A solve did not finish in 10 seconds                                           |
| `run_time_limit`               | The whole run reached 10 minutes before the day was done                       |
| `scipy_unavailable`            | The solver is not installed                                                    |

A day counts only when it could be solved for every scenario of the call, so
all rows of a range cover the same days.

With the recorder loaded, every day without a measured record is tried from
statistics. It then carries `price_incomplete` or `statistics_incomplete`,
also where the performance scoring had a reason of its own for the day.

**What is replayed.** HSEM keeps the meter readings of each completed day
(grid import and export, SoC, both prices, PV) for 90 days in
`.storage/hsem_what_if_history.json`. Recording starts with the version that
added this service. A day is kept only when every slot of it was observed.
The battery's rated capacity and efficiencies of that day are kept with it.
They turn the day's SoC into energy, so a battery that is changed later does
not distort the days before the change.

**Limits of each day.** A day also keeps the end-of-discharge SoC, the
charging cut-off SoC and the charge and discharge power limits it was
recorded with. Both sides of that day are solved with the SoC window of the
day. A limit you moved since is therefore not counted as a gain or a loss,
and a day whose SoC lies outside today's limits is still compared.

The size of a scenario is read against today's battery and carried to the day
as a ratio. Example: today 5 to 100 % of 10 kWh gives 9.5 kWh usable, and the
call asks for 14.5 kWh, so `extra_capacity_kwh` is 5. On a day recorded with
20 to 100 % the configured side has 8 kWh and the scenario 8 × 14.5 ÷ 9.5 =
12.2 kWh. It does not get the full 14.5 kWh there: that would credit 6.5 kWh
of extra capacity against 5 kWh paid for.

A power limit left out of the call keeps the day's value for that day.

The limits are stamped when the day is scored, from the planner input of that
moment. A limit changed during a day applies to the whole day, and to the day
before it when that day is scored after the change.

Days recorded before HSEM stored the limits have none. They are solved with
today's limits, as are all days rebuilt from statistics.
`days_on_todays_limits` counts both, and each entry of `days` says `recorded`
or `today` under `limits`. The history file is version 2. A version 1 file is
still read: its days load without limits.

**Days rebuilt from statistics.** A day without a measured record is rebuilt
for the run when both of these hold:

- HSEM has a real price for every hour of the day. It keeps the hourly mean
  of the published import and export prices for 365 days in the same file. A
  price that was estimated or held over from an outage is never used. A day
  with one hour missing is skipped as `price_incomplete`.
- The house consumption power sensor has an hourly statistic for every hour
  of the day. Where the house power leaves the EV chargers out, the chargers
  need one too. Otherwise the day is skipped as `statistics_incomplete`. An
  hour above 50 kW counts as missing.

When a PV sensor is configured (PV energy or PV power), the day also needs PV
statistics. It is skipped as `statistics_incomplete` when it has no PV hour
at all, or when an hour is missing between its first and its last PV hour. A
renamed PV sensor would otherwise turn its old days into days of pure grid
import. The hours before the first and after the last PV hour of the day
count as no production: an inverter that is unavailable at night leaves no
statistics for them. A site without any PV sensor is replayed without PV.

Only the house and PV statistics are read. The grid meters and the battery
power are not: the grid flows of a rebuilt day follow from load and PV.

Hourly prices are kept from the version that added this, and the price
history HSEM already had (up to 56 days) is used from the first call. The
history therefore reaches back about 8 weeks at first and grows by a day per
day up to 365.

A rebuilt day is the day with an idle battery: the load is the house
consumption, the PV is the PV meter or the PV power, and the grid flows are
what the two leave. Every run of the day starts and ends with an empty
battery.

Such a day is coarser than a measured one:

- One slot is one hour. Peaks of price and load inside an hour are averaged
  out, which tends to understate what a battery can earn with 15-minute
  prices.
- It is solved with today's battery limits, capacity and efficiencies.
- A time zone whose offset is not a whole hour has no rebuilt days: the
  statistics are kept per UTC hour.

`source` in `days` is `measured` or `statistics`, and each row lists the two
groups apart under `measured` and `from_statistics`.

**How a day is compared.** The house load of a measured day is rebuilt from
the meters the same way the [performance sensors](sensors-reference.md#performance-sensors) do
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
- The kept hourly prices belong to one price source. When a price entity or
  its unit changes they are dropped, by the same rule as the price history,
  and the rebuilt days start again from that day. Measured days keep the
  prices they were recorded with for their 90 days.
- An EV is part of the fixed load: it is not rescheduled.

**Run time.** One solve per day for the configured battery plus one per
scenario: 180 solves for 90 days with one scenario, 990 with a range of 10
sizes. A day rebuilt from statistics has 24 slots where a measured day of
15-minute slots has 96, so its solves are smaller. They were not measured
separately. Measured days are replayed first, the newest first, then the
rebuilt days, the newest first. A long history that runs into the 10-minute
limit therefore loses rebuilt days before measured ones and old days before
new ones. They are counted as `run_time_limit`, and `days_measured`,
`days_from_statistics` and `months_compared` say what the figures rest on.

Measured with `scripts/benchmark_what_if.py` on 90 synthetic days of
15-minute slots (evening peak, midday PV, cheap night), in a Linux container
on an Apple M3:

| CPU available       | Scenarios | Per solve, mean | Per solve, max | Whole run |
| ------------------- | --------- | --------------- | -------------- | --------- |
| One core            | 1         | 0.09 s          | 0.28 s         | 19 s      |
| One core            | 10        | 0.09 s          | 0.58 s         | 95 s      |
| A fifth of one core | 1         | 0.47 s          | 1.49 s         | 86 s      |
| A fifth of one core | 10        | 1.66 s          | 5.80 s         | 1652 s    |

**These are not measured on a Raspberry Pi.** The slow rows come from the
same machine held to 20 % of one core with Docker (`--cpus 0.2`), chosen as
a rough stand-in for the single-core speed of a Raspberry Pi 4. The limit
works by pausing the process, and that did not slow it evenly: per solve it
was between 5 and 25 times slower than the unlimited run, and a second full
run gave 105 s and 1752 s. Read the slow rows as a range, not as a figure for
your device. Real days can also be easier or harder for the solver than the
synthetic ones. To measure your own device, run
`python3 scripts/benchmark_what_if.py` on it.

What the numbers say: one scenario over 90 days fits a slow device with room
to spare. A range of 10 sizes over 90 days may not. At the slow end it needed
about 28 minutes, and the service stops after 10.

A run stops after 10 minutes. The limit is checked before every solve, so a
run passes it by one solve at most, which is 10 seconds. The days it did not
finish are counted as `run_time_limit`, and the rows cover the days that were
finished. On a slow device, ask for fewer days or fewer sizes when that
happens.

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
