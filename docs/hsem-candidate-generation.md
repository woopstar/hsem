# HSEM Candidate Generation — Deep Dive

This document explains how candidate plans are generated, how assumptions drive
the candidate set, and the mathematical models behind each strategy.

---

## Table of Contents

1. [Why multiple candidates?](#why-multiple-candidates)
2. [Candidate list](#candidate-list)
3. [Assumptions behind each candidate](#assumptions-behind-each-candidate)
4. [Partial-SoC candidates (BatPred-inspired)](#partial-soc-candidates)
5. [MILP global optimisation](#milp-global-optimisation)
6. [Candidate deduplication](#candidate-deduplication)
7. [Hysteresis](#hysteresis)

---

## Why multiple candidates?

Battery scheduling is a sequential decision problem under uncertainty (prices,
PV, and load are forecasts, not certainties). A single heuristic strategy may
miss the global optimum under certain market conditions. By evaluating multiple
independent strategies and picking the cheapest valid one, the planner:

- Captures more of the available arbitrage value
- Degrades gracefully when forecasts are wrong
- Provides explainable alternative plans for debugging

---

## Candidate list

The generator (`planner/candidate_generator.py`) produces these candidates:

| # | Name | Strategy |
|---|---|---|
| 1 | `baseline` | Current HSEM scheduling output (discharge → charge → excess export → optimisation) |
| 2 | `no_action` | Battery completely idle — no forced charge or discharge |
| 3 | `passive` | Solar charging only where PV surplus exists; no grid charge or forced discharge |
| 4 | `grid_charge` | Grid-charge slots kept; solar charging removed |
| 5 | `solar_only` | Only solar-charge slots kept; grid charging cleared |
| 6 | `discharge_only` | Discharge slots kept; all charge slots cleared |
| 7 | `aggressive` | Cheapest N slots forced to grid-charge; most expensive M slots forced to discharge |
| 8 | `milp` | Globally-optimal LP solution (when scipy is available) |
| 9–14 | `soc_plan_25/50/75/100/125/full` | Partial-SoC candidates charging different fractions of discharge-window need |

---

## Assumptions behind each candidate

### `no_action` (diagnostic floor)

**Assumption:** The inverter can be left in its default self-consumption mode
(no external scheduling). PV surplus is exported; battery only moves according
to its native operating logic.

**Purpose:** Provides a baseline cost that all other candidates must beat.
If no candidate beats `no_action`, the planner falls back to doing nothing.

**Mathematical model:**
- All recommendations cleared to `None`
- No grid charge, no forced discharge, no force export
- SoC simulation still runs: PV charges battery if native logic would do so
- Terminal SoC is still accounted for

### `passive` (inverter default)

**Assumption:** The inverter's default behaviour is solar-following — it charges
from PV surplus when available and stays idle otherwise. No grid price arbitrage.

**Purpose:** Models what happens if HSEM only sets PV-charge recommendations
without any grid-based scheduling.

**Mathematical model:**
- Solar surplus slots (where `estimated_net_consumption < 0`) get `batteries_charge_solar`
- All other charge/discharge/export recommendations cleared
- Battery fills from PV, never from grid
- Battery never discharges unless native inverter logic does so

### `grid_charge`

**Assumption:** Buying grid energy at a low price, storing it, and using it
during a high-price period is profitable after accounting for round-trip losses,
cycle cost, and the minimum price spread.

**Purpose:** Tests whether the price spread available in the horizon justifies
the round-trip cost of grid charging.

**Mathematical model:**
- Grid-charge slots kept from the scheduler's output
- Solar charging cleared
- Discharge slots kept (to recover the stored energy)
- Effectively: "charge from grid when cheap, discharge when expensive,
  ignore solar charging"

### `solar_only`

**Assumption:** Solar energy is always cheaper than any grid import, and the
battery should prioritise storing PV surplus over grid charging.

**Purpose:** Tests whether PV surplus alone can satisfy the discharge-window
demand without incurring grid-import costs.

**Mathematical model:**
- Solar-charge slots kept
- Grid charging cleared
- Discharge slots kept when needed
- Effectively: "store every kWh of PV surplus, use it during expensive slots"

### `discharge_only`

**Assumption:** The battery has enough stored energy from previous cycles to
cover the discharge demand without any additional charging within the horizon.

**Purpose:** Tests whether the existing SoC is sufficient to ride through
the horizon without recharging.

**Mathematical model:**
- Discharge slots kept
- All charge slots cleared
- Battery only discharges, never charges
- Useful when SoC is high and horizon is short

### `aggressive`

**Assumption:** The most extreme price spread in the horizon should be fully
exploited by charging at the cheapest N slots and discharging at the most
expensive M slots, ignoring schedules.

**Purpose:** Upper-bound test — what is the maximum arbitrage value if we
ignore schedule constraints?

**Mathematical model:**
- N dynamically computed from battery headroom: `N = ceil(usable_headroom_kwh / max_charge_per_slot)`
- M dynamically computed from usable energy: `M = ceil(current_kwh_above_floor / max_discharge_per_slot)`
- N cheapest import slots set to `batteries_charge_grid`
- M most expensive import slots set to `batteries_discharge_mode`
- Scales with horizon length and battery capacity (fix for issue #416 Bug 2)

---

## Partial-SoC candidates

Introduced as a BatPred-inspired improvement (issue #445 fix), partial-SoC candidates
charge different fractions of the energy needed for upcoming discharge windows.
This lets the selector find the optimal charge level — charging exactly what's
needed, not filling to 100 % every time.

### Charge fractions

| Candidate | Fraction | Meaning |
|---|---|---|
| `soc_plan_25` | 0.25 | Charge 25 % of the discharge-window need |
| `soc_plan_50` | 0.50 | Charge 50 % |
| `soc_plan_75` | 0.75 | Charge 75 % |
| `soc_plan_100` | 1.00 | Charge exactly what's needed |
| `soc_plan_125` | 1.25 | Charge 125 % (safety margin) |
| `soc_plan_full` | 2.00 | Fill to maximum usable capacity |

### Energy needed calculation

```python
energy_needed = sum(
    discharge_energy for each discharge window in the horizon
) - current_kwh_above_floor

charge_target = energy_needed * fraction
```

### When partial-SoC helps

Partial charging avoids the **cycle-cost overhead** of filling the battery
completely when the discharge demand is small. Consider:

- Discharge window needs 3 kWh
- Battery is at 50 % of 10 kWh usable (5 kWh above floor)
- Full charge (soc_plan_full) would charge 5 more kWh, incurring 5 kWh × cycle_cost
- soc_plan_100 charges exactly 0 kWh (already have enough)
- soc_plan_25 / 50 / 75 may be suboptimal here because existing SoC already
  exceeds the need — the candidates converge to the same plan

When SoC is low and discharge need is large, partial-SoC candidates let the
selector find the precise charge level that minimises the sum of import cost
and cycle cost.

### Assumptions

- Discharge-window energy can be predicted from schedule configuration
- Cycle cost is proportional to throughput
- The optimal charge level is somewhere between 25 % and 100 % of the
  discharge-window need (not necessarily full)

---

## MILP global optimisation

The MILP solver (`planner/milp_optimizer.py`) uses scipy's HiGHS to find the
globally optimal charge/discharge schedule.

### Variable vector

6 × n variables for n slots:

| Index range | Variable | Meaning |
|---|---|---|
| `[0..n-1]` | `ec[t]` | Energy charged in slot t (kWh) |
| `[n..2n-1]` | `ed[t]` | Energy discharged in slot t (kWh) |
| `[2n..3n-1]` | `gi[t]` | Grid import in slot t (kWh) |
| `[3n..4n-1]` | `ge[t]` | Grid export in slot t (kWh) |
| `[4n..5n-1]` | `pv[t]` | PV surplus used in slot t (kWh) |
| `[5n..6n-1]` | `m[t]` | `max(ec[t], ed[t])` auxiliary for cycle cost |

### Objective

$$ \text{minimise} \sum_t [p_{\text{imp}}[t] \cdot gi[t] - p_{\text{exp}}[t] \cdot ge[t] + \alpha \cdot m[t] + \gamma \cdot (ed[t] - ec[t])] $$

Where:
- $\alpha$ = battery cycle cost per kWh
- $\gamma$ = terminal-SoC replacement price
- `m[t] ≥ ec[t]` and `m[t] ≥ ed[t]` (max constraint)

### Constraints

For each slot t:

1. **SoC forward recurrence**: $soc[t] = soc[t-1] + ec[t] - ed[t]$
2. **SoC bounds**: $0 \leq soc[t] \leq usable_{kwh}$
3. **Charge limit**: $ec[t] \leq max\_charge\_per\_slot$
4. **Discharge limit**: $ed[t] \leq max\_discharge\_per\_slot$
5. **Mutual exclusion**: $ec[t] / max\_charge + ed[t] / max\_discharge \leq 1$
6. **Energy balance**: $gi[t] + pv[t] + ed[t] \cdot \eta_{dis} = load[t] + ec[t] / \eta_{chg} + ge[t]$
7. **Non-negativity**: All variables ≥ 0

### Assumptions

- **Linear relaxation**: Binary charge/discharge flags are relaxed to continuous
  because the mutual-exclusion constraint and per-slot caps already prevent
  simultaneous charge+discharge in the optimal solution.
- **Deterministic inputs**: All forecasts (prices, PV, load) are treated as
  known with certainty (no stochastic programming).
- **Cycle cost proxy**: The `m[t] = max(ec[t], ed[t])` formulation counts
  the larger of charge or discharge per slot, matching the 2× denominator
  in the cycle cost formula.

### Fallback

If `scipy` is unavailable or the solver fails (infeasible / numerical issue),
the MILP candidate is silently dropped and the rule-based candidates compete
as normal.

---

## Candidate deduplication

When generating partial-SoC candidates, targets within 0.05 kWh of each other
are deduplicated to prevent near-identical plans from polluting the candidate list:

```python
DUPLICATE_THRESHOLD_KWH = 0.05
filtered = [targets[0]]
for t in sorted(targets)[1:]:
    if t - filtered[-1] >= DUPLICATE_THRESHOLD_KWH:
        filtered.append(t)
```

This is especially important when `current_kwh` is low and all partial-SoC
fractions collapse to the same charge target.

---

## Hysteresis

### Plan-level hysteresis (issue #372)

Prevents the planner from switching strategies for tiny cost improvements.
When active:

1. The previously active plan is re-evaluated with current data
2. If its score improvement over the best new candidate is below both thresholds,
   the previous plan is kept

| Threshold | Default | Behaviour |
|---|---|---|
| Absolute | 0.0 currency | New plan must be cheaper by at least this amount |
| Percentage | 5.0 % | New plan must be cheaper by at least this % of previous score |

### Window-level hysteresis (issue #315)

Prevents rapid charge↔discharge toggles near schedule-window boundaries.

- **Charge-type**: `batteries_charge_grid`, `batteries_charge_solar`, `ev_smart_charging`
- **Discharge-type**: `batteries_discharge_mode`, `force_batteries_discharge`, `force_export`
- **Neutral**: `batteries_wait_mode`, `time_passed`, `missing_input_entities`, `None`

Only cross-category transitions are held. The hold time is configured by
`planner_window_hysteresis_minutes` (default: 0, disabled).
