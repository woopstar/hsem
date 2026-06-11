# HSEM Price Interval Semantics and Scaling

This document explains how HSEM handles the interaction between electricity price
update intervals and planning slot widths.

---

## The problem

HSEM supports two independent interval settings:

| Setting | Values | What it controls |
|---|---|---|
| `electricity_price_update_interval` | 15, 30, or 60 minutes | How often the price source publishes records |
| `recommendation_interval_minutes` | 15 or 60 minutes | The width of each planning slot |

When these differ (most commonly: price update 60 min, slots 15 min), the price rate must
be correctly scaled so the planner always sees the full currency/kWh rate.

---

## The `price_share` conversion factor

$$
\mathrm{price\_share} = \frac{\text{Price update interval}}{\text{Slot width}}
$$

| Price update interval | Slot width | `price_share` | Effect |
|---|---|---|---|
| 60 min | 15 min | 4.0 | Price ÷ 4 stored; planner gets price × 4 back |
| 15 min | 15 min | 1.0 | No scaling |
| 30 min | 15 min | 2.0 | Price ÷ 2 stored; planner gets price × 2 back |
| 60 min | 60 min | 1.0 | No scaling |

---

## Scaling pipeline

```mermaid
flowchart TD
    A[Price source raw price P\nfull hourly currency per kWh]
    B[HourlyDataPopulator.async_populate_price_and_solcast]
    C[Recommendation slot storage\nHourlyRecommendation objects]
    D[coordinator._build_planner_input]
    E[Planner engine PricePoint\nimport_price = P]

    A --> B
    B -->|Per-slot stored value = P / price_share| C
    C --> D
    D -->|Planner input value = stored value × price_share = P| E
```

### What this is NOT

- `price_share` is **not** a VAT multiplier
- `price_share` is **not** a currency conversion
- `price_share` is **not** an energy-splitting factor (prices are rates, not energy)

---

## Price sources

HSEM is provider-agnostic. Prices are read from generic electricity price sensors:

| Config key | Purpose |
|---|---|
| `hsem_import_electricity_price_sensor` | Live import price (required) |
| `hsem_export_electricity_price_sensor` | Live export price (required) |
| `hsem_import_electricity_price_forecast_sensor` | Optional dedicated import forecast (e.g. Amber Electric) |
| `hsem_export_electricity_price_forecast_sensor` | Optional dedicated export forecast |

Supported providers include Energi Data Service, Nordpool, Amber Electric, and any
sensor that publishes hourly (or sub-hourly) price records with a `raw_today` / `raw_tomorrow`
attribute structure. The populator reads the full time-series from sensor attributes
and projects them onto the planning horizon.

---

## Invariants

For any configuration:

1. A 60-min price source value of `P` must reach the planner as `P` (not `P/4` or `P*4`)
2. A 15-min price source value of `P` must reach the planner as `P`
3. Intermediate per-slot stored values must equal `P / price_share`
4. Changing `electricity_price_update_interval` from 60 to 15 with the same
   price input must not change the price seen by the planner engine
5. Negative prices must survive the full pipeline unchanged (no absolute-value
   clipping, no zero-flooring)

---

## Multi-day price data

For horizons beyond 24 hours, prices and PV data are projected onto the shared
time-series index per calendar day:

| Field | Source | Day offset |
|---|---|---|
| Today's prices | Live price sensor attributes | `day_offset = 0` |
| Tomorrow's prices | Tomorrow sensor attributes (or same sensor) | `day_offset = 1` |
| Day+2 prices | Day+2 sensor attributes (if available) | `day_offset = 2` |

Missing future-day data is surfaced in `DataQuality` as:
- `tomorrow_price_missing_hours`
- `day2_price_missing_hours`
- `tomorrow_pv_missing_hours`
- `day2_pv_missing_hours`

Non-critical missing data triggers `Degraded` mode (writes allowed).
