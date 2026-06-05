# HSEM Consumption Prediction

This document explains how HSEM predicts house load (consumption) for the
planning horizon — the weighted-average model, outlier detection, spike
suppression, and reliability weighting.

---

## Overview

HSEM predicts house load using a **multi-window weighted average** of historical
consumption data. The prediction feeds the planner's net consumption calculation:

$$ net\\_consumption[t] = load\\_forecast[t] + ev\\_load[t] - pv\\_forecast[t] $$

Accurate load prediction is critical: over-prediction leads to unnecessary grid
imports; under-prediction leads to insufficient battery charging for peak hours.

HSEM supports two prediction modes, toggled via ``hsem_ml_consumption_enabled``:

- **Legacy** (default): Four-window weighted average using HSEM custom sensors
- **ML** (new): Ridge regression on recorder history with DOW + seasonality + temperature

---

## ML mode (ridge regression)

When enabled, the ML predictor queries the HA recorder directly for historical
energy data from the configured grid import/export sensors.  No custom sensor
entities are required.

### Model

Weighted ridge regression on one-hot (DOW × slot) features with continuous
features for day-of-year seasonality and optional outdoor temperature:

- **672 categorical features**: one per (day_of_week, 15-min slot)
- **2 seasonal features**: sin/cos of day-of-year
- **1 temperature feature**: outdoor ambient temperature in °C (optional)
- **1 lag feature**: previous slot's energy (optional, sequential mode)

Time-decay sample weights ``w = exp(-age / decay_days)`` give more influence
to recent data.  L2 regularization (``α = 1.0``) handles data sparsity
automatically — no hard fallback thresholds needed.

### Sequential prediction (lag feature)

When the ``ML sequential prediction`` switch is enabled, the model adds a
**previous-slot energy** feature.  During training, each observation includes
the actual energy from the chronologically previous slot.  During prediction,
slots are predicted in order: slot 0's output feeds as lag input to slot 1,
and so on.  This captures intra-day momentum — a morning cooking spike at
08:00 naturally elevates 08:15's prediction.

Disabled by default.  Toggle via the switch entity on the HSEM device page.

### Adaptive safety buffer

Each slot gets a per-slot safety margin based on its prediction uncertainty:

- **σ/μ < 0.1**: no buffer (prediction is reliable)
- **σ/μ < 0.3**: 0.5σ buffer (moderate uncertainty)
- **σ/μ ≥ 0.3**: 1.0σ buffer (sparse or variable data)

The MILP sees ``mean + safety_factor × σ``, naturally building headroom in
uncertain slots.  As more history accumulates, the buffer shrinks automatically.

### Today's actuals

For slots that have already passed today, the predictor uses actual meter
readings from the energy sensor instead of predictions.  This anchors
the battery SoC simulation to reality.

### Advantages over legacy mode

- **15-min resolution**: matches Nord Pool spot market
- **Day-of-week awareness**: Monday ≠ Saturday
- **Seasonality**: winter mornings get higher predictions than summer
- **Temperature**: cold/hot outdoor temps → higher heating/cooling load
- **No custom sensors**: reads directly from recorder database

---

## Legacy mode (weighted average)

Four overlapping historical windows are maintained per clock-hour (0–23):

| Window | Span | Default weight | Purpose |
|---|---|---|---|
| **1-day** | Last 24 hours | 25 % | Captures yesterday's pattern (weather, routine) |
| **3-day** | Last 72 hours | 30 % | Short-term trend (weekday pattern) |
| **7-day** | Last 168 hours | 30 % | Weekly rhythm (same weekday last week) |
| **14-day** | Last 336 hours | 15 % | Long-term baseline (weather-independent) |

Default weights sum to 100 %. Configurable via the options flow.

### Hourly averages

Each `HourlyConsumptionAverage` carries per-window averages for one clock-hour:

```python
@dataclass
class HourlyConsumptionAverage:
    hour: int           # 0-23
    avg_1d: float       # kWh average over the last 24 h for this hour
    avg_3d: float       # kWh average over the last 72 h
    avg_7d: float       # kWh average over the last 168 h
    avg_14d: float      # kWh average over the last 336 h
    day_offset: int     # 0 = today, 1 = tomorrow, ...
```

### Forecast computation

The raw forecast for hour `h` is:

$$ \mathrm{forecast}[h] = \frac{w_1 \cdot avg_1 + w_3 \cdot avg_3 + w_7 \cdot avg_7 + w_{14} \cdot avg_{14}}{w_1 + w_3 + w_7 + w_{14}} $$

Before this weighted average, the weights undergo three transformations:

1. **IQR outlier detection** — flag anomalous windows
2. **Spike detection and redistribution** — suppress sudden jumps
3. **Reliability weighting** — down-weight windows that disagree

---

## IQR outlier detection (issue #301)

Replaces the old ratio-based spike detection with the standard Tukey fence.

### Method

For each clock-hour, the four window values form a set of four data points.
The interquartile range (IQR) is computed, and values outside

$$ [Q_1 - k \cdot \mathrm{IQR}, Q_3 + k \cdot \mathrm{IQR}] $$

are flagged as outliers, where $k = 1.5$ (standard Tukey fence).

### Weight redistribution

When a window is flagged as an outlier, its weight is redistributed to the
remaining non-outlier windows **proportionally**. If ALL windows are outliers
(degenerate case), no redistribution occurs — all weights are kept unchanged.

---

## Spike detection caps

Even after IQR filtering, the planner applies additional capping to prevent
short-term spikes from dominating the forecast.

### Caps between 7-day and 14-day

| Cap | Value | Meaning |
|---|---|---|
| `CAP7_DOWN` | 0.85 | 7-day avg cannot be < 85 % of 14-day avg |
| `CAP7_UP` | 1.15 | 7-day avg cannot be > 115 % of 14-day avg |
| `CAP14_DOWN` | 0.90 | 14-day avg cannot be < 90 % of 7-day effective avg |
| `CAP14_UP` | 1.10 | 14-day avg cannot be > 110 % of 7-day effective avg |

### Spike detection (ratio-based)

When a short window is significantly higher than a longer window, it is
flagged as a spike:

| Comparison | Ratio range | Max weight reduction | Redistribution |
|---|---|---|---|
| 1d vs 7d | 1.30 – 2.00 | 50 % of 1d weight | 20 % → 3d, 55 % → 7d, 25 % → 14d |
| 3d vs 7d | 1.20 – 1.80 | 30 % of 3d weight | 60 % → 7d, 40 % → 14d |
| 7d vs 14d | 1.20 – 1.60 | 20 % of 7d weight | 100 % → 14d |
| 14d vs 7d | 1.15 – 1.50 | 15 % of 14d weight | 100 % → 7d |

**Severity scaling:** The fraction of weight actually removed interpolates
between 0 at the `_MIN` ratio and the maximum at the `_MAX` ratio:

$$ reduced\\_fraction = \frac{\mathrm{ratio} - ratio\\_min}{ratio\\_max - ratio\\_min} \cdot max\\_reduction $$

### Baseline capping

Short windows (1-day, 3-day) are also capped against a blended baseline:

$$ \mathrm{baseline} = 0.70 \cdot avg_7 + 0.30 \cdot avg_{14} $$

$$ capped\\_value = \mathrm{clamp}(value, 0.80 \cdot \mathrm{baseline}, 1.20 \cdot \mathrm{baseline}) $$

The 3-day uses slightly looser bounds (0.85 – 1.15) to avoid removing legitimate
multi-day trends.

---

## Reliability weighting

After spike suppression, each window's weight is further scaled by its
agreement with the other windows:

$$ w_i' = w_i \cdot \frac{1}{\epsilon + |avg_i - \mathrm{median}|} $$

Where $\epsilon = 0.05$ kWh (prevents division by zero and over-sensitivity).

The scale strength is configurable via `RELIABILITY_SCALE_STRENGTH` (default 1.0).
Setting it to 0 disables reliability weighting entirely.

Weights are normalised after scaling so they still sum to the original total.

---

## Assumptions and limitations

1. **Stationarity**: The model assumes consumption patterns are relatively stable
   over the 14-day window. Major lifestyle changes (new EV, heat pump, home
   renovation) require 14 days to be fully reflected.

2. **Weather dependence**: The model has no weather inputs. Weather-driven
   consumption (AC, heating) appears as unexplained variance unless correlated
   with the same-day-previous-week pattern (7-day window).

3. **No day-of-week distinction**: All windows are rolling and do not distinguish
   weekdays from weekends. A Monday forecast uses the same weights as a Saturday
   forecast, relying on the 7-day window to capture the weekly rhythm.

4. **Zero-consumption hours**: Hours with consistently zero consumption (e.g.
   night-time) produce zero forecasts, which is correct for most installations.

5. **Outlier detection limitations**: With only four data points (1d, 3d, 7d, 14d)
   per hour, the IQR method has limited statistical power. The spike caps act as
   a second line of defence.

---

## Future improvements (Phase 1+)

See `docs/hsem-adaptive-consumption-predictor.md` for the planned evolution:

- **Phase 1**: Exponential decay weighting (temporal discounting)
- **Phase 2**: Kalman filter (EKF-based adaptive model)
- **Phase 3**: Multi-modal decomposition (weather, solar, EV impacts)
