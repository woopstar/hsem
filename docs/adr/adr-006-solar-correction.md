# ADR-006: Solar Forecast Auto-Correction

**Status:** Accepted

**Date:** 2026-06-26

**Deciders:** Project maintainers

---

## Context

ADR-005 established a **diagnostic-only, non-adaptive** approach to forecast
confidence.  PV forecast errors were tracked and reported but never fed back
into the planner.  The rationale was stability: adaptive confidence creates
feedback loops, and non-stationary errors mean a system tuned for one season
fails in another.

Real-world operation of HSEM revealed a problem with this approach.  Solcast
PV forecasts exhibit **systematic per-hour bias patterns** — for example,
consistently over-forecasting morning production by 15 % while under-forecasting
midday production by 5 %.  The tracker reported these biases but offered no
mechanism to compensate for them.  Users with persistent forecast errors saw
the planner make suboptimal decisions based on systematically wrong PV values,
with no automated correction path.

The key questions were:

- Can we apply adaptive PV correction without introducing instability?
- How do we prevent the correction from over-fitting to short-term noise?
- Should the correction use the same data as the diagnostic tracker?

---

## Decision

We introduce **adaptive per-hour PV forecast correction** via the
`SolarForecastCorrector` (`utils/solar_corrector.py`).  The corrector learns
from historical actual-vs-forecast PV ratios and applies corrections at
slot-population time — **before** the planner runs.  Raw Solcast data is
never mutated.

This partially supersedes ADR-005's "non-adaptive" stance for PV data.
Load forecasts and price data remain non-adaptive, and the diagnostic
tracker infrastructure is unchanged.

### Per-hour accuracy factors (4-day rolling window)

For each clock-hour (0–23), the corrector maintains a rolling window of the
last 4 days of **actual / forecast** PV ratios for that hour.  The correction
factor is the mean ratio, clamped to **[0.3, 1.5]** to prevent extreme values
from a single bad forecast day.

```
factor[h] = clamp(mean(actual_pv / forecast_pv for last 4 days at hour h), 0.3, 1.5)
corrected_pv[t] = raw_pv[t] × factor[hour_of(t)]
```

A 4-day window was chosen as a compromise: short enough to adapt to seasonal
changes, long enough to filter out single-day noise.  Each hour is learned
independently, so the corrector captures intra-day pattern differences
(e.g., morning fog vs. afternoon clear skies).

### Intra-hour residual correction (2h linear decay)

In addition to the per-hour factor, a **residual correction** is applied to
the current and next few slots.  The residual is the mean of the last 4
**closed** slots' actual/forecast ratios.  It decays linearly to 1.0 over
2 hours (8 slots at 15-min granularity).

```
residual = mean(actual/forecast for last 4 closed slots)
decay[t] = 1.0 + (residual - 1.0) × max(0, 1 - elapsed_slots / 8)
final_pv[t] = corrected_pv[t] × decay[t]
```

This handles short-term weather transitions — if clouds rolled in 30 minutes
ago and PV is under-performing, the next few slots are adjusted downward
before the per-hour factor catches up.

### Configurable confidence percentile

A user-configurable **solar confidence** percentile (default 0.50, range
0.10–0.90) scales how aggressively the correction is applied.  At 0.10
(pessimistic), only the bottom 10 % of historical ratios inform the factor.
At 0.90 (optimistic), the top 90 % are used.  At 0.50 (median), the
correction is neutral.

This is exposed via `sensor.hsem_solar_confidence` (a `number` entity) so
users can tune it from the dashboard without restarting.

### Integration point: slot population

Corrections are applied in `planner/slot_population.py` → `populate_solcast()`
**before** the planner runs.  The raw Solcast values from the HA sensor are
never modified — only the per-slot copies in `PlannedSlot` receive the
correction.  This keeps the data pipeline auditable: the original forecast
is always available for comparison.

### Why this avoids the ADR-005 stability concerns

| ADR-005 concern | How ADR-006 addresses it |
|---|---|
| Feedback loops | The correction is applied once per slot-population, before the planner. It does not depend on planner output — only on observed actuals vs forecasts. No loop. |
| Non-stationary errors | The 4-day rolling window adapts to seasonal changes. The [0.3, 1.5] clamp prevents a single bad week from poisoning the factors. |
| User transparency | The per-hour factors and residual are exposed as sensor attributes. The confidence percentile is user-configurable. The corrected PV is logged alongside the raw value. |
| Minimal benefit | The correction is demonstrably beneficial: systematic per-hour bias of ±15 % over a full day translates to ±2–3 kWh of PV energy, which is enough to change the planner's charge/discharge decision. |

---

## Consequences

### Positive

- **Systematic PV bias is compensated automatically:** A Solcast over-forecast
  of 3 kWh/day no longer causes the planner to over-allocate solar surplus.
- **Per-hour granularity captures intra-day patterns:** Morning fog bias is
  corrected independently from midday clear-sky bias.
- **Short-term weather transitions are handled:** The residual correction
  adjusts within 30 minutes of a cloud event.
- **User-tunable:** The confidence percentile lets risk-averse users plan
  conservatively and risk-tolerant users plan optimistically.
- **No raw data mutation:** Solcast API values are preserved for audit.
- **No new dependencies:** Pure Python, zero additional HA imports.

### Negative

- **Added complexity:** The planner pipeline now has an additional correction
  step that must be understood when debugging PV-related planning issues.
- **Cold start:** After a HA restart, the corrector has no history. It takes
  4 days to build full per-hour factors. During this period, only the residual
  correction is active (which itself needs 4 closed slots to initialise).
- **Confidence percentile interaction:** At extremes (0.10 or 0.90), the
  correction can be aggressive. Users who set the percentile too low may see
  the planner ignore real PV surplus.
- **Divergence from ADR-005:** The original "non-adaptive" principle is now
  qualified. Future contributors must understand that PV correction is adaptive
  while load and price treatment remain fixed.

### Mitigations

- Cold start: The corrector defaults to factor 1.0 (no correction) for hours
  with no history. The planner behaves identically to the pre-correction
  behaviour until data accumulates.
- Confidence tuning: The default 0.50 (median) is neutral and safe. Extreme
  values are documented with warnings.
- Debug logging: Corrected vs raw PV is logged at debug level for every slot.
- The diagnostic `ForecastTracker` continues to track **raw** forecast errors,
  providing an independent check on whether the correction is improving accuracy.

---

## Alternatives Considered

### A. Keep non-adaptive (status quo from ADR-005)

Continue reporting forecast errors via the tracker but never correct them.

**Rejected because:** Systematic per-hour bias is a real, measurable problem.
Users with a consistent 15 % Solcast over-forecast see the planner make
suboptimal export and charge decisions every day.  "Report but don't fix"
is insufficient when the fix is straightforward and low-risk.

### B. Full machine-learning correction model

Train a model (e.g., gradient boosting) on weather features + historical
forecast errors to predict per-slot correction factors.

**Rejected because:**
- Massive complexity increase for marginal gain over the per-hour rolling mean.
- Requires weather data integration (cloud cover, irradiance) that HSEM does
  not currently collect.
- Training overhead and model persistence add infrastructure burden.

### C. Apply correction inside the MILP objective

Rather than pre-correcting PV values, add a correction term to the MILP
objective function.

**Rejected because:**
- Increases MILP variable count and solve time.
- Makes the correction opaque — the MILP output no longer reflects the
  physical PV forecast.
- Harder to audit: "Why did the MILP choose this plan?" is harder to answer
  when correction is inside the black box.

---

## Related

- ADR-005: Forecast Confidence (partially superseded by this ADR)
- `docs/forecast-accuracy-tracking.md` — Forecast accuracy technical guide
- `docs/planner-spec.md` — Solar correction invariant (§ Multi-day planning horizon)
- `docs/planner-guide.md` — Solar forecast auto-correction (§ Planning inputs → PV forecast)
- `utils/solar_corrector.py` — Implementation
- `planner/slot_population.py` — `populate_solcast()` integration point
- Issue #602 — Solar forecast accuracy auto-correction
