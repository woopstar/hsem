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
2. Actual PV surplus (`estimated_net_consumption_kwh < 0`) and battery not full → `batteries_charge_solar`
3. Future `force_batteries_discharge` AND battery > required → `batteries_wait_mode`
4. Slot's month is a winter month → `batteries_wait_mode`
5. Slot's month is a summer month, actual PV surplus → `batteries_charge_solar`; else → `batteries_discharge_mode`

> **Note:** `BatteriesChargeSolar` is only assigned when there is a genuine PV
> surplus (negative net consumption).  A small positive house load with zero PV
> must not be mislabeled as solar charging — that would cause the applier to
> write `MaximizeSelfConsumption` instead of `TimeOfUse` + charge TOU
> (issue #720).

> **Per-slot season:** the seasonal check (steps 4–5) uses each slot's own
> calendar month (derived from the slot's `start` timestamp in the local
> timezone), **not** the month of `now`.  This means a planning horizon that
> crosses a season boundary (e.g. Aug 31 → Sep 1) applies the correct seasonal
> strategy to every slot independently — summer slots get discharge/solar and
> winter slots get wait-mode, even within the same 48-hour plan.

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

### Physical conversion-loss accounting

Conversion loss is represented completely by the AC/DC energy balance. If
`ec[t]` is stored battery charge and `ed[t]` is battery energy removed:

```text
charge_ac_draw[t] = ec[t] / charge_efficiency
discharge_ac_delivery[t] = ed[t] * discharge_efficiency
```

The objective and scorer price the resulting `grid_import_kwh` and
`grid_export_kwh`. Charging loss therefore appears as additional billable AC
import or foregone PV export; discharge loss appears as less avoided import or
less AC export. A separate `(1-efficiency)` monetary coefficient would price the
same energy twice.

The public `conversion_loss_cost` and
`discharge_loss_cost_destination_aware` fields remain for schema compatibility
and are always `0.0`.

### Invariants for tests

- Charging 10 kWh at 90 % efficiency must draw approximately 11.11 kWh AC.
- Charging 10 kWh at 100 % efficiency must draw exactly 10 kWh AC.
- Discharging 10 kWh battery energy at 90 % efficiency must deliver 9 kWh AC.
- Lower charge efficiency increases physical AC draw; no separate loss fee is added.
- Lower discharge efficiency reduces physical AC delivery/export; no separate loss fee is added.
- `conversion_loss_cost` and the destination-aware compatibility diagnostic
  remain exactly zero for every efficiency and destination.
- With 98 % charge/discharge efficiency and zero wear, grid-to-export arbitrage
  breaks even at a price ratio of `1 / (0.98 * 0.98)`.

## Live data injection (current slot)

Before scoring, the engine replaces the current (partially elapsed) slot's
forecast PV and consumption with live measurements
(`engine_population.py::_inject_live_data_into_current_slot`).  Live Watts
are converted to a projected full-slot kWh by multiplying by the slot's full
duration.

### Live house availability is explicit, not inferred (issue #792)

`PlannerInput.live_house_consumption_available` is a tri-state
(`bool | None`).  The coordinator (`coordinator_builder.build_planner_input`,
via `_resolve_live_house_measurement`) always sets an explicit `bool`: the
reading is authoritative only when its entity is configured, present,
finite, non-negative, and not on `live.missing_entities_list`.  `None` is
reserved for direct/legacy callers (e.g. hand-built `PlannerInput` instances
in tests) that never set the field — for those, injection falls back to the
old heuristic (`live_house_consumption_w > 1e-9`).

This closes the gap where a genuine, available **0 W** house reading was
indistinguishable from "no reading yet" (both read as `0.0` and failed the
old `> 1e-9` check): a real 0 W reading now overwrites the forecast, while an
explicitly-unavailable reading leaves the forecast untouched regardless of
what stale/default wattage happens to be sitting in
`live_house_consumption_w`.  `_resolve_live_solar_measurement` is hardened
with the same finite/non-negative checks for consistency, though PV
injection itself still keys off `live_solar_production_w > 1e-9` (solar
availability tri-stating is not yet wired end-to-end).

`utils/live_power.py` (`LivePowerEstimate` / `LivePowerWindow`) provides a
short rolling-median sampler for smoothing bursty live power across
multiple ticks before it reaches the planner.  The coordinator wires it in
via `coordinator_live_power.py` (issue #797) — see *Coordinator live-power
window and replan budget* below.

When `house_power_includes_ev = True`, the live house reading may contain EV
charging power that the battery must not serve (issue #592).  Two layers
protect against this:

1. **Known EV power subtraction** — when `ev_session_charge_kw` (and/or the
   second charger's) is available, it is subtracted from the live reading
   (floored at 0) before injection.
2. **Spike cap** — if the remaining live reading still exceeds
   `max(3 × forecast, 0.05 kWh)`, it is capped at the forecast (or at the
   0.05 kWh floor when the forecast is ~0, where the ratio test would be
   degenerate).  A spike of that magnitude is unambiguous unmetered load
   (e.g. a boolean-only EV status sensor); normal house load does not
   triple between slots.

The sub-window averages (`avg_house_consumption_1d/3d/7d/14d_kwh`) of the
current slot are **deliberately left unchanged** (issue #592).  The EV
discharge-cap fallback in `applier.async_apply_battery_settings` picks the
*minimum* of those windows to recover a clean house baseline when the live
reading is unreliable; overwriting them with the live-injected value (which
can still include unmeasured EV load when no EV power sensor is configured)
would destroy that fallback and let polluted history inflate the hardware
discharge cap.

### Coordinator live-power window and replan budget (issue #797)

`coordinator_live_power.py` maintains the rolling `LivePowerWindow` across
the coordinator's lifetime and, when a sustained mismatch against the
accepted plan's estimate persists, requests a bounded corrective replan.

**Why a dedicated fast timer.** `LivePowerWindow` requires samples fresher
than `LIVE_POWER_MAX_SAMPLE_AGE_SECONDS` (20 s) to consider a channel
"available", with a minimum of `LIVE_POWER_MINIMUM_SAMPLES` (3) samples in
a `LIVE_POWER_WINDOW_SECONDS` (60 s) window. The coordinator's normal
per-cycle update interval is minutes-scale (droppable to 1 minute at
fastest today), which cannot keep samples within a 20-second age budget —
at that cadence the window would never accumulate enough fresh evidence
and the feature would be permanently inert. `coordinator_live_power.py`
therefore registers its own `async_track_time_interval` tick at
`LIVE_POWER_MONITOR_INTERVAL_SECONDS` (10 s), independent of the main
interval timer, purely to keep the window fed. Each full coordinator cycle
also seeds the window from its own immutable snapshot
(`_seed_live_power_window`), so a cycle that runs between fast-timer ticks
still contributes a sample.

**EV ambiguity.** When `house_power_includes_ev_charger_power = True`, a
live or planned EV charging signal makes the house-power reading
undecomposable — the same fail-closed rule as *Live house availability*
above, applied to the rolling window: `_live_power_ev_ambiguous` clears the
house channel (never the solar channel) whenever any EV is charging or has
positive power, on both the once-per-cycle snapshot and the fast-timer's
independent reads.

**Materiality.** A channel is considered "changed materially" only when
the full-slot energy delta exceeds `max(LIVE_POWER_REPLAN_MIN_DELTA_KWH,
accepted_kwh × LIVE_POWER_REPLAN_RELATIVE_DELTA)` (0.05 kWh or 10% of the
accepted channel's own full-slot energy, whichever is larger) —
`_live_power_channel_changed_materially`. An availability flip (channel
went from present to absent, or vice versa) is always material.

**Debounced request.** A material mismatch must persist for
`LIVE_POWER_MISMATCH_DEBOUNCE_SECONDS` (30 s) — tracked per current
recommendation slot — before `_track_live_power_mismatch` marks a replan
request pending. The request is revalidated (`_actionable_live_power_replan_slot`)
against fresh evidence and remaining slot time
(`LIVE_POWER_REPLAN_MIN_REMAINING_SECONDS`, 60 s) at the moment the
coordinator actually acts on it, so a request built from stale evidence
never fires blind.

**Bounded correction budget (one correction + one proven reversal).** Each
recommendation slot allows at most `LIVE_POWER_REPLAN_MAX_CORRECTIONS_PER_SLOT`
(2) live-power-triggered replans:

1. The first correction in a slot is always allowed.
2. A second correction in the *same* slot is allowed only when
   `_live_power_site_balance_direction` proves the new mismatch is the
   **opposite sign** of the first correction's direction (e.g. a cloud dip
   that triggered a defensive replan, followed by a genuine PV rebound) —
   never a second correction in the same direction, which would just be
   solve churn chasing noise.
3. A new recommendation slot resets the budget to zero.

`_live_power_site_balance_direction` computes signed net-demand change
(positive = more demand) from whichever of house/solar are comparable
(house is excluded entirely when ambiguous); it returns `None` — an
unprovable direction — when there is no accepted baseline or no comparable
channel, which fails the reversal proof closed.

**Acceptance.** Only `_accept_live_power_plan_estimate`, called after a
plan is actually persisted and published (never on a speculative or
discarded solve), advances `_last_plan_live_power_estimate` and the
budget counters. Consuming a pending request starts/advances the budget;
a normal (non-live-power-triggered) replan that happens to also observe a
fresh material mismatch re-arms the mismatch debounce for the *next* tick
without spending budget — the plan already reflects the newer picture, so
there is nothing to correct yet.

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

### MILP-pre-populated mode (issue #637)

When `milp_prepopulated=True` is passed to `simulate_soc()`, the
simulation uses the slot's **existing** `batteries_discharged_kwh`,
`grid_import_kwh`, and `grid_export_kwh` values verbatim — it does **not**
re-derive them from the recommendation label and net demand.

This mode is used for MILP-sourced candidates.  `solve_milp()` populates
these fields in a **single merged write-out pass** (issue #659) that:

1. Resolves degenerate LP vertices (simultaneous charge+discharge) by
   checking actual resolved SoC headroom at each slot in chronological
   order (issue #662).  The net residual (ec − ed) is clamped against
   the remaining ceiling headroom (``usable_kwh − running_soc``) or
   floor headroom (``running_soc − 0``).  If the available headroom is
   ≤ ``_MIN_ACTION_KWH`` the vertex is treated as solver noise and both
   ec and ed are zeroed.  The structurally-dead ``net_charge_profit``
   heuristic and the per-slot LP ``s_max_pen``/``s_min_pen`` variables
   are **not** used for this resolution — they cannot distinguish
   horizon-wide degeneracy from genuine economic signals.
2. Writes `batteries_charged_kwh` and `batteries_discharged_kwh` from the
   **resolved** ec/ed (not the raw LP arrays).
3. Derives `grid_import_kwh` and `grid_export_kwh` from the slot's energy
   balance equation using the **same resolved** ec/ed values — they are
   **not** read directly from the raw LP `gi[t]`/`ge[t]` arrays, because
   the raw arrays assume the original (potentially now-invalid) ec/ed
   combination.

All four energy-flow fields are consistent with each other and with the
recommendation label for every slot.  The resolved values are the source
of truth; the SoC simulation must never silently overwrite them.

For non-MILP candidates (`milp_prepopulated=False`, the default),
the simulation continues to derive discharge and grid flows greedily
from the recommendation label and net demand — unchanged behaviour.

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

### Named MILP bounds layout

Every solver-variable bounds block is assigned through `MilpBoundsBuilder` at
its declared physical offset. Core battery/grid/PV blocks, each active EV charge
and target-slack block, and the optional fuse-penalty block must collectively
cover every model column exactly once. Duplicate names, overlaps, invalid
lower/upper pairs, out-of-range widths, and unassigned columns fail before
HiGHS is invoked. This is structural hardening only; the finalized bounds and
planner economics are unchanged.

### Battery discharge upper bounds (hard)

In addition to the soft SoC penalties, the MILP applies **hard per-slot upper
bounds** on the discharge variable `ed[t]` (implemented as variable bounds in
`_build_constraints`):

1. **EV discharge guard (issue #592)** — when EV co-optimisation is **not**
   active and a slot has `ev_accounted_load_kwh > 0` (EV load already included
   in the house consumption sensor):

   ```text
   ed[t] <= max(0, base_load[t] - ev_accounted_load_kwh[t]) / discharge_eff
   ```

   The battery may only serve the non-EV portion of demand; the EV load is
   served by grid import or PV.  When co-optimisation **is** active the guard
   is skipped — `base_load` is rebuilt without EV load, so the battery can
   never serve it.

   **Exactness note**: although `base_load` is net of PV and
   `ev_accounted_load_kwh` is the gross EV load, the formula is exact —
   there is no PV double-counting.  With `H` = gross house consumption
   (incl. EV) and `P` = PV production, `base_load = max(H − P, 0)` and the
   non-EV unmet demand is `max(H − ev − P, 0)`.  When `base_load > 0`:
   `base_load − ev = H − P − ev`, identical.  When `base_load = 0`
   (PV surplus): `H − P ≤ 0`, so both sides are 0.  Hence
   `max(base_load − ev, 0) == max(H − ev − P, 0)` in all cases, and the
   battery is never blocked from serving genuine non-EV house load on
   partially PV-covered EV slots.

2. **No-export cap (issue #592)** — when `excess_export_enabled = False`
   (`no_export=True`):

   ```text
   ed[t] <= base_load[t] / discharge_eff
   ```

   The battery can never discharge more than the slot's house load, so it
   cannot export energy to the grid.  When `base_load[t] = 0` (PV-surplus
   slot) the cap is 0 and the battery sits idle.  PV export is unaffected.
   Note this also suppresses battery-driven grid arbitrage — intentional:
   "excess export disabled" means the battery never feeds the grid.

3. **Battery export minimum price floor (issue #752)** — when
   `battery_export_min_price > 0` and the slot's RAW export price is
   strictly below this floor (`p_exp[t] < battery_export_min_price`), the
   battery can serve house load on that slot but cannot intentionally
   export to the grid:

   ```text
   ed[t] <= base_load[t] / discharge_eff  (only on blocked slots)
   ```

   This is the per-slot, soft-switch companion to the global `no_export`
   cap: instead of blocking battery export everywhere, the floor blocks
   it only on slots whose raw export price is below the user's explicit
   guard.  The mask is evaluated on the RAW `p_exp` (before the
   export-≤-import clamp) so the user's explicit price signal is honoured
   even when the recommended threshold or the `export_min_price` floor
   are lower.  Above the floor the optimiser is free to decide whether
   exporting is worthwhile — reaching the threshold does NOT auto-trigger
   export.  The guard applies only to intentional battery-to-grid export
   (`ForceBatteriesDischarge`); it does NOT restrict normal battery
   self-consumption, battery discharge for house load, direct PV export,
   or PV charging of the battery.  The non-MILP `apply_excess_export`
   path applies the same floor by requiring `export_price >=
   max(export_min_price, recommended_threshold, battery_export_min_price)`
   for any slot it would otherwise label `ForceBatteriesDischarge`.

When any apply, the tighter cap wins.  When the battery cannot export on
a slot (`no_export` or a blocked-by-floor slot), the MILP labels
discharge slots `BatteriesDischargeMode` (self-consumption) rather than
`ForceBatteriesDischarge` in post-processing.

### EV co-optimisation (MILP)

When one or more `EVConfig` objects are passed to `solve_milp()`, the LP
expands to co-optimise EV charging alongside the battery.  EV loads are no
longer pre-computed by `ev_planner.py` and treated as fixed inputs; instead
the MILP decides **when and how much each EV charges**.

**EV variables** (per active EV):
- `ev_c[t]` — DC-side energy delivered to the EV battery in slot `t` (kWh).
  Bounded by `[0, ev.max_charge_per_slot]`.
- `ev_pen` — single slack variable absorbing unmet deadline target (kWh).
- `ev_amps[t]` — solver-native whole-amp charger command for a **managed**
  EV (not `fixed_session_only`), semi-integer (HiGHS type 3): either `0` or
  an integer in `[min_amp, rated_amp]` (issue #797).  Linked to `ev_c[t]` by
  the equality `ev_c[t] = ev_amps[t] × one_amp_dc_kwh[t]`, so a solved plan
  is always directly executable — there is no post-solve quantization step
  that can diverge from what was solved.  See
  `planner/milp/_ev_amp_lattice.py`.
- `ev_on[t]` — optional binary, present only for a managed EV whose Huawei
  discharge permission is restrictive (see *Discharge permission* below).

**EV constraints**:
- SOC dynamics (cumulative, no discharge):
  `ev_soc[t] = ev_initial + Σ_{k≤t} ev_c[k]`
- SOC upper bound per slot: `ev_soc[t] ≤ ev_capacity`
- Deadline soft goal: `ev_soc[D] + ev_pen ≥ ev_target` where `D` is the
  LP-slot index of the effective deadline.
- **Post-deadline zero-charge**: For EVs with a deadline and `charge_past_target=False`,
  `ev_c[t] = 0` for all `t > D`. This prevents charging after the deadline.
- **Target-cap constraint** (issue #636, relaxed by issue #797): For EVs
  with a deadline and `charge_past_target=False`, a hard upper bound caps
  cumulative pre-deadline charge near the economic shortfall:
  `Σ_{k≤D} ev_c[k] ≤ target_kwh − initial_soc_kwh + activation_quantum`.
  `activation_quantum` is the largest single-slot energy the EV's charger
  startup minimum could deliver across the pre-deadline slots — the
  smallest amount whole-amp hardware might have to overshoot by when no
  executable point lands exactly on the target.  Without this relaxation, a
  target with no exact whole-amp solution reports an avoidable deadline
  miss even though the nearest reachable whole-amp point is one activation
  quantum away.  `charge_past_target=True` still uses its own surplus-only
  mechanism instead.
- **Surplus-only for charge-past-target**: When `charge_past_target=True`,
  `ev_c[t]/η_charger ≤ max(0, pv[t] − base_load[t])` — charging only from PV surplus.
- **Battery-first for charge-past-target (issue #775)**: When `charge_past_target=True`,
  the house battery must take its share of the slot's PV surplus before the EV
  absorbs any.  A shared per-slot row enforces
  `ec[t] + Σ_ev ev_c[t]/η_charger ≤ max(0, pv[t] − base_load[t])` across all
  charge-past-target EVs, so the EV can only use surplus the battery cannot
  take.  Combined with the objective-side benefit cap (below), this guarantees
  the battery fills first and the EV only absorbs the remainder.
- No discharge: `ev_c[t] ≥ 0` (via bounds).

**Energy balance** includes EV AC load:
```text
gi + pv + ed·η_dis = base_load + ec/η_chg + ge + Σ ev_c/eff
```
where `base_load` is recomputed **without** pre-computed EV planned loads
(only house consumption minus PV).

**Objective** includes a high-cost deadline penalty:
```text
ev_penalty_cost = max(p_imp) * max(energy_needed, 1.0) * 10
```
ensuring the MILP always prefers meeting the target when physically possible.

**Pre-deadline slots** (`t ≤ D`, issue #797): `ev_c[t]` receives **no** direct
per-kWh benefit coefficient.  The slack penalty alone already prices meeting
the deadline at `ev_penalty_cost` per kWh shortfall — almost always far above
any real `p_imp[t]` — so the LP already prefers charging over paying the
penalty without an additional coefficient; charging still pays its own real
grid/PV opportunity cost (PV surplus first, then grid import at `p_imp[t]`
when insufficient).  Each pre-deadline slot instead carries a tiny positive
tiebreak cost, `_EV_TARGET_ENERGY_TIEBREAK_COST = 1e-7` per kWh, nudging the
LP toward the smallest executable (whole-amp) energy that clears the
target-cap constraint rather than leaving it indifferent among
cost-equivalent solutions above the target.  (Prior to issue #797, `ev_c[t]`
carried a large negative `-ev_penalty_cost` coefficient mirroring the slack
penalty; removing it let the target-cap activation-quantum relaxation above
work without also inflating the reward for the extra energy.)

### Discharge permission and whole-amp lattice (issue #797)

Huawei exposes **one global battery discharge limit**, shared by the house
battery and every EV.  `EVConfig.force_max_discharge_power` (permission, not
a command) and `EVConfig.max_discharge_power_w` (ceiling) express the user's
opt-in for the primary battery to discharge while a specific EV charges.
`planner/milp/_ev_amp_lattice.py::resolve_ev_amp_plan` computes each
managed EV's:

- `discharge_cap_kwh` — `0` unless `force_max_discharge_power` is `True`
  with a finite, positive `max_discharge_power_w` (fail-closed).
- Whether it `needs_on`: a conditional `ev_on[t]` binary is created only
  when the EV can command a positive amp (`runnable`) **and**
  `discharge_cap_kwh < max_dis` — i.e. its discharge permission is
  restrictive.  Full permission or a structurally-always-zero amp lattice
  (e.g. a `managed_session_cap_only` sentinel, see below) needs no binary.

When `ev_on[t]` exists, three rows per slot link it to the amp variable and
cap primary discharge conditionally:

```text
ev_amps[t]      ≤ rated_amp · ev_on[t]
min_amp · ev_on[t] ≤ ev_amps[t]
ed[t] + (max_dis − discharge_cap_kwh) · ev_on[t] ≤ max_dis
```

so `ed[t] ≤ discharge_cap_kwh` exactly while the EV has a non-zero command,
and `ed[t]` is unconstrained by this row while `ev_on[t] = 0`.  A live
session's already-flowing current is physical evidence independent of any
amp decision: when a managed EV reports live telemetry
(`session_charge_kw > 0`) with a restrictive `discharge_cap_kwh`, one direct
row caps `ed[0] ≤ discharge_cap_kwh` on the current slot regardless of what
the solver commands for future slots.

**`managed_session_cap_only` sentinel**: when a managed EV's live session is
already at or above target (and past-target charging is disallowed or it is
already at 100%), `engine_ev_milp.py::_build_ev_configs_for_milp` admits it
with `max_charge_per_slot = 0.0` instead of excluding it — so its
current-slot discharge permission/ceiling still applies — rather than
silently marking it `fixed_session_only` (which would misreport it as
unmanaged).  `resolve_ev_amp_plan` naturally reduces its amp bounds to
`(0, 0)` (unrunnable, since `rated_current_a` derives from a zero
`max_charge_per_slot`), so `needs_on` stays `False` for it: no wasted binary
for an EV that can never command a positive amp.

**Column layout**: `ev_{i}_amps` / `ev_{i}_on` blocks are declared last in
`build_milp_column_layout` (`planner/milp/_layout.py`), after every physical
and fuse block, using the same `MilpColumnLayout`/`MilpBoundsBuilder`
machinery as every other named block (see *Named MILP bounds layout*
below) — no separate incremental-width tracking is needed because the full
column count (including amp/on columns) is known before any constraint
matrix is built.

**Write-out**: because `ev_c[t]` is already tied to an executable whole-amp
command by the equality constraint above, `planner/milp/_write_results.py`
publishes a managed EV's solved allocation **verbatim** — no post-solve
concentration, minimum-power redistribution, or quantization.  The legacy
`_redistribute_below_minimum_power` / `_quantize_one_ev_allocation` helpers
(`planner/milp/_ev_quantize.py`) remain available for direct/compatibility
callers but are never invoked from the production write-out path.

**Time-limited incumbents**: semi-integer variables make the model more
expensive for HiGHS to solve to proven optimality within the solver's time
budget.  `planner/milp/_incumbent.py::validate_incumbent` checks a
HiGHS `status=1` ("time limit") result's decision vector against the
complete model (bounds, equality/inequality residuals, integrality) before
`solve_milp()` accepts it as a feasible — if unproven-optimal — plan,
instead of discarding a good solution outright.

**Post-deadline slots** (`t > D`):
- When `charge_past_target=False`: `ev_c[t]` is hard-constrained to zero —
  no charging allowed after the deadline.
- When `charge_past_target=True`: `ev_c[t]` receives a tiny benefit of
  `-0.0001/η_charger` per kWh AC, but is constrained to PV surplus only
  (`ev_c[t]/η_charger ≤ pv[t] − base_load[t]`). The house battery charges
  first (benefit ~`p_imp`), then export at good prices (benefit `p_exp`),
  and only when both are saturated does the EV get the remaining surplus.

**Output**: the MILP writes EV decisions to `ev_planned_load_kwh`,
`ev_accounted_load_kwh`, and `ev_total_planned_load_kwh` on the output slots.
`estimated_net_consumption_kwh` and `estimated_cost_currency` are recomputed
to reflect the new EV loads.

**Auditable meter cash flow.** `estimated_cost_currency` is the signed meter
cash flow computed from the final published grid fields, not from any
intermediate solver vector (`cost_helpers.grid_cash_flow_cost`):

```text
grid_import_kwh * import_price - grid_export_kwh * export_price
```

This equals `PlanCost.import_cost - PlanCost.export_revenue`. Battery cycle wear
is itemised separately in `PlanCost.total_cost`, while terminal value, guard
penalties, and the structural tiebreak exist only in `PlanCost.score`.

Non-finite rates carry no economic authority and are treated as `0.0`. An
export price below the effective battery-origin export floor earns `0.0`,
mirroring the MILP's export block so the published cash flow cannot claim
revenue the optimiser forbade.

#### Invariants

- When `ev_configs=None`, behaviour is identical to the pre-#530 code
  (backward compatible).
- EV charge per slot never exceeds `ev.max_charge_per_slot`.
- Cumulative EV SoC never exceeds `ev.capacity_kwh`.
- For EVs with a deadline and `charge_past_target=False`, cumulative
  pre-deadline charge `Σ_{k≤D} ev_c[k]` never exceeds `target_kwh − initial_soc_kwh`.
- When `ev.deadline_slot` is provided and the target is reachable, the
  deadline penalty `ev_pen` is zero.
- When the target is unreachable within the available slots, `ev_pen > 0`
  absorbs the shortfall — the MILP never becomes infeasible due to EV
  constraints.
- EV diagnostics (total DC kWh delivered, deadline penalty, deadline met)
  are included in the diagnostics dict under the `"ev"` key.

### MILP decision priority

The MILP solves a single global cost-minimization across all future slots
simultaneously.  It has no hard-coded priority order — the cost coefficients
in the objective function create a natural decision hierarchy.  Below is
how that plays out per slot, from cheapest to most expensive action.

**Objective** (minimise):

```text
Σ_t [ p_imp[t]·gi[t] − p_exp[t]·ge[t] + cycle_cost·m[t]
      + p_soc·(s_max_pen[t] + s_min_pen[t]) ]
+ Σ_ev [ ev_penalty·ev_pen + tiebreaker·Σ_t ev_c[t] ]
```

#### 1. Serve house load from PV (free)

PV surplus `pv[t]` has **zero objective cost**.  Curtailment `curt[t]` also
has zero cost.  The LP always uses available PV to cover house load first.

#### 2. Use remaining PV surplus

| Priority | Action | Cost coefficient | When taken |
|---|---|---|---|
| 2a | Charge house battery | Physical AC draw `ec/charge_eff` enters `gi` or reduces PV export, plus cycle wear | Battery below `usable_kwh`, future savings justify the real input/opportunity cost |
| 2b | Charge EV (pre-deadline, below target) | `-ev_penalty_cost` (benefit) + `p_imp[t]` (via grid) or `0` (via surplus) | EV below target, `t ≤ D` — the **deadline benefit** forces charging; PV used first, grid import when PV insufficient |
| 2c | Charge EV (post-deadline, past target) | **−future_value/η_charger** (benefit, capped at battery charge credit while battery has headroom — issue #775) | `t > D`, `charge_past_target=True`. Surplus-only + battery-first constraints: `ev_c/eff ≤ pv − base_load` and `ec + Σ ev_c/eff ≤ pv − base_load`. House battery fills first, then EV gets the remainder, then export |
| 2d | Export to grid | **−p_exp[t]** (revenue) | Battery full, EV doesn't want surplus, export price > 0 |
| 2e | Curtail PV | `0` (free) | Battery full, EV doesn't want surplus, `p_exp ≤ 0` (export costs money or is blocked) |

#### 3. Cover house-load deficit

| Priority | Action | Cost coefficient | When taken |
|---|---|---|---|
| 3a | Discharge battery | Physical AC delivery `ed*discharge_eff` reduces `gi` or raises `ge`, plus cycle wear | Battery has energy, discharging is cheaper than grid import |
| 3b | Import from grid | `p_imp[t]` | Battery empty or discharge not worthwhile (cycle cost > import price spread) |

#### 4. EV deadline charging (hard penalty)

When the EV is **below target SoC** with a deadline approaching:

- Penalty: `max(p_imp) × max(energy_needed, 1.0) × 10` per kWh shortfall
- Constraint: `initial_soc + Σ ev_c + penalty ≥ target`
- **Pre-deadline benefit**: Each slot `t ≤ D` gets coefficient `-ev_penalty_cost`
  on `ev_c[t]`, so the LP always prefers charging over paying the penalty.
- This penalty dominates everything — the LP will import at high prices
  to meet the deadline when physically possible.

#### 5. Post-deadline behaviour

After the deadline slot `D`:

- **Normal mode** (`charge_past_target=False`): Hard constraint `ev_c[t] = 0`
  for all `t > D`. The EV receives zero energy allocation — charging is
  forbidden regardless of PV surplus or grid prices.
- **Charge-past-target mode** (`charge_past_target=True`): The EV may still
  charge, but only from genuine PV surplus that would otherwise be curtailed
  or exported at near-zero prices:
  - Surplus-only constraint: `ev_c[t]/η_charger ≤ max(0, pv[t] − base_load[t])`
  - **Battery-first constraint (issue #775)**: `ec[t] + Σ ev_c[t]/η_charger ≤
    max(0, pv[t] − base_load[t])` — the house battery takes its share of the
    surplus first; the EV only absorbs what the battery cannot take.
  - Benefit: `-future_value_per_kwh/η_charger` per kWh AC (issue #630), where
    `future_value_per_kwh` is the avoided cost of importing the same energy
    later (`confidence_factor × mean(import_price)` over the next 24h — see
    `ev_future_charge_value_per_kwh` in `candidate_selector.py`). Falls back
    to a tiny fixed `0.0001/η_charger` tiebreaker when no future price data is
    available.
  - **Battery-first benefit cap (issue #775)**: the EV's per-kWh benefit is
    capped at the battery's charge credit (`abs(c_obj[ec[t]])`) minus the
    AC-side efficiency difference (`p_imp_obj[t] × (1/η_charge −
    1/η_charger)`) when the battery can absorb the full slot surplus.  The
    efficiency adjustment is required because the LP compares AC-side costs:
    the battery consumes `1/η_charge` AC per 1 DC stored, while the EV
    consumes `1/η_charger` AC per 1 DC.  Without the adjustment, equal
    coefficients still favour the EV when `η_charge < η_charger` (the common
    case).  The battery's per-slot absorption is
    `min(max_charge_per_slot, usable_kwh − current_kwh)`; when that is ≥ the
    slot's PV surplus, the battery takes it all and the EV's (speculative)
    benefit is capped at the battery's (concrete) charge credit.  When the
    battery cannot absorb the full surplus (tiny battery, or battery nearly
    full), the EV keeps its full benefit for the remainder.  Without this cap,
    a high speculative EV value outranks the battery and the two oscillate
    for the same surplus across replans.
  - Because the benefit is priced in real currency terms, charge-past-target
    EV charging competes fairly against house battery charging (worth
    ~`p_imp` via avoided future import) and export (`p_exp`) — but the battery
    always wins the surplus it can absorb (issue #775).
  - Grid import is never used for post-deadline EV charging.

#### 5. Terminal SoC (horizon-end valuation)

At horizon end, the battery's remaining energy is valued **inside the LP objective**
as a linear term so the LP itself optimises for it:

- `terminal_soc_value = (Σed − Σec) × replacement_price`
- Discharging (`ed[t]`) incurs a penalty in the objective
- Charging (`ec[t]`) earns a credit in the objective
- Ending with less energy → penalty (encourages recharging)
- Ending with more energy → credit (discourages wasteful discharging)

**Per-slot incentive cap (issue #655):** the per-slot terminal-SoC term is
capped by the **opportunity-cost differential** between the replacement
price and the slot's own import price:

```
terminal_premium[t] = max(0, replacement_price_per_kwh − p_imp[t])
```

When `replacement_price ≤ p_imp[t]`, the premium is zero — the LP sees no
terminal-SoC incentive and makes discharge decisions purely on per-slot
price signals.  When `replacement_price > p_imp[t]`, the differential
represents the genuine opportunity cost of using energy now vs. later.

This prevents the regression where uniform +replacement_price penalties
dominated per-slot import-saving benefits in flat/near-flat price scenarios,
causing zero discharge even when a full battery could cover house load
(issue #638 regression).

**Charge-credit cap (issue #694):** the terminal premium is applied
**asymmetrically**.  The charge credit is further reduced by the export
opportunity cost — the revenue foregone by not exporting the same PV
surplus:

```
charge_premium[t] = max(0, replacement_price_per_kwh − p_imp[t] − p_exp[t] / η_chg)

c_obj[ec[t]] −= charge_premium[t]   (capped credit for charging)
c_obj[ed[t]] += terminal_premium[t] (full penalty for discharging)
```

Without this cap, a high `replacement_price` can make the charge credit
larger than the export benefit (`−p_exp[t]`), causing the LP to charge the
battery from solar during expensive hours instead of exporting at peak
prices and deferring charging to cheaper slots — a "tunnel-vision" effect.
When `p_exp[t]` is high (expensive slots) the capped credit is small and
the LP exports; when `p_exp[t]` is low (cheap slots) the credit is close
to the full premium and the LP charges to store energy for future
discharge windows.  The discharge penalty is deliberately **not** capped,
preserving the issue #638 protection against unnecessary discharging.

**Deferred-export correction (issue #592):** the #694 cap compares charging
against exporting in the **same slot**.  When a *future* slot carries PV
surplus that exceeds the battery's absorption capacity
(`min(usable_kwh, max_charge_per_slot)`), that surplus is exported
regardless of today's charge decision — so the economically correct refill
price is the future slot's export price, not this slot's.  Letting
`p_exp_deferred[t]` be the minimum export price across all later slots
whose surplus exceeds that absorption capacity, the premium becomes:

```
charge_premium[t] = max(0, repl − p_imp[t] − p_exp[t] / η_chg
                             + min(p_exp_deferred[t], p_exp[t]) / η_chg)
```

When `p_exp_deferred[t] ≥ p_exp[t]` (or no qualifying future slot exists),
the correction is zero and the formula degrades to the #694 cap.  When a
cheaper future slot has unabsorbable surplus, the credit is restored so
the LP charges now at the high export price and lets the inevitable
future surplus refill the battery at the low price.  Both the MILP
(`milp/_objective.py`) and the selector (`cost_function.py`) compute the
premium via the shared helper `cost_helpers.compute_charge_premium()` with
the per-slot deferred price from `cost_helpers.deferred_export_price_by_slot()`
so the LP's decisions and the selector's score never diverge.

- **Undiscounted** — terminal SoC is a single point-in-time valuation at
  horizon end, matching `cost_function.py`'s `terminal_soc_value` treatment.
- The differential uses the finite signed import price. The premium itself
  is floored at zero (`max(0, repl - p_imp)`), so a negative import price
  cannot inflate the terminal premium beyond `replacement_price_per_kwh`.

The post-hoc `terminal_soc_credit` calculation in the diagnostics dict is
retained as a consistency check but no longer drives the LP's decisions.

#### Key constraint: EV surplus-only for charge-past-target

The constraint `ev_c[t]/charger_eff ≤ max(0, pv[t] − base_load[t])` ensures
past-target EV charging **never** draws from the battery or grid — only
genuine PV surplus that has nowhere else to go.

#### Charge-past-target benefit: avoided future import cost (issue #630)

The charge-past-target EV benefit (`EVConfig.future_value_per_kwh`) prices
one kWh of past-target EV charging at what it would otherwise cost to
import that same energy later:

```
future_value_per_kwh = confidence_factor × mean(import_price[t] for t in next 24h of slots)
```

- **24h lookahead**: always available even on the minimum-configured
  planning horizon (24h), long enough to smooth daily price cycles, short
  enough to avoid relying on degraded/missing day+2 forecasts.
- **`confidence_factor`** (default `0.9`, configurable per EV via
  `hsem_ev_past_target_confidence_factor` /
  `hsem_ev_second_past_target_confidence_factor`): discounts the estimate
  to account for the EV's future need being less certain than the house
  battery's scheduled discharge (depends on driving pattern, whether the EV
  stays plugged in, etc.).
- Mirrors `replacement_price_from_next_discharge`, which applies the same
  avoided-cost principle to the house battery's terminal SoC.

Because this benefit is priced in the same currency units as `p_imp` and
`p_exp`, the MILP lets charge-past-target EV charging compete fairly
against house battery charging and export.  However, the house battery always
wins the surplus it can absorb (issue #775): the EV's benefit is capped at the
battery's charge credit while the battery has headroom, and a shared
battery-first constraint (`ec[t] + Σ ev_c[t]/η ≤ pv − base_load`) reserves the
battery's share of the surplus.  The EV only absorbs surplus the battery
cannot take. When no future price data is available (`future_value_per_kwh`
is `None`, e.g. missing forecast), the MILP falls back to a tiny fixed
tiebreaker (`0.0001`/kWh AC) so surplus PV still prefers the EV over being
wastefully curtailed/exported at near-zero or negative prices — but only
after the battery has taken its share.

### Grid import power limit (main fuse / tariff protection)

When `main_fuse_amps` is provided and > 0, the MILP adds a **soft**
constraint on total grid import power per slot:

```text
max_grid_import_per_slot_kwh = main_fuse_amps * 230 * phases / 1000 * (interval_minutes / 60)
```

where ``phases`` is the electrical phase count (1 or 3, default 3).
This assumes balanced load at 230 V phase-to-neutral per phase.

The diagnostic soft row is paired with a hard no-worsening row:

```text
gi[t] <= max(max_grid_import_per_slot_kwh, fixed_site_import[t])
```

`fixed_site_import` is the unavoidable house demand **net of forecast PV**,
plus any fixed live EV session:

```text
fixed_site_import[t] = max(base_load[t] - pv_avail[t] + fixed_session_ac[t], 0)
```

Netting PV is required: without it the cap is inflated by the whole PV
forecast on sunny slots and controllable charging can import straight through
the fuse. An existing overload remains feasible and visible through
`gi_pen[t]`, but controllable battery or flexible EV charging cannot worsen it.

**Diagnostics**:
- `total_fuse_violation_kwh` in the returned diagnostics dict.
- `has_violations` set to `True` when any fuse violation exists.
- Each violating slot is logged at WARNING level with slot timestamp,
  required import, limit, and excess kWh.

**When disabled** (`main_fuse_amps` is `None` or 0): no constraint is
added — behaviour is identical to the pre-#567 code.

#### Optional hard per-phase charging protection (EV charger phase topology)

When the main fuse is active **and** EV co-optimisation is running, the MILP
additionally emits `3 × m` hard rows bounding each phase's worst-case
envelope:

```text
gi[t]/3 - ge[t]/3 + Σ_e (σ_e - 1/3) · ev_ac[e][t] <= max_phase_import_per_slot_kwh
```

with

```text
max_phase_import_per_slot_kwh = main_fuse_amps * 230 / 1000 * slot_hours
```

`σ_e` is charger *e*'s **phase share**: the fraction of its AC command any
single phase may be assumed to carry. It is selected per charger via the
config-flow option `hsem_ev_planned_load_charger_phase_topology` (and its
`ev_second` counterpart):

| Topology | `σ_e` | Meaning |
|---|---|---|
| `single_phase` (default) | `1` | Unknown or single-phase charger. Every phase is checked as if it carries the whole EV command. |
| `three_phase_balanced` | `1/3` | Charger confirmed to draw balanced current on L1/L2/L3, so the balanced `gi-ge` split already assigns its full physical share. |

`single_phase` is the default and the fallback for any missing or
unrecognised stored value (`normalize_ev_phase_topology` in
`utils/phase_power.py`), so an entry written before this option existed keeps
the original worst-case envelope.

The phase share is read from one shared helper
(`ev_phase_share` / `EVConfig.phase_share`) by all three hard per-phase sites:

1. the constraint rows above (`planner/milp/_phase_fuse.py`),
2. the EV-fragment concentration pass during write-out
   (`planner/milp/_write_results.py`), which refuses to merge fragments into
   a slot whose phase envelope would exceed the cap,
3. the post-solve validation of the published plan
   (`phase_envelope_from_published_slots`, surfaced as
   `max_phase_import_kwh` in the diagnostics).

A plan the solver accepts is therefore never erased by a validator that
assumed a different topology.

#### Invariants

- When `main_fuse_amps` is `None` or 0, the MILP produces identical
  results to the pre-#567 code (backward compatible).
- When house load is within the fuse limit, `gi_pen[t]` is zero for all
  slots.
- When house load alone exceeds the fuse limit, `gi_pen[t] > 0` absorbs
  the excess — the MILP never becomes infeasible due to fuse constraints.
- When battery + EV + house load would exceed the fuse, the MILP throttles
  controllable charging to stay within the limit.
- A fixed unavoidable overload remains feasible, but no controllable charge
  may increase it.
- With EV phase topology unknown or `single_phase`, the entire EV command is
  limited by the least-free phase; no three-phase multiplier is applied to EV
  headroom.
- With a charger configured as `three_phase_balanced`, only one third of its
  command is charged to any one phase, and a command that fits three-phase
  headroom is never rejected for exceeding single-phase headroom.
- An unrecognised or missing stored topology resolves to `single_phase`; a
  relaxed envelope is never applied by accident.

#### Published charging ceiling and stranded-residue re-portioning (issue #788)

The planner already decides *how much* to charge each EV in every future
slot (`ev_charger_calculated_power` / `ev_second_charger_calculated_power`).
Two diagnostic sensors publish that decision as a **ceiling** an external
current controller can consume:

- `sensor.hsem_ev_charger_current_limit` (primary EV)
- `sensor.hsem_ev_second_charger_current_limit` (second EV)

Each sensor's state is the current slot's ceiling in **whole amps**; a
`schedule` attribute carries up to 24 future slots (`start`, `current_a`,
`power_w`) so the intended profile can be inspected without re-deriving it
from diagnostics. Conversion from watts to amps is done by
`utils/phase_power.py::charger_power_to_current_a()`:

```text
amps = floor(power_w / (230 * phases))
```

`phases` is `PHASE_COUNT` (3) for a charger configured
`three_phase_balanced`, otherwise `1` — the same topology read by the hard
per-phase fuse rows above (`normalize_ev_phase_topology`). Rounding is
**always down**: a partial amp the charger cannot be commanded to draw must
never be published as available headroom. HSEM owns the economics (how many
amps are worth drawing this slot); the external controller keeps final
authority for fuse safety and may only ramp *within* the published ceiling.

**Stranded-residue re-portioning.** The MILP models EV charge as a
continuous variable, so it may allocate a fragment to a slot too small for
the charger to actually run (`< charger_min_power_w`). The existing
EV-fragment concentration pass (`planner/milp/_write_results.py`) folds such
fragments into other allocated slots first. When every candidate recipient
is already at a hard ceiling (fuse-limited or phase-limited), a residue can
still remain unplaced after concentration. Discarding it would silently miss
the EV's deadline target by that amount.

`_redistribute_below_minimum_power()` handles this residue: when it is
**material** (> 0.001 kWh — the publishing-rounding artefact left by
flooring rated power to whole watts) and the EV has a deadline
(`charge_past_target=False`, since past-target charging is opportunistic
surplus-only demand with no target to protect), it opens **one** further
empty, runnable slot before the deadline at the charger minimum and borrows
the shortfall back from slots that can spare it above their own minimum.
Later slots are drained first, so the cheaper early charging the solver
chose is preserved. Total EV energy is unchanged; no commanded slot ends up
below the charger minimum. The number of slots opened (`0` or `1` per EV) is
surfaced in the MILP EV diagnostics as `reportioned_slots`.

##### Invariants

- The published ceiling equals the planned command converted to whole amps,
  always rounded down, using the charger's configured phase topology.
- Zero, negative, and non-finite planned power all publish a `0 A` ceiling —
  never negative headroom.
- A residue at or below the 0.001 kWh publishing artefact never triggers
  re-portioning — a clean plan is never churned for an immaterial amount.
- A material residue is re-portioned into an additional runnable slot rather
  than discarded; total EV energy is preserved (`sum(placed) + deficit ==
  sum(original)`), and every commanded slot stays at or above the charger
  minimum.
- Only deadline-driven charging (`charge_past_target=False`) may open a
  slot; charge-past-target EVs never trigger re-portioning.
- Without an eligible candidate slot (no headroom before the deadline), the
  residue is reported as unplaceable rather than silently dropped.

#### Executable whole-amp plans (issue #789)

Issue #788 floors the *published ceiling* to whole amps for the diagnostic
current-limit sensors, but the underlying planned energy fields
(`ev_planned_load_kwh`, `ev_accounted_load_kwh`, `ev_charger_calculated_power`,
grid import/export, `estimated_cost_currency`) were still the MILP's raw
continuous decision — a value no whole-amp command can actually deliver. A
plan that promises 2.5 kWh of EV charging while the nearest executable
command only delivers 2.3 kWh silently mispriced the plan and understated
grid export/battery headroom by the gap.

`planner/milp/_ev_quantize.py::_quantize_ev_allocation_to_whole_amps()`
closes this gap by quantizing each EV's *flexible* (non-session) allocation
to whole-amp-achievable energy before any other output field is derived from
it — the plan's numbers are therefore always physically executable, not an
idealised continuous target the ceiling sensor floors on display only:

1. **Per-slot floor.** Each occupied slot's DC energy is converted to AC
   power, floored to whole amps (`charger_power_to_current_a`), and clamped
   to the charger's own amp-rounded rating
   (`charger_max_power_to_current_a` on the configured power) — never above
   its nameplate, never above the concentration pass's own settled ceiling
   for that slot (`slot_ceiling_dc`, `dc + room_dc(t)`).
2. **Residue fill.** The fractional energy lost to flooring across all
   occupied slots is pooled and spent as whole additional amp-steps on
   slots that still have headroom under their own ceiling, cheapest
   (smallest step) slots first, **never exceeding the original target**.
3. **One further slot.** If residue remains and no occupied slot has
   headroom, one further empty, deadline-eligible slot may open at the
   charger's activation minimum, borrowing amp-steps back from slots that
   can spare them above that minimum — the same borrow-and-open pattern as
   the stranded-residue re-portioning above, now amp-step granular.
4. **Managed sessions are quantized too.** A live session's *fixed* LP
   energy is snapped to the same whole-amp command that will be published
   (`command_current_a`, floored, zeroed below the activation minimum)
   whenever the session is *managed* (`fixed_session_only=False` — HSEM
   still emits a command for it); the residue this leaves is folded into the
   flexible quantization's target so other slots can recover it. An
   *unmanaged* session (`fixed_session_only=True`) emits no command and
   keeps its measured physical draw verbatim — quantization never runs on it.
5. **Irreducible residue is reported, never invented.** Whatever cannot be
   placed at whole-amp granularity — because it is smaller than one amp-step
   and every slot with headroom is already spent — surfaces as
   `deadline_penalty_kwh` in the MILP EV diagnostics exactly like an
   unreachable deadline target. A strict target can therefore leave
   `deadline_met=False` even when the pre-#789 continuous LP would have
   reported it met; this is the genuine physical granularity of a whole-amp
   command, not a regression.

Grid import/export, `estimated_net_consumption_kwh`, and
`estimated_cost_currency` are all derived from the same quantized `ev_c[t]`
values used for the published power fields (issue #637's single-merged-pass
rule), so a genuine residual PV surplus left behind by flooring is free for
the house battery or export to claim — it is never double-counted as EV
energy.

##### Invariants

- Every commanded (non-session, non-fixed) slot's `ev_charger_calculated_power`
  is an exact whole-amp multiple of the charger's phase voltage.
- The quantized total for one EV never exceeds the pre-quantization target
  for that EV (`sum(quantized) <= target_dc_kwh`).
- An unmanaged session (`fixed_session_only=True`) is never quantized; its
  measured energy is published verbatim with no HSEM command.
- A managed session's fixed energy is quantized to the same whole-amp
  command the ceiling sensor would publish for it.
- Any irreducible sub-amp-step shortfall is surfaced as
  `deadline_penalty_kwh`, never silently discarded and never invented as
  extra energy.

### Grid export power limit (DNO/inverter export cap — issue #726)

When `max_grid_export_power_kw` is provided and > 0, the MILP adds a
**hard** per-slot bound on grid export:

```
ge[t] <= max_grid_export_power_kw * slot_hours
```

- Implemented as a variable bound on `ge[t]`, not a penalty — the cap is
  physically enforced by the inverter/DNO, so exceeding it is never
  required for feasibility.
- Battery export and PV export compete for the same cap through the
  energy-balance equality, so the optimal plan front-loads battery export
  into low-PV slots and tapers it as PV ramps.
- PV that cannot be exported at the cap is absorbed by the free `curt[t]`
  curtailment variable.

**When disabled** (`max_grid_export_power_kw` is `None` or 0): `ge[t]`
remains unbounded above — behaviour is identical to the pre-#726 code.

#### Invariants

- When `max_grid_export_power_kw` is `None` or 0, the MILP produces
  identical results to the pre-#726 code (backward compatible).
- Every slot's `grid_export_kwh` is ≤ `max_grid_export_power_kw ×
  slot_hours` (within solver tolerance) when the cap is active.
- The battery never discharges purely to displace PV export at a saturated
  cap (export-destined discharge gains nothing once `ge[t]` is at its
  bound).

## Cost function

The cost function returns **two distinct aggregates** for every plan
(issue #413):

- `total_cost` — the **money outcome** of the plan within the horizon.
  Pure monetary value.  Auditable; directly comparable to a real electricity bill.
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
+ terminal_soc_value
```

Where:

- `soc_guard_penalty` and `grid_limit_penalty` are **selector-only** synthetic
  terms. They must **never** appear in `total_cost`, because they do not
  represent real money paid or earned.
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

**Battery export floor (``export_min_price``):**  ``export_min_price`` is
a battery-export floor, not a physical grid limit.  The applier no longer
sets the inverter's grid feed-in limit to block export below this price;
surplus PV export is always allowed (issue #767).  The planner enforces
the floor by preventing intentional battery-to-grid discharge on slots
where ``export_price < export_min_price``:

- The MILP caps ``ed[t]`` to ``base_load[t] / discharge_eff`` on blocked
  slots, so the battery can serve house load but cannot export to the grid.
- The non-MILP ``apply_excess_export`` path requires
  ``export_price >= max(export_min_price, recommended_threshold,
  battery_export_min_price)`` before labelling a slot
  ``ForceBatteriesDischarge``.
- The cost function counts PV export revenue at the live export price. It
  zeroes only battery-destined export revenue on slots blocked by
  ``battery_export_min_price``.

**Negative export prices** are a separate case: when ``p_exp < 0``,
exporting costs money, so the applier still writes a physical watt
limit (``GRID_EXPORT_LIMIT_WATT``) to block all grid export, including
surplus PV.  This is the only price regime where the connection point is
physically throttled.

Invariant: ``export_price < export_min_price`` AND ``export_price >= 0``
→ intentional battery-to-grid export is forbidden; PV export is
unaffected and is valued at the live export price.

**Battery export minimum price floor (``battery_export_min_price``, issue
#752):** When ``battery_export_min_price > 0`` and a slot's raw
``export_price`` is strictly below this floor, the MILP forbids
intentional battery-to-grid discharge in that slot (either by capping
``ed[t]`` to ``base_load[t] / discharge_eff``, or by requiring
``export_price >= battery_export_min_price`` before
``apply_excess_export`` labels a slot ``ForceBatteriesDischarge``). To
keep cost-function scores consistent with the optimisation assumptions:

- ``CostWeights.battery_export_min_price`` mirrors the floor in
  ``score_plan``.
- When ``export_price < battery_export_min_price`` AND the slot is a net
  exporter AND PV alone cannot account for the export (i.e. no material PV
  surplus available on the slot), the export-destined portion is treated as
  battery-destined and the export revenue (and discharge-loss
  destination-aware pricing) is zeroed for that slot — that export can
  never be realised by the battery.
- Slots where PV would be exported (``solcast_pv_estimate_kwh > 0``)
  still receive full export revenue.  The floor never restricts PV export.
- Above the floor the optimizer decides freely — reaching the threshold
  does NOT auto-trigger export.

Invariant: ``battery_export_min_price > 0`` AND ``export_price <
battery_export_min_price`` AND the slot's export is battery-destined (no
PV surplus available) → the cost function scores that slot's
export-destined revenue and discharge-loss valuation as 0.

**Effective battery-origin export floor.** The two floors are combined into a
single production value before the mask is built
(`milp/_price_sanitise.py`):

```text
effective_battery_export_floor = max(
    configured_battery_export_min_price,
    recommended_battery_depreciation_threshold,
)
```

`min_export_price` as passed by the engine already carries the depreciation
threshold, so the maximum of the two is the operative floor. Slots whose **raw**
`p_exp` is strictly below it get `ed[t]` capped to `base_load[t] /
discharge_eff`: the battery may still serve house load but cannot intentionally
export. Direct PV export and its revenue are never restricted by this floor
(issue #767).

**Signed-price boundedness:** Finite actionable import and export rates retain
their sign. Negative import prices therefore credit actual bounded consumption,
and export prices above import are not distorted.

Grid import and export have finite physical upper bounds. A binary
`grid_flow_mode[t]` makes their directions mutually exclusive, and curtailment
is bounded by available PV. These constraints remove unbounded wash-flow
directions without changing market prices.

### Battery cycle cost

Cycle cost should count physical battery throughput.

**Single source of truth:** ``resolve_cycle_cost()`` in ``utils/misc.py``.

```text
battery_throughput_kwh = max(battery_charge_stored_kwh, battery_energy_removed_kwh)
cycle_cost_kwh = resolve_cycle_cost(
    purchase_price, usable_kwh, expected_cycles, capacity_loss_pct, user_margin
)
cycle_cost = battery_throughput_kwh * cycle_cost_kwh
```

Formula:

```text
auto = (purchase_price × capacity_loss_pct / 100) / (2 × usable_kwh × expected_cycles)
result = max(auto, user_margin)
```

The ``2×`` factor accounts for one full round-trip (charge + discharge).
``capacity_loss_pct`` accounts for residual value at EOL (LiFePO4 retains ~70 % at EOL,
so ~30 % is lost).

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
contributes to `score` (not to `total_cost`).  It is computed **per slot**
and summed across the horizon, mirroring `milp_optimizer.py`'s `c_obj`
terminal-SoC term exactly (issue #655/#657) so the selector's score always
matches what the LP actually optimised for:

```text
imp_price_obj[t]     = slot.price.import_price   # finite, signed
terminal_premium[t]  = max(0, replacement_price_per_kwh - imp_price_obj[t])
charge_premium[t]    = max(0, replacement_price_per_kwh - imp_price_obj[t]
                             - slot.price.export_price / charge_eff
                             + min(p_exp_deferred[t], slot.price.export_price)
                               / charge_eff)

terminal_soc_value = sum over all slots of:
    batteries_discharged_kwh[t] * terminal_premium[t]
    - batteries_charged_kwh[t] * charge_premium[t]
```

The formula is **asymmetric** (issue #694): the charge credit is reduced
by the export opportunity cost (`p_exp / η_chg`) so that charging never
beats exporting in the same slot, while the discharge penalty uses the
full `terminal_premium[t]`.  The **deferred-export correction** (issue
#592) adds back the spread when a future slot has PV surplus beyond the
battery's absorption capacity — see the MILP objective section above.
Both sides use the shared helper
`cost_helpers.compute_charge_premium()` so the selector's score always
matches what the LP optimised for.

Sign convention (per slot):

- Charging (`batteries_charged_kwh[t] > 0`) contributes a **negative**
  (credit) term, reducing `score`.
- Discharging (`batteries_discharged_kwh[t] > 0`) contributes a **positive**
  (penalty) term, increasing `score`.

The per-slot premium is capped by the differential between
`replacement_price_per_kwh` and that slot's own signed import price.  When
`replacement_price_per_kwh <= imp_price_obj[t]`, the premium is zero for that
slot - charging/discharging then has no terminal-SoC effect, because the LP
saw no genuine opportunity cost either.  This prevents the selector from
over-penalising discharge in cheap-import slots, matching the MILP exactly.

The recommended `replacement_price_per_kwh` is the **minimum future import
price across the planning horizon**.  This represents the marginal cost of
re-purchasing one stored kWh at the cheapest available opportunity - the
economically correct proxy for the opportunity cost of consuming stored energy
now rather than later.  Using the average over all future slots (including
expensive peak prices) systematically over-values stored energy during
high-price periods and biases the selector against discharging.

Finite actionable import and export rates retain their sign in `score_plan`
and in the MILP objective; neither clamps a negative import price to zero. A
finite negative import price therefore produces a negative `import_cost` for
real `grid_import_kwh`. Non-finite or non-actionable prices remain neutral
rather than becoming economic signals. Primary efficiency changes the physical
`grid_import_kwh` and `grid_export_kwh` fields; the LP and scorer add no
separate loss-price term.

Terminal-SoC accounting is **only active** when both `initial_battery_kwh`
and `replacement_price_per_kwh` are supplied to `score_plan`.  Unit tests
that call `score_plan` without horizon context (e.g. simple per-slot
arithmetic checks) do not need the term and may omit both inputs; in that
case `terminal_soc_value = 0.0` and `score == total_cost + penalties`.

### Invariants for tests

- `total_cost` must equal
  `import_cost - export_revenue + cycle_cost + conversion_loss_cost` exactly.
  No synthetic penalty may enter `total_cost`.
- `conversion_loss_cost` is a compatibility field and must remain exactly zero
  because physical losses are already present in grid flows.
- `score` must equal
  `total_cost + soc_penalty + grid_limit_penalty + terminal_soc_value` exactly.
- Recommendation labels such as `batteries_charge_grid` must not incur a
  separate synthetic override cost; their economics are already represented
  by energy flows, losses, cycle wear, and terminal inventory value.
- When all penalties are zero and terminal-SoC is disabled, `score == total_cost`.
- The candidate selector must pick the candidate with the lowest `score`,
  not the lowest `total_cost`.
- `winner.score == output.plan_cost.score` for every planner run.
- `winner.slots == output.slots` for every planner run.
- Given two otherwise-identical plans, the one that ends with more stored
  battery energy must have the lower `terminal_soc_value` and therefore the
  lower `score` (all else equal).
- (issue #752) When `battery_export_min_price > 0` and a slot's raw
  `export_price` is strictly below this floor, the MILP never schedules
  intentional battery-to-grid export on that slot — `grid_export_kwh` may
  be > 0 there only when PV surplus alone would have been exported.
- (issue #752) The non-MILP `apply_excess_export` path never labels a
  slot `ForceBatteriesDischarge` when `export_price <
  battery_export_min_price`.
- (issue #752) With `battery_export_min_price = 0` (default) the
  planner produces identical results to the pre-#752 code (backward
  compatible).

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

### Source cadence detection and raw-value storage

`energi_data_service_update_interval` remains the configured expectation for
price cadence, but population now trusts the **data itself** when possible.
For each supported attribute array, HSEM measures the gap between consecutive
timestamps and uses that detected cadence for matching. This happens
**per attribute**, so one sensor can legitimately publish:

- `prices_today` every 15 minutes, and
- `forecast` every 60 minutes

without requiring per-provider overrides.

The generic ``prices`` attribute accepts ISO-8601 timestamps in either a
``start`` field or the ``start_time`` field returned by Tibber price sources.
Both formats use ``price`` as the rate field and follow the same cadence
auto-detection rules.

If detection fails (for example because fewer than two parseable timestamps are
available), HSEM falls back to the configured interval for prices and to
60 minutes for Solcast PV data.

### How the population pipeline works

1. **Population** (`hourly_data_populator._async_update_hourly_field`):
   Each matched value is written into every planner slot covered by its
   detected source window after normalizing the timestamp to the start of
   that same source interval. There is **no divide-by-share step**.

   - Prices are rates (`currency / kWh`) and are stored **unchanged** on each
     covered `HourlyRecommendation` slot.
   - Solcast entries are hourly energy totals (`kWh`) and are also stored
     **unchanged** on each covered slot.

   This means a 15-minute price point only covers its own quarter-hour slot,
   while an hourly price or Solcast point fans out to all four quarter-hour
   slots inside that hour when `recommendation_interval_minutes = 15`.

2. **Planner input** (`coordinator_builder.build_planner_input`):
   Recommendation slots are deduplicated on `(day_offset, hour)` for
   consumption averages and Solcast PV (genuinely hour-granular), but
   **price points are emitted per slot** with an explicit `slot_in_day`
   field, so quarter-hourly prices survive as distinct `PricePoint`
   entries (192 for a 48 h horizon at 15-minute slots). Stored price values
   are passed through directly to `PricePoint`; there is **no inverse
   multiply** in the coordinator.

3. **Slot population** (`planner.slot_population.populate_prices`):
   When price points carry `slot_in_day`, slots are keyed by
   `(day_offset, slot_in_day)` so each quarter-hourly price lands on its
   own planner slot; points without `slot_in_day` (legacy hourly callers)
   use the existing `align_hourly_prices` fan-out unchanged.

4. **PV slot population** (`planner.slot_population.populate_solcast`):
   Solcast `pv_estimate` remains the full hourly kWh total. When planner
   slots are shorter than one hour, the slot populator computes the per-slot
   fraction from that raw hourly total.

### Invariants for tests

- A 60-min EDS price of `P` must reach the planner as `P` (not `P/4` or `P*4`).
- A 15-min EDS price of `P` must reach the planner as `P`.
- Intermediate per-slot stored values for prices must equal the raw rate `P`.
- Changing `energi_data_service_update_interval` with the same timestamped
  price input must not change the price seen by the planner engine when
  cadence auto-detection succeeds.
- Negative prices must survive the full pipeline unchanged.
- With 15-min price data and 15-min slots, each quarter-hour price must land
  on exactly its own slot — four distinct prices within an hour must produce
  four distinct slot prices (issue #720).
- With 15-min price data, 15-min slots, and a 48 h horizon, the planner must
  receive 192 distinct price points (not 48 collapsed hourly ones) and the
  MILP must see intra-hour price variation (issue #720 stage 2).
- With hourly Solcast data and 15-minute slots, one hourly kWh total must fan
  out to four quarter-hour planner slots whose combined energy equals the raw
  hourly input.

## Candidate plans

Every candidate plan must be fully simulated and scored.

Required production candidates:

- `no_action` diagnostic comparator
- `passive` executable fail-closed fallback
- `milp` active optimisation candidate when a validated solve is available

A validated MILP is the sole active optimisation authority. Passive is eligible
only when no valid MILP exists; `no_action` never becomes executable.

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

All actionable recommendation changes are held within the hold window,
including within-category flips such as ``batteries_charge_solar`` ↔
``ev_smart_charging``.  Only transitions to/from neutral pass through
immediately.

The hold time is configured by ``planner_window_hysteresis_minutes``
(default: 10).  When set to a positive integer, any recommendation
change on the current slot is suppressed unless the previous
recommendation has been active for at least this many minutes.

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
- Any actionable recommendation change within the hold time keeps the
  previous recommendation (including within-category flips such as
  ``ev_smart_charging`` ↔ ``batteries_charge_solar``).
- Transitions to/from neutral are never held.
- Changes after the hold time expires switch to the new recommendation.
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

### EV discharge cap semantics (issue #592, redefined by issue #797)

Huawei exposes **one global battery discharge limit**, shared by the house
battery and every EV.  `EVConfig.force_max_discharge_power` /
`max_discharge_power_w` are a **permission and ceiling**, never a command —
they never create discharge on their own.

When any EV is charging or about to be commanded (`live.ev.is_charging`, a
positive live EV power reading, or a positive planned
`ev_charger_calculated_power`/`ev_second_charger_calculated_power`), the
applier (`applier._planned_ev_discharge_cap_w()` +
`applier_caps._ev_is_active_or_planned()`) gates `maximum_discharging_power`:

- **Any relevant EV lacks permission** (`force_max_discharge_power=False`)
  → the cap is **0 W**.  This replaces the old historical/live-net
  house-only-load heuristic (`compute_ev_discharge_cap_w`, issue #592):
  the battery no longer covers house load while an unpermitted EV charges,
  it simply does not discharge.
- **Every relevant EV has opted in** → the cap is the planner's own solved
  discharge rate for this slot (`rec.batteries_discharged_kwh` averaged
  over the slot duration), clamped to the hardware maximum and to every
  opted-in EV's configured ceiling — never more than what the plan and the
  user's configuration both allow.

**Primary battery hold**: independent of any EV, when the solved plan
scheduled neither charge nor discharge for the primary battery this slot
(`primary_battery_hold` — see below), the cap is unconditionally 0 W.

**SoC guard:** when the battery's remaining usable energy is at or below
the planner's required reserve (`current_required_battery_kwh` — energy
needed until the next solar surplus), the cap is forced to 0 W so the
battery is preserved for its scheduled plans.

### Primary battery hold and held-export authority (issue #797)

`_primary_battery_hold(rec)` (`applier_caps.py`) returns whether the solved
plan explicitly holds the primary battery: a near-zero
(`batteries_charged_kwh`, `batteries_discharged_kwh`) pair, using the same
3-decimal-residue materiality threshold as everywhere else
(`utils.units.is_material_planned_energy_kwh`).  This survives display
relabelling (e.g. `ev_smart_charging`) because relabelling never touches
these energy fields.

A held idle MILP slot can still deliberately export surplus PV.
`_held_planned_export_is_authoritative(rec)` returns `True` only when the
slot is held **and** carries a material `grid_export_kwh` — in that case
the applier keeps the slot in TOU wait (`DEFAULT_HSEM_BATTERIES_WAIT_MODE`)
with `fed_to_grid` excess routing instead of downgrading it to plain
Maximize-Self-Consumption, so the applier never silently consumes energy
the MILP deliberately sold.  A held slot with no material export uses MSC
with the 0 W discharge cap above, so unexpected PV may still charge the
battery.

This applies to both `batteries_wait_mode` (an unheld strict wait stays in
TOU; self-consumption-with-reserve still applies when unheld) and
`ev_smart_charging` (which otherwise always executes as MSC to retain
unexpected solar).

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

The planner supports configurable planning horizons: 12, 24, 36, 48, and 72
hours. All five are offered by the config-flow selector.

The horizon is controlled by `interval_length_hours` in `PlannerInput` (and
`recommendation_interval_length` in `SensorConfig`). The supported 12, 24, 36,
48, and 72-hour values are accepted without special-casing in the engine.

### Slot count

```text
total_slots = (interval_length_hours * 60) // interval_minutes
```

| Horizon | 15-min slots | 60-min slots |
|---|---|---|
| 12 h | 48 | 12 |
| 24 h | 96 | 24 |
| 36 h | 144 | 36 |
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

In addition to the fixed daily decay, the
:class:`~custom_components.hsem.utils.solar_corrector.SolarForecastCorrector`
(introduced in issue #602) applies learned **per-hour accuracy factors** and
an **intra-hour residual correction** to PV estimates before they enter the
planner.  The corrector maintains a 4-day rolling history of (forecast, actual)
ratios per hour-of-day, clamped to [0.3, 1.5].  A configurable confidence
percentile (0.10–0.90, default 0.50) scales the correction — lower values are
more conservative (less PV expected).  The raw Solcast data is never mutated;
corrections are only applied at consumption time.

#### Solar correction invariant

The `SolarForecastCorrector` applies two multiplicative corrections to each
raw PV estimate before it enters the planner:

```text
corrected_pv = raw_pv × hour_factor × residual_factor
```

Where:
- `hour_factor ∈ [0.3, 1.5]` — the per-hour accuracy ratio clamped to prevent
  single-day distortions
- `residual_factor` — intra-hour live-surplus correction with 4-slot linear
  decay over 2 hours

The clamping is symmetric (0.3 lower, 1.5 upper) so the corrector never
amplifies a single outlier beyond these bounds.  Raw Solcast data is never
mutated; both factors are applied only at consumption time.

### Load-forecast readiness

Historical-average states preserve availability provenance: `unknown`,
`unavailable`, unparseable, and non-finite values remain missing instead of
becoming numeric zero. A genuine finite `0.0` remains valid.

Before candidate generation, the coordinator validates every future slot's
weighted, 1-day, 3-day, 7-day, and 14-day load values. Missing provenance,
non-finite values, and negative values fail closed. A complete identically-zero
profile remains valid while finite live house demand is at most 50 W; above
50 W it reports `zero_forecast_with_live_demand`.

When the profile is not ready, automatic mode must not run or reuse an optimized
plan. It publishes a strict current-slot `batteries_wait_mode` with primary
charge/discharge and grid import/export motion cleared. Manual force mode remains
higher authority, and the coordinator retries at the one-minute pending-data
interval.

The accepted-plan load signature contains each future slot's start and all five
finite load values. Recovery or a material correction forces a fresh same-slot
solve. Only successful publication advances the signature baseline and clears
the durable recovery flag.

Registered state-change events advance a monotonic coordinator generation. A
cycle whose captured generation becomes stale during its solve or before
publication is discarded, its accepted-plan state is restored, and a durable
follow-up cycle runs from a fresh snapshot.

### Conditional battery-export reserve

When excess export is enabled and the configured discharge buffer is positive,
the MILP separates aggregate export into explicit battery-origin and direct-PV
components. Material battery export activates a binary mode for that slot;
direct PV export does not.

A forecast PV-surplus run is a maximal contiguous sequence of slots with
materially positive surplus. Every slot in a run `[a, b]` uses the checkpoint
derived from the run's final slot:

```text
checkpoint[t] = checkpoint[b]  for every t in [a, b]
```

The checkpoint is immediately before the next distinct PV-surplus run, or the
horizon end for the final run. Active battery export requires solved primary SoC
at that checkpoint to retain the configured percentage of usable capacity:

```text
SoC[checkpoint[t]] >= buffer_kwh - usable_kwh * (1 - z_export[t])
```

The source split obeys:

```text
battery_export_dc[t] <= battery_discharge_dc[t]
grid_export_ac[t] <= direct_pv_surplus_ac[t]
                     + battery_export_dc[t] * discharge_efficiency
```

Thus no-export mode and reserve constraints suppress battery-origin export while
normal direct PV export remains available. Run grouping changes checkpoint
preprocessing only; it adds no extra rows beyond the existing per-slot reserve
formulation and does not alter house self-consumption, EV demand, export caps,
price floors, or hardware/dynamic SoC floors.

### Battery export forecast reserve (issue #807)

`hsem_batteries_forecast_reserve_pct` (0–50 %, default 0 = disabled) is an
opt-in, absolute-SoC-points reserve above Huawei's hardware end-of-discharge
floor that intentional battery export must retain **immediately after the
exporting slot itself** — unlike the checkpoint reserve above, a later
forecast PV/grid refill can never justify spending it first. Ordinary
household self-consumption may still use the energy when actual demand
exceeds forecast; direct PV export is unaffected.

This is the complete, permanent scope of the port from
`Ambilights/hsem-ambilights#35` for this repository. The upstream PR's
remaining parts — feedback-free phase-limit reconstruction and a 45 s
fail-closed grid-charge transition deadline — are written against a live
per-phase hardware-write-time limiter (`phase_charge_limiter.py`) and PowMr
transition machinery that do not exist here (see `.github/memories.md` §Fork
Divergence, fork PR #35 row; re-verified 2026-08-25, issue #831). They are not
"unfinished" and are not tracked as pending work.

`_forecast_export_reserve_kwh()` (`planner/candidate_generator.py`) converts
the configured percentage into model kWh:

```text
target_soc_pct     = min(hardware_floor_pct + configured_pct, maximum_soc_pct)
effective_floor_pct = clamp(dynamic_floor_pct, hardware_floor_pct, maximum_soc_pct)
reserve_kwh         = rated_kwh * max(target_soc_pct - effective_floor_pct, 0) / 100
reserve_kwh         = min(reserve_kwh, usable_kwh)
```

Only the remaining distance from the *effective* (dynamic-floor-aware) origin
to the configured target is protected, so a dynamic discharge floor already
raised above the hardware floor is never double-counted against this reserve.

The MILP (`planner/milp/_export_reserve.py`) enforces this with one row per
slot, independent of the checkpoint-reserve rows, active whenever
`battery_export_forecast_reserve_kwh > 0`:

```text
SoC[t] >= forecast_reserve_kwh - usable_kwh * (1 - z_export[t])
```

Because the row is indexed by the *same* slot's `z_export[t]`, it only binds
the SoC immediately after a slot in which battery-origin export occurred — a
later slot with no battery export is free to draw the battery down further
for self-consumption. Diagnostics expose
`battery_export_forecast_reserve_active`, `..._kwh`, `..._slots`, and
`..._min_post_export_soc_kwh`.

#### Dynamic discharge floor normalization

`_resolve_effective_discharge_floor_pct()` (`planner/engine_core.py`)
normalizes the hardware floor, the dynamic discharge floor, and the
configured maximum SoC into one finite, bounded triple before any of them
reach `usable_capacity`, `CostWeights`, or candidate selection:

```text
hardware_floor_pct = clamp(battery_end_of_discharge_soc_pct, 0, 100)
maximum_soc_pct     = clamp(battery_max_soc_pct, hardware_floor_pct, 100)
effective_floor_pct = clamp(dynamic_discharge_floor_pct or hardware_floor_pct,
                             hardware_floor_pct, maximum_soc_pct)
```

This closes a latent gap where a stale or oversized dynamic-floor estimate
could produce an effective floor above the battery's own ceiling
(`effective_floor_pct > maximum_soc_pct`), which would make the SoC bounds
fed to `usable_capacity` and the MILP internally inconsistent. For a
well-formed configuration (hardware floor and max SoC already within
`[0, 100]` and consistent with each other) this is behaviour-preserving.

#### Invariants for tests

- `battery_forecast_reserve_pct = 0` (default) never activates the reserve
  mechanism and is fully backward compatible.
- A material battery-export slot's post-export SoC never falls below
  `forecast_reserve_kwh` while the reserve is active.
- The dynamic floor and the forecast reserve never protect the same SoC
  points twice.
- `hardware_floor_pct <= effective_floor_pct <= maximum_soc_pct` always holds,
  even with a stale or out-of-range dynamic-floor estimate.
- A genuine `0` value for `battery_soc_pct`, `battery_end_of_discharge_soc_pct`,
  `excess_export_discharge_buffer_pct`, or `battery_forecast_reserve_pct` must
  survive config plumbing unchanged — it must never be silently replaced by a
  fallback default (`x or default` treats `0.0` as falsy).

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

`DataQuality.load_forecast_ready` is false when consumption provenance or a
future profile value cannot safely support a solve.
`DataQuality.load_forecast_reason` contains the machine-readable cause and is
`None` when ready. Load readiness participates in `DataQuality.is_complete`
alongside price and PV completeness.

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

- A 12-hour horizon produces exactly `(12 * 60) // interval_minutes` slots.
- A 24-hour horizon produces exactly `(24 * 60) // interval_minutes` slots.
- A 36-hour horizon produces exactly `(36 * 60) // interval_minutes` slots.
- A 48-hour horizon produces exactly `(48 * 60) // interval_minutes` slots.
- A 72-hour horizon produces exactly `(72 * 60) // interval_minutes` slots.
- All slots have a non-``None`` recommendation regardless of horizon.
- Day+1 PV estimates are ≤ day+0 estimates for the same hour when both have
  the same raw input (confidence decay applied).
- Day+2 PV estimates are ≤ day+1 estimates for the same raw input.
- On ordinary dates, `DataQuality.horizon_days` equals 1 / 1 / 2 / 2 / 3 for
  12 h / 24 h / 36 h / 48 h / 72 h. A spring-forward physical horizon can touch
  one extra local date.
- Missing day+2 price data surfaces in `day2_price_missing_hours`.
- Missing day+2 PV data surfaces in `day2_pv_missing_hours`.
- `DataQuality.is_complete` is ``False`` when any future-day data is missing.
- PV estimate after solar correction is always within `[0.3 × raw_pv, 1.5 × raw_pv]`
  for each hour (clamping enforced).
- The residual correction decays to ≤0.05× the initial deviation after 4 slots.

### Dynamic discharge floor

The dynamic discharge floor computes a per-cycle minimum SoC that bridges the
gap between the last discharge slot and the next solar refill window:

```text
effective_floor_pct = max(configured_min_soc_pct, bridge_reserve_pct)
bridge_reserve_pct  = (next_refill_need_kwh / usable_capacity_kwh) × 100
                    × safety_margin
```

Where `safety_margin` is a self-learning multiplier that starts at **1.50**
and decays toward **1.05** as successful solar refills are observed.  The
floor is never lower than the hardware-configured minimum SoC.

#### Dynamic floor invariant

```text
effective_floor_pct ≥ configured_min_soc_pct    (always)
effective_floor_pct ≤ 1.50 × bridge_reserve_raw  (after learning period)
```

### Session EV invariant — bounded by control authority (issue #789)

When an active charging session is detected (`session_charge_kw > 0`), the
size of the certainty window the MILP fixes as measured demand depends on
whether HSEM can actually stop that charger
(`planner/milp/_session_window.py::resolve_session_windows`):

- **Unmanaged** (`fixed_session_only=True` — smart planning disabled,
  disconnected, or incompletely configured, so HSEM emits no command): the
  whole bounded **two-hour** forecast window is certain, uncontrollable
  demand — unchanged from the original fix (issue #615). The number of
  slots covered is derived from the configured slot interval:
  `round(2 / slot_hours)`, which yields 8 slots at 15-minute resolution,
  4 slots at 30-minute resolution, and 2 slots at 60-minute resolution.
- **Managed** (HSEM can start/stop this charger through the bridge every
  cycle): only the **already-running remainder of the current slot** is
  certain. Reserving further slots would lock in energy the planner has no
  reason to commit to and cannot cancel. The pinned amount is additionally
  capped at the EV's own remaining target
  (`min(target_kwh, capacity_kwh) − initial_soc_kwh`), so a session that
  already satisfies its target does not force additional certain charging.

```text
Unmanaged, for t = 0 … SESSION_SLOTS-1 (bounded 2-hour window):
    ev_c[t] = min(session_charge_kw × available_hours[t] × charger_efficiency,
                  ev_max_charge_per_slot × duration_scale[t])

Managed, t = 0 only (current slot's remaining executable minutes):
    ev_c[0] = min(session_charge_kw × available_hours[0] × charger_efficiency,
                  ev_max_charge_per_slot × duration_scale[0],
                  remaining_target_dc)
```

`available_hours[t]` is the slot's remaining duration (the full slot for
every future slot; only the current slot can be partial), and
`duration_scale[t] = available_hours[t] / slot_hours`.

These per-EV, per-slot fixed amounts (`session_dc_by_ev`) are exact bounds
(`ev_c[t] == fixed_dc[t]`), not a shared, site-wide time mask: an unmanaged
second charger's certainty window must never make a different, managed
first charger's flexible slots look session-fixed, or its flexible
allocations would be silently converted into measured demand during
writeback. Every hard per-EV site — constraint-row construction
(`_constraints.py`), variable bounds (`_bounds.py`), the aggregate/per-phase
fuse rows (`_constraints.py`, `_phase_fuse.py`), and the write-out/
whole-amp-quantization pass (`_write_results.py`, `_ev_quantize.py`) — reads
`session_dc_by_ev` (or its per-EV `session_slots_by_ev` slot-index view),
never a single shared `session_slots_set`, except where the check is
genuinely site-wide (the battery grid-charge-prevention row, which blocks
grid-charging across the union of every EV's fixed slots).

When `session_charge_kw == 0`, no fixed-session bounds are applied for that
EV. A managed session's fixed energy is itself quantized to a whole-amp
command like any other published EV power (see "Executable whole-amp plans"
above); an unmanaged session emits no command and its measured demand is
published verbatim.

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

11. **Charge past target SoC (MILP only)**: When `allow_charge_past_target_soc`
    is enabled and the EV has reached its target SoC but is below 100 %, the
    EV can receive surplus PV that would otherwise be exported at low/negative
    prices — or, when its avoided-future-import valuation exceeds the export
    price, surplus PV that would otherwise be exported at any price
    (issue #630).  This is handled exclusively by the MILP:

    - The EV is included with `charge_past_target=True`: `target_kwh = capacity_kwh`,
      `deadline_slot = None` (no grid import pressure), a surplus-only constraint
      (`ev_c/eff ≤ pv − base_load`), and a benefit equal to
      `future_value_per_kwh` (avoided cost of importing the same energy
      later, `confidence_factor × mean(import_price)` over the next 24h),
      falling back to a tiny fixed tiebreaker (0.0001/kWh AC) when no future
      price data is available.
    - `future_value_per_kwh` and the per-EV `confidence_factor` are computed
      in `_build_ev_configs_for_milp` (`engine_core.py`) from
      `ev_future_charge_value_per_kwh` (`candidate_selector.py`).
    - The EV planner's Pass 3 has been removed — the MILP is the single
      authority for all EV charging decisions, including charge-past-target.
    - When the MILP fails (scipy unavailable, solver crash), charge-past-target
      is simply unavailable for that cycle.  The next successful MILP solve
      will pick it up.

    The MILP's decisions are authoritative for all EV charging.

12. **EV charger power fields**: `ev_charger_calculated_power` (primary EV)
    and `ev_second_charger_calculated_power` (second EV) are each computed
    **per-EV** from that EV's own charging plan (`EVChargingPlan.charging_slots`)
    by `_compute_ev_charger_power()` (for non-MILP candidates) or directly by
    the MILP's EV power computation (for MILP candidates).

    The per-EV power fields are set **before** candidate selection and
    correctly adjusted by the main-fuse throttling block (per-field loop).

    After candidate selection, a per-EV minimum-power floor check runs:
    each EV's power field is compared against **its own**
    `charger_min_power_w`.  If the power fell below that EV's own minimum
    (due to fuse throttling), only that EV's power field is zeroed, and
    its energy contribution is reverse-engineered from the power value and
    subtracted from the combined slot energy totals.

    **Important**: per-EV power fields MUST NOT be recomputed from the
    combined `ev_planned_load_kwh + ev_accounted_load_kwh` totals, because
    those fields are the sum across both EVs.  Deriving a per-EV power from
    a combined total would corrupt the per-EV output with the sum of both
    EVs' loads.

    The fields are purely planner outputs — the applier must read them to
    throttle the go-e charger; the planner does not control hardware directly.

13. **Executable EV command coherence**: Charger watts, EV energy, grid flow,
    net consumption, estimated cost, and the EV plan sensor must come from the
    same accepted snapshot. The coordinator must not restore an older frozen
    watt command after replanning. Runtime force-charge and negative-price
    overrides must update all related current-slot fields together, respect
    aggregate fuse headroom, and never energize an explicitly disconnected EV.

### Invariants for tests

- When `ev_planned_load_enabled = False`, all `ev_planned_load_kwh == 0.0`.
- When EV is at or above target SoC (`current_soc >= target_soc`),
  all EV load fields are `0.0` (early return `"fully_charged"`).
  Charge-past-target is handled exclusively by the MILP.
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
