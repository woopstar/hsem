# Forecast Accuracy Tracking — Technical Guide

This document explains how HSEM tracks forecast-vs-actual accuracy for PV
production and house load predictions.  The system is purely diagnostic — it
does **not** influence planner decisions.  It was introduced in
[issue #373](https://github.com/woopstar/hsem/issues/373).

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [ForecastTracker — core data structure](#forecasttracker--core-data-structure)
4. [Error metrics](#error-metrics)
5. [Coordinator integration](#coordinator-integration)
6. [Sensor attributes](#sensor-attributes)
7. [Reboot persistence](#reboot-persistence)
8. [Tests](#tests)

---

## Overview

HSEM relies on forecasts: predicted PV production from Solcast and predicted
house load from weighted historical averages.  These forecasts are never
perfect.  The forecast accuracy tracking system:

1. **Stores** the forecasted PV and load values for every planning slot.
2. **Accumulates** actual PV and load energy from instantaneous power
   readings during each coordinator cycle.
3. **Finalises** each slot after its end time passes, computing error metrics
   (MAE, bias, RMSE, MAPE).
4. **Exposes** the aggregated metrics via a diagnostic Home Assistant sensor.
5. **Persists** the record history across HA restarts so long-term trends
   are not lost.

### What it does NOT do

- It does **not** change planner behaviour — no adaptive corrections,
  no confidence weighting, no feedback into the cost function.
- It does **not** require any new configuration options or feature flags.
- It does **not** write to the inverter or any hardware.
- It does **not** depend on Home Assistant — the core tracker is pure Python
  and fully testable with plain `pytest`.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                        Coordinator Cycle                         │
│                                                                  │
│  1. async_collect_all_states()                                    │
│     → LiveState with instantaneous power readings                │
│                                                                  │
│  2. Planner runs → PlannerOutput with slot forecasts             │
│                                                                  │
│  3. _accumulate_forecast_actuals(now, live)                      │
│     → Reads elapsed time & power from LiveState                  │
│     → compute_accumulated_energy(power, elapsed) → kWh           │
│     → Accumulates into current slot's record                     │
│     → finalise_past_records() for slots whose end time < now     │
│                                                                  │
│  4. _register_forecasts_from_planner(planner_output)              │
│     → Copies solcast_pv_estimate_kwh and                          │
│       avg_house_consumption_kwh into tracker records              │
│                                                                  │
│  5. CoordinatorData packaged and pushed to subscribers            │
│                                                                  │
│  6. HSEMForecastAccuracySensor reads tracker from coordinator     │
│     → native_value = PV MAE (kWh)                                │
│     → extra_state_attributes = all error metrics + latest slot   │
│     → _forecast_tracker_data serialised into attributes          │
└──────────────────────────────────────────────────────────────────┘
```

### File layout

| File | Responsibility |
|---|---|
| `utils/forecast_tracker.py` | Pure-Python tracker, slot records, summary, serialization |
| `custom_sensors/forecast_accuracy_sensor.py` | HA diagnostic sensor (coordinator subscriber) |
| `coordinator.py` | Integrates accumulation & forecast registration into update cycle |
| `sensor.py` | Registers the sensor entity |
| `utils/sensornames.py` | Name/unique_id/entity_id helpers |

---

## ForecastTracker — core data structure

The `ForecastTracker` class in `utils/forecast_tracker.py` is a **rolling
ring-buffer** of `ForecastSlotRecord` objects.  It has **no** Home Assistant
dependencies and can be used in isolation.

### ForecastSlotRecord

Each record captures one planning slot:

| Field | Type | Description |
|---|---|---|
| `start` | `datetime` | Timezone-aware slot start |
| `end` | `datetime` | Timezone-aware slot end |
| `forecast_pv_kwh` | `float` | Solcast PV forecast for this slot (kWh) |
| `forecast_load_kwh` | `float` | Weighted average load forecast (kWh) |
| `actual_pv_kwh` | `float` | Accumulated actual PV energy (kWh) |
| `actual_load_kwh` | `float` | Accumulated actual load energy (kWh) |
| `finalised` | `bool` | `True` after slot's end time passed and metrics computed |
| `mae_pv` | `float \| None` | Mean absolute error PV (kWh), set on finalise |
| `mae_load` | `float \| None` | Mean absolute error load (kWh), set on finalise |
| `bias_pv` | `float \| None` | Signed bias PV (kWh), set on finalise |
| `bias_load` | `float \| None` | Signed bias load (kWh), set on finalise |

Key methods:

- **`accumulate_pv(energy_kwh)`** / **`accumulate_load(energy_kwh)`** —
  Add measured energy to the accumulator.  Called multiple times per slot
  as the coordinator cycles.
- **`finalise()`** — Freezes the record and computes `mae_pv`, `mae_load`,
  `bias_pv`, `bias_load`.  Idempotent — calling a second time is a no-op.
- **`to_dict()`** / **`from_dict(data)`** — JSON-safe serialization for
  reboot persistence (see below).

### ForecastTracker

| Property / Method | Description |
|---|---|
| `records` | Copy of all slot records, oldest first |
| `summary` | Computes and returns a `ForecastErrorSummary` from finalised records |
| `get_or_create_record(start, end)` | Returns existing record or creates a new one |
| `find_record(start)` | Look up a record by slot start time |
| `finalise_record(start)` | Finalise a specific record |
| `finalise_past_records(now)` | Finalise all records whose `end <= now` |
| `set_forecasts(start, pv_kwh, load_kwh)` | Set forecast values (only if not finalised) |
| `to_dict()` / `load_from_dict(data)` | Serialize / deserialize the full record list |

The default maximum is 192 records, which covers approximately 48 hours
of 15-minute slots.  Older records are automatically pruned.

### Energy accumulation

Instantaneous power readings (Watts) are converted to energy (kWh) using:

```text
energy_kwh = power_w × (elapsed_seconds / 3600.0) / 1000.0
```

The helper function `compute_accumulated_energy(power_w, elapsed_seconds)`
handles this conversion.  Elapsed time is computed as the difference between
the current coordinator cycle timestamp and the previous cycle's timestamp,
so the accuracy depends on the coordinator update interval (default 5 minutes).

---

## Error metrics

Once a slot is finalised, the `ForecastErrorSummary` dataclass aggregates
across all finalised records:

### MAE — Mean Absolute Error

```text
MAE = (1/n) × Σ |forecast_kwh − actual_kwh|
```

Units: kWh.  Averages the absolute deviation.  Lower is better.

### Bias (signed error)

```text
Bias = (1/n) × Σ (forecast_kwh − actual_kwh)
```

Units: kWh.  Positive bias = systematic over-forecast (predicted more than
actually occurred).  Negative bias = under-forecast.  Zero bias means the
forecast is accurate on average (but may have large cancellations).

### RMSE — Root Mean Squared Error

```text
RMSE = √( (1/n) × Σ (forecast_kwh − actual_kwh)² )
```

Units: kWh.  Penalises large errors more heavily than MAE.  Useful for
detecting occasional big misses.

### MAPE — Mean Absolute Percentage Error

```text
MAPE = (1/n) × Σ ( |forecast_kwh − actual_kwh| / |actual_kwh| ) × 100
```

Units: percent.  Makes errors comparable across different power levels.
Returns `None` when all actual values are zero (division by zero guard).

### Exposure via `as_dict()`

The summary also includes:
- `window_slots` — total slots in the ring buffer (finalised + unfinalised)
- `finalised_slots` — how many slots contribute to the metrics

---

## Coordinator integration

The coordinator owns the single `_forecast_tracker: ForecastTracker`
instance, created in `__init__` with `max_slots=192`.  Two private methods
are called during each update cycle:

### `_accumulate_forecast_actuals(now, live)`

Called every cycle **after** state collection.  Steps:

1. Compute elapsed seconds since the last accumulation.
2. Find the current recommendation slot (the one whose time range contains `now`).
3. Get or create a tracker record for that slot.
4. Convert instantaneous PV and load power to energy using `compute_accumulated_energy()`.
5. Accumulate the energy into the tracker record.
6. Call `finalise_past_records(now)` to finalise any slots that have ended.

### `_register_forecasts_from_planner(output)`

Called **after** the planner runs, before the current slot is resolved.
Iterates over every slot in the `PlannerOutput` and calls
`tracker.set_forecasts(start, pv_kwh=slot.solcast_pv_estimate_kwh, load_kwh=slot.avg_house_consumption_kwh)`.

This means forecasts are only registered when the planner successfully runs.
If the planner is skipped (missing entities, force mode, consumption data not
ready), forecasts are not updated but accumulation still happens.

---

## Sensor attributes

The `HSEMForecastAccuracySensor` is a diagnostic sensor
(`EntityCategory.DIAGNOSTIC`) that subscribes to the coordinator.

### State

The sensor's `native_value` is the **PV MAE** in kWh, rounded to 3 decimal
places.  Returns `None` while no slots have been finalised yet.

### Extra state attributes

| Attribute | Source | Example |
|---|---|---|
| `window_slots` | `ForecastErrorSummary.window_slots` | `192` |
| `finalised_slots` | `ForecastErrorSummary.finalised_count` | `24` |
| `mae_pv_kwh` | `ForecastErrorSummary.mae_pv_kwh` | `0.1523` |
| `mae_load_kwh` | `ForecastErrorSummary.mae_load_kwh` | `0.0841` |
| `bias_pv_kwh` | `ForecastErrorSummary.bias_pv_kwh` | `0.0421` |
| `bias_load_kwh` | `ForecastErrorSummary.bias_load_kwh` | `-0.0112` |
| `rmse_pv_kwh` | `ForecastErrorSummary.rmse_pv_kwh` | `0.2134` |
| `rmse_load_kwh` | `ForecastErrorSummary.rmse_load_kwh` | `0.1245` |
| `mape_pv_pct` | `ForecastErrorSummary.mape_pv_pct` | `22.5` |
| `mape_load_pct` | `ForecastErrorSummary.mape_load_pct` | `8.3` |
| `latest_pv_forecast_kwh` | Latest finalised record's forecast PV | `1.25` |
| `latest_pv_actual_kwh` | Latest finalised record's actual PV | `1.18` |
| `latest_load_forecast_kwh` | Latest finalised record's forecast load | `0.65` |
| `latest_load_actual_kwh` | Latest finalised record's actual load | `0.72` |
| `latest_bias_pv_kwh` | Latest finalised record's PV bias | `0.07` |
| `latest_bias_load_kwh` | Latest finalised record's load bias | `-0.07` |
| `_forecast_tracker_data` | Serialised record list (used internally) | *(opaque dict)* |

### Template examples

```yaml
# Get PV MAE
{{ state('sensor.forecast_accuracy') }}

# Get PV bias
{{ state_attr('sensor.forecast_accuracy', 'bias_pv_kwh') }}

# Check if PV systematically over-forecasts
{{ state_attr('sensor.forecast_accuracy', 'bias_pv_kwh') > 0.1 }}

# Get load MAPE as percentage
{{ state_attr('sensor.forecast_accuracy', 'mape_load_pct') }}
```

---

## Reboot persistence

The forecast tracker data survives HA restarts using the standard
`RestoreEntity` pattern already used by other HSEM diagnostic sensors:

1. **Every cycle**, the sensor's `extra_state_attributes` includes a
   `_forecast_tracker_data` key containing the full serialised record
   list from `tracker.to_dict()`.

2. **HA's recorder** automatically stores these attributes in its database.

3. **On restart**, `async_added_to_hass` calls `async_get_last_state()`
   to retrieve the previous state, extracts `_forecast_tracker_data`, and
   passes it to `tracker.load_from_dict(data)`.

4. **After restoration**, the tracker resumes normal operation —
   accumulation continues from the current slot, any slots that ended
   during the restart window are finalised on the next cycle, and the
   summary reflects all historical data.

This means forecast accuracy trends are preserved across reboots without
any custom storage, file I/O, or database schema.

---

## Tests

All tests are in `tests/test_forecast_tracker.py`.  They use the real
`ForecastTracker` class **without** Home Assistant — plain `pytest`
against pure Python code.

### Test coverage (31 tests)

| Category | Tests | What's covered |
|---|---|---|
| `TestComputeAccumulatedEnergy` | 5 | 1000W/1h, 500W/30m, zero power, zero elapsed, negative power |
| `TestForecastSlotRecord` | 5 | Finalise metrics, exact match, accumulate, idempotent finalise |
| `TestForecastTrackerLifecycle` | 10 | Create/find records, finalise, prune, set forecasts, finalise past |
| `TestForecastTrackerSummary` | 9 | Empty, exact, over, under, mixed, MAPE div-by-zero, MAPE values, as_dict |
| `TestForecastTrackerIntegration` | 3 | Full cycle single slot, over+under pair, finalise past + summary |
| `TestForecastTrackerSerialization` | 5 | Record to_dict empty, record to_dict finalised, tracker empty, round trip, unfinalised restore |

### Running the tests

```bash
# Requires the venv with HA dependencies:
pytest tests/test_forecast_tracker.py
```

Or run the standalone tests that inline the tracker logic (no HA imports):

```bash
python -m pytest tests/test_forecast_tracker.py
```