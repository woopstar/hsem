# HSEM Repository Memory

This file captures architecture decisions, coding conventions, and hard-won lessons
for the HSEM (Home Smart Energy Management) project. Read this before making any change.

---

## RTK CLI Rule (Mandatory)

- **Always use `rtk` as a prefix for shell commands** (e.g. `rtk git status`, `rtk pytest`, `rtk grep`).
- **Always use `rtk grep` / `rtk rg` for code search** instead of native `grep`/`rg` — output is compacted and grouped by file.
- Use `rtk gain` to check token savings; `rtk proxy <cmd>` runs raw without filtering.

---

## Architecture — Module Responsibilities

### Coordinator layer (`custom_components/hsem/`)

| File                      | Responsibility                                                   |
| ------------------------- | ---------------------------------------------------------------- |
| `coordinator.py`          | HA lifecycle and collect/populate/plan/publication orchestration |
| `coordinator_data.py`     | Atomic `CoordinatorData` snapshot exposed to entities            |
| `coordinator_helpers.py`  | Pure override, strict-hold, and load-readiness/signature helpers |
| `coordinator_tracking.py` | Forecast, daily, financial, and savings accumulation             |

Load-average availability must remain explicit: unknown/non-finite values are
missing, genuine finite zero is valid, and contradictory zero load above 50 W
live demand fails closed. Registered state events received during an in-flight
cycle are durable; stale generations must not publish.

### Planner layer (`custom_components/hsem/planner/`)

| File                      | Responsibility                                                                                                                                                            |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `engine.py`               | Main entry point — orchestrates the full planning pipeline                                                                                                                |
| `slot_population.py`      | Builds the 48/96/192-slot time horizon from price data                                                                                                                    |
| `candidate_generator.py`  | Generates charge/discharge plan candidates (partial-SoC, MILP, solar)                                                                                                     |
| `candidate_selector.py`   | Picks the best candidate using time-discounted score; also hosts avoided-cost pricing helpers (`replacement_price_from_next_discharge`, `ev_future_charge_value_per_kwh`) |
| `charge_scheduler.py`     | Assigns charge recommendations to slots                                                                                                                                   |
| `discharge_scheduler.py`  | Assigns discharge recommendations to slots; `concentrate_discharge_on_expensive_slots` uses **per-calendar-day** budget pools                                             |
| `milp_optimizer.py`       | Solves the MILP LP problem — variable vector is 8\*n base, growing to 8n + 2n·E + E with EV co-optimisation. Accepts optional `EVConfig` list for EV integration.         |
| `milp/_price_sanitise.py` | Pre-solve price transformations: NaN handling, battery-export floor mask, export-≤-import clamp, negative-import clamp.                                                   |
| `milp/_constraints.py`    | Builds LP constraint matrices and variable bounds.                                                                                                                        |
| `milp/_objective.py`      | Builds LP objective vector.                                                                                                                                               |
| `milp/_write_results.py`  | Translates LP solution back into `PlannedSlot` recommendations and energy flows.                                                                                          |
| `milp/_diagnostics.py`    | Computes MILP diagnostics and violation reports.                                                                                                                          |
| `milp/_export_cap.py`     | Resolves DNO/inverter grid-export power cap per slot.                                                                                                                     |
| `cost_function.py`        | Scores a candidate plan — source of truth for cost math                                                                                                                   |
| `soc_simulation.py`       | Simulates battery SoC forward through a slot plan                                                                                                                         |
| `ev_planner.py`           | EV-specific planning logic                                                                                                                                                |

### ML layer (`custom_components/hsem/ml/`)

| File                       | Responsibility                                                                                                             |
| -------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| `consumption_predictor.py` | Two-stage ridge: per-(DOW, slot) weighted group means shrunk toward slot-level means, then DOY/temp fitted on the residual |
| `history_reader.py`        | Queries HA recorder for energy accumulator and instantaneous sensor history                                                |
| `populator.py`             | Bridges ML predictions into `HourlyRecommendation` slots with safety buffer                                                |

### Utils layer (`custom_components/hsem/utils/`)

| File                    | Responsibility                                                                       |
| ----------------------- | ------------------------------------------------------------------------------------ |
| `recommendations.py`    | `Recommendations` enum + canonical `DISCHARGE_RECS` and `CHARGE_RECS` frozensets     |
| `misc.py`               | Shared math helpers: `clamp_efficiency()`, `calculate_recommended_threshold()`, etc. |
| `sensornames.py`        | All HA entity name constants — never hardcode sensor names elsewhere                 |
| `prices.py`             | Price lookup, grid fee calculation, spot price helpers                               |
| `huawei.py`             | Huawei Solar inverter API helpers                                                    |
| `logger.py`             | `HSEM_LOGGER` — rotating file handler, `propagate=False`                             |
| `solar_corrector.py`    | Per-hour PV forecast accuracy auto-correction (issue #602)                           |
| `dynamic_floor.py`      | Dynamic self-learning discharge floor (bridge-to-refill computation)                 |
| `capacity_learner.py`   | Battery usable capacity auto-detection from BMS readings                             |
| `prediction_tracker.py` | Prediction accuracy scorecard (SoC MAE, solar MAPE, action mix)                      |
| `weekday_profile.py`    | Weekday/weekend split house load EWMA profiles                                       |
| `ev_mode_resolver.py`   | Auto-Full EV charging on negative electricity prices                                 |

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
Note: LP matrix _coefficients_ in `planner/milp/_constraints.py` and
`_objective.py` intentionally stay as raw `1.0 / ev.charger_efficiency`
(they are constraint coefficients, not energy conversions) — do not wrap those.

---

## MILP Variable Vector

The MILP in `milp_optimizer.py` uses **9\*n** LP variables for battery-only (n = number of
future slots). When EV co-optimisation is active (one or more `EVConfig` objects passed),
the vector grows by **n·E + E** where E is the number of active EVs. When the main-fuse
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

- **Hard limit: 30 KB AND 1000 lines per file** across the entire codebase.
  Both limits must be satisfied — a file under 30 KB but over 1000 lines still
  needs splitting. These limits keep files small enough for efficient AI
  development (context window, diff size, review latency).
- If a file exceeds either limit, split it before adding more features.
- Current oversized files (as of 2026-08-21):
  - `coordinator.py` — 116 KB, 2555 lines (needs splitting)
  - `applier.py` — 44 KB, 1083 lines (needs splitting)
  - `ev_planner.py` — 32 KB, 795 lines (over 30 KB)
  - `config_flow.py` — 31 KB, 846 lines (over 30 KB)
  - `forecast_tracker.py` — 31 KB, 841 lines (over 30 KB)
  - `state_collector.py` — 31 KB, 837 lines (over 30 KB)
- Check before every PR:
  ```bash
  # Lines
  find custom_components/hsem -name '*.py' -exec sh -c 'l=$(wc -l < "$1"); [ "$l" -gt 1000 ] && echo "$l $1"' _ {} \;
  # Size
  find custom_components/hsem -name '*.py' -exec sh -c 's=$(wc -c < "$1"); [ "$s" -gt 30720 ] && echo "$s $1"' _ {} \;
  ```

---

## Documentation Style

- Use Mermaid fenced code blocks for architecture and flow diagrams.
- Do not use ASCII/Markdown box diagrams for architecture.
- Use math equations (`$$ ... $$`) for formulas instead of plain text or code-block formulas.

---

## Cost Score Formula

The selector score contains no fixed-schedule/override term:

$$
score = total\_cost + soc\_penalty + grid\_limit\_penalty + terminal\_soc\_value
$$

Recommendation labels are not independent economic costs. Their effects are
already represented by energy flows, conversion losses, cycle wear, and terminal
inventory valuation.

The former seven-bucket charge-rate learner and number entities are retired. They
had no wired battery-temperature input and therefore never produced a functional
temperature-dependent planner limit. Use the live Huawei maximum-charge-power
entity as the planner's physical charge limit.

## Cycle Cost Formula

**Single source of truth:** `resolve_cycle_cost()` in `utils/misc.py`.

$$
cycle\_cost\_per\_kwh = \frac{purchase\_price \times capacity\_loss\_pct / 100}{2 \times usable\_kwh \times expected\_cycles}
$$

Returns `max(auto, user_margin)` where `user_margin` is `battery_cycle_cost_per_kwh`
from the config flow (default 0.0).

The `2×` denominator accounts for one full round-trip (charge + discharge = 2 × usable_kwh throughput per cycle).
`capacity_loss_pct` (configurable via `hsem_batteries_capacity_loss_pct`, default 30 %) accounts for the
fraction of battery value consumed over its lifetime. LiFePO4 EOL retention is typically 80 %, so
`capacity_loss_pct` captures the 20 % lost value plus margin for calendar ageing.

With `battery_cycle_cost_per_kwh = 0.0` (the default), the effective cycle cost is purely the
auto-calculated depreciation — no extra margin. Set a positive value to add additional friction
for even more conservative cycling behaviour.

Do **not** remove or change this factor without updating `docs/planner-spec.md`.

---

## Export Price Clamping (MILP + Cost Function)

`export_min_price` is no longer a physical grid-export block. It is a
battery-export floor only: the MILP and discharge scheduler prevent
intentional battery-to-grid export when the export price is below the floor,
but surplus PV export continues unrestricted (issue #767). To keep the
planner consistent:

- **MILP** (`milp_optimizer.py`): the combined battery-export floor
  (`max(export_min_price, battery_export_min_price)`) is applied only to
  `ed[t]`, capping battery discharge to house load on blocked slots. PV
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
  instead of 100 %. This means "100 % export" becomes "100 % of the
  configured grid export limit", not 100 % of inverter capability. When the
  cap is `0` or unset, the applier writes unlimited/100 %.

Negative export prices always override the cap and write `GRID_EXPORT_LIMIT_WATT`
to block all export, because exporting then costs money.

## MILP Grid Flow Direction Exclusivity (Issues #635 / #655 — Unbounded LP Fix)

The historical `np.minimum(p_exp, p_imp)` and `np.maximum(p_imp, 0.0)`
price-sanitisation clamps (issues #635 and #655) have been **removed**.
Unbounded wash-flow directions are now closed structurally in the MILP
by a binary `grid_flow_mode[t]` variable that makes import and export
directionally mutually exclusive per slot. See `docs/planner-spec.md`
(§"Signed-price boundedness") for the full description.

- `planner/milp/_price_sanitise.py::sanitize_prices()` now returns finite
  signed market prices unchanged (NaN→0 only); no `min`/`max` clamps.
- Finite negative import prices correctly credit consumption in the
  objective. Export prices above import no longer need clamping because
  `grid_flow_mode[t]` prevents simultaneous import+export.
- This is the authoritative mechanism — do **not** re-introduce the old
  `np.minimum(p_exp, p_imp)` / `np.maximum(p_imp, 0.0)` clamps.

## MILP EV Discharge Guard + No-Export Cap (Issue #592)

In `planner/milp/_constraints.py`, two hard per-slot upper bounds on `ed[t]`:

1. **EV discharge guard** — when EV co-optimisation is NOT active and
   `ev_accounted_load_kwh > 0`: `ed[t] ≤ max(base_load − ev_accounted, 0) / η_dis`.
   The formula looks like it double-counts PV (base*load is PV-netted,
   ev_accounted is gross) but it is **exact**: with H = gross house load and
   P = PV, `max(base_load − ev, 0) == max(H − ev − P, 0)` in all cases.
   Do not "fix" it by re-adding PV — that would \_under*-block and let the
   battery feed the EV.
2. **No-export cap** — when `no_export=True`: `ed[t] ≤ base_load / η_dis` on
   every slot, so the battery can never export to the grid.

Also in `_build_constraints`, the session-EV AC load is simply
`session_charge_kw × slot_hours` — the DC/AC efficiency conversion cancels
by definition. Do not re-introduce a multiply-then-divide by
`charger_efficiency`.

## Grouped Battery-Export Reserve Checkpoints

The MILP uses explicit battery-origin export and direct-PV export source fields.
When excess export and its discharge buffer are enabled, material battery export
activates a binary reserve condition. All slots in one contiguous forecast
PV-surplus run share the checkpoint derived from the run's final slot—the end of
the following demand window, immediately before the next distinct surplus run,
or horizon end. This prevents adjacent surplus slots from bypassing the reserve
by moving battery export to the run's final slot. Direct PV export remains
unrestricted by the battery reserve.

## Battery Export Minimum Price Floor (Issues #752 and #767)

A third per-slot ed cap, in the same `_build_constraints` loop:

3. **Battery-export-min-price floor** — the combined floor is
   `max(min_export_price, battery_export_min_price)`, where
   `min_export_price` is passed from the engine as
   `max(export_min_price, recommended_threshold)`. When the slot's RAW
   export price is strictly below the combined floor
   (`p_exp[t] < effective_floor`, evaluated on the raw p*exp BEFORE the
   export-≤-import clamp), apply `ed[t] ≤ base_load / η_dis` to that slot.
   This is the per-slot, soft-switch companion to the global `no_export`
   cap — it blocks \_intentional* battery-to-grid export only on slots below
   the floor, not everywhere. Above the floor the optimizer is free to
   decide whether exporting is worthwhile; reaching the threshold does NOT
   auto-trigger export. The non-MILP `apply_excess_export` path enforces
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
     blocked slots so scored costs match the optimisation. The legacy
     blanket `export_min_price` clamp on all export revenue has been
     removed (issue #767).
   - The applier (`custom_sensors/applier.py::async_apply_inverter_power_control`)
     MUST NOT express a battery-export price floor by writing the grid
     feed-in limit for non-negative export prices. Doing so blocks surplus
     PV export as well as battery export once the battery is full
     (issue #767). The applier keeps the grid connection point at
     unlimited/100% for non-negative prices and lets the planner gate the
     battery path. **Negative export prices are the exception:** when
     exporting costs money, the applier still writes a physical watt limit
     to block all grid export.
   - The guard applies ONLY to intentional battery-to-grid export
     (`force_batteries_discharge`). It does NOT affect normal battery
     self-consumption, PV export, or PV charging. With the default
     `0.0` the planner is identical to the pre-#752 code (backward
     compatible) — verify the backward-compat invariant in tests.

## Live-Injection Spike Floor (Issue #592)

In `planner/engine_population.py::_inject_live_data_into_current_slot`, the
unmetered-EV spike cap is `max(3 × forecast, 0.05 kWh)`. The absolute
0.05 kWh floor is mandatory — without it, a ~0 forecast (night slots)
disables the cap and the full EV spike is injected, re-opening the
battery-into-EV hole.

## EV Label Layer 2 — charge_solar is NOT protected (Issue #592 spec compliance)

In `planner/engine_core.py::run_planner`, the `_EV_KEEP` frozenset must NOT
include `BatteriesChargeSolar`. Per `docs/planner-spec.md` Layer 2, both
`batteries_charge_solar` and `batteries_wait_mode` are relabelled
`ev_smart_charging` when `ev_total_planned_load_kwh > 0`. PR #576 added
`BatteriesChargeSolar` to the protected set, silently diverging from the
spec — reverted. Only `batteries_charge_grid`, `force_batteries_discharge`,
`force_export`, `time_passed`, and `missing_input_entities` are protected.

## Consumption Predictor — Two-Stage Fit (test-driven fix)

`ml/consumption_predictor.py::_fit` uses a two-stage (backfitting) ridge.
A joint ridge over 674 one-hot + continuous features with only a handful of
samples is under-determined: the day-of-year sin/cos columns soak up the
variance and the one-hot (DOW, slot) coefficients collapse to the floor,
destroying the per-slot signal. Stage 1 fits each (DOW, slot) coefficient
as a time-decay weighted group mean shrunk toward the **slot-level** mean
(same slot across all weekdays) by `alpha`. Stage 2 fits DOY/temperature/lag
on the residual. Do not revert to a single joint ridge — it reintroduces
the sparse-data collapse (see `tests/ml/test_consumption_predictor.py`).

## EV Plan Rebuild — Solar/Import Split (MILP path)

`planner/ev_planner.py::rebuild_ev_plan_from_slots` computes the
`solar_surplus_kwh` / `import_needed_kwh` split from the slot's PV surplus
(`max(pv − house, 0)`, capped at the AC load, converted to DC). It must
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
for them. Computing them as post-hoc adjustments after `linprog()` returns
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
price signals. When `replacement_price > p_imp[t]`, the differential
represents the genuine opportunity cost of using energy now vs. later.

This prevents the regression identified in issue #638 where flat-price
scenarios saw zero discharge because the uniform +replacement_price penalty
dominated the per-slot import-saving benefit.

The same principle applies to any valuation that affects the LP's decisions:
if `cost_function.py` includes it in `score`, the MILP's `c_obj` must include
it too. Post-hoc adjustments are for diagnostics only.

**Both sides now fixed and matching (issue #657):** `cost_function.py`'s
`score_plan()` previously used the OLD flat, uncapped
`(initial_kwh - final_kwh) * replacement_price_per_kwh` formula (a stale
copy of the pre-#638 MILP formula) even after `milp_optimizer.py` was fixed
in PR #656. It now computes the identical per-slot capped-differential
term shown above, summed across `batteries_charged_kwh[t]` /
`batteries_discharged_kwh[t]`. `score_plan()` also clamps import price via
`imp_price_obj = max(imp_price, 0.0)` before pricing `import_cost`, mirroring
the MILP's `p_imp_obj` clamp. Conversion efficiency is priced once through
physical grid flows; compatibility loss fields remain zero. Always grep both files together when touching any pricing
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

## EV Battery-First Surplus Priority (issue #775)

When the EV is at/above its target SoC (charge-past-target mode), surplus PV
must fill the **house battery first**; the EV only absorbs what the battery
cannot take. Without this, the EV's speculative avoided-future-import value
(issue #630) outranks the battery's concrete charge credit, and the EV and
battery oscillate for the same surplus across replans.

Two mechanisms enforce this (both in `planner/milp/`):

1. **Shared surplus-budget constraint** (`_constraints.py`): one row per slot,
   emitted once by the first charge-past-target EV:
   `ec[t] + Σ ev_c[t]/η_charger ≤ max(0, pv[t] − base_load[t])`. The battery
   (`ec`) and every charge-past-target EV share the slot's PV surplus budget,
   so the EV can only use what the battery leaves. Pre-deadline (below-target)
   EVs are excluded — they keep their deadline benefit.
2. **Objective benefit cap** (`_objective.py`): the EV's per-kWh
   charge-past-target benefit is capped at the battery's charge credit
   (`abs(c_obj[ec_off + t])`) when the battery can absorb the full slot
   surplus. The battery's per-slot absorption is
   `min(max_charge_per_slot, usable_kwh − current_kwh)`; when that is ≥ the
   slot's PV surplus, the EV's (speculative) benefit is capped at the
   battery's (concrete) charge credit so the battery wins. When the battery
   cannot absorb the full surplus (tiny battery, or battery nearly full), the
   EV keeps its full benefit for the remainder.

`_build_objective` now receives `current_kwh`, `pv_avail`, and `base_load`
(alongside the existing `usable_kwh` / `max_charge_per_slot`) to evaluate the
per-slot absorption signal. The constraint row count `ev_battery_first_rows`
is `sum(1 for ev in active_evs if ev.charge_past_target) * m` and is included
in `ev_total_rows`.

EV charger watts must remain coherent with the accepted slot's EV energy,
grid flow, net load, and cost fields. Production no longer invokes the old
per-slot power freeze because restoring stale watts after replanning can revive
a command whose energy is no longer reserved. Runtime overrides update the
complete current-slot accounting and respect aggregate fuse headroom.

## EV Pre-Deadline Target Cap (Issue #636 — Overcharge Fix)

In `milp_optimizer.py`, the EV deadline benefit coefficient (`-ev_penalty_cost`)
is applied to **every** kWh of `ev_c[t]` before the deadline. Without an
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
`ev_c[t]` LP variable. Today `engine_core.py`'s `_build_ev_configs_for_milp()`
sets `EVConfig` fields such that they are never both true in practice, but
`milp_optimizer.py` itself does not enforce this. If any future caller or test
constructs an `EVConfig` where both are true, the two benefit blocks would
stack, double-counting the reward on the same variable.

**Invariant**: The LP construction must guard the pre-deadline benefit block
with `and not ev.charge_past_target`, mirroring the existing
post-deadline zero-charge and target-cap constraint guards. This makes the
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
  update cycle** (e.g. `planner/*.py`, `utils/solar_corrector.py`,
  `utils/dynamic_floor.py`, `utils/capacity_learner.py`). The `RotatingFileHandler`
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
the **same resolved** ec/ed values. There is no second pass that reads
raw LP arrays independently.

- `simulate_soc()` accepts an optional `milp_prepopulated=True` parameter.
  When `True`, it preserves the LP's pre-populated energy flow values
  verbatim — it does **not** re-derive discharge from the recommendation
  label and `net_demand`.
- The candidate selector passes `milp_prepopulated=True` for MILP
  candidates (detected by `candidate.name == CANDIDATE_MILP`).
- Non-MILP candidates (no_action, passive, etc.) use the default
  `milp_prepopulated=False` and continue with the existing greedy
  derivation — their behaviour is unchanged.

**Do NOT** add any post-processing step that silently overwrites the
LP-derived per-slot energy flows (`batteries_discharged_kwh`,
`grid_import_kwh`, `grid_export_kwh`) on MILP-sourced slots.
If a step needs to adjust these values, it must be part of the LP
formulation itself, not a downstream patch.

### Degenerate-Vertex Consistency Rule (issue #659, #662)

When the LP write-out path resolves a degenerate/ambiguous solution (the
mutex / "Bug J" simultaneous charge+discharge resolution), **every**
downstream field derived from that slot's energy flow — charge, discharge,
grid import, grid export — must come from the **same resolved decision**
and must be written in the same loop iteration.

**Never** re-read from raw, pre-resolution LP arrays (`ed_sol`,
`gi_sol`, `ge_sol`) in a separate pass. The raw LP values were
computed under the original (possibly now-invalid) ec/ed combination and
will not satisfy the energy balance once ec/ed are adjusted.

**Headroom-based net preservation (issue #662)**: When collapsing a
degenerate vertex, validate the net residual (ec - ed) against the
**actual resolved SoC headroom** at that slot in chronological order.
Maintain a running resolved SoC (`running_soc`) initialised to
`current_kwh` and updated by `resolved_charge - resolved_discharge`
after every slot. For a net-charge candidate (`net > 0`), clamp to
`min(net, usable_kwh - running_soc)`; for a net-discharge candidate
(`net < 0`), clamp to `min(-net, running_soc)`. If the clamped value
is <= `_MIN_ACTION_KWH`, zero both ec and ed — the vertex is solver
noise with no meaningful headroom.

**Warning**: The `net_charge_profit` expression
(`p_imp * (eta_dis - 1/eta_chg) - 2*cycle_cost`) is structurally
**always** <= 0 for any realistic `discharge_eff <= 1 <= 1/charge_eff`
and `cycle_cost_per_kwh >= 0`. It must **never** be used as a
discriminating signal for degenerate-vertex resolution — it cannot
distinguish a genuine economic signal from solver noise. Likewise,
the LP's `s_max_pen[t]` / `s_min_pen[t]` penalty variables are a
**per-slot, hard-bound-violation** signal, not a horizon-wide degeneracy
signal — they miss degenerate vertices where SoC is merely _near_
(not at) a bound, and must not be used for this resolution.

---

## Phase-Aware Power Formulas (issue #640)

Any future power/current-based formula in the planner that converts amps
to kWh/slot **must** be phase-aware. The hardcoded `3.0` multiplier was
replaced by `main_fuse_phases` (1 or 3) in:

- `milp_optimizer.py` — `solve_milp()` fuse constraint formula
- `engine_core.py` — post-hoc main fuse violation check

The canonical formula is:

```text
max_kwh = amps * 230 * phases / 1000 * (interval_minutes / 60)
```

Do **not** reintroduce a hardcoded `3.0` in new formulas that involve
phase-dependent power calculations.

---

## Per-EV Output Fields Must Come From Per-EV Data (issue #646/#655)

**Canonical rule**: per-EV output fields (`ev_charger_calculated_power`,
`ev_second_charger_calculated_power`) **must always** be derived from
per-EV data — never recompute a per-EV field from a combined/summed-across-
all-EVs total.

The slot energy fields (`ev_planned_load_kwh`, `ev_accounted_load_kwh`,
`ev_total_planned_load_kwh`) are the **sum** across both EVs (accumulated
via `combined_ev_inj` / `combined_ev_raw`). Using any of these to derive
a per-EV power value would write the combined total into a single EV's
field, corrupting the other EV's allocation.

Each EV's power must be computed from that EV's own `EVChargingPlan`
(`_compute_ev_charger_power()`) or directly by the MILP's per-EV power
computation. The post-candidate minimum-power floor check must use
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

- Export revenue: `-p_exp[t]` — negative coefficient = profit, large
  when prices are high.
- Battery charge opportunity cost is physical: stored charge draws
  `ec/charge_efficiency` AC or consumes otherwise-exportable PV.

The LP is a global optimiser — it sees all future slots and will defer
battery charging to cheap slots when future solar is sufficient.

**When the LP may charge during expensive hours:** only when future cheap
slots lack enough solar surplus to fill the battery before it is needed
(e.g., a discharge window starts before cheap solar arrives). In that
case the LP correctly prioritises meeting the discharge commitment over
export revenue.

**Regression tests** in `tests/planner/test_milp_optimizer.py`:

- `test_milp_exports_solar_in_expensive_slots_charges_in_cheap` —
  basic 4-slot setup from the acceptance criteria.
- `test_milp_exports_solar_when_future_cheap_solar_sufficient` —
  battery starts partially full, replacement price active.
- `test_milp_charges_early_only_when_future_solar_insufficient` —
  verifies the LP only charges early when necessary.
- `test_milp_solar_export_with_house_load_and_replacement_price` —
  realistic scenario with house load and terminal-SoC incentive.

---

## EV Power Entity Unit Normalisation (issue #592)

HSEM expects every EV charger power entity in **Watts**. Users frequently
point HSEM at template sensors that emit kW (e.g. `3.6` instead of
`3600`), which silently disabled the EV discharge guard (the 3.6 kW
session looked like 3.6 W).

`state_collector._read_ev_power_w()` is the single read path for both
chargers' power entities:

- `unit_of_measurement='kW'` → value × 1000.
- value > 100 kW while charging → implausible, treated as unreadable.
- 0 < value < 50 W while charging (no kW unit) → suspiciously low; the
  value is kept but a warning is logged telling the user to fix the
  template or set `unit_of_measurement='kW'`.

Never read `cfg.ev.power_entity` / `cfg.ev_second.power_entity` through
the generic `_read(..., "float")` path — always use `_read_ev_power_w`.

---

## Live Injection Must Preserve Sub-Window Averages (issue #592)

`engine_population._inject_live_data_into_current_slot` overwrites the
current slot's `avg_house_consumption_kwh` with the live reading, but
must **not** touch `avg_house_consumption_1d/3d/7d/14d_kwh`. The EV
discharge-cap fallback in `applier.async_apply_battery_settings` picks
the _minimum_ of those windows to recover a clean house baseline; the
live-injected value can still be inflated by unmeasured EV load, so
overwriting the sub-windows destroys the fallback and lets polluted v5
history inflate the hardware discharge cap.

---

## Deferred-Export Charge Premium (issue #592)

The #694 charge-credit cap (`repl − p_imp − p_exp/η_chg`) only compares
charging against exporting in the SAME slot. When a future slot has PV
surplus exceeding `min(usable_kwh, max_charge_per_slot)`, that surplus is
exported regardless — so the true refill price is the future (cheaper)
export price, and early charging at a high export price is correct.

Canonical implementation — never re-implement the formula inline:

- `cost_helpers.compute_charge_premium(...)` — the capped premium.
- `cost_helpers.deferred_export_price_by_slot(...)` — per-slot deferred
  export price (min export price across later unabsorbable-surplus slots).

Both the MILP (`milp/_objective.py`) and the selector
(`cost_function.py`) MUST use these helpers so LP decisions and
selector scores never diverge. The correction activates only when the
caller supplies `usable_kwh` and `max_charge_per_slot` (MILP: new
`_build_objective` kwargs; selector: `CostWeights` fields
`battery_usable_capacity_kwh` / `max_charge_per_slot_kwh`, populated
in `engine_core.run_planner` and `candidate_selector`).

Regression tests: `test_milp_defers_charging_to_cheap_slots_when_future_pv_exceeds_headroom`
and `test_milp_charges_now_when_no_future_surplus_exceeds_headroom` in
`tests/planner/test_milp_optimizer.py`.

---

## EV Discharge Cap Must Not Feed Back Into the Planner (issue #592, beta7)

The applier's EV discharge cap writes a small value (e.g. 321 W) to the
`maximum_discharging_power` number entity. Reading that entity back as
`battery_max_discharge_power_w` in `coordinator_builder.build_planner_input`
created a feedback loop: the entire planning horizon was limited to the EV
cap (`discharge=0.073 kWh` per 15-min slot) and the battery could never
cover evening house load.

Canonical rule: **planner inputs must reflect physical capability, not
commanded entity state.** Use
`coordinator_builder._resolve_max_discharge_power_w(live)` — it always
derives the max from the rated capacity via
`get_max_discharge_power()`; the live entity read-back is only a
degraded fallback when the rated capacity is unknown. (Beta8 gated the
substitution on `any_ev_charging`, but a single EV-status flicker let
the still-capped entity poison an off-schedule run — the substitution is
now unconditional.)

`get_max_discharge_power()` covers single- and two-stack S0/S1
capacities explicitly (5000–30000 Wh); unknown capacities log a warning
and fall back to 2500 W rather than failing silently (issue #723).

## EV Discharge Cap Is the Historical Baseline — Live Never Moves It (issue #592)

`applier.compute_ev_discharge_cap_w()` is the single place that computes
the cap. Two opposite failures proved the live reading must not move the
cap at all when history exists:

- **beta8 ratchet:** `min(live, history)` let CT-clamp/EV-sensor drift
  pull the cap 363→40 W over one night (the battery's own capped
  discharge shrinks the CT reading further — a self-poisoning input).
- **v6.2.0-beta1 swings:** `max(history, min(live, 3×history))` let
  ordinary house noise (cooking, heat pump) swing the cap 652→1968→928 W
  for 5+ hours, draining the battery before the 06:00 scheduled plan.

Rules:

- history available → cap = historical baseline, live is ignored
- no EV power sensor → minimum positive sub-window average
- no history → trust live

**SoC guard (v6.2.0-beta1):** after computing the cap, if
`battery_current_capacity_kwh <= current_required_battery_kwh` (the
planner's reserve until the next solar surplus), the cap is forced to
0 W — the battery is preserved for its schedule and the house load is
served from the grid until the battery recovers.

Regression tests: `tests/test_coordinator_builder.py`,
`tests/sensors/test_applier.py::TestComputeEvDischargeCapW`.

> **⚠️ DEPRECATION NOTE (2026-08-25, issue #817):**
> Both "EV Discharge Cap" sections above describe `compute_ev_discharge_cap_w()`
> and the historical-baseline machinery. The `fix/797-ev-dispatch-authoritative-solar-aware`
> branch (issue #797) deletes this machinery outright — `force_max_discharge_power`
> becomes a permission gated on actual active/planned EV state, not a
> computed cap from live/readback values. **These two sections must be retired
> once #797 merges.** Until then they still accurately describe the current
> `main`-branch behaviour.

---

## Consumption Blend Peer-Median Clamp (issue #592, beta2)

The 1d/3d/7d/14d consumption blend has THREE layers, applied in order in
`slot_population.weighted_avg_consumption`:

1. `clamp_window_to_peer_median` — each window clamped to at most
   `max(3 × median of the other three, 0.15 kWh/h)`. Upward-only: a
   genuine consumption drop flows through immediately. Catches the
   stale-14d pollution pattern (three windows clean after a behavioural
   change, one long window still holding pre-change nights) that the IQR
   mask misses — with 4 points the median lands between the clusters and
   flags BOTH ends, zeroing the clean windows too.
2. `detect_outliers_iqr` weight redistribution (issue #301).
3. 7d/14d mutual caps + reliability scaling.

Never clamp the downward side — a real drop in house consumption must not
be inflated back up.

Regression tests: `tests/sensors/test_hourly_data_populator.py::TestPeerMedianClamp`.

---

## File Organization — By Responsibility, Not By Theme

## Price/Solcast Slot Matching — Floor to Source Interval, Not Hour (issue #720)

`hourly_data_populator/prices_solcast.py` matches sensor data points to
recommendation slots by timestamp. The original code truncated both sides
with `.replace(minute=0, second=0)`, which collapsed all four 15-min
price points of an hour onto one key — the last write won, and all
quarter-hour slots showed the same (hourly) price even when EDS published
96 distinct prices per day.

Canonical rule: **floor data points to the start of their enclosing
_source_ window** (`electricity_price_update_interval` for prices, 60 min
for Solcast) via `normalize_slot_start(dt, source_interval_minutes)`,
then match a slot when its start lies inside that source window
(`dt_key <= obj_start < dt_key + source_window`). This preserves both
fan-out directions:

- 15-min prices + 15-min slots → each point lands on exactly one slot
- 60-min prices + 15-min slots → the hourly point covers all four slots

Regression tests: `tests/test_15min_price_matching.py`.

**Stage 2 — planner input collapse (same issue):** even with population
fixed, `coordinator_builder.build_planner_input` deduplicated on
`(day_offset, hour)` and appended price points _inside_ that guard, so
only the first quarter of each hour survived (192 slots → 48 hourly
points), and `populate_prices` fanned the survivor back across the hour
via `align_hourly_prices`. Fix: `PricePoint` carries an optional
`slot_in_day`; price points are emitted per slot (consumption averages
and Solcast PV stay hour-deduplicated); `populate_prices` keys by
`(day_offset, slot_in_day)` with an hourly fallback when present.
Hour-granular callers (`slot_in_day=None`) are unaffected.

Regression tests: `tests/test_quarter_hourly_planner_input.py`.

---

## Avg Sensor Must Not Store Partial-Day Samples (issue #720 follow-up)

`HSEMAvgSensor._async_store_utility_meter_value` samples the daily
utility meter every 5 minutes. The meter resets at `hour_start` and
accumulates energy only during the `hour_start` → `hour_end` block
(the power sensor reports `unknown` outside that window). The original
code stored every sample under the current date, so a mid-day reading
recorded a **partial** day as if it were complete. For a new energy
sensor with limited history, partial days fill the rolling window and
inflate the forecast (reporter: 14.267 kWh sampled at 05:45 → ~4.7 kWh/h
forecast for a ~260 W house).

Canonical rule: **only persist the day's sample once the hour block is
complete** — `now.hour >= hour_end` for normal blocks; for the
overnight 23→00 block (`hour_end == 0`) any hour except 23 counts, and
post-midnight samples are attributed to the previous date.

Regression tests: `tests/test_avg_sensor_partial_day.py`.

---

## Solar-Charge Mislabel at Zero PV (issue #720 follow-up)

`apply_optimization_strategy` used `NEAR_ZERO_CONSUMPTION_THRESHOLD_KWH`
(0.1 kWh) to decide whether an unassigned summer slot should charge from
solar. A slot with a small positive house load (e.g. 0.08 kWh) and zero
PV would pass the `<= 0.1` check and get `BatteriesChargeSolar` even
though there was no PV surplus at all. The result was a grid-charging
slot masquerading as solar charging, which:

- Confused the `hourly_recommendations` output
- Caused the applier to write `MaximizeSelfConsumption` instead of
  `TimeOfUse` + charge TOU
- Made the plan look more fragmented than it actually was

**Canonical rule:** `BatteriesChargeSolar` is only assigned when there
is a genuine PV surplus (`estimated_net_consumption_kwh < 0`). A small
positive house load with zero PV must not be treated as a solar-charging
opportunity.

Regression tests: `tests/planner/test_zero_pv_solar_charge_mislabel.py`.

---

## Nordpool Price Format — raw_today/raw_tomorrow (issue #750)

`custom-components/nordpool` publishes `raw_today` / `raw_tomorrow`
attributes as `{"start": datetime, "end": datetime, "value": price}`.
HSEM's price populator (`custom_sensors/hourly_data_populator/prices_solcast.py`)
mapped those attributes as `{"k": "hour", "v": "price"}`, so every entry
was silently skipped and all planner slots got `import_price = 0.0`.

**Canonical rule:** the `data_sources` mapping for `raw_today` /
`raw_tomorrow` accepts both the legacy `hour`/`price` format and
the nordpool `start`/`value` format. The mapping is a list of
fallbacks — add new formats as additional `{"k": ..., "v": ...}`
entries rather than replacing existing ones.

Regression tests: `tests/test_15min_price_matching.py`
(`TestNordpoolRawFormat`).

**Observability rule:** when a configured price sensor yields zero matched
data points, the populator logs a `warning` naming the sensor. A
`debug` message is logged for Solcast sensors (PV forecast is optional).
This makes format mismatches visible instead of silently planning with
`import_price = 0.0` (issue #750).

---

## File Organization — By Responsibility, Not By Theme

AI agents naturally bucket related things together (e.g. "all planner inputs in one file").
This is an anti-pattern. **Organize files by responsibility — one file does one thing.**

What this means per layer:

- **`models/`**: One dataclass per file. Exception: tightly-coupled nested types that are
  never imported independently (e.g. `EVChargerConfig` lives in `sensor_config.py` because it
  only exists as a field of `SensorConfig`).
- **`planner/`**: One algorithm/strategy per file (already the case).
- **`utils/`**: One problem domain per file — a group of closely related functions
  (already the case with `prices.py`, `misc.py`, etc.).
- **`custom_sensors/`**: One sensor/coordinator per file.

Do **not** create files like `planner_inputs.py` (6 unrelated dataclasses) or
`planner_outputs.py` (7 unrelated dataclasses). Each dataclass is its own responsibility.

**Why**: Smaller, focused files give AI agents exactly the context they need.
Thematic bucketing loads irrelevant code into every prompt, reducing precision
and causing edit collisions between unrelated classes.

## EV Charger Power Must Be Slot-Stable (issue #738)

`ev_charger_calculated_power` and `ev_second_charger_calculated_power` are HSEM's
commands to the EV chargers. They must be published with matching EV energy,
grid import/export, net-load, cost, and EV-plan fields from the same accepted
snapshot. A replan may change the command, but production must never restore an
older watt value in isolation. Force-charge and negative-price Auto-Full use the
same coherent accounting path, respect aggregate fuse headroom, and suppress a
request when the corresponding EV is explicitly disconnected.

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
  used. Once the battery reaches the reserve, the applier falls back to strict
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

## GitHub Operations — `gh` CLI Is Available (Corrected 2026-08-30)

**The `gh` CLI IS installed and authenticated in the devcontainer** (`/usr/bin/gh`,
added via devcontainer features). The earlier "never use `gh`, MCP tools only" rule
was written when `gh` genuinely was absent; that premise no longer holds and the rule
has been inverted repo-wide (`AGENTS.md`, `CLAUDE.md`, `.github/copilot-instructions.md`,
`.claude/skills/hsem-pr-workflow/SKILL.md`).

**GitHub MCP tools are not present in every session.** Never assume they exist — check
first and fall back to `gh`, which is the reliable baseline. When both are available
either path is fine.

| Operation                      | `gh` command                                           | MCP tool (when available)       |
| ------------------------------ | ------------------------------------------------------ | ------------------------------- |
| Create a PR                    | `gh pr create --base main --title ... --body-file -`   | `create_pull_request`           |
| Update a PR (title/body)       | `gh pr edit <n> --title ... --body-file -`             | `update_pull_request`           |
| Read a PR / diff / comments    | `gh pr view <n> --json ...` / `gh pr diff <n>`         | `pull_request_read`             |
| Review a PR                    | `gh pr review <n>`                                     | `pull_request_review_write`     |
| Create / update / close issues | `gh issue create` / `gh issue edit` / `gh issue close` | `issue_write` / `issue_read`    |
| List / search issues & PRs     | `gh issue list` / `gh search issues`                   | `list_issues` / `search_issues` |
| Merge a PR                     | `gh pr merge <n>`                                      | `merge_pull_request`            |

**Multiline bodies must go through `--body-file`** (a path, or `-` with a piped
heredoc). Inlining a long markdown body as a shell argument invites quoting bugs.

**Prefer `rtk gh ...`** — RTK filters `gh` output and cuts 26-87 % of the tokens.

**Local `git` is unchanged** for `git add` / `git commit` / `git checkout` / `git push`
(the branch pushes over SSH to `origin` = `woopstar/hsem`).

## Fork Divergence — `Ambilights/hsem-ambilights` Is Not Back-Portable

The AGPL fork `Ambilights/hsem-ambilights` (v7.x) **diverged** from this repo at
commit `dd8db6ec` (2026-08-11). It is not a linear descendant, so its numbered
"fix" PRs cannot be cherry-picked onto `main` — they are layered on fork-only
architecture that does not exist here. Before re-attempting any port, remember:

| Fork PR          | Subject                                                     | Status for `main`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| ---------------- | ----------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| #2               | Complete standalone repository migration                    | **N/A** — fork-specific repo rename/metadata migration (manifest, ownership, CI workflows, release tooling). No planner or runtime code changes. Nothing to port.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| #3               | Update compatible dependencies for v7.0.2                   | **N/A** — fork-specific dependency bump (HA 2026.8.2, NumPy 2.5.2, Ruff 0.16.3). This repo manages its own dependencies independently via Dependabot.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| #5               | ENTSO-E published-price backup                              | **Feature**, not a fix; new provider. Not needed — no request. The orphaned `models/price_source.py` stub was removed in PR #783.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| #7               | Remove unused controls + embedded OCPP                      | **Removal** — explicitly rejected (upstream ships OCPP, schedules, charge-rate learner).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| #9 / #22         | PowMr site-attribution / control stability                  | **Feature** — `origin/main` has no PowMr/secondary-storage subsystem at all. N/A.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| #11              | ML history ↔ tracking time                                 | **Ported** (ML core + DST-fold `slot_key` prerequisite). The actual-price-interval half (`ActualPriceInterval`, `_compute_actual_charge_savings`) is deliberately out of scope.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| #13              | Bound terminal inventory at price boundary                  | Coupled to fork-only `future_value.py`/`_solver_execution.py`/`secondary_storage.py`/`price_forecast.py`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| #15              | Group export-reserve checkpoints                            | **Ported in PR #783** — `milp/_export_reserve.py` now exists here, and the run-grouping loop is exact. Supersedes the earlier "N/A".                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| #17              | Harden MILP bounds layout                                   | **Ported in PR #783** as `MilpBoundsBuilder` over a declared `MilpColumnLayout`: bounds are addressed by block name, and wrong offsets, overlaps, and unassigned columns all fail before the solve.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| #19              | Reject unavailable load + stale solves                      | **Ported in PR #783** (`292afe3f`, 2026-08-23) — a genuine, deliberate port that happened to land under different internal naming, not independently pre-existing code. `git log --oneline -S "_update_generation" -- custom_components/hsem/` shows `_update_generation` was introduced to `main` only in `292afe3f`; the intermediate branch commit implementing it is dated ~10 hours _after_ the fork's own PR #19 commit, ruling out the earlier "already complete... pre-existed" framing. Load fail-closed _and_ stale-solve rejection are both native: `_update_generation` is captured per cycle and re-checked mid-solve and before publication, with `_restore_accepted_plan_state` rollback. Do not look for the fork's `_forecast_authority_generation` name; the local equivalent uses different naming.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| #21              | Coalesce boundary price refreshes                           | **Mostly N/A; the one real gap fixed in PR #783.** This repo drives replans from a periodic interval timer, not slot-boundary timers, so there is no boundary to coalesce. Clearing the pending flag inside the update lock was already native. The portable remainder was retrying a _failed_ cycle when newer state is pending — previously the exception escaped and stranded `_event_update_pending`. Now bounded by `_MAX_FAILED_UPDATE_RETRIES`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| #23              | Stabilize storage economics                                 | **Mostly ported in PR #783** (`292afe3`, "harden planning safety, MILP bounds layout, and file limits"). The portable planner hunks (`cost_function.py`, `cost_types.py`, `engine_core.py`, `milp/_objective.py`, `milp/_diagnostics.py`, `utils/misc.py`) were extracted and landed. Fork-only files (`secondary_storage.py`, `future_value.py`, `secondary_storage_applier.py`, PowMr-coupled coordinator) are N/A. **Two real gaps were found and are now fixed**: issue #813 (price-availability gating in `FinancialTracker.accumulate()`, merged as PR #821) and issue #814 (EV `net_load` double-count when co-optimisation is active, merged as PR #820).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| #24              | Harden safety and economic accounting                       | **Mostly ported in PR #783** (`292afe3`, "harden planning safety, MILP bounds layout, and file limits"). The portable planner/tracking hunks (`financial_tracker.py`, `prediction_tracker.py`, planner files, tests) were extracted and landed. Fork-only entanglement (monolithic coordinator +2910 lines, PowMr-coupled applier) is N/A. **Two real gaps were found and are now fixed**: issue #813 (price-availability gating in `FinancialTracker.accumulate()`, merged as PR #821) and issue #814 (EV `net_load` double-count when co-optimisation is active, merged as PR #820).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| #26              | Model EV charger phase topology                             | **Ported** — tracked as issue #787, merged as PR #793 (`feat(ev): per-phase fuse constraints from charger phase topology`). File list matches almost exactly (`utils/phase_power.py`, `models/ev_config.py`, `flows/ev_planned_load_helpers.py`, `tests/planner/test_ev_charger_phase_topology.py`).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| #27              | Publish the charging ceiling and re-portion stranded energy | **Ported** — tracked as issue #788, merged as PR #794 (identical title).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| #28              | Make charging commands authoritative and executable         | **Ported** — tracked as issue #789, merged as PR #796 (near-identical title, same file set: `ev_delivered_energy.py`, `integration_version.py`, `phase_power.py`, `milp/_write_results.py`, `engine_ev_milp.py`, `services.py`, `diagnostics.py`).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| #29              | Stabilize current-slot live power                           | **Fully ported — tracked/closed as issue #792 (self-contained half) and issue #797 (coordinator wiring).** Self-contained half: `utils/live_power.py` (`LivePowerEstimate`/`LivePowerWindow`), `PlannerInput.live_house_consumption_available` tri-state, `engine_population.py` honoring it (legacy `> 1e-9` fallback when `None`), and hardened `coordinator_builder._resolve_live_solar_measurement` / `_resolve_live_house_measurement`. **Coordinator-level `LivePowerWindow` wiring landed on `main` via commit `5300956`** ("fix(ev): make charging dispatch authoritative and solar-aware (#806)") — a fresh reimplementation per `coordinator_live_power.py`'s module docstring, not a merge of the `fix/797-ev-dispatch-authoritative-solar-aware` branch (that branch's commit `fc353133` never landed; `git merge-base --is-ancestor fc353133 main` is false). The "unwired" gap is closed.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| #30 (`623269a2`) | Safely release current-slot SBU lock                        | **N/A — tracked/closed as issue #795, documented in PR #802.** Every hunk is cross-device Huawei/PowMr coordination: the `current_slot_sbu_allow_utility_escape` flag/MILP relaxation, `acquire_secondary_utility_authority`/`secondary_utility_authority_is_valid`, and the `working_mode_sensor.py` "stop PowMr Utility, then allow Huawei" resequencing all only mean something against a two-inverter setup this repo doesn't have. No self-contained half — re-verified 2026-08-24 directly against the live diff. Do not re-attempt without an explicit PowMr-support request.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| #31              | Make charging dispatch authoritative and solar-aware        | **Ported — landed on `main` via commit `5300956`** ("fix(ev): make charging dispatch authoritative and solar-aware (#806)"), superseding the earlier `fix/797-ev-dispatch-authoritative-solar-aware` branch (that branch's commits, including `fc353133`, never merged; #806 is a fresh reimplementation per `coordinator_live_power.py`'s module docstring). This was a genuine architectural improvement, not just a fix: it replaces the continuous-EV-variable-then-quantize-in-writeback approach (still native here, in `milp/_ev_quantize.py`/`_ev_power_writeout.py` from #796 — confirmed "pure move, no behaviour change" from the old heuristic) with solver-native semi-integer/semi-continuous MILP amp variables, eliminating the plan-vs-execution divergence class of bug by construction. Also includes a real discharge-cap-semantics fix (`force_max_discharge_power` becomes a permission gated on actual active/planned EV state, not a bare boolean override) and the coordinator live-power replan-budget wiring that **resolves the #29 coordinator-wiring prerequisite**. **`phase_charge_limiter.py` hunks are correctly out of scope** — fork-only PowMr runtime, no local equivalent (the Huawei-only equivalent shipped separately via issue #831). **Real risk** (unchanged): semi-integer variables are more expensive for HiGHS to solve than the LP relaxation, and the fork's own very next PR (#23, "separate solver and wall budgets") landed the day after this one — a strong signal they hit solve-time trouble from this exact change. |
| #32              | Throttle live charging current safely                       | **Investigated 2026-08-24 — tracked as issue #798, not started.** The full live-throttling feature is PowMr-only (adds `current_slot_charge_current_limit_a`, gated on `SECONDARY_MODE_CHARGE` lock) and stays deferred, matching #30's pattern. But #798 correctly identifies that some of the underlying _bug-fix patterns_ are hardware-agnostic and worth a design-review audit even without PowMr: (1) stop-before-reduce write ordering creating a self-defeating control-loop transient, (2) stale/missing telemetry falling through to a raw value instead of a monotonic never-increase fallback, (3) transition-reservation ordering when one device is written before another that shares fuse/phase headroom, (4) high-rate listener events routed to a lightweight path vs. dropped entirely. Correcting my earlier flat "N/A" verdict — #798's audit-vs-defer split is the right framing, not a blanket rejection. **Update 2026-08-27:** patterns (3) and, partially, (2) were independently addressed after this investigation by issue #816/PR #823 (phase-headroom write-ordering, 2026-08-25) and the #831 series (feedback-free floor + fail-closed deadline, 2026-08-25–27) — both Huawei-only, not #798 itself. The live-throttling feature (PowMr current limiting) is still genuinely not started; only part of the underlying audit's concern set remains open.                                                                                                                                                                                       |
| #33              | Dependabot: bump python-dependencies group                  | **N/A** — this repo runs its own independent Dependabot (see PRs #799/#800 bumping ruff/pytest-socket separately); the fork's dependency set and versions aren't a "port" target. No tracking issue, and none needed.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| #36              | Preserve charge lease across state echoes                   | **N/A** — investigated fresh 2026-08-25 (`git log --oneline -i --grep="powmr" origin/main` returns zero commits: PowMr support never existed here, not just currently absent). No PowMr/secondary-storage subsystem exists to hold a "charge lease" against. The underlying bug-fix pattern (stale telemetry echoes clobbering a just-issued command) is already addressed for real Huawei hardware via issue #816 / PR #823's phase-headroom write-ordering fix.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| #38              | Tolerate stale current echoes                               | **N/A** — same PowMr-only basis as #36/#39, confirmed by the same zero-hit grep. The stale-telemetry-fallback pattern it fixes is the same class addressed natively (Huawei-only) by issue #816 / PR #823.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| #39              | Stabilize charge transitions across replans                 | **N/A** — same PowMr-only basis as #36/#38, confirmed by the same zero-hit grep. The transition-stabilization pattern it fixes is the same class addressed natively (Huawei-only) by issue #831's `PhaseChargeTransitionMixin` (PRs #836/#838/#839).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |

**Fork PR dependency graph (2026-08-25, corrected 2026-08-27):** #26 → #27 → #28 form a chain ported in order (fork tags v7.2.0/v7.3.0/v7.3.1, tracked as #787/#788/#789, all merged). #29's self-contained core (#792) needs #26; its coordinator integration landed with #31. #30 (#795) needs #26 + #29 and is N/A regardless. #31 (#797) landed on `main` via commit `5300956` (PR #806) — this also resolves the #29 coordinator-wiring prerequisite. #23/#24 portable parts are in PR #783; the two gaps found (#813, #814) are fixed (PRs #821, #820). #32 (#798) needs #26 + #29 + #30 for its full feature; its bug-fix-pattern audit half does not. #36/#38/#39 are PowMr-only and N/A regardless of chain position; their bug-fix patterns are addressed natively via issue #816 (PR #823) and issue #831 (PRs #836/#838/#839).

**Decision:** `origin/main` remains the baseline. Port only self-contained,
upstream-relevant _fixes_; re-implement concrete bugs natively (with regression
tests), never import the fork's v7 planner/coordinator architecture. ENTSO-E and
PowMr are provider/hardware _features_ and stay out unless an explicit request
arrives.

**Embedded OCPP stays — reconfirmed 2026-08-22 (PR #783).** Fork PR #7 deletes
`ocpp_server.py`, `ocpp_sensors.py`, `utils/sensornames/ocpp.py`, and
`models/ocpp_session.py`. This repository ships OCPP as a supported product
feature, so removing it is a user-facing breaking change with no upstream
benefit. Treat OCPP's continued presence as a deliberate divergence, never as an
unfinished port. The same holds for fixed battery schedules. Only the dead
seven-bucket charge-rate learner from #7 was taken.

**One OCPP server per EV (2026-08-23).** Each EV gets its own embedded OCPP
server on its own port (defaults 9000 / 9001): the primary plan drives the
primary server, the second-EV plan drives the second server. The second
server's config fields (`hsem_ocpp_second_enabled` / `_port` / `_cpid`) are only
shown in the ocpp flow step when the second EV is enabled, and the second set
of diagnostic sensors (`*_second`) only registers when both the second EV and
the second server are enabled. Sensor name helpers take a `charger_index`
argument (1 = primary, 2 = second); `CoordinatorData` carries
`ocpp_second_chargers` / `ocpp_second_sessions` alongside the primary fields.

## EV Charger Phase Topology (issue #787)

`utils/phase_power.py` is the single authority for EV charger phase topology:
`EV_PHASE_TOPOLOGIES`, `ev_phase_share()`, `normalize_ev_phase_topology()`,
plus `executable_ev_phase_kwh()` / `fixed_session_phase_ac_kwh()` /
`ev_phase_share_for_slot()` shared helpers. Never re-derive a per-phase EV
fraction inline.

- `single_phase` (σ=1) is the safe default everywhere; unknown/missing/stale
  stored values normalize to it — never relax a hard fuse constraint by
  accident.
- The hard per-phase rows live in `planner/milp/_phase_fuse.py`
  (`add_phase_fuse_constraints`) and are emitted from `_build_constraints`
  when `phase_fuse_active` (main fuse active AND active EVs). Envelope:
  `gi/3 - ge/3 + Σ(σ_e - 1/3)·ev_ac ≤ main_fuse_amps·230/1000·slot_hours`.
- Three sites must agree via `EVConfig.phase_share`: constraint rows, the
  EV fragment concentration in `_write_results.py` (clamps `room_dc` by
  per-phase headroom), and post-solve validation
  (`phase_envelope_from_published_slots` → `max_phase_import_kwh`
  diagnostic). A plan the solver accepts must never be erased by a
  validator assuming a different topology.
- Config key: `hsem_ev_planned_load_charger_phase_topology` (+ `ev_second_`
  variant), wired const → flows → translations (config AND options steps)
  → sensor_config → planner_input → engine_ev_milp → EVConfig.

## OCPP/Huawei Phase-Headroom Write Ordering (issue #816)

**Race confirmed and fixed (2026-08-25).** When the plan transitions from "EV charging" to "battery discharging", the OCPP anti-flap stop window (180s) means the charger hasn't stopped yet when the Huawei discharge cap is updated. The cap was computed from the **planned** EV power (0 kW), not accounting for the still-running EV draw (e.g., 7 kW), causing a transient phase-fuse overload.

**Fix:** `_ev_phase_headroom_reservation_w()` in `applier_caps.py` computes the reservation: when an EV is live charging but the planned power is 0 (or lower), the reservation is the difference between live and planned draw. The Huawei discharge cap in `applier.py` subtracts the total reservation from the planned cap, preventing the overload.

**Pattern:** Same class of race as Ambilights/hsem-ambilights#32 (PowMr-vs-Huawei), but locally between OCPP EV charger and Huawei battery. The upstream fix tracked `_active_secondary_slot` and `_active_secondary_current_retarget_downward`; the local fix uses live-vs-planned power difference, which is simpler and doesn't require cross-component state tracking.

**Key insight:** The MILP planner's per-phase fuse constraint already allocates headroom correctly, but the **hardware writes** don't happen instantaneously. The reservation bridges the gap between the planner's solved state and the live hardware state during transitions.

## OCPP Transaction ID Allocation + Stop-Retry Symmetry (issue #906)

**Real bug, not speculation — confirmed 2026-09-02.** `_handle_start_transaction()` echoed `payload.get("transactionId", 0)`. Per OCPP 1.6 §5.14, `StartTransaction.req` (charger → CS) has no `transactionId` field at all — allocating one is the CS's job, returned in `.conf`. Real chargers never send it, so every session silently got id `0`. A charger firmware treating `0` as an unset/sentinel value can then reject or ignore `RemoteStopTransaction` naming it — a plausible root cause for "the charger won't stop" reports. Fix: `OCPPServer._next_transaction_id` is a monotonic counter allocated in `_handle_start_transaction()`, never trusting the inbound field.

**Separately, the anti-flap stop-window guard was asymmetric with the start path.** The stop branch's outer condition was `if flap_state == "charging" or flap_state == "starting":` — missing `"stopping"` itself. Once the state machine entered `"stopping"`, the block became unreachable on every later cycle, so a failed-to-send or charger-ignored `RemoteStopTransaction` was attempted exactly once and never retried, despite a comment claiming otherwise. The start path's equivalent guard correctly includes its own in-progress state (`flap_state in ("idle", "stopping", "starting")`), which is why start retries always worked. Fixed to mirror start: guard now includes `"stopping"`, and the `"stopping" → "idle"` transition is gated on `session.transaction_id is None` (ground truth via the charger's own `StopTransaction` call) rather than on `_send_remote_stop()`'s return value, with a `_remote_stop_due()` cooldown mirroring `_remote_start_due()`.

**Also added:** `ChargerSession.pending_calls`/`last_call_status` — outbound `RemoteStartTransaction`/`SetChargingProfile`/`RemoteStopTransaction` CALLRESULTs were previously logged at debug level with their `status` field never read, so a charger silently rejecting a command was indistinguishable from acceptance in diagnostics. Now tracked and surfaced via `sensor.hsem_ocpp_charger_status`'s per-CPID `last_call_status` attribute; a rejected `SetChargingProfile` is retried on a cooldown without waiting for a material target change.

## OCPP Event-Driven Coordinator Refresh (issue #908)

**Gap confirmed 2026-09-02/03.** `sensor.hsem_ocpp_charger_status` and the other OCPP diagnostic sensors are `CoordinatorEntity` subclasses with `should_poll = False` and no override of `_handle_coordinator_update()` — the only path that pushes their state into HA (`async_write_ha_state()`) is the coordinator calling `async_set_updated_data()` from its own cycle. The embedded OCPP server mutates the live `ChargerSession` the instant a WebSocket message arrives (`_handle_status_notification`, `_handle_start_transaction`, `_handle_stop_transaction`, connect/disconnect in `_handle_charger`), but nothing in `ocpp_server.py`/`ocpp_message_handlers.py`/`ocpp_commands.py` ever told the coordinator to refresh. The coordinator has no HA-managed poll interval (`update_interval=None`, "Bronze rule: appropriate-polling") — it runs its own `async_track_time_interval` timer at `hsem_update_interval` minutes (default 5). Net effect: a car plugging in/out or a charge starting/stopping was invisible in the frontend for up to 5 minutes despite being recorded internally instantly.

**Fix:** mirrors the existing `async_options_updated()`/`_async_options_update_debounced()`/`_async_options_update_background()` debounce trio in `coordinator_lifecycle.py` (cancel-and-reschedule `asyncio.Task`, not HA's `Debouncer` helper — not used elsewhere in this codebase) — a new `async_ocpp_event()` trio with its own `OCPP_EVENT_DEBOUNCE_SECONDS` (2.0s, `coordinator_helpers.py`). `OCPPServer.__init__()` takes an optional `on_significant_event` async callback, wired to `coordinator.async_ocpp_event` for both the primary and second server in `async_setup()`. `OCPPServer._notify_significant_event()` awaits it directly from the WebSocket message loop (cheap — the callback only does task bookkeeping, doesn't block on the actual refresh) — called from connect/disconnect in `_handle_charger()`, and from `_handle_status_notification` (only on an actual status _change_, not a repeat), `_handle_start_transaction`, and `_handle_stop_transaction`. Deliberately **not** called from `MeterValues`/`Heartbeat`/`Authorize` — those arrive far more often and carry no transition information worth an out-of-band planner cycle. A burst of related messages around one connect/start (BootNotification + StatusNotification + StartTransaction, typically within ~1s of each other) coalesces into one refresh via the debounce window, not one per message.

**Pattern reuse note:** `_make_bare_coordinator()` in `tests/test_coordinator.py` needs `_ocpp_event_task`/`_ocpp_event_debounce_task` initialised alongside the existing `_options_update_task`/`_options_update_debounce_task`, or `async_ocpp_event()` raises `AttributeError` in tests that bypass `__init__`.

## Live Phase-Aware Grid-Charge Safety Limiter (issue #831, complete — 3 of 3 PRs merged)

**Built from scratch, Huawei-only — not a straight fork port.** Fork PR `Ambilights/hsem-ambilights#35`'s phase-limit feedback stabilization was originally judged "not applicable" (no `phase_charge_limiter.py`, no live per-phase read path, no write path for `hsem_huawei_solar_batteries_grid_charge_maximum_power` existed at all). On reflection this was the wrong call: the fork author hit a real hardware safety gap (an appliance load change between MILP solve time and the hardware write can push a phase over the fuse rating), and this repo's Huawei-only control path can benefit from the same protection without PowMr. Decision reversed 2026-08-27 — build the feature properly instead of declining it.

**Delivery was split into 3 PRs** (tracked in issue #831), all merged:

- **Part 1** (PR #836): foundation wiring only. New config toggle `hsem_phase_aware_charging_enabled` (default `False`), four new Huawei entity pickers (`hsem_huawei_solar_batteries_grid_charge_maximum_power`, `hsem_huawei_solar_batteries_charge_discharge_power`, `hsem_huawei_solar_power_meter_phase_{a,b,c}_active_power`), `SensorConfig`/`LiveState` fields, diagnostics exposure. No behaviour change.
- **Part 2** (PR #838): the limiter core. `utils/phase_power.py` gains `PhasePowers`, `PhaseChargeLimits`, `phase_powers_valid()`, `compute_phase_charge_limits()` — pure Huawei-only math (no PowMr delta/imbalance terms, unlike the fork). New `custom_sensors/phase_charge_limiter.py::build_phase_aware_charge_commands()` wraps it with recommendation-awareness and fail-closed gating. Wired into `applier.py::async_apply_battery_settings()` right after the `match recommendation` block: for a `batteries_charge_grid` slot with the feature enabled, writes the computed cap to the grid-charge-maximum-power entity before the existing wait-mode/TOU/working-mode writes.
- **Part 3** (this entry): the feedback-free floor for verified downward transitions + 45-second fail-closed deadline (the original #831 ask).

**Two-source data model, both already existed upstream but were unwired here:** `number.batteries_grid_charge_maximum_power` (the write target) and `sensor.batteries_charge_discharge_power` (`STORAGE_CHARGE_DISCHARGE_POWER`, signed: positive = charging, negative = discharging — required to remove the battery's own contribution from the live phase snapshot before computing headroom). Both are now documented in `docs/huawei_entities.md`.

**Canonical helper:** never re-derive the phase-headroom-minus-own-contribution math inline — always call `compute_phase_charge_limits()`. It floors the command to a 100 W step and always returns a `PhaseChargeLimits` object (never `None`). The fail-closed-to-`0.0`-on-unsafe-telemetry gate itself lives one layer up, in `build_phase_aware_charge_commands()` — that function returns `primary_charge_power_w=0.0` whenever the live inputs make the computation unsafe, and separately returns `primary_grid_charge_power_w=None` (no override, entity untouched) when the feature is disabled or the slot isn't a grid-charge slot — callers must distinguish `None` (leave alone) from `0.0` (write zero).

**Part 3 — transition/deadline safety.** New `custom_sensors/phase_charge_transition.py::PhaseChargeTransitionMixin`, mixed into `HSEMWorkingModeSensor` (which needed splitting out of `working_mode_sensor.py` to stay under the 30 KB/1000-line limit — the same mixin-extraction pattern the coordinator uses, see `coordinator_state.py`/`coordinator_live_power.py` etc.). Tracks exactly one `PrimaryGridChargeTransition` at a time, keyed by `(utc_key(rec.start), utc_key(rec.end))`:

- Arms only on a **verified downward** cap write (`_record_verified_primary_grid_charge_transition`); repeated verification of the same target never extends the 45 s deadline (`PRIMARY_GRID_CHARGE_TRANSITION_MAX_SECONDS`); a nested lower target keeps the original (higher) `previous_limit_w` reference.
- While unsettled, `_primary_grid_charge_transition_status()` hands `previous_limit_w` to `build_phase_aware_charge_commands(..., primary_grid_charge_transition_reference_w=...)`, which uses it only when the reference exceeds the live battery-power reading — it can only tighten the effective floor, never relax it below the raw live value.
- Clears once both the live cap (within 1 W) and live battery power (within 300 W) agree with the target; otherwise, after 45 s an entity-owned `asyncio.Task` (`_schedule_primary_grid_charge_deadline`) fires and forces a fresh `_async_apply_hardware_writes()` pass even with zero new telemetry, so a frozen echo cannot indefinitely defer the 0 W fail-close.
- Cancelled and cleared in `async_will_remove_from_hass()` — a config-entry reload can never strand a transition or leak its deadline task.

Tests: `tests/test_phase_charge_limiter.py` (limiter core + Part 2 applier integration), `tests/test_phase_charge_transition_safety.py` (Part 3 transition/deadline logic, 18 tests mirroring the fork's coverage style but Huawei-only).

## Error-Mode Grid-Charge Emergency Stop (issue #840, follow-up to #831)

**Gap left open by #831:** the Part 3 transition/deadline logic only runs while `hardware_writes_allowed()` is true. If `classify_degraded_mode()` escalates to `DegradedMode.Error` mid-cycle (e.g. a critical sensor goes unavailable) while HSEM itself is the one holding an armed grid-charge cap, `_async_apply_hardware_writes()` takes the `elif not writes_safe:` branch and simply skips all writes. The last HSEM-written cap is left live on the inverter with no further supervision until Error mode clears. #840 closes that gap with a narrow, downward-only exception that is allowed to run even in Error mode.

**New module:** `custom_sensors/applier_emergency_stop.py`, following the same applier extraction pattern as `applier_caps.py` / `applier_forcible_discharge.py`, mixed into `HSEMWorkingModeSensor` via `GridChargeEmergencyStopMixin` (same mixin-extraction pattern as `PhaseChargeTransitionMixin` from Part 3, required to keep `working_mode_sensor.py` under the 30 KB/1000-line limit).

**Ownership model, not a blanket safety net.** HSEM must never touch a grid-charge cap it did not itself arm; the emergency stop only fires when HSEM believes it currently owns an armed cap:

- Ownership (`self._primary_grid_charge_owned`) is set `True` only when HSEM successfully writes and verifies a `batteries_charge_grid` recommendation while writes are safe (i.e. it piggybacks on the existing Part 2/3 write path, never a new inference).
- Ownership is cleared as soon as `primary_grid_charge_is_known_disarmed()` confirms the live telemetry itself (cap <= 0 W, working mode not Time-Of-Use, or the current TOU period not in `DEFAULT_HSEM_TOU_MODES_FORCE_CHARGE`) shows no charge is armed -- this runs every cycle via `_release_primary_grid_charge_ownership_if_safe()`, independent of degraded mode, so ownership never outlives the condition that justified it.
- An externally-armed charge (ownership `False`) is never touched by the emergency stop, even in Error mode -- HSEM only ever un-arms what it itself armed.

**Downward-only, retry-safe write.** `huawei_grid_charge_emergency_needed()` is a pure gating function: fires only when `ownership_latched` is `True` and the current recommendation is no longer `batteries_charge_grid` (or degraded mode is `Error`) and telemetry has not already gone to `primary_grid_charge_is_known_disarmed()`. `async_emergency_disable_grid_charge()` writes `0` to `cfg.huawei_solar_batteries_grid_charge_maximum_power` via the existing `async_write_and_verify()` helper -- the same verified-write primitive Part 2/3 already use, so no new write path was introduced. On a failed/unverified write, ownership is deliberately **not** cleared, so the next cycle retries the same 0 W write until it verifies or the condition resolves itself via telemetry.

**Canonical helper:** never re-derive the "is a grid charge actually armed right now" check inline -- always call `primary_grid_charge_is_known_disarmed()`. Never gate the emergency stop on anything other than `huawei_grid_charge_emergency_needed()`; it already encodes the ownership + Error-mode + telemetry precedence correctly.

Tests: `tests/test_grid_charge_emergency_stop.py` (26 tests: disarmed-telemetry detection, ownership+gating logic, `CycleApplySummary` verification helper, the write helper itself, and full mixin lifecycle including the externally-armed-is-never-touched and failed-write-retains-ownership-for-retry cases).
