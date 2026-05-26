# ADR-003: Cost Scoring Architecture

**Status:** Accepted

**Date:** 2026-05-11

**Deciders:** Project maintainers

---

## Context

The HSEM planner needs to evaluate and compare candidate battery charge/discharge plans. The evaluation function must achieve two goals:

1. **Financial accuracy** — produce an auditable cost figure that corresponds to real money (what the user will actually pay or save).
2. **Plan selection** — provide a ranking metric that the candidate selector can use to pick the best plan, including trade-offs that have no direct monetary value (e.g., battery wear, safety constraints, future opportunity cost).

A naive single-aggregate approach (one number for everything) conflates these two purposes. A selector score that mixes real costs with synthetic penalties makes the "total cost" reported to the user un-auditable and misleading. Conversely, a pure monetary cost cannot express soft constraints like "don't drain the battery to zero just because the horizon is short."

We needed an architecture that cleanly separates financial reporting from plan selection without duplicating calculation logic.

---

## Decision

We split the cost function into **two distinct aggregates** returned for every candidate plan:

### 1. `total_cost` — real-money terms only

```
total_cost = grid_import_cost − export_revenue + cycle_cost + conversion_loss_cost + tariff_cost
```

- Every term in `total_cost` corresponds to a real monetary flow.
- **No synthetic penalties** enter this aggregate.
- Suitable for auditing, bill comparison, and user-facing display.
- The value is comparable to the user's actual electricity bill for the horizon period.

### 2. `score` — selector objective

```
score = total_cost + soc_penalties + grid_limit_penalty + override_penalty + terminal_soc_value
```

- Starts from `total_cost` (always includes real money).
- Adds **synthetic penalties** (quadratic SoC guard, grid power limit, override cost).
- Adds the **terminal SoC opportunity cost** — a value representing the lost future benefit of stored energy consumed during the horizon.
- The selector always picks the candidate with the **lowest** `score`, not the lowest `total_cost`.

### Why two aggregates instead of a single weighted sum

| Concern | `total_cost` | `score` |
|---|---|---|
| Auditable money | ✅ Yes | ✅ (as subset) |
| Avoids drain-to-zero bias | ❌ No | ✅ (via terminal SoC value) |
| Avoids SoC bound violations | ❌ No | ✅ (via quadratic guard) |
| Picks cheapest plan | ✅ If penalties=0 | ✅ Always |

A single number cannot serve both purposes without one of them being wrong.

### Terminal SoC value formulation

Terminal SoC value uses:

```
terminal_soc_value = (E_initial − E_final) × p_replacement
```

Where `p_replacement` = **minimum** future import price across the horizon.

Using the minimum price (not the average) prevents over-valuing stored energy during peak-price periods, which would bias the selector against discharging at the most profitable time.

### Quadratic SoC guard

```python
penalty = weight * (soc - bound)**2  # if soc outside [min, max]
```

Quadratic form heavily penalises large violations while tolerating tiny numerical rounding errors.

### Past-slot exclusion

Slots marked `time_passed` are excluded from SoC penalty calculation because the SoC simulator writes `estimated_battery_soc = 0.0` as a sentinel, which would generate a false penalty of `weight * min_soc²` per past slot — identical across all candidates but log-misleading.

---

## Consequences

### Positive

- `total_cost` is auditable: every term maps to a real money flow. Users can compare it to their electricity bill.
- The selector can express preferences that have no monetary value (e.g., "don't violate SoC bounds") without corrupting the financial aggregate.
- Clear separation of concerns: cost function returns two numbers; the selector uses one, diagnostics expose both.
- Adding a new penalty (e.g., carbon intensity) adds it to `score` only, leaving `total_cost` untouched.

### Negative

- Callers must be aware of which aggregate to use. The wrong choice (using `total_cost` for selection, or `score` for billing) produces incorrect results.
- Slightly more complex API surface: every evaluation returns two floats instead of one.
- The terminal-SoC opportunity cost is a synthetic value — it is not money the user will actually pay or receive, but represents a lower-bound estimate of future import cost.

### Trade-offs considered

- **Single weighted-sum approach** was rejected because it conflates monetary and non-monetary terms, making the "total cost" neither auditable nor a pure selector score.
- **Lexicographic ordering** (first minimise cost, then minimise penalties) was rejected because it cannot express trade-offs between cost and safety (e.g., paying slightly more to avoid draining the battery).
- **Post-selection re-costing** (compute pure cost after picking by score) was rejected because it introduces a possible mismatch between the selected plan and the reported cost, violating the `winner.cost == final_output.cost` invariant.

### Invariant

For every planner run:

- `winner.score == final_output.score` (no post-selection mutation)
- `winner.total_cost == final_output.total_cost`
- `score >= total_cost` always (penalties are non-negative, terminal value can be positive or negative)
- When all penalties are zero and terminal-SoC value is zero: `score == total_cost`
