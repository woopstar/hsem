# EV Optimal Charging Plan (Legacy Template)

> **Note:** This is a legacy Home Assistant template sensor provided as a user reference. HSEM now includes a native EV planner (`hsem-ev-charge-plan` service) that handles optimal charging natively. This template is kept for users who prefer a standalone template-based approach.

This page describes a cost‑optimal charging plan template sensor for Home Assistant based on:

- Household consumption
- Solar production (net consumption)
- Dynamic energy prices and tariffs
- Your EV's current state of charge and charging deadline

The sensor exposes:

- A simple state you can use in automations (`charging`, `waiting`, `not_connected`, etc.)
- A detailed `charging_slots` attribute with per‑slot cost and solar data

---

## Prerequisites

You need the following entities in Home Assistant:

| Entity | Purpose |
|---|---|
| `binary_sensor.go_echarger_222819_car` | `on` when the car is connected to the charger |
| `sensor.audi_e_tron_state_of_charge` | Current SoC in percent |
| `input_number.audi_e_tron_charging_target` | Target SoC (e.g., 80) |
| `input_datetime.audi_e_tron_charge_end_time` | Latest time the car must be ready |
| `input_boolean.audi_e_tron_smart_charging` | Smart charging toggle |
| `sensor.hsem_workingmode_sensor` | HSEM working mode sensor with `hourly_recommendations` |

The `hourly_recommendations` items must contain at least:

- `start` and `end` (datetimes)
- `import_price` (price per kWh including tariffs)
- `estimated_net_consumption` (kWh, house load minus solar, negative = surplus)

---

## Concept Overview

The template sensor solves one problem:

> "Given my current SoC, target SoC, deadline, prices, and solar forecast, in which time slots should I charge to minimize imported energy cost?"

It does this by:

1. **Estimating** how many kWh your EV needs to reach the target SoC
2. **Looking at** all future slots between now and your deadline
3. **Calculating** for each slot:
   - How many kWh the car can take in that slot
   - How much of that is covered by solar surplus
   - How much must be imported from the grid
   - What the cost of that import will be
4. **Sorting** the slots by effective cost
5. **Picking** the cheapest slots until the required kWh are covered
6. **Exposing** those as `charging_slots` and switching state between `charging` and `waiting`

The plan:
- Prefers slots with solar surplus
- Prefers cheap grid prices
- Respects your end‑time deadline
- Adjusts dynamically as SoC or forecasts change

---

## Entities Created

The template creates one sensor:

**`sensor.hsem_ev_optimal_charging_plan`**

**State** (string):

| State | Meaning |
|---|---|
| `not_connected` | Car not plugged in |
| `smart_charging_disabled` | Smart charging boolean is off |
| `fully_charged` | Current SoC ≥ target SoC |
| `charging` | Inside a selected charging slot |
| `waiting` | Connected and not full, but outside selected slots |

**Attributes:**

| Attribute | Type | Description |
|---|---|---|
| `smart_charging` | boolean | Smart charging toggle state |
| `battery_capacity_kwh` | float | Fixed EV battery capacity |
| `charge_power_kw` | float | Fixed charging power |
| `current_soc` | float | Current state of charge |
| `target_soc` | float | Target state of charge |
| `ev_connected` | boolean | EV connection status |
| `total_kwh_needed` | float | Energy needed to reach target |
| `deadline` | datetime | Latest charging deadline |
| `charging_slots` | list | Planned charging slots (see below) |

Each `charging_slots` item:

```json
{
  "start": "2026-03-10T01:00:00+01:00",
  "end": "2026-03-10T01:15:00+01:00",
  "import_price": 0.75,
  "solar_surplus_kwh": 1.2,
  "import_needed_kwh": 0.4,
  "estimated_charged_kwh": 1.6,
  "estimated_cost": 0.30
}
```

---

## Template Sensor YAML

Add this to your `configuration.yaml` (or `template:` include file). **Adjust** entity IDs, `battery_capacity_kwh`, and `charge_power_kw` to match your setup.

```yaml
template:
  - trigger:
      - trigger: time_pattern
        seconds: /5
      - trigger: state
        entity_id:
          - input_boolean.audi_e_tron_smart_charging
          - binary_sensor.go_echarger_222819_car
        to:
    sensor:
      - name: "HSEM EV Optimal Charging Plan"
        unique_id: hsem_ev_optimal_charging_plan
        state: >-
          {%- set ev_connected = is_state('binary_sensor.go_echarger_222819_car', 'on') %}
          {%- if not ev_connected %}
            not_connected
          {%- else %}
            {%- set smart_charging = is_state('input_boolean.audi_e_tron_smart_charging', 'on') -%}
            {%- if not smart_charging -%}
              smart_charging_disabled
            {%- else %}
              {%- set current_soc = states('sensor.audi_e_tron_state_of_charge') | float(0) %}
              {%- set target_soc = states('input_number.audi_e_tron_charging_target') | float(80) %}
              {%- if current_soc >= target_soc %}
                fully_charged
              {%- else %}
                {%- set now_ts = now().timestamp() %}
                {%- set slots = state_attr('sensor.hsem_ev_optimal_charging_plan', 'charging_slots') %}
                {%- set ns = namespace(active=false) %}
                {%- if slots %}
                  {%- for slot in slots %}
                    {%- set slot_start = as_datetime(slot.start).timestamp() %}
                    {%- set slot_end = as_datetime(slot.end).timestamp() %}
                    {%- if now_ts >= slot_start and now_ts < slot_end %}
                      {%- set ns.active = true %}
                    {%- endif %}
                  {%- endfor %}
                {%- endif %}
                {{ 'charging' if ns.active else 'waiting' }}
              {%- endif %}
            {%- endif %}
          {%- endif %}

        attributes:
          smart_charging: >-
            {{ is_state('input_boolean.audi_e_tron_smart_charging', 'on') }}
          battery_capacity_kwh: "86.5"
          charge_power_kw: "10.6"
          current_soc: >-
            {{ states('sensor.audi_e_tron_state_of_charge') | float(0) }}
          target_soc: >-
            {{ states('input_number.audi_e_tron_charging_target') | float(80) }}
          ev_connected: >-
            {{ is_state('binary_sensor.go_echarger_222819_car', 'on') }}
          total_kwh_needed: >-
            {%- set current_soc = states('sensor.audi_e_tron_state_of_charge') | float(0) %}
            {%- set target_soc = states('input_number.audi_e_tron_charging_target') | float(80) %}
            {%- set battery_capacity_kwh = 86.5 %}
            {{ [((target_soc - current_soc) / 100) * battery_capacity_kwh, 0] | max | round(2) }}
          deadline: >-
            {%- set now_ts = now().timestamp() %}
            {%- set end_time = states('input_datetime.audi_e_tron_charge_end_time') %}
            {%- set deadline_today = today_at(end_time) %}
            {%- set deadline_ts = deadline_today.timestamp() if deadline_today.timestamp() > now_ts else (deadline_today.timestamp() + 86400) %}
            {{ deadline_ts | timestamp_local }}
          charging_slots: >-
            {%- set ev_connected = is_state('binary_sensor.go_echarger_222819_car', 'on') %}
            {%- set current_soc = states('sensor.audi_e_tron_state_of_charge') | float(0) %}
            {%- set target_soc = states('input_number.audi_e_tron_charging_target') | float(80) %}
            {%- set smart_charging = is_state('input_boolean.audi_e_tron_smart_charging', 'on') %}
            {%- if smart_charging and (not ev_connected or current_soc >= target_soc) %}
              []
            {%- else %}
              {%- set battery_capacity_kwh = 86.5 %}
              {%- set charge_power_kw = 10.6 %}
              {%- set recommendation_interval_minutes = state_attr('sensor.hsem_workingmode_sensor', 'recommendation_interval_minutes') | int(15) %}
              {%- set slot_duration_h = recommendation_interval_minutes / 60 %}
              {%- set kwh_per_slot = charge_power_kw * slot_duration_h %}
              {%- set total_needed_kwh = ((target_soc - current_soc) / 100) * battery_capacity_kwh %}
              {%- set now_ts = now().timestamp() %}
              {%- set end_time = states('input_datetime.audi_e_tron_charge_end_time') %}
              {%- set deadline_today = today_at(end_time) %}
              {%- set deadline_ts = deadline_today.timestamp() if deadline_today.timestamp() > now_ts else (deadline_today.timestamp() + 86400) %}
              {%- set recs = state_attr('sensor.hsem_workingmode_sensor', 'hourly_recommendations') %}
              {%- if recs %}
                {%- set ns = namespace(candidates_future=[], candidates_all=[]) %}
                {%- for slot in recs %}
                  {%- set slot_start = as_datetime(slot.start).timestamp() %}
                  {%- set slot_end = as_datetime(slot.end).timestamp() %}
                  {%- if slot_end > now_ts and slot_end <= deadline_ts %}
                    {%- set start_str = as_datetime(slot.start).strftime('%Y-%m-%dT%H:%M:%S%z') %}
                    {%- set end_str = as_datetime(slot.end).strftime('%Y-%m-%dT%H:%M:%S%z') %}
                    {%- set net = slot.estimated_net_consumption | float(0) %}
                    {%- set solar_surplus = [(-net), 0] | max %}
                    {%- set minutes_remaining = ((slot_end - now_ts) / 60) | round(0, 'ceil') | int %}
                    {%- set fraction = [minutes_remaining / recommendation_interval_minutes, 1] | min %}
                    {%- set kwh_this_slot = kwh_per_slot * fraction %}
                    {%- set import_needed_kwh = [kwh_this_slot - solar_surplus, 0] | max %}
                    {%- set effective_cost = slot.import_price | float %}
                    {%- set kandidat = {
                          'start': start_str,
                          'end': end_str,
                          'import_price': slot.import_price | float,
                          'solar_surplus': solar_surplus | round(3),
                          'kwh_this_slot': kwh_this_slot | round(3),
                          'import_needed_kwh': import_needed_kwh | round(3),
                          'effective_cost': effective_cost | round(3)
                        } %}
                    {%- if slot_end > now_ts %}
                      {%- set ns.candidates_future = ns.candidates_future + [kandidat] %}
                    {%- endif %}
                    {%- set ns.candidates_all = ns.candidates_all + [kandidat] %}
                  {%- endif %}
                {%- endfor %}
                {%- set future_kwh = ns.candidates_future | sum(attribute='kwh_this_slot') %}
                {%- set candidates = ns.candidates_future if future_kwh >= total_needed_kwh else ns.candidates_all %}
                {%- set sorted = candidates | sort(attribute='effective_cost') %}
                {%- set ns2 = namespace(result=[], kwh_remaining=total_needed_kwh) %}
                {%- for slot in sorted %}
                  {%- if ns2.kwh_remaining > 0 %}
                    {%- set actual_cost = [slot.kwh_this_slot - slot.solar_surplus, 0] | max * slot.import_price %}
                    {%- set ns2.result = ns2.result + [{
                      'start': slot.start,
                      'end': slot.end,
                      'import_price': slot.import_price,
                      'solar_surplus_kwh': slot.solar_surplus,
                      'import_needed_kwh': slot.import_needed_kwh,
                      'estimated_charged_kwh': slot.kwh_this_slot | round(3),
                      'estimated_cost': actual_cost | round(3)
                    }] %}
                    {%- set ns2.kwh_remaining = ns2.kwh_remaining - slot.kwh_this_slot %}
                  {%- endif %}
                {%- endfor %}
                {{ ns2.result | sort(attribute='start') }}
              {%- else %}
                []
              {%- endif %}
            {%- endif %}
```

---

## How the Calculation Works (Step-by-Step)

1. **Total energy needed** — uses current SoC, target SoC, and `battery_capacity_kwh`. Example: SoC 40 → target 80 on 86.5 kWh battery → `((80 − 40) / 100) × 86.5 ≈ 34.6 kWh`

2. **Deadline handling** — reads `input_datetime`. If today's time has passed, shifts deadline to tomorrow (+86400 seconds).

3. **Slot selection window** — takes all `hourly_recommendations` where slot end is after now and before/at deadline.

4. **Solar and net consumption** — `solar_surplus = max(-net, 0)`

5. **Slot charging capacity** — `kwh_per_slot = charge_power_kw × (interval_minutes / 60)`. If slot is partially passed, scales by remaining minutes.

6. **Import need and cost** — `import_needed_kwh = max(kwh_per_slot − solar_surplus, 0)`, `estimated_cost = import_needed_kwh × import_price`

7. **Optimal schedule** — sorts slots by `effective_cost`, picks cheapest until total kWh ≥ needed, outputs sorted by `start`.

---

## Example Automation

```yaml
automation:
  - alias: "HSEM EV Smart Charging"
    mode: restart
    trigger:
      - platform: state
        entity_id:
          - sensor.hsem_ev_optimal_charging_plan
    condition:
      - condition: state
        entity_id: input_boolean.audi_e_tron_smart_charging
        state: "on"
      - condition: state
        entity_id: binary_sensor.go_echarger_222819_car
        state: "on"
    action:
      - choose:
          - conditions:
              - condition: state
                entity_id: sensor.hsem_ev_optimal_charging_plan
                state: "charging"
            sequence:
              - service: switch.turn_on
                target:
                  entity_id: switch.go_echarger_222819_relay
          - conditions:
              - condition: state
                entity_id: sensor.hsem_ev_optimal_charging_plan
                state: "waiting"
            sequence:
              - service: switch.turn_off
                target:
                  entity_id: switch.go_echarger_222819_relay
```

You can extend this with extra conditions (night‑only charging, max amps, etc.).
