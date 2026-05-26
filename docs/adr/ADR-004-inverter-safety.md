# ADR-004: Inverter Safety — Layered Hardware Write Protection

**Status:** Accepted

**Date:** 2026-05-11

**Deciders:** Project maintainers

---

## Context

The HSEM integration controls a Huawei Solar inverter and battery system by writing to hardware registers: selecting the battery working mode, adjusting export power limits, and setting TOU (Time-Of-Use) charging/discharging periods. Writing incorrect or unsafe values to inverter registers can cause:

- **Battery damage** — discharging below the minimum SoC floor or overcharging beyond the maximum SoC ceiling.
- **Electrical safety risk** — exporting power to the grid at unsafe times (e.g., during grid maintenance) or exceeding inverter power limits.
- **Data corruption** — writing partial or inconsistent TOU period sets that leave the inverter in an undefined schedule state.
- **User financial loss** — exporting at a loss when import prices are higher than export prices, or importing from the grid at peak prices when battery energy is available.

The system must also handle degraded operating conditions gracefully:

- Sensor unavailability (e.g., SoC sensor temporarily offline)
- Missing forecast data (e.g., tomorrow's prices not yet published)
- User override modes (read-only mode, dry-run diagnostics)
- Inverter communication delays or failures

We needed a safety architecture that prevents unsafe writes while still allowing normal operation under degraded but recoverable conditions.

---

## Decision

We implement a **four-layer, hierarchical safety system** where each layer independently blocks or restricts hardware writes. A write is only executed if all applicable layers allow it.

```
Layer 1 — Degraded Mode Classification (health check)
Layer 2 — Read-Only / Dry-Run Gates (user control)
Layer 3 — Write-Verify Applier (hardware confirmation)
Layer 4 — Runtime Recommendation Resolver (live override)
```

---

### Layer 1: Degraded Mode Classification

Every update cycle, the system classifies overall health into one of three states based on which Home Assistant entities are currently available and readable:

| Mode | Writes allowed | Meaning |
|---|---|---|
| `OK` | ✅ Yes | All required entities present |
| `Degraded` | ✅ Yes (with warnings) | Non-critical data missing |
| `Error` | ❌ Blocked | Critical data missing — writes blocked |

**Critical entities** (any missing → `Error` mode, writes blocked):

- `batteries_state_of_capacity` — battery SoC sensor
- `batteries_maximum_charging_power` — max charge power setting
- `batteries_maximum_discharging_power` — max discharge power setting
- `batteries_rated_capacity` — battery nameplate capacity
- `house_consumption_power` — house load sensor

**Non-critical entities** (any missing → `Degraded` mode, writes allowed):

- Tomorrow's price/PV forecast gaps
- EV charger states
- Export price sensor

**Rationale:** The battery SoC is the single most critical value for safe operation. Without it, the planner cannot know whether to charge or discharge, and the applier cannot verify that battery limits are respected. House load is equally critical because the planner must know whether the house is importing or exporting to decide battery action.

### Layer 2: Read-Only / Dry-Run Gates

Two independent mechanisms block all hardware writes at the user's discretion:

- **Read-only mode** — toggled via `switch.hsem_read_only`. When active, the applier bypasses all hardware writes. Intended for monitoring the planner without taking operational control.
- **Dry-run mode** — set programmatically via `PlannerInput.is_read_only`. Same effect, used internally during testing.

These override all other layers — even if the system is healthy and the recommendation is correct, writes are blocked.

### Layer 3: Write-Verify Applier

The `WriteVerifyApplier` wraps every hardware write with a read-back confirmation loop:

1. Check `is_read_only` → skip if True
2. Check degraded mode → skip if Error
3. Check inverter unloading → skip if True
4. Write the desired value via the Huawei Solar service call
5. Wait settle time (default 10 s) for the inverter to persist the value
6. Read back the entity state
7. Compare: does the value match within tolerance?
   - Yes → return `ok`
   - No → retry up to `max_retries`
8. All retries exhausted → return `failed`

**Verified writes** include:

- Battery working mode (`select.batteries_working_mode`)
- Grid export power percent (`set_maximum_feed_grid_power_percent`)
- TOU charging/discharging periods (`set_tou_periods`)

**Result statuses:**

| Status | Meaning |
|---|---|
| `ok` | Read-back value matched within tolerance |
| `unverified` | Write accepted but read-back timed out or returned `None` |
| `failed` | All retries exhausted |
| `skipped` | Current value already matched — no write performed |

### Layer 4: Runtime Recommendation Resolver

Applied **only to the current slot** immediately before hardware writes. Overrides the planner output based on live sensor data that was unavailable at planning time:

| Priority | Condition | Action |
|---|---|---|
| 1 (highest) | Live import price < 0 | → `force_export` |
| 2 | Current recommendation = `batteries_charge_grid` | Kept (never overridden) |
| 3 | Any EV actively charging | → `ev_smart_charging` |
| 4 | Battery energy > remaining schedule need | → `batteries_discharge_mode` |

**Protection rules:**

- `force_export` (negative price) always wins — it overrides everything, including EV charging.
- `batteries_charge_grid` is **never** overridden by EV or discharge rules.
- The resolver reads live sensor data that was stale at planning time (actual working mode, real-time EV state).

---

## Consequences

### Positive

- **Defence in depth** — Four independent layers mean a single failure (e.g., a missed degraded classification) is caught by another layer (e.g., the write-verify loop).
- **Graceful degradation** — The system continues operating under partial data loss (`Degraded` mode) instead of failing open or closed aggressively.
- **Auditability** — Every write result is logged with its status (`ok`, `failed`, `skipped`, `unverified`), providing a full hardware write history.
- **User control** — Read-only mode gives users explicit, immediate control over hardware writes without disabling the planner.

### Negative

- **Latency overhead** — The write-verify loop adds ~10–30 s to every hardware write (settle time + read-back). TOU schedule writes that modify multiple periods are particularly affected.
- **Complexity** — Four layers with overlapping responsibilities can be confusing to debug. A write being blocked could be caused by any of the four layers.
- **False positives in degraded mode** — Transient sensor unavailability (e.g., a brief network glitch) can trigger `Error` mode and block writes unnecessarily.

### Mitigations

- The write-verify settle time (10 s) is configurable and can be reduced on fast-responding inverters.
- Degraded mode classification uses a per-cycle assessment — a sensor that recovers on the next cycle automatically lifts the block.
- Each layer's decision is logged with a reason code, making it possible to trace why a write was blocked.

---

## Alternatives Considered

### A. Single gate (degraded mode only)

*Rejected because:* Missing the read-only/dry-run gates would prevent users from monitoring the planner without hardware control. Also, the write-verify loop catches hardware-level failures (inverter non-responsive) that degraded mode cannot detect.

### B. Pessimistic: block all writes when any entity is missing

*Rejected because:* This would make the system unusable during minor data gaps (e.g., missing tomorrow's prices). The `Degraded` mode allows safe continued operation.

### C. Trust the planner — no write verification

*Rejected because:* Inverter communication is inherently unreliable (Wi-Fi dropouts, Modbus timeouts). A write that appears successful to the HA service call may not have been persisted by the inverter. The read-back step is the only way to confirm.

### D. Software kill switch only

*Rejected because:* A software-only gate is vulnerable to bugs. The degraded mode classification is conceptually independent of the read-only toggle, providing defence-in-depth even if one layer has a logic error.

---

## Related

- `docs/hsem-safety-modes.md` — detailed description of all four layers
- `utils/degraded_mode.py` — health classification implementation
- `utils/inverter_verify.py` — write-verify applier implementation
- `custom_sensors/recommendation_resolver.py` — runtime resolver implementation
- ADR-001: Planner Extraction (the pure-Python planner does no hardware writes — safety is enforced at the HA boundary)