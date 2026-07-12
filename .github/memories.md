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
| `cost_function.py` | Scores a candidate plan — source of truth for cost math |
| `soc_simulation.py` | Simulates battery SoC forward through a slot plan |
| `ev_planner.py` | EV-specific planning logic |

### ML layer (`custom_components/hsem/ml/`)

| File | Responsibility |
|---|---|
| `consumption_predictor.py` | Weighted ridge regression model with DOW + DOY + temperature features |
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

---

## MILP Variable Vector

The MILP in `milp_optimizer.py` uses **8*n** LP variables for battery-only (n = number of
future slots).  When EV co-optimisation is active (one or more `EVConfig` objects passed),
the vector grows to **8n + 2n·E + E** where E is the number of active EVs.

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
--- EV co-optimisation (when ev_configs is provided) ---
[8n .. 9n-1]     ev0_c[t]     EV0 DC-side charge per slot (kWh)
[9n .. 10n-1]    ev1_c[t]     EV1 DC-side charge per slot (kWh) (if second EV active)
[10n]            ev0_pen      EV0 deadline target slack (kWh shortfall)
[10n+1]          ev1_pen      EV1 deadline target slack (if second EV active)
```

Cycle cost is counted as `α * m[t]` — **not** `α * (ec[t] + ed[t])`.
The `m[t]` constraints are: `m[t] >= ec[t]` and `m[t] >= ed[t]`.

---

## File Size Rules

- **Hard limit: 30 KB per file** in the planner and utils layers.
- If a file exceeds 30 KB, split it before adding more features.
- Current oversized files tracked in issues: engine.py (#441), charge_scheduler.py (#442), coordinator.py (#443).

---

## Documentation Style

- Use Mermaid fenced code blocks for architecture and flow diagrams.
- Do not use ASCII/Markdown box diagrams for architecture.
- Use math equations (`$$ ... $$`) for formulas instead of plain text or code-block formulas.

---

## Cycle Cost Formula

$$
cycle\_cost\_per\_kwh = \frac{purchase\_price \times capacity\_loss\_pct / 100}{2 \times usable\_kwh \times expected\_cycles}
$$

The `2x` denominator accounts for one full round-trip (charge + discharge = 2 × usable_kwh throughput per cycle).
`capacity_loss_pct` (configurable via `hsem_batteries_capacity_loss_pct`, default 30 %) accounts for the
fraction of battery value consumed over its lifetime.
Do **not** remove or change this factor without updating `docs/planner-spec.md`.

---

## Export Price Clamping (MILP + Cost Function)

When `export_min_price > 0`, the applier physically blocks all grid export
by setting the inverter to `GRID_EXPORT_LIMIT_WATT` for any slot where
`export_price < export_min_price`.  To keep the planner consistent:

- **MILP** (`milp_optimizer.py`): `p_exp` is clamped to 0 for all slots
  where `p_exp < export_min_price` **before** the LP solves.  This prevents
  the LP from optimising around a price signal that will never be realised.
- **Cost function** (`cost_function.py`): `CostWeights.export_min_price`
  applies the same clamping in `score_plan()` so that scored costs match
  the MILP's assumptions.

Negative export prices are **not** clamped.  The LP's `curt[t]` variable
(zero objective cost) naturally handles them: when `p_exp < 0`, exporting
costs money (`−p_exp·ge` becomes a positive cost in the objective) and the
LP prefers curtailment (cost 0) over export (cost > 0).

The raw `slot.price.export_price` is **not** mutated — clamping only affects
optimisation and scoring.

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
- This must **never** be silently reverted in future refactors — it is
  a solver-stability requirement, not a cosmetic convenience.

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

## Open Refactor Issues

| Issue | Title | Status |
|---|---|---|
| #439 | Move `_DISCHARGE_RECS` to `utils/recommendations.py` | Closed |
| #440 | Extract `clamp_efficiency()` to `utils/misc.py` | Closed |
| #441 | Split `engine.py` into 3 modules | Closed |
| #442 | Split `charge_scheduler.py` into charge + discharge | Closed |
| #443 | Split `coordinator.py` into coordinator + builder | Closed |

## Open Bug Issues

| Issue | Title | Status |
|---|---|---|
| #444 | MILP cycle cost `ec+ed` vs `max(ec,ed)` | Closed |
| #445 | `_apply_soc_plan` uses `0.30` proxy threshold | Closed |
| #446 | `concentrate_discharge` greedy `break` skips viable slots | Fixed in #452 |
| #447 | Partial-SoC fractions collapse to floor at low SoC | Open
| #582 | EV charger power oscillates due to frequent MILP re-solves | Closed (reverted) |
| #630 | EV charge-past-target valued at flat 0.0001 instead of avoided-cost | Closed |

---

## Testing Rules

- Every bug fix requires a regression test.
- Every planner math change requires a unit test verifying the cost identity:
  `winner.total_cost == final_output.total_cost`
- Run `pytest tests/` before every PR.
- Run `tox -e lint` then `tox -e quality` before every commit.
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
