---
name: hsem-canonical-helpers
description: Activate at the start of any code change to ensure canonical helpers and patterns are used instead of re-implementing them inline.
---

# HSEM Canonical Helpers — Use These, Never Re-Invent

Activate this skill when writing any code that may need efficiency conversion, discharge recommendations, threshold calculation, or logging. These helpers exist for a reason — **never re-implement them inline**.

## Canonical Helpers

### 1. Efficiency Conversion — `clamp_efficiency(pct)`

Location: `custom_components/hsem/utils/misc.py`

```python
from custom_components.hsem.utils.misc import clamp_efficiency

charge_eff = clamp_efficiency(charge_efficiency_pct)   # returns fraction 0.01-1.0
```

Never inline `max(min(..., 100.0), 1.0) / 100.0`.

### 2. Discharge/Charge Recommendations — `DISCHARGE_RECS`, `CHARGE_RECS`

Location: `custom_components/hsem/utils/recommendations.py`

```python
from custom_components.hsem.utils.recommendations import DISCHARGE_RECS, CHARGE_RECS

if slot.recommendation in DISCHARGE_RECS:
    ...
```

These are canonical frozensets. Never redefine them locally.

### 3. Recommended Threshold — `calculate_recommended_threshold(...)`

Location: `custom_components/hsem/utils/misc.py`

```python
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

Never use `cycle_cost * 0.30` as a proxy for the discharge threshold.

### 4. Planner Logger — `HSEM_LOGGER`

Location: `custom_components/hsem/utils/logger.py`

```python
from custom_components.hsem.utils.logger import HSEM_LOGGER
```

Use for ALL planner code. Never use `logging.getLogger(__name__)` in planner files.

When creating log statements, never use runtime string formatting — use `%` placeholders and the `extra` argument:
```python
HSEM_LOGGER.debug("Processing slot %d with price %.4f", slot_index, price)
```

### 5. Sensor Name Constants

Location: `custom_components/hsem/utils/sensornames.py`

All HA entity name constants live here. Never hardcode sensor names elsewhere.

## Canonical Patterns

### Floating-Point Comparisons

```python
# Production code — epsilon guard (NEVER == or !=)
if abs(value) > 1e-9:        # instead of: if value != 0
if abs(a - b) < 1e-9:        # instead of: if a == b

# Test code — pytest.approx()
assert result == pytest.approx(expected, rel=1e-6)
```

### Module Responsibilities (Know Where Code Lives)

| Layer | Location | Key files |
|-------|----------|-----------|
| Planner | `custom_components/hsem/planner/` | `engine.py`, `cost_function.py`, `soc_simulation.py`, `candidate_generator.py`, `candidate_selector.py`, `slot_population.py`, `charge_scheduler.py`, `discharge_scheduler.py`, `milp_optimizer.py`, `ev_planner.py` |
| ML | `custom_components/hsem/ml/` | `consumption_predictor.py`, `history_reader.py`, `populator.py` |
| Utils | `custom_components/hsem/utils/` | `recommendations.py`, `misc.py`, `sensornames.py`, `prices.py`, `huawei.py`, `logger.py`, `solar_corrector.py`, `dynamic_floor.py`, `capacity_learner.py`, `charge_rate_learner.py`, `prediction_tracker.py`, `weekday_profile.py`, `ev_mode_resolver.py` |

### Utility Function Centralization

If a utility function is used in 2+ modules, it belongs in `utils/`. Before writing any utility:
1. Check `utils/misc.py` and other `utils/*.py` modules for existing implementations
2. If found: import and reuse
3. If not found AND used 2+ times: create in utils with a public name and docstring
4. If a one-off helper used in only one module: make it private (`_function_name()`), but refactor to utils if needs grow

### MILP Variable Vector

The MILP in `milp_optimizer.py` uses **8*n** LP variables for battery-only. With EV co-optimisation, it grows to **8n + 2n·E + E** where E is the number of active EVs.

### Cycle Cost Formula

Always uses the mandatory **2x denominator**. Never change this without updating the spec and all affected tests.

### File Size Limit

Hard limit: **30 KB** per file in `planner/` and `utils/`. Check: `wc -c custom_components/hsem/planner/*.py`
