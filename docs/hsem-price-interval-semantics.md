# HSEM Price Interval Semantics and EDS Scaling

This document explains how HSEM handles the interaction between Energi Data Service
(EDS) price update intervals and planning slot widths.

---

## The problem

HSEM supports two independent interval settings:

| Setting | Values | What it controls |
|---|---|---|
| `energi_data_service_update_interval` | 15 or 60 minutes | How often EDS publishes price records |
| `recommendation_interval_minutes` | 15 or 60 minutes | The width of each planning slot |

When these differ (most commonly: EDS 60 min, slots 15 min), the price rate must
be correctly scaled so the planner always sees the full currency/kWh rate.

---

## The `eds_share` conversion factor

$$ \text{eds\_share} = \frac{\text{EDS interval}}{\text{Slot width}} $$

| EDS interval | Slot width | `eds_share` | Effect |
|---|---|---|---|
| 60 min | 15 min | 4.0 | Price ÷ 4 stored; planner gets price × 4 back |
| 15 min | 15 min | 1.0 | No scaling |
| 60 min | 60 min | 1.0 | No scaling |

---

## Scaling pipeline

```
EDS raw price: P (currency/kWh, full hourly rate)
        │
        ▼
HourlyDataPopulator._async_update_hourly_field
        │
        │  Per-slot stored value = P / eds_share
        │  (each 15-min slot gets 1/4 of the hourly rate)
        ▼
Recommendation slot storage
(HourlyRecommendation objects)
        │
        ▼
coordinator._build_planner_input
        │
        │  Planner sees: (P / eds_share) * eds_share = P
        │  (exact inverse — the planner always receives the original rate)
        ▼
Planner engine (PricePoint[])
    import_price = P (full currency/kWh)
```

### What this is NOT

- `eds_share` is **not** a VAT multiplier
- `eds_share` is **not** a currency conversion
- `eds_share` is **not** an energy-splitting factor (prices are rates, not energy)

---

## Invariants

For any configuration:

1. A 60-min EDS price of `P` must reach the planner as `P` (not `P/4` or `P*4`)
2. A 15-min EDS price of `P` must reach the planner as `P`
3. Intermediate per-slot stored values must equal `P / eds_share`
4. Changing `energi_data_service_update_interval` from 60 to 15 with the same
   price input must not change the price seen by the planner engine
5. Negative prices must survive the full pipeline unchanged (no absolute-value
   clipping, no zero-flooring)

---

## Multi-day price data

For horizons beyond 24 hours, prices and PV data are projected onto the shared
time-series index per calendar day:

| Field | Source | Day offset |
|---|---|---|
| Today's prices | Live EDS sensor | `day_offset = 0` |
| Tomorrow's prices | EDS tomorrow sensor | `day_offset = 1` |
| Day+2 prices | EDS day+2 sensor | `day_offset = 2` |

Missing future-day data is surfaced in `DataQuality` as:
- `tomorrow_price_missing_hours`
- `day2_price_missing_hours`
- `tomorrow_pv_missing_hours`
- `day2_pv_missing_hours`

Non-critical missing data triggers `Degraded` mode (writes allowed).