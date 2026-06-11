# EV Surplus Charging Automation

This guide explains how to wire your physical EV charger to follow HSEM's
surplus-charging recommendations. HSEM calculates how much power the EV should
draw (`ev_charger_calculated_power`), but the charger itself needs a different
signal — it needs to know the **grid surplus** so it can dynamically adjust.

---

## Table of contents

1. [Concept](#concept)
2. [The formula](#the-formula)
3. [Safe template pattern](#safe-template-pattern)
4. [go-e Charger (MQTT)](#go-e-charger-mqtt)
5. [Easee Charger](#easee-charger)
6. [Zaptec Charger](#zaptec-charger)

---

## Concept

HSEM's `ev_charger_calculated_power` (from `sensor.hsem_workingmode_sensor`
→ `hourly_recommendation`) tells you the **target AC power** the EV should
draw right now. For example, `2900` means "charge at 2.9 kW."

Dynamic chargers (go-e, Easee, Zaptec) don't accept a direct charge-power
command. Instead, they monitor the grid import/export and adjust their draw
to keep the grid near zero — consuming exactly the available surplus.

If you send `pGrid = -2900` (exporting 2.9 kW), the charger sees surplus and
ramps up. But once it reaches 2.9 kW, the grid is balanced and `pGrid` should
be near zero. If you keep sending `-2900`, the charger thinks there's still
surplus and may overshoot or behave erratically.

**The fix:** subtract the charger's **current actual power** from the target.

---

## The formula

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

## Safe template pattern

Always guard against unavailable sensors. If `sensor.hsem_workingmode_sensor`
is offline, `state_attr()` returns `None` and accessing `.ev_charger_calculated_power`
will crash the template.

Use this pattern in all charger automations:

```jinja2
{% set rec = state_attr('sensor.hsem_workingmode_sensor', 'hourly_recommendation') %}
{% set target = rec.ev_charger_calculated_power | int(0) if rec is not none else 0 %}
{% set charge = states('sensor.<your_charger_power>') | int(0) %}
{{ (target * -1 + charge) }}
```

If the HSEM sensor is unavailable, `target` defaults to `0` and the charger
is told to back off rather than the template crashing.

---

## go-e Charger (MQTT)

go-e chargers accept `pGrid`, `pAkku`, and `pPv` via MQTT on the
`go-eCharger/<serial>/ids/set` topic.

The recommended approach combines two signals:

- **`pGrid`** — fed with your **real grid power sensor** every few seconds.
  The go-e charger's built-in PID loop chases actual surplus in real time,
  responding to clouds and load changes instantly.
- **`amp` (requested current)** — set to HSEM's `ev_charger_calculated_power`
  converted to amps. This acts as a **ceiling** — the charger will never
  draw more than the MILP-optimized target, but it will draw less (or nothing)
  when real surplus is low.

> **Why two signals?** HSEM recalculates `ev_charger_calculated_power` every
> 5 minutes (the planner's update interval). If you feed that directly into
> `pGrid`, the charger can't respond to a passing cloud — it keeps drawing
> the old target and imports from the grid. With real grid power in `pGrid`,
> the charger adjusts second-by-second. The `amp` ceiling ensures it never
> exceeds what HSEM planned.

> **Why `pGrid` instead of setting amps only?** The `ids` topic (which carries
> `pGrid`) is designed for frequent updates — the value decays after 10–15
> seconds and is expected to be refreshed continuously. Setting the charge
> current via config keys like `amp` writes to persistent storage, so we only
> update it when HSEM recalculates (every 5 minutes). See the
> [go-eCharger MQTT docs](https://github.com/syssi/homeassistant-goecharger-mqtt#charge-with-pv-surplus)
> for details.

### Prerequisites

| Entity | Purpose |
|---|---|
| `sensor.hsem_workingmode_sensor` | HSEM hourly recommendation with `ev_charger_calculated_power` |
| `sensor.power_meter_active_power` | Real grid import/export power in watts (negative = export) |
| `binary_sensor.go_echarger_<serial>_car` | `on` when car is plugged in |
| `number.go_echarger_<serial>_amp` | Requested current number entity (for the ceiling) |

### Automation 1: Real-time surplus signal (every 3 seconds)

This feeds actual grid power into `pGrid` so the charger chases real surplus.

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
        {% set grid = states('sensor.power_meter_active_power') | int(0) %}
        {
          "pGrid": "{{ grid }}",
          "pAkku": "0",
          "pPv": "0"
        }
    action: mqtt.publish
mode: single
```

### Automation 2: HSEM ceiling (every 5 minutes)

This sets the maximum charge current from HSEM's MILP-optimized target.
Only updates when the target changes, avoiding unnecessary writes to
persistent storage.

```yaml
alias: go-e Charge Ceiling from HSEM
description: >-
  Sets go-e charger's maximum current from HSEM's ev_charger_calculated_power.
  Acts as a ceiling — the charger won't exceed this, but will draw less
  when real surplus is low.
triggers:
  - trigger: time_pattern
    minutes: /5
  - trigger: state
    entity_id:
      - sensor.hsem_workingmode_sensor
conditions:
  - condition: state
    entity_id: binary_sensor.go_echarger_222819_car
    state: "on"
actions:
  - variables:
      rec: >-
        {{ state_attr('sensor.hsem_workingmode_sensor', 'hourly_recommendation') }}
      target_w: >-
        {{ rec.ev_charger_calculated_power | int(0) if rec is not none else 0 }}
      target_amps: >-
        {{ (target_w / 690) | round(0, 'floor') | int }}
      clamped_amps: >-
        {{ ([6, target_amps, 32] | sort)[1] }}
  - if:
      - condition: template
        value_template: >-
          {{ (clamped_amps - states('number.go_echarger_222819_amp') | int(0)) | abs > 0 }}
    then:
      - action: number.set_value
        target:
          entity_id: number.go_echarger_222819_amp
        data:
          value: "{{ clamped_amps }}"
mode: single
```

> **Notes:**
> - Replace `222819` with your charger's serial number.
> - Replace `sensor.power_meter_active_power` with your actual grid power
>   sensor (negative = export, positive = import).
> - Replace `690` with `230` if you have a single-phase installation.
> - `clamped_amps` keeps the ceiling between 6 A (minimum most EVs accept)
>   and 32 A (typical installation limit). Adjust to match your circuit
>   breaker.
> - The `pAkku` and `pPv` fields are set to `0` because HSEM manages the
>   home battery separately — the charger only needs the grid surplus signal.
> - The ceiling automation only calls `number.set_value` when the target
>   actually changes, avoiding writes to persistent storage on every tick.

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
  - trigger: time_pattern
    seconds: /10
conditions:
  - condition: state
    entity_id: binary_sensor.easee_12345_cable_connected
    state: "on"
actions:
  - variables:
      rec: >-
        {{ state_attr('sensor.hsem_workingmode_sensor', 'hourly_recommendation') }}
      target_w: >-
        {{ rec.ev_charger_calculated_power | int(0) if rec is not none else 0 }}
      target_amps: >-
        {{ (target_w / 690) | round(0, 'floor') | int }}
      clamped_amps: >-
        {{ ([0, target_amps, 32] | sort)[1] }}
  - if:
      - condition: template
        value_template: "{{ target_amps > 0 }}"
    then:
      - action: easee.set_charger_dynamic_limit
        data:
          charger_id: "12345"
          current: "{{ clamped_amps }}"
          time_to_live: 30
    else:
      - action: easee.set_charger_dynamic_limit
        data:
          charger_id: "12345"
          current: 0
          time_to_live: 30
mode: single
```

> **Notes:**
> - Replace `12345` with your charger ID (find it in the Easee integration
>   device list).
> - Replace `690` with `230` if you have a single-phase installation.
> - `clamped_amps` keeps the value between 0 and 32 A (Easee's range is 0–40 A;
>   adjust the upper bound to match your installation's circuit breaker).
> - `time_to_live: 30` means the limit expires after 30 seconds if HA stops
>   sending updates — a safety net that prevents the charger from getting
>   stuck at a high limit.
> - The trigger interval is `/10` seconds (not `/3` like go-e) because Easee's
>   cloud API has rate limits. Do not go below 5 seconds.

### Alternative: circuit-level control

If you have multiple chargers on one circuit, use `easee.set_circuit_dynamic_limit`
instead:

```yaml
actions:
  - action: easee.set_circuit_dynamic_limit
    data:
      circuit_id: 12345
      current_p1: "{{ clamped_amps }}"
      current_p2: "{{ clamped_amps }}"
      current_p3: "{{ clamped_amps }}"
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
> often than every **15 minutes**. The automation below uses a 15-second
> trigger for responsiveness but only calls the service when the target
> changes significantly.

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
  - trigger: time_pattern
    seconds: /15
conditions:
  - condition: state
    entity_id: binary_sensor.zaptec_my_charger_connected
    state: "on"
actions:
  - variables:
      rec: >-
        {{ state_attr('sensor.hsem_workingmode_sensor', 'hourly_recommendation') }}
      target_w: >-
        {{ rec.ev_charger_calculated_power | int(0) if rec is not none else 0 }}
      target_amps: >-
        {{ (target_w / 690) | round(0, 'floor') | int }}
      clamped_amps: >-
        {{ ([6, target_amps, 32] | sort)[1] }}
  - if:
      - condition: template
        value_template: >-
          {{ (target_amps - states('number.my_installation_available_current') | int(0)) | abs > 1 }}
    then:
      - action: number.set_value
        target:
          entity_id: number.my_installation_available_current
        data:
          value: "{{ clamped_amps }}"
mode: single
```

> **Notes:**
> - Replace `my_charger` and `my_installation` with your actual Zaptec entity
>   names.
> - Replace `690` with `230` if you have a single-phase installation.
> - `clamped_amps` keeps the value between 6 A (minimum most EVs accept) and
>   32 A (typical installation limit). Adjust to match your circuit breaker.
> - The condition `abs > 1` prevents unnecessary API calls — the service is
>   only called when the target changes by more than 1 A.
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
      available_current_phase1: "{{ clamped_amps }}"
      available_current_phase2: "{{ clamped_amps }}"
      available_current_phase3: "{{ clamped_amps }}"
```

> This sets the current on a specific charger rather than the whole
> installation. Find the `device_id` in the Zaptec charger device page.