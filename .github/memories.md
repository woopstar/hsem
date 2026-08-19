# HSEM Repository Memory

This file captures architecture decisions, coding conventions, and hard-won lessons
for the HSEM (Home Smart Energy Management) project. Read this before making any change.

---

## Architecture — Module Responsibilities

### Planner layer (`custom_components/hsem/planner/`)

| File | Responsibility |
|---|---|
| `engine.py` | Main entry point — orchestrates the full planning pipeline |
| `slot_population.py` | Builds the 48/96/192-slot time horizon from price data |
| `candidate_generator.py` | Generates charge/discharge plan candidates (partial-SoC, MILP, solar) |
| `candidate_selector.py` | Picks the best candidate using time-discounted score; also hosts avoided-cost pricing helpers (`replacement_price_from_next_discharge`, `ev_future_charge_value_per_kwh`) |
| `charge_scheduler.py` | Assigns charge recommendations to slots |
| `discharge_scheduler.py` | Assigns discharge recommendations to slots; `concentrate_discharge_on_expensive_slots` uses **per-calendar-day** budget pools |
| `milp_optimizer.py` | Solves the MILP LP problem — variable vector is 8*n base, growing to 8n + 2n·E + E with EV co-optimisation.  Accepts optional `EVConfig` list for EV integration. |
| `milp/_price_sanitise.py` | Pre-solve price transformations: NaN handling, battery-export floor mask, export-≤-import clamp, negative-import clamp. |
| `milp/_constraints.py` | Builds LP constraint matrices and variable bounds. |
| `milp/_objective.py` | Builds LP objective vector. |
| `milp/_write_results.py` | Translates LP solution back into `PlannedSlot` recommendations and energy flows. |
| `milp/_diagnostics.py` | Computes MILP diagnostics and violation reports. |
| `milp/_export_cap.py` | Resolves DNO/inverter grid-export power cap per slot. |
| `cost_function.py` | Scores a candidate plan — source of truth for cost math |
| `soc_simulation.py` | Simulates battery SoC forward through a slot plan |
| `ev_planner.py` | EV-specific planning logic |

### ML layer (`custom_components/hsem/ml/`)

| File | Responsibility |
|---|---|
| `consumption_predictor.py` | Two-stage ridge: per-(DOW, slot) weighted group means shrunk toward slot-level means, then DOY/temp fitted on the residual |
| `history_reader.py` | Queries HA recorder for energy accumulator and instantaneous sensor history |
| `populator.py` | Bridges ML predictions into `HourlyRecommendation` slots with safety buffer |

### Utils layer (`custom_components/hsem/utils/`)

| File | Responsibility |
|---|---|
| `recommendations.py` | `Recommendations` enum + canonical `DISCHARGE_RECS` and `CHARGE_RECS` frozensets |
| `misc.py` | Shared math helpers: `clamp_efficiency()`, `calculate_recommended_threshold()`, etc. |
| `sensornames.py` | All HA entity name constants — never hardcode sensor names elsewhere |
| `prices.py` | Price lookup, grid fee calculation, spot price helpers |
| `huawei.py` | Huawei Solar inverter API helpers |
| `logger.py` | `HSEM_LOGGER` — rotating file handler, `propagate=False` |
| `solar_corrector.py` | Per-hour PV forecast accuracy auto-correction (issue #602) |
| `dynamic_floor.py` | Dynamic self-learning discharge floor (bridge-to-refill computation) |
| `capacity_learner.py` | Battery usable capacity auto-detection from BMS readings |
| `charge_rate_learner.py` | Temperature-adaptive charge rate learning (7 buckets, p90) |
| `prediction_tracker.py` | Prediction accuracy scorecard (SoC MAE, solar MAPE, action mix) |
| `weekday_profile.py` | Weekday/weekend split house load EWMA profiles |
| `ev_mode_resolver.py` | Auto-Full EV charging on negative electricity prices |

---

## Canonical Patterns — Use These, Never Re-Invent

### Efficiency conversion
```python
# ALWAYS use this — never inline max(min(..., 100.0), 1.0) / 100.0
from custom_components.hsem.utils.misc import clamp_efficiency
charge_eff = clamp_efficiency(charge_efficiency_pct)   # returns fraction 0.01-1.0
```

### Discharge recommendation check
```python
# ALWAYS import from utils/recommendations.py — never redefine locally
from custom_components.hsem.utils.recommendations import DISCHARGE_RECS, CHARGE_RECS
if slot.recommendation in DISCHARGE_RECS:
    ...
```

### Recommended threshold
```python
# ALWAYS use calculate_recommended_threshold() — never use cycle_cost * 0.30 as proxy
from custom_components.hsem.utils.misc import calculate_recommended_threshold
threshold = calculate_recommended_threshold(
    purchase_price=purchase_price,
    cycle_cost_per_kwh=cycle_cost_per_kwh,
    charge_efficiency_pct=charge_efficiency_pct,
    discharge_efficiency_pct=discharge_efficiency_pct,
    capacity_loss_pct=capacity_loss_pct,
    grid_fee=grid_fee,
)
```

### Floating point comparisons
```python
# NEVER use == or != for floats in production code
# Use epsilon guard:
if abs(value) > 1e-9:   # instead of: if value != 0
# In tests always use:
assert result == pytest.approx(expected, rel=1e-6)
```

### Grid fuse limit
```python
# ALWAYS use fuse_max_energy_per_slot_kwh() — never inline amps*230*phases/1000*hours
from custom_components.hsem.utils.units import fuse_max_energy_per_slot_kwh
max_kwh = fuse_max_energy_per_slot_kwh(amps, phases, slot_hours)
```
Used by BOTH the MILP grid-import constraint and the post-hoc EV/battery
throttle so the optimiser and the safety clamp never disagree.

### EV charger DC ↔ AC conversion
```python
# ALWAYS use these — never inline x / charger_efficiency or x * charger_efficiency
from custom_components.hsem.utils.units import ev_dc_to_ac_kwh, ev_ac_to_dc_kwh
ac_load = ev_dc_to_ac_kwh(dc_kwh, eff)   # grid/PV draw for a DC-side delivery
dc_kwh  = ev_ac_to_dc_kwh(ac_kwh, eff)   # energy delivered to the EV battery
```
EV charger efficiency **percentage** → fraction uses the same
`clamp_efficiency()` as battery efficiency (never `max(pct, 1.0) / 100.0` inline).
Note: LP matrix *coefficients* in `planner/milp/_constraints.py` and
`_objective.py` intentionally stay as raw `1.0 / ev.charger_efficiency`
(they are constraint coefficients, not energy conversions) — do not wrap those.

---

## MILP Variable Vector

The MILP in `milp_optimizer.py` uses **9*n** LP variables for battery-only (n = number of
future slots).  When EV co-optimisation is active (one or more `EVConfig` objects passed),
the vector grows by **n·E + E** where E is the number of active EVs.  When the main-fuse
soft constraint is active, a further **n** `gi_pen[t]` variables are appended.

```
Index range      Variable     Meaning
[0 .. n-1]       ec[t]        Energy charged in slot t (kWh)
[n .. 2n-1]      ed[t]        Energy discharged in slot t (kWh)
[2n .. 3n-1]     gi[t]        Grid import in slot t (kWh)
[3n .. 4n-1]     ge[t]        Grid export in slot t (kWh)
[4n .. 5n-1]     pv[t]        PV surplus used in slot t (kWh)
[5n .. 6n-1]     m[t]         max(ec[t], ed[t]) auxiliary variable for cycle cost
[6n .. 7n-1]     s_max_pen[t] Penalty: kWh SoC exceeds usable_kwh
[7n .. 8n-1]     s_min_pen[t] Penalty: kWh SoC drops below 0
[8n .. 9n-1]     curt[t]      PV curtailment in slot t (kWh)
--- EV co-optimisation (when ev_configs is provided) ---
[9n .. 10n-1]    ev0_c[t]     EV0 DC-side charge per slot (kWh)
[10n .. 11n-1]   ev1_c[t]     EV1 DC-side charge per slot (kWh) (if second EV active)
[11n]            ev0_pen      EV0 deadline target slack (kWh shortfall)
[11n+1]          ev1_pen      EV1 deadline target slack (if second EV active)
--- Main-fuse soft constraint (when main_fuse_amps > 0) ---
[... .. +n-1]    gi_pen[t]    Grid-import excess above fuse limit per slot (kWh)
```

Grid export power cap (issue #726): when `max_grid_export_power_kw > 0` the
`ge[t]` upper bound is `max_grid_export_power_kw * slot_hours` (hard bound, no
extra variables); otherwise `ge[t]` is unbounded above.

Cycle cost is counted as `α * m[t]` — **not** `α * (ec[t] + ed[t])`.
The `m[t]` constraints are: `m[t] >= ec[t]` and `m[t] >= ed[t]`.

---

## File Size Rules

- **Hard limit: 30 KB per file** in the planner and utils layers.
- If a file exceeds 30 KB, split it before adding more features.
- Current oversized planner files: `ev_planner.py` (31.8 KB).
- Check before every PR: `wc -c custom_components/hsem/planner/*.py`.

---

## Documentation Style

- Use Mermaid fenced code blocks for architecture and flow diagrams.
- Do not use ASCII/Markdown box diagrams for architecture.
- Use math equations (`$$ ... $$`) for formulas instead of plain text or code-block formulas.

---

## Cycle Cost Formula

**Single source of truth:** ``resolve_cycle_cost()`` in ``utils/misc.py``.

$$
cycle\_cost\_per\_kwh = \frac{purchase\_price \times capacity\_loss\_pct / 100}{2 \times usable\_kwh \times expected\_cycles}
$$

Returns ``max(auto, user_margin)`` where ``user_margin`` is ``battery_cycle_cost_per_kwh``
from the config flow (default 0.0).

The ``2×`` denominator accounts for one full round-trip (charge + discharge = 2 × usable_kwh throughput per cycle).
``capacity_loss_pct`` (configurable via ``hsem_batteries_capacity_loss_pct``, default 30 %) accounts for the
fraction of battery value consumed over its lifetime.  LiFePO4 EOL retention is typically 80 %, so
``capacity_loss_pct`` captures the 20 % lost value plus margin for calendar ageing.

With ``battery_cycle_cost_per_kwh = 0.0`` (the default), the effective cycle cost is purely the
auto-calculated depreciation — no extra margin.  Set a positive value to add additional friction
for even more conservative cycling behaviour.

Do **not** remove or change this factor without updating ``docs/planner-spec.md``.

---

## Export Price Clamping (MILP + Cost Function)

`export_min_price` is no longer a physical grid-export block.  It is a
battery-export floor only: the MILP and discharge scheduler prevent
intentional battery-to-grid export when the export price is below the floor,
but surplus PV export continues unrestricted (issue #767).  To keep the
planner consistent:

- **MILP** (`milp_optimizer.py`): the combined battery-export floor
  (`max(export_min_price, battery_export_min_price)`) is applied only to
  `ed[t]`, capping battery discharge to house load on blocked slots.  PV
  export (`ge[t]`) is left unrestricted for non-negative prices.
- **Cost function** (`cost_function.py`): battery-destined export revenue is
  zeroed on blocked slots; PV export is counted at the live export price.

Negative export prices are physically blocked at the inverter by the
applier (`GRID_EXPORT_LIMIT_WATT`), because exporting then costs money.
The LP's `curt[t]` variable (zero objective cost) naturally handles them:
when `p_exp < 0`, exporting costs money (`−p_exp·ge` becomes a positive cost
in the objective) and the LP prefers curtailment (cost 0) over export
(cost > 0).

The raw `slot.price.export_price` is **not** mutated — clamping only affects
optimisation and scoring.

## Grid Export Power Cap — Applier Enforcement (Issue #770)

`max_grid_export_power_kw` (config step `power`) is a hard cap on grid export.
It is enforced in two places:

- **Planner** (`milp/_export_cap.py`): when > 0, the MILP upper-bounds `ge[t]`
  to `max_grid_export_power_kw * slot_hours` so the plan never schedules more
  export than the DNO/inverter limit.
- **Applier** (`custom_sensors/applier.py::async_apply_inverter_power_control`):
  when export is allowed for non-negative prices, the applier writes the
  configured limit in watts to the inverter (`set_maximum_feed_grid_power`)
  instead of 100 %.  This means "100 % export" becomes "100 % of the
  configured grid export limit", not 100 % of inverter capability.  When the
  cap is `0` or unset, the applier writes unlimited/100 %.

Negative export prices always override the cap and write `GRID_EXPORT_LIMIT_WATT`
to block all export, because exporting then costs money.

## MILP Export-≤-Import Clamp (Issue #635 — Unbounded LP Fix)

In `milp_optimizer.py`, after the `min_export_price` clamp, `p_exp` is
further clamped so it never exceeds `p_imp` for the same slot:

```python
p_exp = np.minimum(p_exp, p_imp)
```

This prevents an unbounded LP (HiGHS status=3) when any slot has
`p_exp > p_imp`.  Without this clamp, the LP can drive both `gi[t]` and
`ge[t]` to infinity (import cheap, export expensive) while the terms
cancel in the energy-balance equality, causing `solve_milp()` to return
`None` for the entire horizon.

- This is applied **after** the `min_export_price` clamp.
- A debug log line reports how many slots were clamped and the max delta.

## MILP Negative-Import-Price Clamp (Issue #655 — Second Unbounded Direction)

After the export-≤-import clamp, a second sanitisation step creates
`p_imp_obj = np.maximum(p_imp, 0.0)` and uses it for **all objective
coefficients** that involve the import price (`gi[t]`, conversion loss on
`ec[t]` and `ed[t]`).  The original `p_imp` is preserved for the
export-≤-import clamp and penalty scaling.

When `p_imp[t] < 0`, the `gi[t]` objective coefficient becomes negative,
incentivising the LP to import infinite energy.  `curt[t]` (zero objective
cost) participates in the energy balance, so the LP can also import-and-
curtail for unbounded profit even without `p_exp > p_imp`.

- `p_imp_obj` ensures the LP never *wants* to import for the sake of a
  negative price alone.
- The terminal-SoC differential (issue #655/#638 regression) also uses
  `p_imp_obj` so negative import prices don't inflate the terminal premium.
- Both clamps together close all known unbounded LP directions.
- These must **never** be silently reverted in future refactors — they are
  solver-stability requirements, not cosmetic conveniences.

## MILP EV Discharge Guard + No-Export Cap (Issue #592)

In `planner/milp/_constraints.py`, two hard per-slot upper bounds on `ed[t]`:

1. **EV discharge guard** — when EV co-optimisation is NOT active and
   `ev_accounted_load_kwh > 0`: `ed[t] ≤ max(base_load − ev_accounted, 0) / η_dis`.
   The formula looks like it double-counts PV (base_load is PV-netted,
   ev_accounted is gross) but it is **exact**: with H = gross house load and
   P = PV, `max(base_load − ev, 0) == max(H − ev − P, 0)` in all cases.
   Do not "fix" it by re-adding PV — that would *under*-block and let the
   battery feed the EV.
2. **No-export cap** — when `no_export=True`: `ed[t] ≤ base_load / η_dis` on
   every slot, so the battery can never export to the grid.

Also in `_build_constraints`, the session-EV AC load is simply
`session_charge_kw × slot_hours` — the DC/AC efficiency conversion cancels
by definition.  Do not re-introduce a multiply-then-divide by
`charger_efficiency`.

## Battery Export Minimum Price Floor (Issues #752 and #767)

A third per-slot ed cap, in the same `_build_constraints` loop:

3. **Battery-export-min-price floor** — the combined floor is
   `max(min_export_price, battery_export_min_price)`, where
   `min_export_price` is passed from the engine as
   `max(export_min_price, recommended_threshold)`.  When the slot's RAW
   export price is strictly below the combined floor
   (`p_exp[t] < effective_floor`, evaluated on the raw p_exp BEFORE the
   export-≤-import clamp), apply `ed[t] ≤ base_load / η_dis` to that slot.
   This is the per-slot, soft-switch companion to the global `no_export`
   cap — it blocks *intentional* battery-to-grid export only on slots below
   the floor, not everywhere.  Above the floor the optimizer is free to
   decide whether exporting is worthwhile; reaching the threshold does NOT
   auto-trigger export.  The non-MILP `apply_excess_export` path enforces
   the same floor via
   `export_price >= max(export_min_price, recommended_threshold, battery_export_min_price)`.

   - The combined mask is computed in
     `planner/milp/_price_sanitise.py::sanitize_prices`
     (`battery_export_blocked`) and passed through `solve_milp` to
     `_build_constraints` via the `battery_export_blocked=` kwarg.
     Wire it end-to-end:
     `const.py` (`hsem_batteries_export_min_price` default 0.0) →
     `flows/batteries_excess_export.py` schema/validator →
     `models/sensor_config.py::batteries_export_min_price` →
     `custom_sensors/config_reader.py` →
     `models/planner_input.py::battery_export_min_price` →
     `coordinator_builder.py::build_planner_input` →
     `planner/candidate_generator.py::generate_candidates` →
     `planner/milp_optimizer.py::solve_milp` (kwarg) →
     `planner/milp/_price_sanitise.py::sanitize_prices` (mask) →
     `planner/milp/_constraints.py::_build_constraints` (mask).
   - The cost function mirrors the `battery_export_min_price` floor via
     `CostWeights.battery_export_min_price`; battery-destined export
     revenue (and discharge-loss export-destined pricing) is zeroed on
     blocked slots so scored costs match the optimisation.  The legacy
     blanket `export_min_price` clamp on all export revenue has been
     removed (issue #767).
   - The applier (`custom_sensors/applier.py::async_apply_inverter_power_control`)
     MUST NOT express a battery-export price floor by writing the grid
     feed-in limit for non-negative export prices.  Doing so blocks surplus
     PV export as well as battery export once the battery is full
     (issue #767).  The applier keeps the grid connection point at
     unlimited/100% for non-negative prices and lets the planner gate the
     battery path.  **Negative export prices are the exception:** when
     exporting costs money, the applier still writes a physical watt limit
     to block all grid export.
   - The guard applies ONLY to intentional battery-to-grid export
     (`force_batteries_discharge`).  It does NOT affect normal battery
     self-consumption, PV export, or PV charging.  With the default
     `0.0` the planner is identical to the pre-#752 code (backward
     compatible) — verify the backward-compat invariant in tests.

## Live-Injection Spike Floor (Issue #592)

In `planner/engine_population.py::_inject_live_data_into_current_slot`, the
unmetered-EV spike cap is `max(3 × forecast, 0.05 kWh)`.  The absolute
0.05 kWh floor is mandatory — without it, a ~0 forecast (night slots)
disables the cap and the full EV spike is injected, re-opening the
battery-into-EV hole.

## EV Label Layer 2 — charge_solar is NOT protected (Issue #592 spec compliance)

In `planner/engine_core.py::run_planner`, the `_EV_KEEP` frozenset must NOT
include `BatteriesChargeSolar`.  Per `docs/planner-spec.md` Layer 2, both
`batteries_charge_solar` and `batteries_wait_mode` are relabelled
`ev_smart_charging` when `ev_total_planned_load_kwh > 0`.  PR #576 added
`BatteriesChargeSolar` to the protected set, silently diverging from the
spec — reverted.  Only `batteries_charge_grid`, `force_batteries_discharge`,
`force_export`, `time_passed`, and `missing_input_entities` are protected.

## Consumption Predictor — Two-Stage Fit (test-driven fix)

`ml/consumption_predictor.py::_fit` uses a two-stage (backfitting) ridge.
A joint ridge over 674 one-hot + continuous features with only a handful of
samples is under-determined: the day-of-year sin/cos columns soak up the
variance and the one-hot (DOW, slot) coefficients collapse to the floor,
destroying the per-slot signal.  Stage 1 fits each (DOW, slot) coefficient
as a time-decay weighted group mean shrunk toward the **slot-level** mean
(same slot across all weekdays) by `alpha`.  Stage 2 fits DOY/temperature/lag
on the residual.  Do not revert to a single joint ridge — it reintroduces
the sparse-data collapse (see `tests/ml/test_consumption_predictor.py`).

## EV Plan Rebuild — Solar/Import Split (MILP path)

`planner/ev_planner.py::rebuild_ev_plan_from_slots` computes the
`solar_surplus_kwh` / `import_needed_kwh` split from the slot's PV surplus
(`max(pv − house, 0)`, capped at the AC load, converted to DC).  It must
NOT be hardcoded to 0 — the MILP co-optimisation path decides EV charging
alongside PV, so PV-surplus attribution is well-defined per slot.

---

## Candidate Deduplication

When generating discharge fraction candidates, deduplicate targets within `0.05 kWh` of each other.
This prevents near-identical plans from polluting the candidate list, especially when `current_kwh` is low.

```python
DUPLICATE_THRESHOLD_KWH = 0.05
filtered = [targets[0]]
for t in sorted(targets)[1:]:
    if t - filtered[-1] >= DUPLICATE_THRESHOLD_KWH:
        filtered.append(t)
```

---

## LP Opportunity-Cost Valuations: Linear Terms in c_obj (issue #638/#655)

Opportunity-cost valuations that represent the marginal value of stored energy
(e.g., terminal-SoC replacement price, EV future-value-per-kWh) **must** be
linear terms in the LP's `c_obj` objective vector so the LP itself optimises
for them.  Computing them as post-hoc adjustments after `linprog()` returns
creates a mismatch between the LP's optimisation target and the selector's
scoring function.

Canonical pattern for terminal-SoC valuation (opportunity-cost differential):

```python
# terminal_soc_value = (Σed - Σec) * replacement_price_per_kwh
# Per-slot incentive is capped by the DIFFERENTIAL between replacement
# price and this slot's import price, so the terminal-SoC term cannot
# override a genuine discharge decision in flat/near-flat price scenarios.
# Uses p_imp_obj (non-negative) to prevent negative prices from inflating
# the terminal premium.
if replacement_price_per_kwh is not None and abs(replacement_price_per_kwh) > 1e-9:
    terminal_premium = max(0.0, replacement_price_per_kwh - p_imp_obj[t])
    c_obj[ec_off + t] -= terminal_premium  # credit (capped)
    c_obj[ed_off + t] += terminal_premium  # penalty (capped)
```

When `replacement_price ≤ p_imp[t]`, the premium is zero — the LP sees no
terminal-SoC incentive and makes discharge decisions purely on per-slot
price signals.  When `replacement_price > p_imp[t]`, the differential
represents the genuine opportunity cost of using energy now vs. later.

This prevents the regression identified in issue #638 where flat-price
scenarios saw zero discharge because the uniform +replacement_price penalty
dominated the per-slot import-saving benefit.

The same principle applies to any valuation that affects the LP's decisions:
if `cost_function.py` includes it in `score`, the MILP's `c_obj` must include
it too.  Post-hoc adjustments are for diagnostics only.

**Both sides now fixed and matching (issue #657):** `cost_function.py`'s
`score_plan()` previously used the OLD flat, uncapped
`(initial_kwh - final_kwh) * replacement_price_per_kwh` formula (a stale
copy of the pre-#638 MILP formula) even after `milp_optimizer.py` was fixed
in PR #656.  It now computes the identical per-slot capped-differential
term shown above, summed across `batteries_charged_kwh[t]` /
`batteries_discharged_kwh[t]`.  `score_plan()` also now clamps import price
via `imp_price_obj = max(imp_price, 0.0)` before pricing `import_cost` and
both conversion-loss terms — mirroring the MILP's `p_imp_obj` clamp — so a
negative-price slot never scores as a synthetic profit that the LP itself
never realises. Always grep both files together when touching any pricing
term; a mismatch here is easy to introduce silently and hard to notice
without side-by-side numeric verification using varying (non-flat) prices.

---

## EV Charge-Past-Target Valuation (issue #630)

When `allow_charge_past_target_soc` is enabled and the EV has reached its
target SoC but is below 100 %, surplus PV diverted to the EV is priced at
`EVConfig.future_value_per_kwh` — the avoided cost of importing that same
energy later:

```
future_value_per_kwh = confidence_factor * mean(import_price[t] for t in next 24h of slots)
```

Computed by `ev_future_charge_value_per_kwh()` in `planner/candidate_selector.py`
(mirrors `replacement_price_from_next_discharge()`, which applies the same
avoided-cost principle to the house battery's terminal SoC), and wired into
`EVConfig` per-EV in `_build_ev_configs_for_milp()` (`planner/engine_core.py`).
`confidence_factor` defaults to 0.9 and is configurable per EV via
`hsem_ev_past_target_confidence_factor` / `hsem_ev_second_past_target_confidence_factor`.
When no future price data is available, the MILP falls back to a tiny fixed
tiebreaker (0.0001/kWh AC) in `milp_optimizer.py`. Never hardcode a
replacement constant here — always source it from `ev_future_charge_value_per_kwh()`.

## EV Pre-Deadline Target Cap (Issue #636 — Overcharge Fix)

In `milp_optimizer.py`, the EV deadline benefit coefficient (`-ev_penalty_cost`)
is applied to **every** kWh of `ev_c[t]` before the deadline.  Without an
additional constraint, the LP sees a net positive benefit for charging all the
way to `capacity_kwh` — not just to `target_kwh`.

To fix this, a **hard upper-bound row** is added per EV (only when
`deadline_slot` is set, `target_kwh > initial_soc_kwh + 1e-9`, and
`charge_past_target` is **not** enabled):

```python
# Σ_{k≤D} ev_c[k] ≤ target_kwh - initial_soc_kwh
shortfall = ev.target_kwh - ev.initial_soc_kwh
for k in range(d + 1):
    A_ub[ev_row, ev_off + k] = 1.0
b_ub[ev_row] = shortfall
```

- This is **separate** from the existing `ev_soc_rows` capacity constraint
  (which caps at `capacity_kwh - initial_soc_kwh` — the physical ceiling).
- This must **never** apply to `charge_past_target=True` EVs — that mode
  intentionally allows charging beyond `target_kwh` via its own surplus-only
  mechanism.
- The row count variable `ev_target_rows` (1 per eligible EV) is included
  in `ev_total_rows` alongside `ev_soc_rows`, `ev_deadline_rows`,
  `ev_post_deadline_rows`, and `ev_surplus_rows`.

## EV Pre-Deadline Benefit / Charge-Past-Target Mutual Exclusivity (Issue #643)

In `milp_optimizer.py`, the pre-deadline benefit block and the
charge-past-target benefit block both add benefit coefficients to the same
`ev_c[t]` LP variable.  Today `engine_core.py`'s `_build_ev_configs_for_milp()`
sets `EVConfig` fields such that they are never both true in practice, but
`milp_optimizer.py` itself does not enforce this.  If any future caller or test
constructs an `EVConfig` where both are true, the two benefit blocks would
stack, double-counting the reward on the same variable.

**Invariant**: The LP construction must guard the pre-deadline benefit block
with `and not ev.charge_past_target`, mirroring the existing
post-deadline zero-charge and target-cap constraint guards.  This makes the
LP itself robust regardless of what any caller does.

- The guard is a one-line addition to the outer condition of the pre-deadline
  benefit block (search for `ev_penalty_cost = max(p_imp_max, 0.1) *
  max(energy_needed, 1.0) * 10.0`).
- This follows the exact same pattern already used by the post-deadline
  zero-charge constraint and the target-cap constraint in the same file.

---

## Discharge Concentration — Per-Day Budget Pools

`concentrate_discharge_on_expensive_slots` groups discharge slots by calendar day
and gives each day its own independent `usable_kwh` budget. Do NOT revert to
a single global pool — the battery is recharged by solar between discharge
windows on different days, so day N+1 must not compete with day N.

```python
by_day: dict[date, list[PlannedSlot]] = defaultdict(list)
for s in discharge_slots:
    by_day[as_tz(s.start, now.tzinfo).date()].append(s)
```

---

## Huawei Solar Entity Wiring

When adding a new sensor/entity from the inverter:
1. `const.py` — add config key constant
2. `flows/huawei_solar.py` — add to config flow step
3. `translations/en.json` — add to both `config` and `options` `huawei_solar` steps
4. `models/sensor_config.py` — add field
5. `custom_sensors/config_reader.py` — read from config entry
6. `custom_sensors/state_collector.py` — collect live HA state
7. `models/live_state.py` — add to live state model
8. `coordinator.py` — wire into coordinator

Never hardcode entity IDs — always use `sensornames.py` constants.
Always check `docs/huawei_entities.md` before looking elsewhere.

---

## Testing Rules

- Every bug fix requires a regression test.
- Every planner math change requires a unit test verifying the cost identity:
  `winner.total_cost == final_output.total_cost`
- Run `pytest tests/` before every PR.
- Run `./scripts/quality.sh lint` then `./scripts/quality.sh quality` before every commit.
- Use `pytest.approx()` for all float comparisons in tests.

---

## Logging

- Use `HSEM_LOGGER` from `utils/logger.py` for all planner output.
- Never use `logging.getLogger(__name__)` directly in planner files.
- `HSEM_LOGGER.propagate = False` keeps output out of `home-assistant.log`.
- Log to `hsem.log` (10 MB × 5 files rotating) in HA config dir.
- **Never call `HSEM_LOGGER.debug()`/`.info()`/`.warning()` directly from pure-Python
  planner/utils modules that can run synchronously inside the coordinator's async
  update cycle** (e.g. `planner/*.py`, `utils/solar_corrector.py`, `utils/dynamic_floor.py`,
  `utils/capacity_learner.py`, `utils/charge_rate_learner.py`). The `RotatingFileHandler`
  performs blocking `open()`/`write()` calls that trigger Home Assistant's
  "Detected blocking call to open" warning when invoked from the event loop.
  Always use `log_planner(level, msg, *args)` instead — it offloads file I/O to a
  thread-pool executor when a running event loop is detected, falling back to a
  direct call only when no loop is present (tests, early init). See issue #632.

---

## MILP Energy Flow Source-of-Truth Rule (issue #637)

`solve_milp()` writes **every** per-slot energy flow field
(`batteries_charged_kwh`, `batteries_discharged_kwh`, `grid_import_kwh`,
`grid_export_kwh`) from a **single merged write-out pass** that first
resolves degenerate LP vertices (simultaneous charge+discharge) and then
derives grid import/export from the slot's energy balance equation using
the **same resolved** ec/ed values.  There is no second pass that reads
raw LP arrays independently.

- `simulate_soc()` accepts an optional `milp_prepopulated=True` parameter.
  When ``True``, it preserves the LP's pre-populated energy flow values
  verbatim — it does **not** re-derive discharge from the recommendation
  label and `net_demand`.
- The candidate selector passes ``milp_prepopulated=True`` for MILP
  candidates (detected by ``candidate.name == CANDIDATE_MILP``).
- Non-MILP candidates (no_action, passive, etc.) use the default
  ``milp_prepopulated=False`` and continue with the existing greedy
  derivation — their behaviour is unchanged.

**Do NOT** add any post-processing step that silently overwrites the
LP-derived per-slot energy flows (``batteries_discharged_kwh``,
``grid_import_kwh``, ``grid_export_kwh``) on MILP-sourced slots.
If a step needs to adjust these values, it must be part of the LP
formulation itself, not a downstream patch.

### Degenerate-Vertex Consistency Rule (issue #659, #662)

When the LP write-out path resolves a degenerate/ambiguous solution (the
mutex / "Bug J" simultaneous charge+discharge resolution), **every**
downstream field derived from that slot's energy flow — charge, discharge,
grid import, grid export — must come from the **same resolved decision**
and must be written in the same loop iteration.

**Never** re-read from raw, pre-resolution LP arrays (``ed_sol``,
``gi_sol``, ``ge_sol``) in a separate pass.  The raw LP values were
computed under the original (possibly now-invalid) ec/ed combination and
will not satisfy the energy balance once ec/ed are adjusted.

**Headroom-based net preservation (issue #662)**: When collapsing a
degenerate vertex, validate the net residual (ec - ed) against the
**actual resolved SoC headroom** at that slot in chronological order.
Maintain a running resolved SoC (``running_soc``) initialised to
``current_kwh`` and updated by ``resolved_charge - resolved_discharge``
after every slot.  For a net-charge candidate (``net > 0``), clamp to
``min(net, usable_kwh - running_soc)``; for a net-discharge candidate
(``net < 0``), clamp to ``min(-net, running_soc)``.  If the clamped value
is <= ``_MIN_ACTION_KWH``, zero both ec and ed — the vertex is solver
noise with no meaningful headroom.

**Warning**: The ``net_charge_profit`` expression
(``p_imp * (eta_dis - 1/eta_chg) - 2*cycle_cost``) is structurally
**always** <= 0 for any realistic ``discharge_eff <= 1 <= 1/charge_eff``
and ``cycle_cost_per_kwh >= 0``.  It must **never** be used as a
discriminating signal for degenerate-vertex resolution — it cannot
distinguish a genuine economic signal from solver noise.  Likewise,
the LP's ``s_max_pen[t]`` / ``s_min_pen[t]`` penalty variables are a
**per-slot, hard-bound-violation** signal, not a horizon-wide degeneracy
signal — they miss degenerate vertices where SoC is merely *near*
(not at) a bound, and must not be used for this resolution.

---

## Phase-Aware Power Formulas (issue #640)

Any future power/current-based formula in the planner that converts amps
to kWh/slot **must** be phase-aware.  The hardcoded ``3.0`` multiplier was
replaced by ``main_fuse_phases`` (1 or 3) in:

- ``milp_optimizer.py`` — ``solve_milp()`` fuse constraint formula
- ``engine_core.py`` — post-hoc main fuse violation check

The canonical formula is:

```text
max_kwh = amps * 230 * phases / 1000 * (interval_minutes / 60)
```

Do **not** reintroduce a hardcoded ``3.0`` in new formulas that involve
phase-dependent power calculations.

---

## Per-EV Output Fields Must Come From Per-EV Data (issue #646/#655)

**Canonical rule**: per-EV output fields (`ev_charger_calculated_power`,
`ev_second_charger_calculated_power`) **must always** be derived from
per-EV data — never recompute a per-EV field from a combined/summed-across-
all-EVs total.

The slot energy fields (`ev_planned_load_kwh`, `ev_accounted_load_kwh`,
`ev_total_planned_load_kwh`) are the **sum** across both EVs (accumulated
via `combined_ev_inj` / `combined_ev_raw`).  Using any of these to derive
a per-EV power value would write the combined total into a single EV's
field, corrupting the other EV's allocation.

Each EV's power must be computed from that EV's own `EVChargingPlan`
(`_compute_ev_charger_power()`) or directly by the MILP's per-EV power
computation.  The post-candidate minimum-power floor check must use
each EV's own `charger_min_power_w` — never `max(min1, min2)`.

## EV Field Routing By Identity, Not List Position (issue #655)

In `milp_optimizer.py`'s EV power write-out loop and in
`engine_core.py`'s session-charge-power assignment, EV identity is
**always** determined by `EVConfig.is_second` — **never** by list
position (`ev_idx == 0`) in the `active_evs` list.

When the primary EV is disabled, `active_evs[0]` **is** the second EV.
Position-based routing (`ev_idx == 0 → ev_charger_calculated_power`) would
incorrectly write the second EV's power into the primary field.

- `EVConfig.is_second: bool` — set by `_build_ev_configs_for_milp()`
  based on which `PlannerInput` fields the config came from
  (primary vs. `ev_second_*`).
- MILP write-out: `if ev.is_second: write to ev_second_charger_calculated_power`
- Session power: iterates `configs` and routes by `cfg.is_second`

---

## MILP Solar Export Priority (issue #694)

The MILP objective function naturally prioritises exporting solar surplus
during expensive hours over charging the battery, because:

- Export revenue: ``-p_exp[t]`` — negative coefficient = profit, large
  when prices are high.
- Battery charge cost: ``charge_loss × p_imp_obj[t]`` — small positive
  cost, proportional to the import price (conversion loss).

The LP is a global optimiser — it sees all future slots and will defer
battery charging to cheap slots when future solar is sufficient.

**When the LP may charge during expensive hours:** only when future cheap
slots lack enough solar surplus to fill the battery before it is needed
(e.g., a discharge window starts before cheap solar arrives).  In that
case the LP correctly prioritises meeting the discharge commitment over
export revenue.

**Regression tests** in ``tests/planner/test_milp_optimizer.py``:

- ``test_milp_exports_solar_in_expensive_slots_charges_in_cheap`` —
  basic 4-slot setup from the acceptance criteria.
- ``test_milp_exports_solar_when_future_cheap_solar_sufficient`` —
  battery starts partially full, replacement price active.
- ``test_milp_charges_early_only_when_future_solar_insufficient`` —
  verifies the LP only charges early when necessary.
- ``test_milp_solar_export_with_house_load_and_replacement_price`` —
  realistic scenario with house load and terminal-SoC incentive.

---

## EV Power Entity Unit Normalisation (issue #592)

HSEM expects every EV charger power entity in **Watts**.  Users frequently
point HSEM at template sensors that emit kW (e.g. ``3.6`` instead of
``3600``), which silently disabled the EV discharge guard (the 3.6 kW
session looked like 3.6 W).

``state_collector._read_ev_power_w()`` is the single read path for both
chargers' power entities:

- ``unit_of_measurement='kW'`` → value × 1000.
- value > 100 kW while charging → implausible, treated as unreadable.
- 0 < value < 50 W while charging (no kW unit) → suspiciously low; the
  value is kept but a warning is logged telling the user to fix the
  template or set ``unit_of_measurement='kW'``.

Never read ``cfg.ev.power_entity`` / ``cfg.ev_second.power_entity`` through
the generic ``_read(..., "float")`` path — always use ``_read_ev_power_w``.

---

## Live Injection Must Preserve Sub-Window Averages (issue #592)

``engine_population._inject_live_data_into_current_slot`` overwrites the
current slot's ``avg_house_consumption_kwh`` with the live reading, but
must **not** touch ``avg_house_consumption_1d/3d/7d/14d_kwh``.  The EV
discharge-cap fallback in ``applier.async_apply_battery_settings`` picks
the *minimum* of those windows to recover a clean house baseline; the
live-injected value can still be inflated by unmeasured EV load, so
overwriting the sub-windows destroys the fallback and lets polluted v5
history inflate the hardware discharge cap.

---

## Deferred-Export Charge Premium (issue #592)

The #694 charge-credit cap (``repl − p_imp − p_exp/η_chg``) only compares
charging against exporting in the SAME slot.  When a future slot has PV
surplus exceeding ``min(usable_kwh, max_charge_per_slot)``, that surplus is
exported regardless — so the true refill price is the future (cheaper)
export price, and early charging at a high export price is correct.

Canonical implementation — never re-implement the formula inline:

- ``cost_helpers.compute_charge_premium(...)`` — the capped premium.
- ``cost_helpers.deferred_export_price_by_slot(...)`` — per-slot deferred
  export price (min export price across later unabsorbable-surplus slots).

Both the MILP (``milp/_objective.py``) and the selector
(``cost_function.py``) MUST use these helpers so LP decisions and
selector scores never diverge.  The correction activates only when the
caller supplies ``usable_kwh`` and ``max_charge_per_slot`` (MILP: new
``_build_objective`` kwargs; selector: ``CostWeights`` fields
``battery_usable_capacity_kwh`` / ``max_charge_per_slot_kwh``, populated
in ``engine_core.run_planner`` and ``candidate_selector``).

Regression tests: ``test_milp_defers_charging_to_cheap_slots_when_future_pv_exceeds_headroom``
and ``test_milp_charges_now_when_no_future_surplus_exceeds_headroom`` in
``tests/planner/test_milp_optimizer.py``.

---

## EV Discharge Cap Must Not Feed Back Into the Planner (issue #592, beta7)

The applier's EV discharge cap writes a small value (e.g. 321 W) to the
``maximum_discharging_power`` number entity.  Reading that entity back as
``battery_max_discharge_power_w`` in ``coordinator_builder.build_planner_input``
created a feedback loop: the entire planning horizon was limited to the EV
cap (``discharge=0.073 kWh`` per 15-min slot) and the battery could never
cover evening house load.

Canonical rule: **planner inputs must reflect physical capability, not
commanded entity state.**  Use
``coordinator_builder._resolve_max_discharge_power_w(live)`` — it always
derives the max from the rated capacity via
``get_max_discharge_power()``; the live entity read-back is only a
degraded fallback when the rated capacity is unknown.  (Beta8 gated the
substitution on ``any_ev_charging``, but a single EV-status flicker let
the still-capped entity poison an off-schedule run — the substitution is
now unconditional.)

``get_max_discharge_power()`` covers single- and two-stack S0/S1
capacities explicitly (5000–30000 Wh); unknown capacities log a warning
and fall back to 2500 W rather than failing silently (issue #723).

## EV Discharge Cap Is the Historical Baseline — Live Never Moves It (issue #592)

``applier.compute_ev_discharge_cap_w()`` is the single place that computes
the cap.  Two opposite failures proved the live reading must not move the
cap at all when history exists:

- **beta8 ratchet:** ``min(live, history)`` let CT-clamp/EV-sensor drift
  pull the cap 363→40 W over one night (the battery's own capped
  discharge shrinks the CT reading further — a self-poisoning input).
- **v6.2.0-beta1 swings:** ``max(history, min(live, 3×history))`` let
  ordinary house noise (cooking, heat pump) swing the cap 652→1968→928 W
  for 5+ hours, draining the battery before the 06:00 scheduled plan.

Rules:

- history available → cap = historical baseline, live is ignored
- no EV power sensor → minimum positive sub-window average
- no history → trust live

**SoC guard (v6.2.0-beta1):** after computing the cap, if
``battery_current_capacity_kwh <= current_required_battery_kwh`` (the
planner's reserve until the next solar surplus), the cap is forced to
0 W — the battery is preserved for its schedule and the house load is
served from the grid until the battery recovers.

Regression tests: ``tests/test_coordinator_builder.py``,
``tests/sensors/test_applier.py::TestComputeEvDischargeCapW``.

---

## Consumption Blend Peer-Median Clamp (issue #592, beta2)

The 1d/3d/7d/14d consumption blend has THREE layers, applied in order in
``slot_population.weighted_avg_consumption``:

1. ``clamp_window_to_peer_median`` — each window clamped to at most
   ``max(3 × median of the other three, 0.15 kWh/h)``.  Upward-only: a
   genuine consumption drop flows through immediately.  Catches the
   stale-14d pollution pattern (three windows clean after a behavioural
   change, one long window still holding pre-change nights) that the IQR
   mask misses — with 4 points the median lands between the clusters and
   flags BOTH ends, zeroing the clean windows too.
2. ``detect_outliers_iqr`` weight redistribution (issue #301).
3. 7d/14d mutual caps + reliability scaling.

Never clamp the downward side — a real drop in house consumption must not
be inflated back up.

Regression tests: ``tests/sensors/test_hourly_data_populator.py::TestPeerMedianClamp``.

---

## File Organization — By Responsibility, Not By Theme
## Price/Solcast Slot Matching — Floor to Source Interval, Not Hour (issue #720)

``hourly_data_populator/prices_solcast.py`` matches sensor data points to
recommendation slots by timestamp.  The original code truncated both sides
with ``.replace(minute=0, second=0)``, which collapsed all four 15-min
price points of an hour onto one key — the last write won, and all
quarter-hour slots showed the same (hourly) price even when EDS published
96 distinct prices per day.

Canonical rule: **floor data points to the start of their enclosing
*source* window** (``electricity_price_update_interval`` for prices, 60 min
for Solcast) via ``normalize_slot_start(dt, source_interval_minutes)``,
then match a slot when its start lies inside that source window
(``dt_key <= obj_start < dt_key + source_window``).  This preserves both
fan-out directions:

- 15-min prices + 15-min slots → each point lands on exactly one slot
- 60-min prices + 15-min slots → the hourly point covers all four slots

Regression tests: ``tests/test_15min_price_matching.py``.

**Stage 2 — planner input collapse (same issue):** even with population
fixed, ``coordinator_builder.build_planner_input`` deduplicated on
``(day_offset, hour)`` and appended price points *inside* that guard, so
only the first quarter of each hour survived (192 slots → 48 hourly
points), and ``populate_prices`` fanned the survivor back across the hour
via ``align_hourly_prices``.  Fix: ``PricePoint`` carries an optional
``slot_in_day``; price points are emitted per slot (consumption averages
and Solcast PV stay hour-deduplicated); ``populate_prices`` keys by
``(day_offset, slot_in_day)`` with an hourly fallback when present.
Hour-granular callers (``slot_in_day=None``) are unaffected.

Regression tests: ``tests/test_quarter_hourly_planner_input.py``.

---

## Avg Sensor Must Not Store Partial-Day Samples (issue #720 follow-up)

``HSEMAvgSensor._async_store_utility_meter_value`` samples the daily
utility meter every 5 minutes.  The meter resets at ``hour_start`` and
accumulates energy only during the ``hour_start`` → ``hour_end`` block
(the power sensor reports ``unknown`` outside that window).  The original
code stored every sample under the current date, so a mid-day reading
recorded a **partial** day as if it were complete.  For a new energy
sensor with limited history, partial days fill the rolling window and
inflate the forecast (reporter: 14.267 kWh sampled at 05:45 → ~4.7 kWh/h
forecast for a ~260 W house).

Canonical rule: **only persist the day's sample once the hour block is
complete** — ``now.hour >= hour_end`` for normal blocks; for the
overnight 23→00 block (``hour_end == 0``) any hour except 23 counts, and
post-midnight samples are attributed to the previous date.

Regression tests: ``tests/test_avg_sensor_partial_day.py``.

---

## Solar-Charge Mislabel at Zero PV (issue #720 follow-up)

``apply_optimization_strategy`` used ``NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH``
(0.1 kWh) to decide whether an unassigned summer slot should charge from
solar.  A slot with a small positive house load (e.g. 0.08 kWh) and zero
PV would pass the ``<= 0.1`` check and get ``BatteriesChargeSolar`` even
though there was no PV surplus at all.  The result was a grid-charging
slot masquerading as solar charging, which:

- Confused the ``hourly_recommendations`` output
- Caused the applier to write ``MaximizeSelfConsumption`` instead of
  ``TimeOfUse`` + charge TOU
- Made the plan look more fragmented than it actually was

**Canonical rule:** ``BatteriesChargeSolar`` is only assigned when there
is a genuine PV surplus (``estimated_net_consumption_kwh < 0``).  A small
positive house load with zero PV must not be treated as a solar-charging
opportunity.

Regression tests: ``tests/planner/test_zero_pv_solar_charge_mislabel.py``.

---

## Nordpool Price Format — raw_today/raw_tomorrow (issue #750)

``custom-components/nordpool`` publishes ``raw_today`` / ``raw_tomorrow``
attributes as ``{"start": datetime, "end": datetime, "value": price}``.
HSEM's price populator (``custom_sensors/hourly_data_populator/prices_solcast.py``)
mapped those attributes as ``{"k": "hour", "v": "price"}``, so every entry
was silently skipped and all planner slots got ``import_price = 0.0``.

**Canonical rule:** the ``data_sources`` mapping for ``raw_today`` /
``raw_tomorrow`` accepts both the legacy ``hour``/``price`` format and
the nordpool ``start``/``value`` format.  The mapping is a list of
fallbacks — add new formats as additional ``{"k": ..., "v": ...}``
entries rather than replacing existing ones.

Regression tests: ``tests/test_15min_price_matching.py``
(``TestNordpoolRawFormat``).

**Observability rule:** when a configured price sensor yields zero matched
data points, the populator logs a ``warning`` naming the sensor.  A
``debug`` message is logged for Solcast sensors (PV forecast is optional).
This makes format mismatches visible instead of silently planning with
``import_price = 0.0`` (issue #750).

---

## File Organization — By Responsibility, Not By Theme

AI agents naturally bucket related things together (e.g. "all planner inputs in one file").
This is an anti-pattern.  **Organize files by responsibility — one file does one thing.**

What this means per layer:

- **`models/`**: One dataclass per file.  Exception: tightly-coupled nested types that are
  never imported independently (e.g. `EVChargerConfig` lives in `sensor_config.py` because it
  only exists as a field of `SensorConfig`).
- **`planner/`**: One algorithm/strategy per file (already the case).
- **`utils/`**: One problem domain per file — a group of closely related functions
  (already the case with `prices.py`, `misc.py`, etc.).
- **`custom_sensors/`**: One sensor/coordinator per file.

Do **not** create files like `planner_inputs.py` (6 unrelated dataclasses) or
`planner_outputs.py` (7 unrelated dataclasses).  Each dataclass is its own responsibility.

**Why**: Smaller, focused files give AI agents exactly the context they need.
Thematic bucketing loads irrelevant code into every prompt, reducing precision
and causing edit collisions between unrelated classes.

## EV Charger Power Must Be Slot-Stable (issue #738)

`ev_charger_calculated_power` and `ev_second_charger_calculated_power` are HSEM's
*command* to the EV charger. They must remain **constant for the entire current
15-minute slot** once computed at slot start.

The EV planner recomputes these fields whenever the planner reruns, and the
recomputation uses live-injected PV and house-consumption data for the current
slot. Without freezing, a Go-E or similar charger that uses the field as a
power setpoint sees the target jump whenever:

- a cloud changes the live PV reading,
- the charger itself toggles on/off (changing `is_charging`), or
- any other replan trigger fires inside the slot.

The coordinator freezes the slot-start values in
`_freeze_ev_charger_power_for_current_slot` and restores them to the current
slot on every replan. Explicit overrides (force-charge-now, auto-full-EV on
negative price) are applied **after** the freeze, so they can still change the
current slot while active; when they end, the frozen value is restored.

This is a runtime stability rule, not a planner algorithm change. The planner
still sees live data for battery/SoC decisions; only the per-EV charger power
command is held constant.

---

## Wait Mode Self-Consumption with Reserve (issue #742)

`batteries_wait_mode` can now optionally allow normal household self-consumption
instead of keeping the battery strictly idle.

- Config key: `hsem_batteries_wait_mode_behavior`
- Values: `"strict"` (default) or `"self_consumption_with_reserve"`
- When set to `"self_consumption_with_reserve"`, the applier
  (`custom_components/hsem/custom_sensors/applier.py`) switches the inverter to
  `MaximizeSelfConsumption` and caps the discharge power so only surplus energy
  above the planner's required reserve (`current_required_battery_kwh`) can be
  used.  Once the battery reaches the reserve, the applier falls back to strict
  TOU wait mode.
- PV surplus during wait-mode self-consumption is directed to charge the battery
  (`desired_excess = "charge"`), not exported to grid.
- The cap is computed from the surplus energy and the slot duration so the
  reserve is preserved even if the house load is high.
- EV-active slots keep their existing EV discharge cap logic; the wait-mode cap
  is not applied while an EV is charging.

Files involved: `flows/batteries_wait_mode.py`, `config_flow.py`,
`options_flow.py`, `translations/en.json`, `const.py`,
`models/sensor_config.py`, `custom_sensors/config_reader.py`,
`custom_sensors/applier.py`.
