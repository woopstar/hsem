# HSEM Services Reference

HSEM exposes four Home Assistant services that allow automation, script, and
manual control over the planner and hardware writes.

---

## Service listing

| Service | Description | Response |
|---|---|---|
| `hsem.force_recalculation` | Trigger an immediate full planner re-run | None |
| `hsem.set_temporary_override` | Force a specific battery working mode | None |
| `hsem.clear_override` | Return to automatic planner control | None |
| `hsem.export_diagnostics` | Export structured diagnostic data | Dict |

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
target:
  entity_id: sensor.hsem_working_mode
```

---

## 2. `hsem.set_temporary_override`

Temporarily bypasses the automatic planner by writing a specific working mode
directly to the inverter. While the override is active, the planner output is
ignored.

**Schema:**

| Field | Required | Type | Description |
|---|---|---|---|
| `working_mode` | Yes | Select | One of the supported override modes |
| `duration_minutes` | No | Integer (1–1440) | Minutes until override auto-expires; planner resumes after expiry |


**Supported override modes:**

| Mode | Behaviour |
|---|---|
| `batteries_charge_grid` | Force-charge the battery from the grid |
| `batteries_charge_solar` | Charge the battery from PV only |
| `batteries_discharge_mode` | Discharge the battery to cover house load |
| `batteries_wait_mode` | Battery idle — neither charging nor discharging |
| `ev_smart_charging` | Prioritise EV charging |
| `force_batteries_discharge` | Force-discharge the battery to the grid (export) |
| `force_export` | Export all available energy to the grid |

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

## 4. `hsem.export_diagnostics`

Exports a structured diagnostics dump containing the most recent planner input,
planner output, hardware write status, and integration version. All entity IDs
are redacted for safe sharing in issue reports.

**Schema:** No fields.

**Response:** A dict with the following structure:

| Key | Type | Description |
|---|---|---|
| `integration_version` | `str` | HSEM version from `manifest.json` |
| `planner_input` | `dict` | Latest `PlannerInput` (redacted) |
| `planner_output` | `dict` | Latest `PlannerOutput` (redacted) |
| `hardware_writes` | `dict` | Latest hardware write status summary |
| `timestamp` | `str` | ISO-8601 timestamp of the dump |

**Example:**
```yaml
service: hsem.export_diagnostics
response_variable: diagnostics_result
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
      duration_minutes: 480  # auto-resume at midnight
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
