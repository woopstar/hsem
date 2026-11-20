# HSEM Services Reference

HSEM exposes fourteen Home Assistant services that allow automation, script, and
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

| Key              | Type   | Description                                                                                                                        |
| ---------------- | ------ | ---------------------------------------------------------------------------------------------------------------------------------- |
| `hsem_version`   | `str`  | HSEM version from `manifest.json`                                                                                                  |
| `dump_timestamp` | `str`  | ISO-8601 timestamp of the dump                                                                                                     |
| `planner_input`  | `dict` | Latest `PlannerInput` (redacted)                                                                                                   |
| `planner_output` | `dict` | Latest `PlannerOutput` summary (redacted)                                                                                          |
| `apply_result`   | `dict` | Latest hardware write status summary                                                                                               |
| `ocpp_tls`       | `dict` | OCPP TLS mode, server certificate expiry, SANs, CA fingerprint and the CN-mismatch setting, or the setup error. Never key material |

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

| Key                | Description                                                                                   |
| ------------------ | --------------------------------------------------------------------------------------------- |
| `schema`           | Always `hsem_backup`                                                                          |
| `schema_version`   | Version of the file layout, currently `1`                                                     |
| `hsem_version`     | HSEM version that wrote the file                                                              |
| `created`          | ISO-8601 timestamp                                                                            |
| `reason`           | `manual`, `before_import` or `deleted`                                                        |
| `history`          | The five history files from `.storage`: prediction, financial, savings, daily and performance |
| `solar_corrector`  | The learned solar forecast correction, or `null`                                              |
| `forecast_tracker` | The forecast accuracy records, or `null`                                                      |
| `average_sensors`  | The daily samples of the hourly consumption averages, keyed `<hour_start>_<hour_end>_<days>`  |
| `configuration`    | `entry_version`, `entry_minor_version`, and the config entry `data` and `options`, redacted   |

The last three learners live in the restore state of HSEM's sensors, not in a
file. They are `null` only when HSEM has not run since Home Assistant started.

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
  replaces anything.
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
   `null` in the backup is left as it is.
5. HSEM starts again and loads the solar correction, the forecast accuracy
   records and the hourly averages from the backup. If the start fails and
   Home Assistant retries it later, they are loaded then.

The import is rejected when HSEM is retrying or failed its setup, or failed to
unload. Fix that first. A disabled HSEM can be imported into: it stays
disabled and loads the learned state when it is enabled.

**Restoring the configuration.** With `restore_configuration: true` the options
from the backup are merged into the current options, entity IDs included.
Redacted values cannot be restored: secrets, charge point IDs and certificate
paths keep their current value and are listed in the response. On a new Home
Assistant, set HSEM up through the config flow first, then import. Entity IDs
that do not exist there show up as missing entities until you correct them in
the options.

The options are only restored when the backup was written with the same config
entry version as the installed HSEM. Otherwise the call is rejected and nothing
changes. Import again without `restore_configuration` to restore the learned
data only.

**Response:**

| Key                          | Description                                 |
| ---------------------------- | ------------------------------------------- |
| `imported_from`              | The file name that was imported             |
| `before_import_path`         | Path of the backup taken before the import  |
| `history_files_restored`     | The history files that were replaced        |
| `learned_state_restored`     | Learners loaded after the restart           |
| `learned_state_pending`      | Learners that load when HSEM next starts    |
| `configuration_restored`     | Whether the options were restored           |
| `configuration_keys_skipped` | Options left unchanged because of redaction |

**Example:**

```yaml
service: hsem.import_data
data:
  filename: hsem_backup_20261120_143000.json
response_variable: import_result
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
