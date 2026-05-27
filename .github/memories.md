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
| `candidate_selector.py` | Picks the best candidate using time-discounted score |
| `charge_scheduler.py` | Assigns charge recommendations to slots |
| `discharge_scheduler.py` | Assigns discharge recommendations to slots; `concentrate_discharge_on_expensive_slots` uses **per-calendar-day** budget pools |
| `milp_optimizer.py` | Solves the MILP LP problem — variable vector is `6*n` (ec, ed, gi, ge, pv, m) |
| `cost_function.py` | Scores a candidate plan — source of truth for cost math |
| `soc_simulation.py` | Simulates battery SoC forward through a slot plan |
| `ev_planner.py` | EV-specific planning logic |

### Utils layer (`custom_components/hsem/utils/`)

| File | Responsibility |
|---|---|
| `recommendations.py` | `Recommendations` enum + canonical `DISCHARGE_RECS` and `CHARGE_RECS` frozensets |
| `misc.py` | Shared math helpers: `clamp_efficiency()`, `calculate_recommended_threshold()`, etc. |
| `sensornames.py` | All HA entity name constants — never hardcode sensor names elsewhere |
| `prices.py` | Price lookup, grid fee calculation, spot price helpers |
| `huawei.py` | Huawei Solar inverter API helpers |
| `logger.py` | `HSEM_LOGGER` — rotating file handler, `propagate=False` |

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

The MILP in `milp_optimizer.py` uses **6*n** LP variables (n = number of slots):

```
Index range    Variable   Meaning
[0 .. n-1]     ec[t]      Energy charged in slot t (kWh)
[n .. 2n-1]    ed[t]      Energy discharged in slot t (kWh)
[2n .. 3n-1]   gi[t]      Grid import in slot t (kWh)
[3n .. 4n-1]   ge[t]      Grid export in slot t (kWh)
[4n .. 5n-1]   pv[t]      PV surplus used in slot t (kWh)
[5n .. 6n-1]   m[t]       max(ec[t], ed[t]) auxiliary variable for cycle cost
```

Cycle cost is counted as `α * m[t]` — **not** `α * (ec[t] + ed[t])`.
The `m[t]` constraints are: `m[t] >= ec[t]` and `m[t] >= ed[t]`.

---

## File Size Rules

- **Hard limit: 30 KB per file** in the planner and utils layers.
- If a file exceeds 30 KB, split it before adding more features.
- Current oversized files tracked in issues: engine.py (#441), charge_scheduler.py (#442), coordinator.py (#443).

---

## Cycle Cost Formula

```
cycle_cost_per_kwh = purchase_price / (2 * usable_kwh * expected_cycles)
```

The `2x` denominator accounts for one full round-trip (charge + discharge = 2 * usable_kwh throughput per cycle).
Do **not** remove or change this factor without updating `docs/hsem-planner-spec.md`.

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
| #439 | Move `_DISCHARGE_RECS` to `utils/recommendations.py` | Open |
| #440 | Extract `clamp_efficiency()` to `utils/misc.py` | Open |
| #441 | Split `engine.py` into 3 modules | Open |
| #442 | Split `charge_scheduler.py` into charge + discharge | Open |
| #443 | Split `coordinator.py` into coordinator + builder | Open |

## Open Bug Issues

| Issue | Title | Status |
|---|---|---|
| #444 | MILP cycle cost `ec+ed` vs `max(ec,ed)` | Open |
| #445 | `_apply_soc_plan` uses `0.30` proxy threshold | Open |
| #446 | `concentrate_discharge` greedy `break` skips viable slots | Fixed in #452 |
| #447 | Partial-SoC fractions collapse to floor at low SoC | Open |

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
