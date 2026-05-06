# Visual Comparison: Adaptive Filtering Approaches for Power Prediction

## Current HSEM Approach
```
┌─────────────────────────────────────────────────────────────────┐
│                  SIMPLE WEIGHTED AVERAGE                         │
├─────────────────────────────────────────────────────────────────┤
│                                                                   │
│  avg_1d (25%)  ──┐                                               │
│                  ├──→ [×weight] ──→ SUM ÷ total_weight ──→ Pred  │
│  avg_3d (30%)  ──┤                                               │
│  avg_7d (30%)  ──┤                                               │
│  avg_14d(15%) ──┘                                               │
│                                                                   │
│  ✗ Static weights (no learning)                                  │
│  ✗ No adaptation to recent changes                              │
│  ✗ Hard window boundaries                                        │
│  ✗ No uncertainty quantification                                │
│  ✓ Simple, fast                                                  │
│                                                                   │
└─────────────────────────────────────────────────────────────────┘
```

---

## Advanced Adaptive Filtering (EKF Approach)
```
┌─────────────────────────────────────────────────────────────────┐
│           EXTENDED KALMAN FILTER (EKF) APPROACH                  |
├─────────────────────────────────────────────────────────────────┤
│                                                                   |
│  Augmented State Vector:                                          |
│  ┌─────────────────────────────────────┐                         |
│  │ x = [P, α, β_w, β_s, β_ev, β_o]   │                         |
│  │      └─────────────────────────┘   │                         |
│  │   Power + 5 learned parameters     │                         |
│  └─────────────────────────────────────┘                         │
│                      ↓                                            │
│  ┌──────────────────────────────────────────┐                   │
│  │ PREDICT: Propagate state + covariance    │                   │
│  - Model power dynamics with decay        │                   |
│  │ - Apply Jacobian F for linearization     │                   │
│  │ - Add process noise Q (adaptive per mode)│                   │
│  └──────────────────────────────────────────┘                   │
│                      ↓                                            │
│  ┌──────────────────────────────────────────┐                   │
│  │ UPDATE: Correct with measurement         │                   │
│  │ - Compute innovation (meas - predict)    │                   │
│  │ - Detect anomalies (normalized innov)    │                   │
│  │ - Compute Kalman gain K                  │                   │
│  │ - Update state: x = x + K·innovation     │                   │
│  └──────────────────────────────────────────┘                   │
│                      ↓                                            │
│  Parameters learn online:                                         │
│  ✓ Adaptive decay constants (tau_fast, tau_slow)                │
│  ✓ Mode-separated learning (weather ≠ solar ≠ ev ≠ idle)       |
│  ✓ Anomaly detection & outlier resistance                       │
│  ✓ Covariance-based confidence scoring                          │
│  ✓ Uncertainty quantification for each component                │
│                                                                   │
└─────────────────────────────────────────────────────────────────┘
```

---

## Power Consumption Prediction Enhancements

### Option A: Phase 1 (Easy) - Exponential Decay Weighting
```
┌─────────────────────────────────────────────────────────────────┐
│         ADAPTIVE DECAY WEIGHTING (No Learning Yet)              |
├─────────────────────────────────────────────────────────────────┤
│                                                                   |
│  For each historical measurement:                                |
│  weight(t) = exp(-t / tau_days)                                 |
│                                                                   |
│  1 day ago:  weight=1.00 ███████████ (100%)                     |
│  2 days:     weight=0.87 █████████░ (87%)                       |
│  4 days:     weight=0.64 ███████░░░░░ (64%)                     |
│  7 days:     weight=0.37 ████░░░░░░░░░░ (37%)  ← tau value      |
│  14 days:    weight=0.13 ██░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ (13%)│
│                                                                   │
│  Prediction = Σ(consumption_i × weight_i) / Σ(weight_i)       │
│                                                                   │
│  ✓ Smooth decay (vs. hard window edges)                          │
│  ✓ Single tunable parameter (tau_days)                           │
│  ✓ Better matches real system dynamics                           │
│  ✓ Easy migration from current approach                          │
│  ✗ Still no online learning                                      │
│  ✗ No mode separation (weather, EV, solar)                      │
│  ✗ No adaptive parameter tuning                                  │
│                                                                   │
│  Expected improvement: +8-12% RMSE reduction                    │
│                                                                   │
└─────────────────────────────────────────────────────────────────┘
```

---

### Option B: Phase 2 (Medium) - Adaptive Kalman Filter
```
┌──────────────────────────────────────────────────────────────────┐
│    KALMAN FILTER FOR POWER (Learning + Adaptation)              |
├──────────────────────────────────────────────────────────────────┤
│                                                                   |
│  State Vector:                                                    |
│  ┌────────────────────────────────────┐                          |
│  │ x = [P_base, P_recent, P_drift]   │                          |
│  │       Baseline power ────────┐     │                          |
│  │       Recent variation ────┐ │     │                          |
│  │       Long-term trend ───┐ │ │     │                          |
│  └────────────────────────────────────┘                          │
│                  ↓                                                │
│  ┌──────────────────────────────────────────┐                   │
│  PREDICT (using exponential decay)       │                   |
│  - P_recent *= exp(-Δt / tau_fast)      │                   |
│  - Add drift: P_drift * Δt                │                   |
│  │ - Covariance: P ← F·P·F^T + Q          │                   │
│  └──────────────────────────────────────────┘                   │
│                  ↓                                                │
│  Actual Power: P_measured = 2.1 kW (avg)                       |
│                  ↓                                                |
│  ┌──────────────────────────────────────────┐                   |
│  │ UPDATE (innovation-based learning)      │                   |
│  │ - Innovation: y = P_measured - P_pred   │                   |
│  │ - Normalized innov: |y|/σ               │                   │
│  │ - If |innov| > 2.5σ: OUTLIER detected  │                   │
│  │ - State: x ← x + K·innovation          │                   │
│  │ - Covariance: P ← (I - K·H)·P          │                   │
│  └──────────────────────────────────────────┘                   │
│                  ↓                                                │
│  Parameters adapt online:                                        │
│  ✓ tau_fast learned from recent variance                        │
│  ✓ P_base learns baseline power                                 |
│  ✓ P_drift learns gradual seasonal changes                      |
│  ✓ Anomalies detected automatically                             │
│  ✓ Confidence score = f(covariance)                             │
│                                                                   │
│  Expected improvement: +15-25% RMSE reduction                   │
│                                                                   │
└──────────────────────────────────────────────────────────────────┘
```

---

### Option C: Phase 3 (Advanced) - Multi-Modal Decomposition
```
┌──────────────────────────────────────────────────────────────────┐
│           MULTI-COMPONENT POWER MODEL                           │
├──────────────────────────────────────────────────────────────────┤
│                                                                   │
│  Decompose power into independent components:                    │
│                                                                   │
│  P_total = P_baseline + P_recent + P_weather + P_solar + P_ev   │
│             └─────┬────┘   └─────┬──┘   └─────┬────┘   └──┬──┘   │
│                   │              │            │          │       │
│            Structural      Recent trend   Weather     Solar      EV │
│            (kW base)       (decay)        dependent   reduction charging│
│                                          (beta_w)     (beta_s)  (beta_e)│
│                                          kW/°C        kW/kW      kW     │
│                                                                   │
│  Each component has:                                             │
│  ┌─────────────────────────────────────┐                         │
│  │ • Own Kalman filter state           │                         │
│  │ • Own observability gate (mode-gated)                        │
│  │ • Own process noise schedule        │                         │
│  │ • Own measurement validation        │                         │
│  └─────────────────────────────────────┘                         │
│                                                                   │
│  Learning gates (mode-gated updates):                            │
│  ┌──────────────────────────────────────────┐                   │
│  │ P_weather only learns when:              │                   │
│  │  - Temperature differs significantly     │                   │
│  │  - Occupancy is stable (not changing)   │                   │
│  │  - No anomalies detected                 │                   │
│  │                                          │                   │
│  │ P_solar only learns when:                │                   │
│  │  - Grid feed-in/export is active         │                   │
│  │  - Solar irradiance is measurable        │                   │
│  │                                          │                   │
│  │ P_ev only learns when:                   │                   │
│  │  - EV charger is actively charging       │                   │
│  │  - Load is clearly distinguishable      │                   │
│  └──────────────────────────────────────────┘                   │
│                                                                   │
│  Benefits:                                                        │
│  ✓ Separate weather sensitivity from base load                  │
│  ✓ Detect EV charging impact precisely                          │
│  ✓ Solar self-consumption parameter isolated                    │
│  ✓ Component breakdown for explainability                       │
│  ✓ Each component learns only when observable                   │
│  ✓ Transferable across different seasons/sites                  │
│                                                                   │
│  Expected improvement: +20-35% RMSE reduction                   │
│                                                                   │
└──────────────────────────────────────────────────────────────────┘
```

---

## Key Insight: Comparing Window Models

### Current Fixed Windows vs. Exponential Decay

```
Measurement History:           Contribution to Prediction
─────────────────────           ──────────────────────────

Day 0 (today):      40 kWh     Fixed: │████████████ 25%    Decay (τ=7): │████████████████ 100%
Day 1 (1d ago):     42 kWh     Fixed: │████████████ 25%    Decay (τ=7): │█████████████░░░ 87%
Day 2:              41 kWh     Fixed: │████████████ 25%    Decay (τ=7): │███████████░░░░░░ 76%
Day 3:              45 kWh     Fixed: │████████████ 30%    Decay (τ=7): │█████████░░░░░░░░░░ 64%
Day 4:              43 kWh     Fixed: │████████████ 30%    Decay (τ=7): │████████░░░░░░░░░░░░░ 56%
Day 5:              40 kWh     Fixed: │████████████ 30%    Decay (τ=7): │███████░░░░░░░░░░░░░░░░ 48%
Day 6:              44 kWh     Fixed: │████████████ 30%    Decay (τ=7): │██████░░░░░░░░░░░░░░░░░░░ 42%
Day 7 (7d ago):     39 kWh     Fixed: │████░ 15%          Decay (τ=7): │█████░░░░░░░░░░░░░░░░░░░░░░░ 37%
Day 14 (14d ago):   38 kWh     Fixed: │████░ 15%          Decay (τ=7): │██░░░░░░░░░░░░░░░░░░░░░░░░░░░░ 13%

ISSUES WITH FIXED WINDOWS:
- Day 6-7 are in different windows despite being adjacent → weight drops from 30% to 15%
- All days within window weighted equally → loses temporal information
- Hard boundary = discontinuity at window edges
```

---

## High-Level Roadmap

| Phase | Approach | Effort | RMSE Gain | Complexity |
|-------|----------|--------|-----------|-----------|
| **Current** | Static weights | - | 0% | Low |
| **Phase 1** | Decay weighting | 2-4h | +8-12% | Low |
| **Phase 2** | Basic KF | 1-2d | +15-25% | Medium |
| **Phase 3** | Multi-modal | 2-3d | +20-35% | Medium-High |
| **Phase 4** | Full EKF | 5-7d | +25-40% | High |

---

## Implementation Priority Matrix

```
                    Effort
               Low      Medium      High
           ┌──────┬──────────┬──────────┐
           │      │          │          │
  High     │ P1 ★ │   P3 ★   │   P4 ★   │
  Impact   │      │          │          │
           ├──────┼──────────┼──────────┤
           │      │          │          │
  Medium   │      │   P2 ✓   │          │
  Impact   │      │          │          │
           ├──────┼──────────┼──────────┤
           │      │          │          │
  Low      │      │          │   P0 ✗   │
  Impact   │      │          │          │
           └──────┴──────────┴──────────┘

★ = HIGH PRIORITY (start here)
✓ = RECOMMENDED (good ROI)
✗ = AVOID (low return)

Recommendation: Start with Phase 1 (quick win), then Phase 2 (solid ROI).
```

---

## Recommended Quick Start

1. **Copy `adaptive_consumption_predictor.py`** to your project
2. **Test Phase 1** offline with your historical data
3. **Measure RMSE improvement** vs. current approach
4. **If >10% improvement**: integrate into production
5. **Then**: Plan Phase 2 for learning and confidence scoring

---

## References Decoded

### RoomMind Architecture (What to Learn From)
- **Time-dependent decay**: Shows real systems (both thermal and consumption) have exponential dynamics
- **Mode-gated learning**: Only update parameters when they're observable (not always active)
- **Covariance tracking**: Uncertainty quantification → confidence scoring
- **Anomaly detection**: Via normalized innovation (z-score) not arbitrary thresholds
- **State persistence**: Learned parameters serialized, recovered on restart
- **Online adaptation**: No batch retraining needed, parameters evolve continuously

### Why This Applies to Consumption
- **Thermal dynamics** (room heating) ↔ **Consumption dynamics** (usage patterns)
- **Temperature setpoint** ↔ **Baseline consumption**
- **Outdoor temperature input** ↔ **Weather/temperature input**
- **HVAC power** ↔ **Appliance/load power**
- **Solar gain** ↔ **Solar self-consumption**
- **Occupancy heat** ↔ **Occupancy-driven consumption**

Same filter, different domain.
