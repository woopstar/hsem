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

Battery discharge must satisfy:

```text
usable_battery_discharge_kwh
= battery_energy_removed_kwh * discharge_efficiency
```

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

