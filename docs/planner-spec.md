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

## Recommendation priority rules

### Three-layer model

Recommendations are assigned and potentially overridden in three layers.
Every layer must respect the rules below.

#### Layer 1 — Planner engine (pre-simulation)

Slots are assigned recommendations by the scheduling functions in strict
priority order.  Once a slot has a non-`None` recommendation, later rules
in the same layer must not change it.

**Discharge schedule windows** (highest priority in layer 1):

1. Slot falls inside a configured discharge window and price spread is met → `batteries_discharge_mode`

**Charge schedule windows** (before each discharge window):

1. Import price < 0 → `batteries_charge_grid`
2. Solar surplus (`estimated_net_consumption < threshold`) → `batteries_charge_solar`
3. Cheapest grid hour where spread ≥ `min_price_difference + cycle_cost` → `batteries_charge_grid`

**Opportunistic grid charge** (outside any schedule):

1. Import price < 0 → `batteries_charge_grid`
2. Import price ≤ depreciation threshold − cycle cost → `batteries_charge_grid`

**Excess export** (only when enabled):

1. Export price > threshold AND battery above required capacity → `force_batteries_discharge`

**Seasonal fill** (remaining `None` slots):

1. Export price > import price AND export price ≥ `export_min_price` → `force_export`
2. Solar surplus and battery not full → `batteries_charge_solar`
3. Future `force_batteries_discharge` AND battery > required → `batteries_wait_mode`
4. Winter month → `batteries_wait_mode`
5. Summer month, solar surplus → `batteries_charge_solar`; else → `batteries_discharge_mode`

### Layer 2 — EV planned load labelling (post-simulation)

After the final SoC simulation, slots with `ev_total_planned_load_kwh > 0` are relabelled.
`ev_total_planned_load_kwh` is used (not `ev_planned_load_kwh`) so that EV-scheduled
slots are correctly labelled even when `base_load_includes_ev = True`, where
`ev_planned_load_kwh` is `0.0` but EV charging is still planned.

`base_load_includes_ev` is automatically derived from the
`hsem_house_power_includes_ev_charger_power` setting in the EV charger config step.
There is no separate user input for it.

- `batteries_charge_solar` → `ev_smart_charging`
- `batteries_wait_mode` → `ev_smart_charging`
- All other recommendations: **kept unchanged** (must not be overridden by EV label)

The following must never be overridden by the EV label:
`batteries_charge_grid`, `force_batteries_discharge`, `force_export`,
`time_passed`, `missing_input_entities`.

`batteries_discharge_mode` is **not** in this protected set — it is intentionally
overrideable.  When an EV is scheduled to charge in a slot that is also inside a
discharge window, the `ev_smart_charging` label wins so dashboards correctly reflect
EV activity rather than showing a discharge recommendation during an active charge
session.

#### Layer 3 — Runtime resolver (current slot only, at hardware-write time)

Applied to the current slot immediately before hardware writes, using live sensor data:

1. `import_price < 0` → `force_export` (overrides everything)
2. `batteries_charge_grid` → kept (must never be overridden by EV or discharge rule)
3. Any EV actively charging → `ev_smart_charging`
4. Battery energy > remaining discharge-schedule need → `batteries_discharge_mode`

### Invariants for tests

- A slot assigned `batteries_charge_grid` by the planner must never be relabelled by
  the EV load labelling pass (layer 2).
- A slot assigned `batteries_discharge_mode` **may** be relabelled `ev_smart_charging`
  by the EV load labelling pass when `ev_total_planned_load_kwh > 0`.
- A slot with `ev_planned_load_kwh > 0` and recommendation `batteries_charge_solar`
  must be relabelled `ev_smart_charging` after layer 2.
- A slot with `ev_planned_load_kwh > 0` and recommendation `batteries_wait_mode`
  must be relabelled `ev_smart_charging` after layer 2.
- The runtime resolver must set `force_export` when `import_price < 0`, regardless
  of the planner recommendation.
- The runtime resolver must NOT override `batteries_charge_grid` even when an EV
  is actively charging.
- The runtime resolver must NOT override `batteries_charge_grid` even when
  `import_price < 0` is False and EV is charging.
- Priority 1 (negative price → `force_export`) always beats priority 3 (EV charging).

## Energy balance per slot

For every slot:

```text
net_load_kwh = house_load_kwh + ev_planned_load_kwh - pv_kwh
```

`ev_planned_load_kwh` is the **extra** EV AC load to add to net consumption — the
portion not already captured in `house_load_kwh`.  See the EV load semantics section
for the three-field breakdown.

When EV integration is disabled, `ev_planned_load_kwh` is `0.0` for every slot
and the formula is identical to the non-EV case.

Positive `net_load_kwh` means the house (plus any extra EV load) needs energy.

Negative `net_load_kwh` means there is net surplus (solar minus house and EV load).

### EV charger energy source

The EV charger is an **AC appliance** that draws directly from the grid or from
PV surplus.  **It never draws from the house battery.**  This means:

- The battery's net demand is computed from `house_load - pv` only.
- `ev_planned_load_kwh` is added to `grid_import_kwh` — not to the battery
  discharge calculation.
- When PV surplus is available the EV consumes from it first (reducing
  `grid_export_kwh`); any residual EV demand that cannot be met by PV is
  imported from the grid.
- `batteries_discharged` is therefore independent of `ev_planned_load_kwh`.

Battery and grid flows must satisfy:

```text
house_load_kwh
= pv_used_for_house_kwh
+ battery_discharge_to_house_kwh
+ grid_import_for_house_kwh

grid_import_kwh
= grid_import_for_house_kwh
+ grid_import_for_battery_kwh
+ ev_grid_import_kwh
```

PV production must satisfy:

```text
pv_kwh
= pv_used_for_house_kwh
+ pv_used_for_ev_kwh
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
| Charge efficiency | `battery_charge_efficiency_pct` | 97 % | Fraction of input energy stored. |
| Discharge efficiency | `battery_discharge_efficiency_pct` | 97 % | Fraction of stored energy delivered to house. |

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

## MILP soft constraints (penalty approach)

The MILP optimizer (`milp_optimizer.py`) uses **soft constraints** with penalty
variables to prevent infeasibility when the initial SoC is outside bounds
(e.g., overcharged battery).

### Penalty variables

- `s_max_pen[t]` — kWh by which SoC exceeds `usable_kwh` in slot `t`
- `s_min_pen[t]` — kWh by which SoC drops below 0 in slot `t`

### Soft SOC bounds

```text
Upper: soc[t] - s_max_pen[t] <= usable_kwh
Lower: -soc[t] - s_min_pen[t] <= 0
```

### Penalty cost

```text
p_soc = max(p_imp) * 100
```

The penalty cost is added to the objective:
`p_soc * (s_max_pen[t] + s_min_pen[t])`.  It is high enough that the solver
never uses penalties unless forced by an out-of-bounds initial SoC.

### Invariants

- The MILP is **never** infeasible due to initial SoC boundary violations.
- When `current_kwh` is within `[0, usable_kwh]`, all penalty values are zero.
- When `current_kwh > usable_kwh`, `s_max_pen[0]` absorbs the excess and
  decreases over time as the solver discharges.
- Violations are logged at WARNING level.
- The diagnostics dict (returned alongside the slot list) captures penalty
  values for the engine to surface.

### EV co-optimisation (MILP)

When one or more `EVConfig` objects are passed to `solve_milp()`, the LP
expands to co-optimise EV charging alongside the battery.  EV loads are no
longer pre-computed by `ev_planner.py` and treated as fixed inputs; instead
the MILP decides **when and how much each EV charges**.

**EV variables** (per active EV):
- `ev_c[t]` — DC-side energy delivered to the EV battery in slot `t` (kWh).
  Bounded by `[0, ev.max_charge_per_slot]`.
- `ev_pen` — single slack variable absorbing unmet deadline target (kWh).

**EV constraints**:
- SOC dynamics (cumulative, no discharge):
  `ev_soc[t] = ev_initial + Σ_{k≤t} ev_c[k]`
- SOC upper bound per slot: `ev_soc[t] ≤ ev_capacity`
- Deadline soft goal: `ev_soc[D] + ev_pen ≥ ev_target` where `D` is the
  LP-slot index of the effective deadline.
- No discharge: `ev_c[t] ≥ 0` (via bounds).

**Energy balance** includes EV AC load:
```text
gi + pv + ed·η_dis = base_load + ec/η_chg + ge + Σ ev_c/eff
```
where `base_load` is recomputed **without** pre-computed EV planned loads
(only house consumption minus PV).

**Objective** includes a high-cost deadline penalty:
```text
ev_penalty_cost = max(p_imp) * max(capacity, 1.0) * 100
```
ensuring the MILP always prefers meeting the target when physically possible.

**Output**: the MILP writes EV decisions to `ev_planned_load_kwh`,
`ev_accounted_load_kwh`, and `ev_total_planned_load_kwh` on the output slots.
`estimated_net_consumption_kwh` and `estimated_cost_currency` are recomputed
to reflect the new EV loads.

#### Invariants

- When `ev_configs=None`, behaviour is identical to the pre-#530 code
  (backward compatible).
- EV charge per slot never exceeds `ev.max_charge_per_slot`.
- Cumulative EV SoC never exceeds `ev.capacity_kwh`.
- When `ev.deadline_slot` is provided and the target is reachable, the
  deadline penalty `ev_pen` is zero.
- When the target is unreachable within the available slots, `ev_pen > 0`
  absorbs the shortfall — the MILP never becomes infeasible due to EV
  constraints.
- EV diagnostics (total DC kWh delivered, deadline penalty, deadline met)
  are included in the diagnostics dict under the `"ev"` key.

## Cost function

The cost function returns **two distinct aggregates** for every plan
(issue #413):

- `total_cost` — the **money outcome** of the plan within the horizon.
  Pure DKK / EUR.  Auditable; directly comparable to a real electricity bill.
- `score` — the **selector objective**.  Equals `total_cost` plus every
  synthetic penalty plus the terminal-SoC opportunity cost.  The candidate
  selector picks the plan with the **lowest score** — not the lowest money
  cost.

```text
total_cost
= grid_import_cost
- export_revenue
+ battery_cycle_cost
+ conversion_loss_cost
+ tariff_cost
```

```text
score
= total_cost
+ soc_guard_penalty
+ grid_limit_penalty
+ override_penalty
+ terminal_soc_value
```

Where:

- `soc_guard_penalty`, `grid_limit_penalty`, `override_penalty` are
  **selector-only** synthetic terms.  They must **never** appear in
  `total_cost`, because they do not represent real money paid or earned.
- `terminal_soc_value` is **selector-only**.  It is negative (credit) when
  the plan ends with more stored energy than it started with, and positive
  (penalty) when the plan empties the battery.  It prevents the selector
  from preferring plans that look cheap only because they drained the
  battery to zero before end-of-horizon.

The implementation exposes both numbers on `PlanCostBreakdown` together with
a deprecated `total` alias that equals `score` (kept so older code and tests
that compared plans by `.total` still select the same winner).

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

When the export price is negative (curtailment penalty), ``export_revenue``
is negative — exporting costs money rather than earning it.  The
``total_cost`` formula ``import_cost − export_revenue`` correctly handles
this: subtracting a negative adds the cost.

**Export price clamping (``export_min_price``):**  When
``export_min_price > 0``, the inverter physically blocks all export for
slots where ``export_price < export_min_price`` (applier sets
``GRID_EXPORT_LIMIT_WATT``).  To keep the planner model consistent with
this physical behaviour:

- The MILP clamps ``export_price`` to 0 for all slots where
  ``export_price < export_min_price`` *before* solving the LP.
- The cost function (``score_plan``) applies the same clamping via
  ``CostWeights.export_min_price``.
- This clamping only affects the planner's decision-making; the raw slot
  ``export_price`` is preserved for diagnostics.

Invariant: ``export_price < export_min_price`` → planner treats export
revenue as 0 in both optimisation and scoring.

### Battery cycle cost

Cycle cost should count physical battery throughput.

Recommended:

```text
battery_throughput_kwh = battery_charge_stored_kwh + battery_energy_removed_kwh
cycle_cost = battery_throughput_kwh * cycle_cost_per_kwh
```

If using equivalent full cycles, document the formula.

Avoid double-counting the same energy as both charge and discharge unless the cycle-cost definition explicitly expects throughput.

### Past-slot exclusion

The cost function must **skip** any slot whose recommendation is `time_passed`.

Past slots have `estimated_battery_soc = 0.0` as a sentinel value written by
the SoC simulator.  Including them in SoC-guard penalty calculations would
generate a false `soc_low_penalty` of `soc_low_penalty_weight × min_soc_pct²`
**per past slot**, added equally to every candidate plan.  Because the spurious
penalty is identical across all candidates it does not change the winner but
inflates the reported `total` cost and makes the logs misleading.

All other energy-flow fields (`grid_import_kwh`, `batteries_charged`, etc.) are
also zeroed on past slots by the simulator, so skipping them has no effect on
any cost term other than eliminating the bogus SoC penalty.

**Invariant for tests:**
```text
score_plan(slots_with_past).soc_penalty
== score_plan(future_only_slots).soc_penalty
```

### Terminal SoC value

Plans must not look better merely because they empty the battery before the
horizon ends.

The cost function implements this via a `terminal_soc_value` term that
contributes to `score` (not to `total_cost`):

```text
initial_kwh = stored battery energy above the discharge floor at the start of the horizon
final_kwh   = stored battery energy above the discharge floor at the end of the horizon
            (taken from the last future slot's estimated_battery_capacity)
delta_kwh   = initial_kwh - final_kwh

terminal_soc_value = delta_kwh * replacement_price_per_kwh
```

Sign convention:

- `delta_kwh < 0` (plan ends with **more** energy than it started with) →
  `terminal_soc_value < 0` → **credit**, reduces `score`.
- `delta_kwh > 0` (plan ends with **less** energy) →
  `terminal_soc_value > 0` → **penalty**, increases `score`.

The recommended `replacement_price_per_kwh` is the **minimum future import
price across the planning horizon**.  This represents the marginal cost of
re-purchasing one stored kWh at the cheapest available opportunity — the
economically correct proxy for the opportunity cost of consuming stored energy
now rather than later.  Using the average over all future slots (including
expensive peak prices) systematically over-values stored energy during
high-price periods and biases the selector against discharging.

Terminal-SoC accounting is **only active** when both `initial_battery_kwh`
and `replacement_price_per_kwh` are supplied to `score_plan`.  Unit tests
that call `score_plan` without horizon context (e.g. simple per-slot
arithmetic checks) do not need the term and may omit both inputs; in that
case `terminal_soc_value = 0.0` and `score == total_cost + penalties`.

### Invariants for tests

- `total_cost` must equal
  `import_cost - export_revenue + cycle_cost + conversion_loss_cost`
  exactly.  No synthetic penalty may enter `total_cost`.
- `score` must equal
  `total_cost + soc_penalty + grid_limit_penalty + override_penalty + terminal_soc_value`
  exactly.
- When all penalties are zero and terminal-SoC is disabled, `score == total_cost`.
- The candidate selector must pick the candidate with the lowest `score`,
  not the lowest `total_cost`.
- `winner.score == output.plan_cost.score` for every planner run.
- `winner.slots == output.slots` for every planner run.
- Given two otherwise-identical plans, the one that ends with more stored
  battery energy must have the lower `terminal_soc_value` and therefore the
  lower `score` (all else equal).

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

### Plan-level hysteresis (anti-flapping, issue #372)

The selector may optionally apply **plan-level hysteresis** to avoid switching
strategies for tiny cost improvements.  When hysteresis is active, the
previously active plan (identified by candidate name) is re-evaluated with
current data.  If its score improvement over the best new candidate is below
both configured thresholds, the previous plan is kept.

Two thresholds are supported, evaluated in order:

1. **Absolute threshold** (currency): the new plan's score must be lower
   (better) by at least this amount.  ``0.0`` disables the check.
2. **Percentage threshold** (relative): the new plan's score must be lower
   by at least this percentage of the previous plan's score.  ``0.0`` disables
   the check.

If the previous plan's candidate is not found in the current candidate set
(e.g. because the underlying strategy no longer applies), hysteresis falls
back to normal selection.

The hysteresis decision is surfaced in
:attr:`PlanExplanation.hysteresis_active`,
:attr:`PlanExplanation.hysteresis_reason`, and
:attr:`PlanExplanation.previous_plan_name`.

The previous winner's name and score are persisted across planner runs by the
coordinator and passed as part of :class:`PlannerInput`.

Hysteresis is enabled by default with a 5 % percentage threshold; setting
``planner_hysteresis_enabled = False`` disables it entirely.

### Window-level hysteresis (anti-flapping, issue #315)

In addition to plan-level hysteresis, HSEM applies **window-level hysteresis**
on the **current time slot** to prevent rapid charge↔discharge toggles near
schedule-window boundaries.  This is a separate, independent mechanism that
operates on the slot recommendation level rather than the plan level.

When the planner produces a new recommendation for the current slot that
belongs to a different *category* than the previous recommendation, and the
new category has been in effect for less than the configured hold time,
the previous recommendation is kept.

Two categories are defined:

- **Charge-type**: ``batteries_charge_grid``, ``batteries_charge_solar``,
  ``ev_smart_charging``
- **Discharge-type**: ``batteries_discharge_mode``,
  ``force_batteries_discharge``, ``force_export``
- **Neutral**: ``batteries_wait_mode``, ``time_passed``,
  ``missing_input_entities``, ``None``

Only cross-category transitions (charge ↔ discharge) are held.  Same-category
changes (e.g. grid-charge → solar-charge) and transitions to/from neutral
are always allowed.

The hold time is configured by ``planner_window_hysteresis_minutes``
(default: 0, disabled).  When set to a positive integer, a charge→discharge
or discharge→charge transition on the current slot is suppressed unless the
new category has been active for at least this many minutes.

The previous recommendation and its slot start time are persisted across
planner runs by the coordinator so the elapsed time is measured from the
moment the previous category was established — not from the planner cycle
time.

Window-level hysteresis is applied **after** the planner engine completes but
**before** the current slot recommendation is resolved.  The held
recommendation is written back into the planner output slots so it propagates
to the ``hourly_recommendations`` list and ultimately to hardware writes.

### Invariants for window-level hysteresis tests

- First run (no previous state) always accepts the new recommendation.
- Same-category transitions are never held.
- Cross-category transitions within the hold time keep the previous recommendation.
- Cross-category transitions after the hold time expires switch to the new one.
- Neutral recommendations never trigger hold behaviour.
- Feature disabled (hold minutes = 0) always allows the switch.

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
- Hysteresis keeps the previous plan when improvement is below absolute threshold.
- Hysteresis keeps the previous plan when improvement is below percentage threshold.
- Hysteresis switches to the new plan when improvement exceeds both thresholds.
- Hysteresis is inactive on the first planner run (no previous plan).
- Hysteresis falls back to normal selection when the previous plan name is not found.
- Hysteresis is inactive when the feature is disabled.
- `PlanExplanation.hysteresis_active` reflects the hysteresis decision.
- `PlanExplanation.hysteresis_reason` describes why hysteresis kept or released the plan.

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

### Discharge concentration across days

``concentrate_discharge_on_expensive_slots`` clears the cheapest
discharge slots when the battery cannot cover all of them.  This
pre-processing step runs before the SoC simulation and ensures the
battery is reserved for the most expensive slots.

The function groups discharge slots by **calendar day** and gives each
day its own independent ``usable_kwh`` budget.  This correctly accounts
for the fact that the battery is recharged by solar (or cheap grid
hours) between discharge windows on different days.  Without per-day
budgets, slots on day N+1 would compete with slots on day N for the
same capacity pool — even though the battery is fully recharged in
between.

Within each day the estimate is conservative: it assumes the battery
starts at full capacity and there is no incoming charge between
discharge slots on the same day.

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

## EV planned load integration

`base_load_includes_ev` is automatically derived from the
`hsem_house_power_includes_ev_charger_power` setting in the EV charger config step.
When the house consumption sensor includes EV charger power, `base_load_includes_ev`
is `True` (EV load is already in the base consumption averages). Otherwise it is `False`.
There is no separate user-facing configuration for this field.

### EV load field semantics

Three per-slot fields capture EV load intent precisely:

| Field | Meaning |
|---|---|
| `ev_planned_load_kwh` | Extra EV AC load **added to net consumption** — only the portion not already in `avg_house_consumption`. Zero when `base_load_includes_ev = True`. |
| `ev_accounted_load_kwh` | EV AC load **already included** in the house consumption sensor. Non-zero when `base_load_includes_ev = True`. Must not be added to net consumption again. |
| `ev_total_planned_load_kwh` | Total planned EV AC load regardless of accounting mode: `ev_planned_load_kwh + ev_accounted_load_kwh`. Always non-zero when any EV charging is planned. |
| `ev_charger_calculated_power` | Target AC power (W) for the primary EV charger during this slot. Computed from the EV planner's per-slot energy target: `round((ac_load_kwh / slot_duration_hours) × 1000)`. For the **current** (partially elapsed) slot, `slot_duration_hours` is the remaining time (minimum 1 s), because the EV planner already scales `ac_load_kwh` to the remaining minutes. For future slots the full slot width is used. Zero when no charging is planned. |
| `ev_second_charger_calculated_power` | Same as above, for the second EV. |

When `base_load_includes_ev = False`:
```text
ev_planned_load_kwh      = summed EV AC load (primary + second)
ev_accounted_load_kwh    = 0
ev_total_planned_load_kwh = summed EV AC load
```

When `base_load_includes_ev = True`:
```text
ev_planned_load_kwh      = 0
ev_accounted_load_kwh    = summed EV AC load (primary + second)
ev_total_planned_load_kwh = summed EV AC load
```

Multiple EVs are always **summed**, never overwritten:
```text
ev_total_planned_load_kwh = primary_ev_ac_load + second_ev_ac_load
```

### Net load formula with EV

```text
effective_net_load_kwh
    = avg_house_consumption
    + ev_planned_load_kwh
    − solcast_pv_estimate
```

Only `ev_planned_load_kwh` (the extra, non-accounted portion) is added.
Using `ev_total_planned_load_kwh` when `base_load_includes_ev = True` would
double-count the EV load.

### Design invariants

The EV planner (`planner/ev_planner.py`) MUST satisfy these invariants:

1. **One-pass, no circularity**: EV plans are built entirely from raw inputs
   (EV SoC, target SoC, capacity, charger power, deadline, and the net
   surplus signal). They must never depend on the home battery planner output.

2. **Net surplus as starting point**: The surplus signal passed to the EV
   planner must represent **net surplus after house consumption**, not raw PV.
   The house always uses solar first; only the leftover is available to the EV
   at no extra grid cost.

   The engine computes base net consumption first, then derives:
   ```text
   slot_net_surplus = max(−estimated_net_consumption, 0.0)
                    = max(pv_estimate − avg_house_consumption, 0.0)
   ```

   `populate_net_consumption` is called **before** EV planning so that
   `estimated_net_consumption` already reflects PV confidence decay
   (day+1 at 90 %, day+2 at 80 %) and any other pre-EV transforms.

3. **`ev_planned_load_kwh` injected before final `populate_net_consumption`**:
   After the EV planner writes per-slot loads, `populate_net_consumption` is
   called a **second time** to incorporate `ev_planned_load_kwh` into the
   final `estimated_net_consumption` values. The final values include both
   house load and any extra EV load.

4. **Additive aggregation**: `apply_ev_planned_load_to_slots` must **add** to
   the existing slot total, never overwrite it (`+=` not `=`). This ensures
   primary and second EV loads are summed when they share a slot.

5. **No double-counting**: When `base_load_includes_ev = True` for an EV, its
   planned load must NOT be added to `ev_planned_load_kwh`. It is captured in
   `ev_accounted_load_kwh` instead.

6. **Partial current slot**: The currently active slot must be scaled by
   remaining slot duration, not the full slot width.

7. **Deadline enforcement**: Slots with `slot_start >= effective_deadline`
   must receive zero EV load (see invariant 8 for the definition of
   `effective_deadline`).

8. **One-midnight-crossing horizon cap** (issue #413): The EV charging
   window may extend into tomorrow but must NEVER reach into the day after
   tomorrow, regardless of the planner's overall slot horizon (which may be
   48 h or 72 h).

   Define:

   ```text
   horizon_cap         = midnight_at_start_of(now.date() + 2 days)
                         in now's timezone
   effective_deadline  = min(user_deadline, horizon_cap) if user_deadline
                         is not None else horizon_cap
   ```

   The EV planner must use `effective_deadline` as the upper bound when
   filtering candidate slots and when clamping per-slot allocation duration.
   This guarantees a single-midnight EV window even when the user-configured
   deadline is missing (`None`) or set to a future instant beyond
   end-of-tomorrow.

   `plan.deadline` (the value surfaced on the EV charging-plan sensor) keeps
   the **user-configured** deadline so dashboards display what the user
   asked for.  When the cap actually changes the deadline, the
   `effective_deadline` and `deadline_clamped` fields are surfaced on
   `plan.data_quality` for debuggability.

9. **Guard states**: The EV planner must return a valid `EVChargingPlan` with
   an appropriate `state` string in all edge cases (disabled, not connected,
   smart charging off, fully charged, no slots before deadline, invalid config).

10. **Disabled EV is zero-cost**: When `ev_planned_load_enabled = False`, all
    three EV load fields must be `0.0` and the home battery planner output
    must be identical to the non-EV case.

11. **Charge past target SoC (Pass 3)**: When `allow_charge_past_target_soc`
    is enabled and the EV has reached its target SoC but is below 100 %, a
    third pass scans remaining PV-surplus slots.  The EV only receives
    stranded surplus — slots where the house battery is **predicted to be
    full** (from the cumulative net consumption trajectory).  Pass 3 never
    draws from the grid; all energy is solar-surplus with zero cost.

12. **EV charger power field**: `ev_charger_calculated_power` is computed
    from the EV planner's `ac_load_kwh` (AC-side energy) divided by the
    slot duration in hours.  For the **current** (partially elapsed)
    slot the divisor is the remaining slot time (minimum 1 s), because
    the EV planner already scales `ac_load_kwh` to the remaining minutes.
    Using the full slot width would understate the required charge power.
    The field is zero when no EV charging is planned in that slot.  It is
    purely a planner output — the applier must read this value to throttle
    the go-e charger; the planner does not control hardware directly.

### Invariants for tests

- When `ev_planned_load_enabled = False`, all `ev_planned_load_kwh == 0.0`.
- When EV is at or above target SoC (`current_soc >= target_soc`) and
  `allow_charge_past_target_soc` is disabled or `current_soc >= 100`,
  all EV load fields are `0.0` (early return `"fully_charged"`).
- When `allow_charge_past_target_soc` is enabled and
  `target_soc <= current_soc < 100`, Pass 3 may allocate surplus-PV
  charging slots past the target SoC (energy_needed ≈ 0 does **not**
  trigger an early return).
- Pass 3 surplus-PV slots: allocated kWh = `min(max_charge, net_surplus)`,
  `import_needed_kwh == 0.0`, `estimated_cost == 0.0`.
- When `base_load_includes_ev = True`:
  - `ev_planned_load_kwh == 0.0` for all slots.
  - `ev_accounted_load_kwh > 0` for charging slots.
  - `ev_total_planned_load_kwh == ev_accounted_load_kwh`.
  - Net consumption is not affected by the EV (no double-count).
- `ev_total_planned_load_kwh == ev_planned_load_kwh + ev_accounted_load_kwh` for every slot.
- Net surplus slots are allocated before grid-import slots.
- `sum(ev_total_planned_load_kwh over all slots)` equals `total_kwh_needed` (±charger rounding).
- Deadline: no EV load on slots with `slot_start >= effective_deadline`.
- One-midnight-crossing cap: when `user_deadline is None` and the planner
  horizon extends beyond 24 h, no EV load is scheduled on slots whose
  `slot_start >= midnight_at_start_of(now.date() + 2 days)`.
- Deadline-clamp diagnostic: when the user-configured deadline is later
  than the horizon cap, `plan.data_quality["deadline_clamped"] is True`
  and `plan.data_quality["effective_deadline"]` holds the ISO-format clamp.
- Partial slot: current slot load ≤ `charger_power_kw × remaining_minutes / 60`.
- When EV consumes all net surplus, home battery `batteries_charged == 0.0` in that slot.
- `winner.cost == final_output.cost` still holds when EV load is active (no post-selection mutation).
- Both `ev_charging_plan` and `ev_second_charging_plan` on `PlannerOutput` are `None` when disabled.
- Enabling only the second EV does not affect primary EV fields and vice versa.
- Two EVs charging in the same slot: `ev_total_planned_load_kwh == primary_ac + second_ac`.
- One EV with zero load does not clear the other EV's load.
- `ev_smart_charging` label is applied when `ev_total_planned_load_kwh > 0`, even when
  `ev_planned_load_kwh == 0` (i.e. `base_load_includes_ev = True`).

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
