# HSEM Services Reference

HSEM exposes ten Home Assistant services that allow automation, script, and
manual control over the planner and hardware writes.

These services are **integration-level actions**: they operate on the single
configured HSEM instance and do not require a `target` (entity, device, or
area). The UI and YAML calls should only provide the `data` fields shown
below.

---

## Service listing

| Service                          | Description                                                     | Response |
| -------------------------------- | --------------------------------------------------------------- | -------- |
| `hsem.force_recalculation`       | Trigger an immediate full planner re-run                        | None     |
| `hsem.set_temporary_override`    | Force a specific battery working mode                           | None     |
| `hsem.clear_override`            | Return to automatic planner control                             | None     |
| `hsem.create_dashboard`          | Create or update the bundled Lovelace dashboard                 | Dict     |
| `hsem.export_diagnostics`        | Export structured diagnostic data                               | Dict     |
| `hsem.ocpp_debug_start_charging` | Diagnostics-only: manually start an OCPP charger                | None     |
| `hsem.ocpp_debug_stop_charging`  | Diagnostics-only: manually stop an OCPP charger                 | None     |
| `hsem.ocpp_debug_diagnostics`    | Diagnostics-only: query the charger's config and computed limit | None     |

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

| Mode                        | Behaviour                                                                                            |
| --------------------------- | ---------------------------------------------------------------------------------------------------- |
| `batteries_charge_grid`     | Force-charge the battery from the grid                                                               |
| `batteries_charge_solar`    | Charge the battery from PV only                                                                      |
| `batteries_discharge_mode`  | Discharge the battery to cover house load                                                            |
| `batteries_wait_mode`       | Battery idle by default; follows the configured **Wait mode behaviour** when selected by the planner |
| `ev_smart_charging`         | Prioritise EV charging                                                                               |
| `force_batteries_discharge` | Force-discharge the battery to the grid (export)                                                     |
| `force_export`              | Export all available energy to the grid                                                              |

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

| Field            | Required | Type   | Description                                                                            |
| ---------------- | -------- | ------ | -------------------------------------------------------------------------------------- |
| `dashboard_path` | No       | String | Absolute file path for the dashboard YAML. Defaults to `<config>/hsem_dashboard.yaml`. |

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

| Key                   | Type   | Description                          |
| --------------------- | ------ | ------------------------------------ |
| `integration_version` | `str`  | HSEM version from `manifest.json`    |
| `planner_input`       | `dict` | Latest `PlannerInput` (redacted)     |
| `planner_output`      | `dict` | Latest `PlannerOutput` (redacted)    |
| `hardware_writes`     | `dict` | Latest hardware write status summary |
| `timestamp`           | `str`  | ISO-8601 timestamp of the dump       |

**Example:**

```yaml
service: hsem.export_diagnostics
response_variable: diagnostics_result
```

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
    entity_id: sensor.energi_data_service
action:
  - service: hsem.force_recalculation
```

### Export diagnostics for troubleshooting

```yaml
alias: "HSEM: Export diagnostics on error"
trigger:
  - platform: state
    entity_id: sensor.hsem_degraded_mode
    to: "error"
action:
  - service: hsem.export_diagnostics
    response_variable: diag
  - service: persistent_notification.create
    data:
      title: "HSEM Error Diagnostics"
      message: "{{ diag }}"
```
