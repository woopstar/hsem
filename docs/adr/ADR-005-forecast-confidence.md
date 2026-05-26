# ADR-005: Forecast Confidence

**Status:** Accepted

**Date:** 2026-05-11

**Deciders:** Project maintainers

---

## Context

The HSEM planner relies on three categories of forecast data to make decisions:

1. **PV production** — from Solcast, with per-slot estimates extending 48+ hours.
2. **House load consumption** — from weighted historical averages of recent days.
3. **Electricity prices** — from Energi Data Service, firm for today and increasingly uncertain for future days.

These forecasts are never perfect. For multi-day horizons (up to 48 hours), the confidence in future-day predictions decays significantly. Using Day+2 PV and load forecasts with the same weight as current-hour data would lead the planner to make confident decisions based on highly uncertain inputs.

Additionally, the system had no mechanism to track forecast accuracy over time — no way to answer "how wrong was our PV prediction yesterday?" This made it impossible to detect systematic bias (e.g., Solcast consistently over-forecasts by 15 %) or to feed accuracy metrics back into the planning process.

The key questions were:

- How should the planner treat future-day forecasts with lower confidence?
- Should forecast errors affect planning decisions (adaptive confidence)?
- What diagnostics should be exposed to help users understand forecast reliability?
- Should the system persist accuracy data across Home Assistant restarts?

---

## Decision

We adopt a **diagnostic-only, non-adaptive** approach to forecast confidence, with a **decayed PV weighting** for future days and a separate **forecast accuracy tracking system** that monitors PV and load errors but never feeds back into the planner.

### PV confidence decay (deterministic discounting)

Instead of a probabilistic or adaptive confidence model, PV estimates for future days are multiplied by a fixed decay factor at slot-population time. This is a **deterministic discount**, not a statistical confidence interval.

| Day offset | Decay factor | Rationale |
|---|---|---|
| 0 (today)   | 1.00          | Solcast nowcast is calibrated hourly |
| 1 (tomorrow) | 0.90         | Day-ahead PV forecasts degrade ~10 % |
| 2 (day after)| 0.80         | 48-hour PV forecasts degrade ~20 % |

The decayed PV value is used in the slot's `estimated_net_consumption` calculation:

```text
pv_decayed[t] = pv_raw[t] × decay_factor[day_offset]
```

**Prices are NOT decayed.** Spot-market prices published by EDS are typically firm by mid-day for the following day, and the user's actual tariff components are known with certainty. Decaying prices would introduce a spurious penalty against future-day planning that is not justified by actual price uncertainty.

**Load forecasts are NOT decayed.** The weighted-average load model is already a conservative estimate — it averages over multiple days, which inherently dampens volatility. Adding a confidence decay would double-count the smoothing effect.

### Forecast accuracy tracking (diagnostic, not adaptive)

A separate `ForecastTracker` system (`utils/forecast_tracker.py`) stores per-slot forecast and actual values, computes error metrics, and exposes them via a diagnostic HA sensor. Key properties:

1. **Purely diagnostic** — the tracker never modifies planner behaviour, candidate selection, or cost calculation.
2. **Ring-buffer storage** — holds at most 192 records (~48 hours of 15-min slots), automatically pruning older records.
3. **Accumulation-based actuals** — instantaneous power readings from the coordinator are converted to energy (kWh) and accumulated per slot.
4. **Idempotent finalisation** — once a slot's end time passes, the record is frozen and error metrics are computed.
5. **Reboot persistence** — serialised tracker data is stored in HA entity attributes and restored via `RestoreEntity` so long-term accuracy trends survive restarts.

### Error metrics exposed

Once a slot is finalised, these aggregates are computed across all finalised records:

| Metric | Formula | Interpretation |
|---|---|---|
| MAE (Mean Absolute Error) | `(1/n) × Σ \|forecast − actual\|` | Average absolute deviation (kWh) |
| Bias (signed error) | `(1/n) × Σ (forecast − actual)` | Systematic over/under forecast (kWh) |
| RMSE (Root Mean Squared Error) | `√((1/n) × Σ (forecast − actual)²)` | Large-error penalised (kWh) |
| MAPE (Mean Absolute % Error) | `(1/n) × Σ (\|forecast − actual\| / \|actual\|) × 100` | Relative error (%, None if all actual=0) |

### Why diagnostic-only (non-adaptive)

We deliberately chose **not** to feed accuracy metrics back into the planner for several reasons:

1. **Stability risk** — adaptive confidence weighting creates feedback loops. A bad forecast week would cause the planner to discount all future-day PV, which in turn reduces arbitrage value, which may change the user's behaviour, changing the load forecast, etc. Breaking this loop is difficult.

2. **Non-stationary errors** — forecast bias changes seasonally (summer cloud cover vs winter clear skies). An adaptive system tuned for July would be wrong in December.

3. **User transparency** — a deterministic decay factor is easy to explain ("tomorrow's PV counts 90 %"). An adaptive factor driven by a moving window of MAE is opaque and surprises users.

4. **Minimal benefit** — the planner already generates multiple candidates (solar-only, grid-charge, passive, etc.) and picks the cheapest. A small confidence adjustment on future PV would rarely change the winner, because the no-action baseline already handles PV uncertainty conservatively.

---

## Consequences

### Positive

- **Deterministic and predictable:** PV decay factors are fixed and documented. Users understand exactly how future-day forecasts are treated.
- **No hidden feedback loops:** Forecast errors never influence planning decisions, keeping the system stable and auditable.
- **Rich diagnostics:** Users can see MAE, bias, RMSE, and MAPE per PV and load, enabling them to identify systematic forecast issues (e.g., "Solcast consistently over-forecasts by 15 %") and take corrective action (e.g., adjust their Solcast configuration).
- **Reboot-safe tracking:** Accuracy data survives HA restarts without custom file I/O or database schema.
- **Testable in isolation:** `ForecastTracker` is pure Python with zero HA imports (aside from the sensor wrapper). Tests run in plain `pytest`.

### Negative

- **Forecast errors are ignored by the planner:** If Solcast consistently over-forecasts by 30 %, the planner will see optimistic PV values that never materialise. The tracker reports this error but does nothing about it. Users must manually adjust their Solcast configuration.
- **Fixed decay factors are not adaptive:** If a particular season has unusually inaccurate Day+1 forecasts, the 0.90 decay factor is still applied. An adaptive system could theoretically do better — but at the cost of stability (see above).
- **Load forecasts are not decayed:** If the weighted-average load model is systematically wrong (e.g., holiday week vs normal week), the planner may over- or under-estimate consumption. The tracker reports this but does not compensate.
- **Memory overhead:** Each slot record stores ~10 floats plus metadata. At 192 records this is ~30 KB total — negligible for HA, but non-zero.

### Mitigations

- The tracker's diagnostic data is exposed as sensor attributes, enabling users to create automations that alert on high bias (e.g., "PV bias > 20 % → send notification").
- The fixed decay factors (1.00, 0.90, 0.80) are documented and could be made configurable via options flow if user demand arises.
- The `DataQuality` object in `PlannerOutput` surfaces missing-price and missing-PV hours per future day, allowing users to diagnose data gaps separately from accuracy issues.

---

## Alternatives Considered

### A. Full Bayesian confidence model

Assign a probability distribution to every forecast and integrate over the uncertainty during optimisation.

**Rejected because:**
- Massive complexity increase: the MILP would need to handle stochastic programming or scenario trees.
- The additional computational cost (multiple scenario evaluations × MILP iterations) is not justified by the marginal benefit.
- Hard to explain and debug — a "80 % confidence PV" number is less transparent than a "0.90 multiplier."

### B. Adaptive confidence from tracker metrics

Feed the tracker's MAE/bias back into the planner, e.g., subtract bias from PV forecasts, or weigh Day+2 PV by `(1 − trailing_mape)`.

**Rejected because:**
- Feedback loops (see Stability risk above).
- Non-stationary errors — a moving window tuned for one season fails in another.
- User transparency — "why is Day+2 PV suddenly worth only 50 %?" is hard to answer.
- The `no_action` baseline already handles PV uncertainty: if PV is over-forecast, the battery simply does not charge as expected, and the no-action candidate's cost converges to the actual outcome.

### C. Probabilistic decay with Monte Carlo

Run the planner multiple times with different PV scenarios (e.g., p10/p50/p90 from Solcast) and select the plan with the best expected outcome across scenarios.

**Rejected because:**
- Solcast does not expose probabilistic forecast bands via its HA integration — only point estimates.
- Multiple scenario runs would multiply planner compute time (currently < 100 ms per run) by the scenario count, potentially exceeding HA's coordinator timeout.
- The marginal benefit over the existing candidate set + PV decay is unproven.

---

## Related

- ADR-001: Planner Extraction (forecast tracker is a pure-Python utility)
- `docs/forecast-accuracy-tracking.md` — Full technical guide
- `docs/hsem-planner-spec.md` — Multi-day planning horizon (confidence decay section)
- `docs/hsem-energy-accounting.md` — PV confidence decay formula
- `utils/forecast_tracker.py` — Implementation
- `custom_sensors/forecast_accuracy_sensor.py` — HA sensor wrapper
- Issue #373 — Forecast accuracy tracking (origin)