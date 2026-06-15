# EV Surplus Charging Automation

This guide explains how to wire your physical EV charger to follow HSEM's
surplus-charging recommendations. HSEM calculates how much power the EV should
draw (`ev_charger_calculated_power`), but the charger itself needs a different
signal — it needs to know the **grid surplus** so it can dynamically adjust.

---

## Table of contents

1. [Concept](#concept)
2. [The formula](#the-formula)
3. [go-e Charger (MQTT)](#go-e-charger-mqtt)
4. [Easee Charger](#easee-charger)
5. [Zaptec Charger](#zaptec-charger)

---

## Concept

HSEM recalculates `ev_charger_calculated_power` every 5 minutes (the planner's
update interval). It tells you the target AC power the EV should draw right now —
e.g. `2900` means "charge at 2.9 kW."

There are two ways to control a dynamic charger with this value:

**Option A — HSEM directly controls the charge rate:**
Feed `ev_charger_calculated_power` into the charger. The charger draws exactly
that amount. Simple, but if a cloud passes between HSEM updates the charger
keeps drawing the old target — importing from the grid for up to 5 minutes.

**Option B — Charger chases real surplus, HSEM sets a ceiling:**
Feed your real grid power sensor into the charger's surplus input so it
responds to clouds instantly. Use HSEM's value as a maximum current limit
so the charger never exceeds what the MILP planned. Requires two automations
(or one script) per charger.

Both options are documented below. Choose Option B if your goal is "never
import from grid for the EV."

---

## The formula

For Option A (HSEM direct control), the charger needs to know the **remaining**
surplus after accounting for what it's already drawing:

```
pGrid = (ev_charger_calculated_power × -1) + current_charge_power
```

| Variable | Source | Example |
|---|---|---|
| `ev_charger_calculated_power` | `state_attr('sensor.hsem_workingmode_sensor', 'hourly_recommendation').ev_charger_calculated_power` | `2900` W |
| `current_charge_power` | Your charger's power sensor (W) | `2000` W |
| `pGrid` | Sent to charger | `-900` W |

**Worked example:**

- HSEM says charge at **2900 W** → `target = 2900`
- Charger is currently drawing **2000 W** → `charge = 2000`
- `pGrid = -2900 + 2000 = -900`

The charger sees 900 W of remaining surplus and increases its draw. When it
reaches 2900 W, `pGrid = -2900 + 2900 = 0`, and the charger holds steady —
grid is balanced.

When HSEM says **0 W** (no surplus), `pGrid = 0 + 2000 = +2000`, signaling
the charger that it's importing from the grid and should back off.

---

## go-e Charger (MQTT)

go-e chargers accept `pGrid`, `pAkku`, and `pPv` via MQTT on the
`go-eCharger/<serial>/ids/set` topic. The `ids` value decays after 10–15
seconds, so it must be refreshed continuously — the charger's PID loop then
adjusts the actual charge power to drive `pGrid` toward zero.

> **Why `pGrid` instead of setting amps?** The `ids` topic uses RAM-only
> registers designed for frequent writes. Setting the charge current directly
> via config keys like `amp`/`ama` writes to persistent storage (flash) on
> every update. See the
> [go-eCharger MQTT docs](https://github.com/syssi/homeassistant-goecharger-mqtt#charge-with-pv-surplus)
> for details.

### Prerequisites

| Entity | Purpose |
|---|---|
| `sensor.hsem_workingmode_sensor` | HSEM hourly recommendation with `ev_charger_calculated_power` |
| `binary_sensor.go_echarger_<serial>_car` | `on` when car is plugged in |
| `sensor.go_echarger_<serial>_nrg_12` | Current charging power in watts |

### Option A: HSEM direct control

Single automation — HSEM's `ev_charger_calculated_power` directly drives the
charge rate. The go-e charger sees remaining surplus and ramps up/down to
match the target.

```yaml
alias: go-e Surplus Charging from HSEM
description: >-
  Sends grid surplus to go-e charger so it dynamically follows HSEM's
  EV charging recommendation.
triggers:
  - trigger: time_pattern
    seconds: /3
conditions:
  - condition: state
    entity_id: binary_sensor.go_echarger_222819_car
    state: "on"
actions:
  - data:
      qos: "0"
      topic: go-eCharger/222819/ids/set
      retain: false
      payload: |-
        {% set rec = state_attr('sensor.hsem_workingmode_sensor', 'hourly_recommendation') %}
        {% set target = rec.ev_charger_calculated_power | int(0) if rec is not none else 0 %}
        {% set charge = states('sensor.go_echarger_222819_nrg_12') | int(0) %}
        {
          "pGrid": "{{ (target * -1 + charge) }}",
          "pAkku": "0",
          "pPv": "0"
        }
    action: mqtt.publish
mode: single
```

### Option B: Real surplus with HSEM ceiling

Two automations working together:

1. **Surplus signal** (every 3s) — feeds real grid power into `pGrid` so the
   charger's PID loop chases actual surplus second-by-second.
2. **HSEM ceiling** (on state change) — sets `number.go_echarger_<serial>_pgt`
   (grid target in watts) to HSEM's `ev_charger_calculated_power`. The charger
   will never exceed this, but will draw less when real surplus is low.

**Automation B1 — Real-time surplus signal:**

```yaml
alias: go-e Surplus Signal from Grid Power
description: >-
  Feeds real grid power into go-e charger's pGrid so it dynamically
  follows actual solar surplus second-by-second.
triggers:
  - trigger: time_pattern
    seconds: /3
conditions:
  - condition: state
    entity_id: binary_sensor.go_echarger_222819_car
    state: "on"
actions:
  - data:
      qos: "0"
      topic: go-eCharger/222819/ids/set
      retain: false
      payload: |-
        {
          "pGrid": "{{ states('sensor.power_meter_active_power') | int(0) }}",
          "pAkku": "0",
          "pPv": "0"
        }
    action: mqtt.publish
mode: single
```

**Automation B2 — HSEM ceiling:**

```yaml
alias: go-e Surplus Charging from HSEM
description: >-
  Simple automation to update values needed for using solar surplus with go-e
  Chargers
triggers:
  - trigger: time_pattern
    seconds: /3
conditions:
  - condition: state
    entity_id: binary_sensor.go_echarger_222819_car
    state: "on"
actions:
  - data:
      qos: "0"
      topic: go-eCharger/222819/ids/set
      retain: false
      payload: |-
        {
          "pGrid": "{{ (state_attr('sensor.hsem_workingmode_sensor', 'hourly_recommendation').ev_charger_calculated_power | int(0) * -1 + states('sensor.go_echarger_222819_nrg_12') | int(0)) }}",
          "pAkku": "0",
          "pPv": "0"
        }
    action: mqtt.publish
mode: single
```

> **Notes:**
> - Replace `222819` with your charger's serial number.
> - Replace `sensor.power_meter_active_power` with your actual grid power
>   sensor (negative = export, positive = import).
> - `pgt` expects a negative value for surplus (export), so HSEM's positive
>   watt target is negated (`target * -1`).
> - The `pAkku` and `pPv` fields are set to `0` because HSEM manages the
>   home battery separately — the charger only needs the grid surplus signal.

---

## Easee Charger

Easee chargers are controlled via the [Easee EV Charger](https://github.com/nordicopen/easee_hass)
integration. The integration exposes a `easee.set_charger_dynamic_limit` service
that sets the maximum charging current in amps.

Unlike go-e, Easee does not use a grid-surplus signal. Instead, you convert
HSEM's power target directly to amps and set it as the charger's dynamic limit.

### How it works

1. Convert HSEM's `ev_charger_calculated_power` (watts) to amps
2. Call `easee.set_charger_dynamic_limit` with the amp value
3. Use `time_to_live` so the limit expires if HA stops updating

**Amps formula:**

```
amps = ev_charger_calculated_power / (voltage × phases)
```

For a typical European 3-phase setup: `amps = power_w / (230 × 3)` ≈ `power_w / 690`.
For single-phase: `amps = power_w / 230`.

### Prerequisites

| Entity | Purpose |
|---|---|
| `sensor.hsem_workingmode_sensor` | HSEM hourly recommendation with `ev_charger_calculated_power` |
| `binary_sensor.easee_<id>_cable_connected` | `on` when car is plugged in |
| `sensor.easee_<id>_power` | Current charging power in watts (for diagnostics) |

> **Note:** You also need an Easee Equalizer (HAN/Nevion) installed for dynamic
> current limiting to work. Without it, the charger ignores dynamic current commands.

### Automation

```yaml
alias: Easee Surplus Charging from HSEM
description: >-
  Sets Easee charger dynamic current limit based on HSEM's
  EV charging recommendation.
triggers:
  - trigger: state
    entity_id:
      - sensor.hsem_workingmode_sensor
conditions:
  - condition: state
    entity_id: binary_sensor.easee_12345_cable_connected
    state: "on"
actions:
  - action: easee.set_charger_dynamic_limit
    data:
      charger_id: "12345"
      current: >-
        {% set rec = state_attr('sensor.hsem_workingmode_sensor', 'hourly_recommendation') %}
        {% set target = rec.ev_charger_calculated_power | int(0) if rec is not none else 0 %}
        {% set amps = (target / 690) | round(0, 'floor') | int %}
        {{ ([0, amps, 32] | sort)[1] }}
      time_to_live: 30
mode: single
```

> **Notes:**
> - Replace `12345` with your charger ID (find it in the Easee integration
>   device list).
> - Replace `690` with `230` if you have a single-phase installation.
> - The `([0, amps, 32] | sort)[1]` pattern clamps between 0 and 32 A (Easee's
>   range is 0–40 A; adjust the upper bound to match your circuit breaker).
> - `time_to_live: 30` means the limit expires after 30 seconds if HA stops
>   sending updates — a safety net that prevents the charger from getting
>   stuck at a high limit.

### Alternative: circuit-level control

If you have multiple chargers on one circuit, use `easee.set_circuit_dynamic_limit`
instead:

```yaml
actions:
  - action: easee.set_circuit_dynamic_limit
    data:
      circuit_id: 12345
      current_p1: >-
        {% set amps = (states('...') | int(0) / 690) | round(0, 'floor') | int %}
        {{ ([0, amps, 32] | sort)[1] }}
      current_p2: "{{ ([0, amps, 32] | sort)[1] }}"
      current_p3: "{{ ([0, amps, 32] | sort)[1] }}"
      time_to_live: 30
```

---

## Zaptec Charger

Zaptec chargers are controlled via the [Zaptec](https://github.com/custom-components/zaptec)
integration. The integration exposes a `number.<name>_available_current` entity
on the **Installation** device that sets the maximum charging current in amps.

Like Easee, Zaptec does not use a grid-surplus signal. You convert HSEM's power
target to amps and set it as the available current.

> **Important:** Zaptec recommends not changing the available current more
> often than every **15 minutes**. The automation below triggers on HSEM
> state changes only.

### Prerequisites

| Entity | Purpose |
|---|---|
| `sensor.hsem_workingmode_sensor` | HSEM hourly recommendation with `ev_charger_calculated_power` |
| `binary_sensor.zaptec_<name>_connected` | `on` when car is plugged in |
| `number.<installation_name>_available_current` | Sets max current for the installation |

> **Before you start:** Disable **Zaptec Sense** (APM/Automatic Power
> Management) and **stand-alone mode** in the Zaptec Portal. The charger
> must be in cloud-managed mode for the current limit to take effect.

### Automation

```yaml
alias: Zaptec Surplus Charging from HSEM
description: >-
  Sets Zaptec installation available current based on HSEM's
  EV charging recommendation.
triggers:
  - trigger: state
    entity_id:
      - sensor.hsem_workingmode_sensor
conditions:
  - condition: state
    entity_id: binary_sensor.zaptec_my_charger_connected
    state: "on"
actions:
  - action: number.set_value
    target:
      entity_id: number.my_installation_available_current
    data:
      value: >-
        {% set rec = state_attr('sensor.hsem_workingmode_sensor', 'hourly_recommendation') %}
        {% set target = rec.ev_charger_calculated_power | int(0) if rec is not none else 0 %}
        {% set amps = (target / 690) | round(0, 'floor') | int %}
        {{ ([6, amps, 32] | sort)[1] }}
mode: single
```

> **Notes:**
> - Replace `my_charger` and `my_installation` with your actual Zaptec entity
>   names.
> - Replace `690` with `230` if you have a single-phase installation.
> - `([6, amps, 32] | sort)[1]` clamps between 6 A (minimum most EVs accept)
>   and 32 A (typical installation limit). Adjust to match your circuit breaker.
> - If you have multiple chargers on one installation, changing the
>   installation-level current affects **all** of them.

### Alternative: per-charger control

If you need per-charger control (e.g. different cars on different schedules),
use the `zaptec.limit_current` service instead:

```yaml
actions:
  - action: zaptec.limit_current
    data:
      device_id: abc123def456
      available_current_phase1: >-
        {% set amps = (states('...') | int(0) / 690) | round(0, 'floor') | int %}
        {{ ([6, amps, 32] | sort)[1] }}
      available_current_phase2: "{{ ([6, amps, 32] | sort)[1] }}"
      available_current_phase3: "{{ ([6, amps, 32] | sort)[1] }}"
```

> This sets the current on a specific charger rather than the whole
> installation. Find the `device_id` in the Zaptec charger device page.
