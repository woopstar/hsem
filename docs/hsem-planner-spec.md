# HSEM Planner Specification

This document defines how the HSEM planner should work.

Use it as the reference for reviewing planner code, cost planning, and optimization changes.

## Goals

The planner must:

- minimize expected total cost within the configured horizon
- respect battery and inverter constraints
- keep energy accounting physically consistent
- avoid hardware writes when inputs are unsafe
- explain why a plan was selected
- produce deterministic output for the same input

## Core concepts

### Slot

A slot is one time interval in the planning horizon.

Each slot must have:

- start time
- end time
- duration in hours
- expected house load in kWh
- expected PV production in kWh
- import price per kWh
- export price per kWh
- optional tariff per kWh
- recommendation
- planned battery charge in kWh
- planned battery discharge in kWh
- expected SoC before and after the slot

Power values in kW must be converted to energy using:

```text
energy_kwh = power_kw * duration_hours
```

## Energy balance per slot

For every slot:

```text
net_load_kwh = house_load_kwh - pv_kwh
```

Positive `net_load_kwh` means the house needs energy.

Negative `net_load_kwh` means there is PV surplus.

Battery and grid flows must satisfy:

```text
house_load_kwh
= pv_used_for_house_kwh
+ battery_discharge_to_house_kwh
+ grid_import_for_house_kwh
```

PV production must satisfy:

```text
pv_kwh
= pv_used_for_house_kwh
+ pv_used_for_battery_kwh
+ pv_exported_kwh
+ pv_curtailed_kwh
```

Battery charge must satisfy:

```text
battery_charge_stored_kwh
= pv_used_for_battery_kwh * charge_efficiency
+ grid_import_for_battery_kwh * charge_efficiency
```

Grid import for charging:

```text
grid_import_for_battery_kwh = battery_charge_stored_kwh / charge_efficiency
```

Battery discharge must satisfy:

```text
usable_battery_discharge_kwh
= battery_energy_removed_kwh * discharge_efficiency
```

Battery energy to remove in order to deliver a target house load:

```text
battery_energy_removed_kwh = house_load_kwh / discharge_efficiency
```

## Battery efficiency

HSEM tracks charge-side and discharge-side efficiency independently.

### Parameters

| Parameter | Field | Default | Description |
|---|---|---|---|
| Charge efficiency | `battery_charge_efficiency_pct` | 95 % | Fraction of input energy stored. |
| Discharge efficiency | `battery_discharge_efficiency_pct` | 95 % | Fraction of stored energy delivered to house. |

### Semantics

```text
battery_stored = grid_or_pv_input × (charge_efficiency_pct / 100)
house_delivered = battery_removed × (discharge_efficiency_pct / 100)
grid_import_for_battery = battery_stored / (charge_efficiency_pct / 100)
battery_to_remove = house_load / (discharge_efficiency_pct / 100)
```

Round-trip yield:

```text
roundtrip_yield = (charge_efficiency_pct / 100) × (discharge_efficiency_pct / 100)
roundtrip_loss  = 1 − roundtrip_yield
```

Example (90 % / 90 %): yield = 0.81, loss = 19 %.

### Invariants for tests

- Charging 10 kWh at 90 % efficiency must draw 10 / 0.9 ≈ 11.11 kWh from the grid.
- Charging 10 kWh at 100 % efficiency must draw exactly 10 kWh from the grid.
- Discharging 10 kWh battery energy at 90 % efficiency must deliver 9 kWh to the house.
- The round-trip cost term (`conversion_loss_cost`) must use
  `1 − charge_eff × discharge_eff` when explicit efficiencies are set.
- When both efficiencies are 100 %, the legacy `conversion_loss_pct` field drives
  the `conversion_loss_cost` term (backwards compatibility).

## SoC simulation

SoC must be simulated forward through the full horizon.

For each slot:

```text
soc_after_kwh
= soc_before_kwh
+ battery_charge_stored_kwh
- battery_energy_removed_kwh
```

The simulator must enforce:

- `soc_after_kwh >= min_soc_kwh`
- `soc_after_kwh <= max_soc_kwh`
- charge power limit
- discharge power limit
- grid import limit
- export limit if configured

The simulator must read the slot recommendation.

If a slot recommends forced discharge, force export, or discharge-only behavior, that energy flow must appear in:

- `batteries_discharged`
- SoC change
- import/export calculation
- plan cost

No recommendation may be energetically invisible.

## Cost function

The total plan cost is:

```text
total_cost
= grid_import_cost
- export_revenue
+ battery_cycle_cost
+ conversion_loss_cost
+ tariff_cost
+ constraint_penalties
+ terminal_soc_penalty_or_credit
```

### Grid import cost

Grid import cost must use actual grid energy pulled.

If the battery stores `x` kWh from grid and charge efficiency is `e`, grid import is:

```text
grid_import_for_battery_kwh = x / e
```

Do not price stored energy as if it was grid energy.

### Export revenue

Export revenue is:

```text
grid_export_kwh * export_price_per_kwh
```

### Battery cycle cost

Cycle cost should count physical battery throughput.

Recommended:

```text
battery_throughput_kwh = battery_charge_stored_kwh + battery_energy_removed_kwh
cycle_cost = battery_throughput_kwh * cycle_cost_per_kwh
```

If using equivalent full cycles, document the formula.

Avoid double-counting the same energy as both charge and discharge unless the cycle-cost definition explicitly expects throughput.

### Terminal SoC value

Plans must not look better merely because they empty the battery before the horizon ends.

The cost function must include either:

- terminal SoC credit
- terminal SoC penalty
- or a constraint that compares plans at equal terminal SoC

A simple default:

```text
terminal_soc_delta_kwh = baseline_terminal_soc_kwh - candidate_terminal_soc_kwh
terminal_soc_penalty = terminal_soc_delta_kwh * replacement_energy_price
```

## Price interval semantics

### Background

HSEM supports two price-data granularities depending on the configured EDS
(Energi Data Service) integration:

| `energi_data_service_update_interval` | Meaning |
|---|---|
| 15 | EDS publishes one price record every 15 minutes |
| 60 | EDS publishes one price record per hour |

The planning slot width is controlled separately by
`recommendation_interval_minutes` (also 15 or 60).

Electricity prices are **rates** (currency per kWh), not energy quantities.
Every slot inside the same EDS update interval shares the same price; the
price is **never summed or averaged** across slots.

### The eds_share conversion factor

When EDS and slot widths differ (most common case: EDS 60 min, slots 15 min),
a conversion factor is needed so internal per-slot storage and the planner
engine both see correct values:

```text
eds_share = energi_data_service_update_interval / recommendation_interval_minutes
```

Common configurations:

| EDS interval | Slot width | eds_share | Effect |
|---|---|---|---|
| 60 min | 15 min | 4.0 | price÷4 stored; planner gets price×4 back |
| 15 min | 15 min | 1.0 | no scaling — price stored and used unchanged |
| 60 min | 60 min | 1.0 | no scaling — price stored and used unchanged |

### How the scaling pipeline works

1. **Population** (`hourly_data_populator._async_update_hourly_field`):
   Each raw EDS value is divided by `eds_share` before writing to the
   per-slot `HourlyRecommendation` object.
   This gives each slot its proportional share of the price-rate value so
   slot boundaries align correctly.

2. **Planner input** (`coordinator._build_planner_input`):
   When assembling `PricePoint` objects for the planner engine, each stored
   per-slot price is multiplied by `eds_share` to recover the original
   hourly-equivalent rate.
   The planner's cost function always works with full currency/kWh rates, not
   fractions.

The divide and multiply are exact inverses — they cancel perfectly and the
planner always receives the original price rate regardless of configuration.

### What this is NOT

- `eds_share` is **not** a VAT multiplier.
- `eds_share` is **not** a currency conversion.
- `eds_share` is **not** an energy-splitting factor (prices are rates, not energy).

### Invariants for tests

- A 60-min EDS price of `P` must reach the planner as `P` (not `P/4` or `P*4`).
- A 15-min EDS price of `P` must reach the planner as `P`.
- Intermediate per-slot stored values must equal `P / eds_share`.
- Changing `energi_data_service_update_interval` from 60 to 15 with the same
  price input must not change the price seen by the planner engine.
- Negative prices must survive the full pipeline unchanged.

## Candidate plans

Every candidate plan must be fully simulated and scored.

Required candidates:

- no-action baseline
- current heuristic plan
- grid-charge candidates
- discharge candidates
- excess-export candidates if enabled
- aggressive candidates if enabled

The selected plan must be the lowest-cost valid candidate within the implemented search space.

The final returned plan must be the same plan that was selected.

This invariant must always hold:

```text
output.plan_cost == selected_candidate.cost
output.slots == selected_candidate.slots
```

No post-selection pass may mutate slots unless the plan is re-simulated and re-scored.

## No-action baseline

The no-action plan means:

- no forced grid charge
- no forced discharge
- no force export
- normal self-consumption behavior only

It must still account for:

- PV charging battery if that is normal inverter behavior
- PV export
- house load
- battery self-consumption behavior if modeled
- terminal SoC

No-action must not be treated as “zero battery movement” unless the physical model says no battery movement occurs.

## Safety gates

The planner may compute in read-only or degraded states.

The applier must not write to hardware when:

- read-only mode is enabled
- dry-run mode is enabled
- degraded mode blocks writes
- error mode is active
- required data is missing
- config entry is unloading

## Invariants for tests

Add tests for these invariants:

- Energy balance holds for every slot.
- SoC never leaves configured bounds.
- Forced discharge changes SoC and cost.
- Force export changes SoC and export revenue.
- Grid charge prices actual grid import, not stored energy.
- Candidate winner cost equals final output cost.
- Final output slots equal selected candidate slots.
- No post-selection mutation happens without re-score.
- No-action includes normal PV/battery behavior.
- Terminal SoC affects cost.
- Emptying the battery is not free.
- `winner.cost <= no_action.cost` within the implemented candidate set.
- Current partial slot uses remaining duration only.
- Missing price/PV data does not become real zero silently.
- Read-only/degraded/dry-run gates block writes.

## Multi-day planning horizon

The planner supports configurable planning horizons: 24, 48, and 72 hours.

The horizon is controlled by `interval_length_hours` in `PlannerInput` (and
`recommendation_interval_length` in `SensorConfig`).  All three values are
accepted without special-casing in the engine.

### Slot count

```text
total_slots = (interval_length_hours * 60) // interval_minutes
```

| Horizon | 15-min slots | 60-min slots |
|---|---|---|
| 24 h | 96 | 24 |
| 48 h | 192 | 48 |
| 72 h | 288 | 72 |

### Confidence decay for future days

Price and PV forecast accuracy degrades for days further in the future.
To avoid over-committing to uncertain future plans, the planner applies a
**confidence decay factor** to PV estimates (not prices) for slots on
day+1 and beyond:

| Day offset | Decay factor | Meaning |
|---|---|---|
| 0 (today) | 1.00 | No decay — current-day forecast |
| 1 (tomorrow) | 0.90 | 10 % conservative discount |
| 2 (day after) | 0.80 | 20 % conservative discount |

Only PV estimates are discounted.  Electricity prices are used as-is because:
- Spot-market prices are typically known for day+1 by mid-day.
- Discounting known prices would distort the cost function.

Decay is applied **after** missing-data diagnostics, so `DataQuality` always
reflects original data gaps, not decayed values.

### Missing future data handling

For every day in the horizon the engine detects and surfaces missing price
and PV data explicitly.  Day-labelled `missing_inputs` entries are emitted
with the format:

```text
tomorrow_price_missing_hours:HH,HH,...
tomorrow_pv_missing_hours:HH,HH,...
day2_price_missing_hours:HH,HH,...
day2_pv_missing_hours:HH,HH,...
```

These labels are **non-critical** — they do not match battery or house-load
keywords — so they trigger `DegradedMode.Degraded` (hardware writes allowed)
rather than `Error` (writes blocked).

Missing slots default to `0.0` in the planner.  The planner **must never**
silently treat absent data as real zero without surfacing a diagnostic.

### DataQuality fields for multi-day horizons

`DataQuality.horizon_days` reflects the number of calendar days covered.
`DataQuality.day2_price_missing_hours` and `DataQuality.day2_pv_missing_hours`
carry the day+2 gap lists for 72-hour horizon runs.

### Invariants for multi-day horizon tests

- A 24-hour horizon produces exactly `(24 * 60) // interval_minutes` slots.
- A 48-hour horizon produces exactly `(48 * 60) // interval_minutes` slots.
- A 72-hour horizon produces exactly `(72 * 60) // interval_minutes` slots.
- All slots have a non-``None`` recommendation regardless of horizon.
- Day+1 PV estimates are ≤ day+0 estimates for the same hour when both have
  the same raw input (confidence decay applied).
- Day+2 PV estimates are ≤ day+1 estimates for the same raw input.
- `DataQuality.horizon_days` equals 1 / 2 / 3 for 24 h / 48 h / 72 h.
- Missing day+2 price data surfaces in `day2_price_missing_hours`.
- Missing day+2 PV data surfaces in `day2_pv_missing_hours`.
- `DataQuality.is_complete` is ``False`` when any future-day data is missing.

## Documentation expectations

Every planner change should update:

- this spec if semantics change
- plan explanation output
- tests for at least one hand-calculated scenario

Every test fixture should state:

- slot duration
- input units
- expected SoC trajectory
- expected import/export
- expected total cost

