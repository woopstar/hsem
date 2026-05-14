# HSEM Planner — Technical Guide

This guide explains how the HSEM (Home Assistant Solar Energy Management) planner works.
It covers inputs, outputs, the cost function, safety modes, and worked examples for five
common scenarios a real installation will encounter.

> **See also:** `docs/hsem-planner-spec.md` — the normative specification that governs
> all planner invariants and implementation rules.

---

## Table of contents

1. [Overview](#overview)
2. [Planning inputs](#planning-inputs)
3. [Planning outputs](#planning-outputs)
4. [Cost function](#cost-function)
5. [Candidate generation and selection](#candidate-generation-and-selection)
6. [Safety modes](#safety-modes)
7. [Data quality diagnostics](#data-quality-diagnostics)
8. [Scenario examples](#scenario-examples)
   - [Winter day — cold, low PV, peak-hour pricing](#scenario-1-winter-day)
   - [Summer day — high PV surplus](#scenario-2-summer-day-high-pv)
   - [Cheap night price — grid charge opportunity](#scenario-3-cheap-night-price)
   - [High PV day — excess export opportunity](#scenario-4-high-pv-day-excess-export)
   - [Flat price day — no arbitrage value](#scenario-5-flat-price-day)
9. [Reading the plan explanation](#reading-the-plan-explanation)
10. [Known limitations](#known-limitations)

---

## Overview

The HSEM planner is a forward-looking, cost-minimising battery scheduler.
Every time the coordinator runs (typically every 15 minutes) the planner:

1. Reads the current battery state, electricity prices, and PV forecast.
2. Generates a time grid of **slots** covering the planning horizon (24, 48, or 72 hours).
3. Populates each slot with expected house load, PV production, and prices.
4. Evaluates several candidate strategies (charge from grid, discharge only, solar only, etc.).
5. Scores every candidate with the cost function.
6. Writes the lowest-cost valid plan to the `HourlyRecommendation` objects consumed by the coordinator.

The planner is **pure Python with no Home Assistant imports**. It runs synchronously,
produces deterministic output for identical input, and is fully testable with plain pytest.

---

## Planning inputs

All inputs are collected in the `PlannerInput` dataclass
(`custom_components/hsem/models/planner_inputs.py`).

### Temporal context

| Field | Type | Description |
|---|---|---|
| `now_iso` | `str` | ISO-8601 timezone-aware timestamp of the planning moment (e.g. `"2024-06-15T14:00:00+02:00"`) |
| `interval_minutes` | `int` | Slot width in minutes — `15` or `60` |
| `interval_length_hours` | `int` | Planning horizon length — `24`, `48`, or `72` hours |

The total number of slots generated is `(interval_length_hours * 60) // interval_minutes`.

| Horizon | 15-min slots | 60-min slots |
|---|---|---|
| 24 h | 96 | 24 |
| 48 h | 192 | 48 |
| 72 h | 288 | 72 |

### Battery hardware

| Field | Type | Description |
|---|---|---|
| `battery_soc_pct` | `float` | Current SoC percentage (0–100) |
| `battery_rated_capacity_kwh` | `float` | Nameplate capacity in kWh |
| `battery_end_of_discharge_soc_pct` | `float` | Minimum allowed SoC floor (%) |
| `battery_max_soc_pct` | `float` | Maximum allowed SoC ceiling (%, default 100) |
| `battery_max_charge_power_w` | `float` | Maximum charge power in Watts |
| `battery_max_discharge_power_w` | `float \| None` | Maximum discharge power in Watts (`None` = unlimited) |
| `battery_conversion_loss_pct` | `float` | Round-trip conversion loss (%) |

The planner converts power limits to per-slot energy limits internally:

```text
max_charge_per_slot_kwh = battery_max_charge_power_w / 1000 * (interval_minutes / 60)
```

### Battery economics

| Field | Type | Description |
|---|---|---|
| `battery_purchase_price` | `float` | Purchase price of the battery (local currency) |
| `battery_expected_cycles` | `int` | Expected total lifetime cycles |
| `battery_cycle_cost_per_kwh` | `float` | Explicit depreciation cost per kWh cycled |

When `battery_cycle_cost_per_kwh` is `0.0`, the planner auto-derives cycle cost from
purchase price, rated capacity, and expected cycles:

```text
cycle_cost_per_kwh = purchase_price / (rated_capacity_kwh × expected_cycles)
```

### Consumption prediction

HSEM predicts house load using a weighted average of historical consumption data.

| Field | Type | Description |
|---|---|---|
| `consumption_averages` | `list[HourlyConsumptionAverage]` | Per-hour (0–23) historical averages |
| `weight_1d` | `int` | Weight for the 1-day average (integer %) |
| `weight_3d` | `int` | Weight for the 3-day average (integer %) |
| `weight_7d` | `int` | Weight for the 7-day average (integer %) |
| `weight_14d` | `int` | Weight for the 14-day average (integer %) |

Weights must sum to 100. Default split: 1d=25 %, 3d=30 %, 7d=30 %, 14d=15 %.

Each `HourlyConsumptionAverage` carries:

- `hour` — 0-based clock-hour (0–23)
- `avg_1d`, `avg_3d`, `avg_7d`, `avg_14d` — average kWh for that hour over each window

The planner applies a spike-aware blending algorithm that down-weights historical
outliers before combining the averages.

### Price data

| Field | Type | Description |
|---|---|---|
| `price_points` | `list[PricePoint]` | Hourly import/export prices |

Each `PricePoint` carries:

- `hour` — 0-based clock-hour
- `import_price` — cost to buy 1 kWh from the grid (local currency/kWh)
- `export_price` — revenue from selling 1 kWh to the grid (local currency/kWh)

Prices sourced from Energi Data Service (EDS) are normalised through the
`eds_share` pipeline before reaching the planner so the engine always receives
the full hourly rate, regardless of the EDS update interval (15 min or 60 min).
See [Price interval semantics](hsem-planner-spec.md#price-interval-semantics) in the spec.

### PV forecast

| Field | Type | Description |
|---|---|---|
| `solcast_slots` | `list[SolcastSlot]` | Forecast PV production per hour |

Each `SolcastSlot` carries:

- `hour` — 0-based clock-hour
- `pv_estimate` — expected PV energy (kWh) for that hour

For multi-day horizons, a **confidence decay** factor is applied to PV estimates
for future days to account for forecast uncertainty:

| Day offset | Decay | Meaning |
|---|---|---|
| 0 (today) | 1.00 | No decay |
| 1 (tomorrow) | 0.90 | 10 % conservative discount |
| 2 (day after) | 0.80 | 20 % conservative discount |

Prices are **not** decayed because spot-market prices are typically firm by mid-day.

### Schedule windows

| Field | Type | Description |
|---|---|---|
| `battery_schedules` | `list[BatteryScheduleInput]` | Up to three charge/discharge windows |

Each `BatteryScheduleInput` defines:

- `enabled` — whether this window is active
- `start` / `end` — wall-clock time range (`datetime.time`)
- `min_price_difference` — minimum import/export spread required to activate (local currency/kWh)

HSEM charges the battery **before** a discharge window so it is full when high prices arrive.
The pre-charge window ends at `schedule.start` and is sized to fill the battery from current SoC.

### Excess export and grid controls

| Field | Default | Description |
|---|---|---|
| `excess_export_enabled` | `False` | Enable forced battery → grid export during high-price slots |
| `excess_export_discharge_buffer_pct` | `10.0` | Safety SoC buffer kept before forced export |
| `excess_export_price_threshold` | `0.10` | Minimum export price to trigger forced export (local currency/kWh) |
| `export_min_price` | `0.0` | Below this export price the inverter throttles export to zero |

### Seasonal configuration

| Field | Default | Description |
|---|---|---|
| `months_winter` | `[1,2,3,4,10,11,12]` | Months classified as winter |
| `house_power_includes_ev` | `True` | Whether the house consumption sensor already includes EV charger power |

---

## Planning outputs

All outputs are collected in the `PlannerOutput` dataclass
(`custom_components/hsem/models/planner_outputs.py`).

### Per-slot decisions (`slots`)

Each `PlannedSlot` in the output list covers one time interval and carries:

| Field | Unit | Description |
|---|---|---|
| `start` / `end` | datetime | Slot boundaries (timezone-aware) |
| `price.import_price` | currency/kWh | Import price for this slot |
| `price.export_price` | currency/kWh | Export price for this slot |
| `solcast_pv_estimate` | kWh | Forecast PV production |
| `avg_house_consumption` | kWh | Predicted house load (weighted average) |
| `estimated_net_consumption` | kWh | `avg_house_consumption − solcast_pv_estimate` (negative = PV surplus) |
| `batteries_charged` | kWh | Energy scheduled to be stored (after losses) |
| `batteries_discharged` | kWh | Energy drawn from battery |
| `grid_import_kwh` | kWh | Grid import this slot |
| `grid_export_kwh` | kWh | Grid export this slot |
| `estimated_battery_soc` | % | Estimated SoC at end of slot |
| `estimated_battery_capacity` | kWh | Usable remaining capacity at end of slot |
| `estimated_cost` | currency | Net grid cost this slot (positive = import, negative = export) |
| `recommendation` | string | The action chosen for this slot (see below) |

#### Recommendation values

| Value | Meaning |
|---|---|
| `batteries_charge_grid` | Charge battery from grid (forced by schedule or price signal) |
| `batteries_charge_solar` | Battery is charging from PV surplus |
| `batteries_discharge_mode` | Battery discharges to cover house load during high-price window |
| `force_batteries_discharge` | Forced discharge (excess export to grid) |
| `batteries_wait_mode` | Battery idle — neither charging nor discharging |
| `time_passed` | Slot is in the past — no recommendation applied |
| `ev_smart_charging` | EV smart charging active (EV-specific scheduling) |
| `missing_input_entities` | Required HA entities were unavailable when this slot was scheduled |

### Charge and discharge windows (`charge_windows`, `discharge_windows`)

Higher-level groupings of consecutive slots with the same charge or discharge recommendation:

- `ChargeWindow` — `start`, `end`, `total_energy_kwh`, `avg_import_price`, `recommendation`
- `DischargeWindow` — `start`, `end`, `total_energy_kwh`, `avg_export_price`, `recommendation`

### Plan metadata

| Field | Description |
|---|---|
| `plan_cost` | Total estimated grid cost for the selected plan (local currency) |
| `missing_inputs` | List of diagnostic labels for absent input data |
| `warnings` | Human-readable warning messages about data quality or configuration |
| `data_quality` | Structured `DataQuality` report (see below) |
| `explanation` | `PlanExplanation` with strategy summary, score, and rejected alternatives |
| `time_series_index` | `TimeSeriesIndex` — shared slot grid used internally |

### Plan explanation (`explanation`)

The `PlanExplanation` object is designed to be surfaced directly as a HA sensor attribute:

| Field | Description |
|---|---|
| `selected_strategy` | Short identifier (e.g. `"charge_grid_discharge_peak"`) |
| `summary` | One-sentence reason for the selected plan |
| `score` | Savings vs. doing nothing (positive = saves money) |
| `estimated_total_cost` | Net grid cost for the horizon |
| `price_spread` | Max − min import price (larger = more arbitrage potential) |
| `peak_import_price` / `off_peak_import_price` | Price extremes |
| `forecast_pv_kwh` | Total PV production for the horizon |
| `forecast_net_consumption_kwh` | Total load − PV (negative = net solar surplus) |
| `battery_soc_pct` / `battery_soc_at_end_pct` | Starting and ending SoC |
| `constraints` | Active flags (e.g. `"winter_month"`, `"excess_export_enabled"`) |
| `rejected_plans` | Alternatives with name, reason, and estimated cost |

---

## Cost function

The cost function scores a candidate plan as a single number.
**Lower is better** — the planner selects the candidate with the minimum score.

### Formula

```text
total_cost
  = grid_import_cost
  − export_revenue
  + conversion_loss_cost
  + cycle_cost
  + soc_penalty
  + grid_limit_penalty
  + override_penalty
```

### Grid import cost

```text
grid_import_cost = Σ (grid_import_kwh[slot] × import_price[slot])
```

The cost function prices actual grid energy drawn, not stored energy.
If the battery stores `x` kWh and charge efficiency is `e`, the grid
import is `x / e`. This means conversion losses are implicitly included
in the import cost before the explicit conversion-loss term.

### Export revenue

```text
export_revenue = Σ (grid_export_kwh[slot] × export_price[slot])
```

Revenue is subtracted from total cost (it reduces the net expense).

### Conversion loss cost

Energy lost in the round trip (charge → store → discharge) is priced at
the average of the slot's import and export prices as an opportunity cost:

```text
avg_price[slot] = (import_price[slot] + export_price[slot]) / 2
loss_kwh[slot]  = (batteries_charged[slot] + batteries_discharged[slot])
                  × (conversion_loss_pct / 100) / 2
conversion_loss_cost = Σ (loss_kwh[slot] × avg_price[slot])
```

### Battery cycle cost

Battery depreciation per kWh cycled through the physical cells:

```text
throughput_kwh[slot] = batteries_charged[slot] + batteries_discharged[slot]
cycle_cost = Σ (throughput_kwh[slot] × cycle_cost_per_kwh)
```

Auto-derived cycle cost (when not explicitly configured):

```text
cycle_cost_per_kwh = purchase_price / (rated_capacity_kwh × expected_cycles)
```

**Example:** A 10 kWh battery bought for 30 000 DKK with 6 000 expected cycles costs
`30000 / (10 × 6000) = 0.50 DKK/kWh` of throughput.

### SoC penalties

Quadratic guard penalties discourage plans that violate SoC bounds:

```text
# Below the floor
if estimated_battery_soc[slot] < min_soc_pct:
    violation = min_soc_pct − estimated_battery_soc[slot]
    soc_penalty += soc_low_penalty_weight × violation²

# Above the ceiling
if estimated_battery_soc[slot] > max_soc_pct:
    violation = estimated_battery_soc[slot] − max_soc_pct
    soc_penalty += soc_high_penalty_weight × violation²
```

These penalties are a soft guard — the SoC simulation already hard-clamps SoC
at the hardware limits, so violations are rare in practice.

### Grid limit penalty

When a grid power limit is configured, slots that exceed it incur a proportional penalty:

```text
slot_power_kw = grid_import_or_export_kwh / slot_duration_hours
if slot_power_kw > grid_limit_kw:
    excess_kwh = (slot_power_kw − grid_limit_kw) × slot_duration_hours
    grid_limit_penalty += excess_kwh × grid_limit_penalty_per_kwh
```

### Override penalty

Slots forcibly set by a manual schedule (recommendation = `batteries_charge_grid`)
can optionally incur a flat penalty to express that deviating from the natural
optimal state has a cost:

```text
override_penalty = count(override_slots) × override_penalty_per_slot
```

Default `override_penalty_per_slot` is `0.0` — disabled unless explicitly configured.

### Terminal SoC accounting

Plans that empty the battery before the horizon ends look artificially cheap
because they avoid future discharge costs. The cost function accounts for this
by pricing the battery's remaining energy at the end of the horizon.

The terminal SoC penalty (or credit) ensures that two plans with different
ending SoC levels are compared fairly:

```text
terminal_soc_delta_kwh = baseline_terminal_soc_kwh − candidate_terminal_soc_kwh
terminal_soc_adjustment = terminal_soc_delta_kwh × replacement_energy_price
```

Emptying the battery is **not free** — the cost function charges for the energy
that would need to be replaced to restore the battery to a useful state.

---

## Candidate generation and selection

The planner evaluates multiple independent strategies before committing to a plan.

### Candidate strategies

| Name | Description |
|---|---|
| `baseline` | Current HSEM scheduling output — the result of running all schedulers normally |
| `no_action` | Battery completely idle — no forced charge, no forced discharge |
| `grid_charge` | Grid-charge slots kept, solar-charge slots cleared |
| `solar_only` | Only solar charging active, grid charging cleared |
| `discharge_only` | Discharge slots kept, all charging cleared |
| `aggressive` | Cheapest 3 slots forced to grid-charge, most expensive 3 forced to discharge |

Each candidate is built from a **deep copy** of the baseline slots so strategies
cannot interfere with each other. After generation, `simulate_soc` is called for
each candidate to fill in `batteries_discharged`, `grid_import_kwh`, `grid_export_kwh`,
and `estimated_battery_soc`.

### Selection

After scoring, the selector picks the candidate with the lowest `total_cost`.

The invariant **must always hold**:

```text
output.plan_cost == selected_candidate.cost
output.slots == selected_candidate.slots
```

No post-selection mutation is permitted. If a candidate needs adjusting, it must
be re-simulated and re-scored before it can become the output.

---

## Safety modes

HSEM uses a layered safety system to prevent hardware writes when inputs are
unsafe or the system is in a degraded state.

### Degraded mode levels

| Mode | Hardware writes | Trigger |
|---|---|---|
| `Normal` | Allowed | All inputs present and valid |
| `Degraded` | Allowed (with warnings) | Non-critical data missing (e.g. tomorrow's prices) |
| `Error` | **Blocked** | Critical data missing (battery SoC, house load, working mode) |
| `ReadOnly` | **Blocked** | `is_read_only = True` in config or `PlannerInput` |
| `DryRun` | **Blocked** | Dry-run mode active |

### Critical vs. non-critical missing data

Critical keywords in `missing_inputs` block hardware writes:

- `battery` — battery SoC or capacity unavailable
- `house_consumption` — house load sensor unavailable
- `working_mode` — inverter working-mode select unavailable

Non-critical labels (e.g. `tomorrow_price_missing_hours:…`) trigger `Degraded`
mode. The plan is computed and applied, but the coordinator logs a warning and
surfaces the gap in `data_quality`.

### Safety gate behaviour

The write-verify applier (`WriteVerifyApplier`) enforces these gates
before any Huawei Solar service call:

1. Checks `is_read_only` — skip writes if `True`.
2. Checks degraded mode — skip writes in `Error` mode.
3. Verifies the inverter is not unloading.
4. After writing, reads back the entity state to confirm the change applied.

---

## Data quality diagnostics

The `DataQuality` object on `PlannerOutput` reports completeness of the planning inputs.

### Fields

| Field | Type | Description |
|---|---|---|
| `today_price_missing_hours` | `list[int]` | Hours (0–23) with no price data today |
| `today_pv_missing_hours` | `list[int]` | Hours (0–23) with no PV forecast today |
| `tomorrow_price_missing_hours` | `list[int]` | Hours with no price data for tomorrow |
| `tomorrow_pv_missing_hours` | `list[int]` | Hours with no PV forecast for tomorrow |
| `day2_price_missing_hours` | `list[int]` | Hours with no price data for day +2 (72-h horizon only) |
| `day2_pv_missing_hours` | `list[int]` | Hours with no PV forecast for day +2 |
| `horizon_has_tomorrow` | `bool` | `True` when horizon extends beyond 24 h |
| `horizon_days` | `int` | Number of calendar days covered (1, 2, or 3) |
| `is_complete` | `bool` | `True` when no missing data was detected |

### Home Assistant attribute serialisation

`data_quality.as_dict()` returns a JSON-safe dictionary that can be attached
directly to a sensor's `extra_state_attributes`:

```json
{
  "is_complete": true,
  "horizon_has_tomorrow": true,
  "horizon_days": 2,
  "tomorrow_price_missing_hours": [],
  "tomorrow_pv_missing_hours": [],
  "day2_price_missing_hours": [],
  "day2_pv_missing_hours": [],
  "today_price_missing_hours": [],
  "today_pv_missing_hours": []
}
```

---

## Scenario examples

All examples use the following base configuration:

- Battery: 10 kWh rated, 10 % end-of-discharge floor → 9 kWh usable
- Charge efficiency: 90 % (10 % conversion loss)
- Max charge power: 5 kW (5 kWh/h)
- Horizon: 24 h, 1-hour slots
- Prices and PV in local currency (DKK) and kWh

---

### Scenario 1: Winter day

**Conditions:**
- Month: January (winter month)
- PV forecast: 0 kWh across all hours (no solar production)
- House load: ~2 kWh/h constant
- Import prices: flat at 1.50 DKK/kWh all day
- Battery at start: 50 % SoC (4.5 kWh above floor)
- Discharge window schedule: 16:00–21:00 (evening peak)

**What the planner does:**

```
Hours 00–14:  batteries_wait_mode  (cheap flat price, no PV, conserve battery)
Hours 14–16:  batteries_charge_grid (pre-charge before evening window)
              → charges to max_soc, importing ≈ 4.5 kWh from grid
Hours 16–21:  batteries_discharge_mode
              → discharges to cover 2 kWh/h house load
              → avoids 5 × 2 kWh = 10 kWh grid import during the window
Hours 21–24:  batteries_wait_mode  (window ended, battery near floor)
```

**Why this plan wins:**

The selected plan charges cheaply before the discharge window so the evening
load is covered entirely by the battery. On a flat-price winter day the
net saving is small (no price arbitrage benefit), but the plan ensures the
battery is available for the programmed window. The `no_action` candidate
(battery idle all day) produces an identical grid cost here, so the planner
may select `no_action` when the schedule does not force grid charge.

**Explanation excerpt:**

```json
{
  "selected_strategy": "baseline",
  "summary": "Pre-charge for evening discharge window; no PV surplus available.",
  "constraints": ["winter_month", "schedule_window_active"],
  "forecast_pv_kwh": 0.0,
  "battery_soc_at_end_pct": 10.0
}
```

---

### Scenario 2: Summer day — high PV surplus

**Conditions:**
- Month: July (summer month)
- PV forecast: 0→2→6→8→6→4→1→0 kWh (ramps from 06:00 to 14:00, falls off by 19:00)
- House load: 0.5 kWh/h (typical summer light load)
- Import prices: moderate, 2.00 DKK/kWh peak (09–11), 0.80 DKK/kWh off-peak
- Battery at start: 20 % SoC (1.8 kWh above floor)
- Excess export disabled

**What the planner does:**

```
Hours 00–06:  batteries_wait_mode  (night, no PV, load from grid)
Hours 06–09:  batteries_charge_solar
              → PV arrives, surplus charges battery
              → net_consumption = 0.5 kWh − PV (surplus) → battery fills
Hours 09–14:  batteries_charge_solar / batteries_wait_mode
              → PV covers load; surplus continues charging battery
              → battery reaches max_soc around 11:00
Hours 14–19:  batteries_discharge_mode (PV falling, prices still moderate)
              → battery discharges to cover load, reduces grid import
Hours 19–24:  batteries_wait_mode (battery near floor, no PV)
```

**Why this plan wins:**

The planner identifies the large solar surplus and assigns `batteries_charge_solar`
slots in the morning. This avoids peak-price grid imports in the morning hours
and accumulates free solar energy. The battery then covers evening load when PV
has stopped. The `no_action` candidate wastes PV surplus by exporting it at the
low export price instead of storing it for later use.

**Explanation excerpt:**

```json
{
  "selected_strategy": "solar_only",
  "summary": "High PV day: solar surplus stored for evening discharge.",
  "constraints": ["summer_month"],
  "forecast_pv_kwh": 27.0,
  "forecast_net_consumption_kwh": -15.0,
  "battery_soc_at_end_pct": 12.0
}
```

---

### Scenario 3: Cheap night price — grid charge opportunity

**Conditions:**
- Month: March (winter month)
- PV forecast: small midday peak (2–3 kWh/h, 10:00–14:00)
- House load: ~1.5 kWh/h
- Import prices:
  - 00:00–06:00: 0.25 DKK/kWh (very cheap night tariff)
  - 06:00–09:00: 2.50 DKK/kWh
  - 09:00–16:00: 1.80 DKK/kWh
  - 16:00–21:00: 3.20 DKK/kWh (peak)
  - 21:00–24:00: 1.20 DKK/kWh
- Export price: 0.10 DKK/kWh (low, net-metering not attractive)
- Battery at start: 15 % SoC (0.45 kWh above floor)
- No discharge window schedule configured

**What the planner does:**

```
Hours 00–06:  batteries_charge_grid
              → cheap night rate: 0.25 DKK/kWh import
              → charge 5 kWh/h × 5h = 25 kWh capacity requested,
                capped at usable range → battery fills to max_soc (90 %)
Hours 06–10:  batteries_wait_mode (prices rise, battery full)
Hours 10–14:  batteries_charge_solar (PV surplus topping up)
Hours 14–22:  batteries_discharge_mode
              → discharges during expensive slots (1.80–3.20 DKK/kWh)
              → avoids 8h × 1.5 kWh = 12 kWh at avg 2.5 DKK/kWh = 30 DKK import
              → charge cost: ≈ 9 kWh × 0.25 DKK + cycle cost ≈ 2.25 + 4.50 = 6.75 DKK
              → net saving ≈ 23 DKK
Hours 22–24:  batteries_wait_mode
```

**Why this plan wins:**

The price spread of 2.95 DKK/kWh (peak 3.20 − night 0.25) far exceeds the
cycle cost (~0.50 DKK/kWh for a typical installation). The `aggressive` candidate
also finds the cheap slots but may over-charge if the battery is already full.
The `baseline` candidate with schedule-driven pre-charge produces the same plan
here. The `no_action` candidate pays full peak prices.

**Key cost comparison:**

| Candidate | Estimated cost (DKK) |
|---|---|
| `baseline` (grid charge) | 6.75 |
| `solar_only` | 22.50 (no night charge) |
| `no_action` | 30.00 (full peak import) |

**Explanation excerpt:**

```json
{
  "selected_strategy": "grid_charge",
  "summary": "Cheap night rate (0.25 DKK/kWh) enables grid pre-charge; discharges during peak (3.20 DKK/kWh).",
  "score": 23.25,
  "price_spread": 2.95,
  "constraints": ["winter_month", "grid_charge_price_spread_met"],
  "battery_soc_at_end_pct": 10.0
}
```

---

### Scenario 4: High PV day — excess export opportunity

**Conditions:**
- Month: June (summer month)
- PV forecast: 1→3→7→10→10→8→5→2→0 kWh/h (strong sun, 07:00–18:00)
- House load: 0.3 kWh/h (light load)
- Export price: 2.80 DKK/kWh (09:00–13:00 midday peak), 0.50 DKK/kWh otherwise
- Import price: 1.80 DKK/kWh (09:00–13:00), 0.80 DKK/kWh otherwise
- Battery at start: 20 % SoC
- **Excess export enabled**, buffer 10 %, threshold 1.00 DKK/kWh

**What the planner does:**

```
Hours 07–09:  batteries_charge_solar
              → PV arrives, surplus charges battery
Hours 09–10:  batteries_charge_solar then force_batteries_discharge
              → battery reaches max_soc before midday export peak
Hours 10–13:  force_batteries_discharge (export_price = 2.80 DKK/kWh > threshold 1.00)
              → battery discharges AND PV exports simultaneously
              → export revenue: ~8 kWh × 2.80 = 22.40 DKK
Hours 13–18:  batteries_charge_solar (re-charging after export window)
              → battery refills from PV surplus
Hours 18–24:  batteries_discharge_mode (cover evening load from battery)
```

**Why this plan wins:**

The high midday export price (2.80 DKK/kWh) exceeds the `excess_export_price_threshold`
(1.00 DKK/kWh), so the planner triggers `force_batteries_discharge` during the peak
export window. The battery is pre-charged from solar in the morning and re-charged
from PV after the export window ends. The `solar_only` candidate does not exploit
the export window and earns significantly less revenue.

**Key cost comparison:**

| Candidate | Net cost (DKK) |
|---|---|
| `baseline` (excess export) | −18.40 (net revenue) |
| `solar_only` | −8.00 |
| `no_action` | −5.60 |

**Explanation excerpt:**

```json
{
  "selected_strategy": "baseline",
  "summary": "High PV surplus and peak export price trigger forced battery export.",
  "score": 12.80,
  "constraints": ["summer_month", "excess_export_enabled", "export_price_above_threshold"],
  "forecast_pv_kwh": 46.0,
  "forecast_net_consumption_kwh": -39.4
}
```

---

### Scenario 5: Flat price day — no arbitrage value

**Conditions:**
- Month: April (winter/spring boundary, configured as winter)
- PV forecast: modest (1–2 kWh/h, 09:00–15:00)
- House load: 1.0 kWh/h
- Import price: 1.20 DKK/kWh flat all 24 hours
- Export price: 0.10 DKK/kWh flat
- Battery at start: 50 % SoC
- No discharge window schedule; excess export disabled

**What the planner does:**

```
All hours: batteries_wait_mode
           (except 09–15 where batteries_charge_solar from PV surplus)
```

**Why this plan wins:**

With a flat import price of 1.20 DKK/kWh, there is no price arbitrage to exploit.
Grid-charging the battery at 1.20 DKK/kWh and discharging it later to avoid
buying at 1.20 DKK/kWh would not save money — the cycle cost makes it
net-negative. The planner compares the `grid_charge` candidate against `no_action`
and finds:

```text
grid_charge cost: charge 9 kWh × 1.20 DKK + cycle cost (9 kWh × 0.50 DKK)
               = 10.80 + 4.50 = 15.30 DKK
no_action cost: buy 1 kWh/h from grid × 24h × 1.20 DKK = 28.80 DKK
                (with PV reducing demand: ≈ 20 DKK)
```

Since the discharge savings equal the import cost (same price), and cycle
depreciation tips the scale negative, `no_action` or `solar_only` wins.

The `solar_only` candidate accepts the free PV energy into the battery during
the morning hours, avoiding some afternoon imports — this is marginally better
than pure `no_action` because the PV surplus would otherwise export at only
0.10 DKK/kWh.

**Explanation excerpt:**

```json
{
  "selected_strategy": "solar_only",
  "summary": "Flat price day: no grid charge arbitrage; solar surplus stored to reduce afternoon imports.",
  "score": 0.60,
  "price_spread": 0.00,
  "constraints": ["winter_month", "no_price_spread"],
  "battery_soc_at_end_pct": 38.0,
  "rejected_plans": [
    {
      "name": "grid_charge",
      "reason": "Grid charge cost exceeds cycle depreciation benefit on flat-price day.",
      "estimated_cost": 15.30
    }
  ]
}
```

---

## Reading the plan explanation

The `PlanExplanation` object is exposed as a HA sensor attribute on the
`hsem_working_mode` sensor. In the Home Assistant developer tools (States) you
can inspect it directly:

```
Entity: sensor.hsem_working_mode
Attributes:
  explanation:
    selected_strategy: grid_charge
    summary: "Pre-charge for evening discharge: 0.25 DKK night vs 3.20 DKK peak"
    score: 23.25
    estimated_total_cost: 6.75
    price_spread: 2.95
    peak_import_price: 3.20
    off_peak_import_price: 0.25
    forecast_pv_kwh: 4.5
    forecast_net_consumption_kwh: 16.5
    battery_soc_pct: 15.0
    battery_soc_at_end_pct: 10.0
    constraints: [winter_month, grid_charge_price_spread_met]
    rejected_plans:
      - name: no_action
        reason: "Peak-price import cost exceeds grid-charge cost plus cycle cost."
        estimated_cost: 30.00
```

### Understanding `score`

`score` is the estimated saving of the selected plan versus the `no_action` baseline:

- **Positive score** — the plan saves money compared to doing nothing. A score of 23.25
  means the planner expects to save 23.25 DKK over the planning horizon.
- **Zero or near-zero score** — flat price day or no arbitrage available.
- **Negative score** (unusual) — the pre-charge overhead exceeds the discharge benefit
  within this specific horizon window. This can happen if the horizon ends before the
  discharge window is fully executed.

### Understanding `constraints`

Common constraint tags and their meaning:

| Tag | Meaning |
|---|---|
| `winter_month` | Current month is in `months_winter`; winter scheduling strategy active |
| `summer_month` | Not in winter months; summer scheduling strategy active |
| `no_price_spread` | Max − min import price is near zero; no grid-charge arbitrage |
| `grid_charge_price_spread_met` | Price spread exceeds min_price_difference threshold |
| `excess_export_enabled` | Excess export feature is active in config |
| `export_price_above_threshold` | Export price exceeds `excess_export_price_threshold` |
| `schedule_window_active` | At least one `battery_schedules` entry is enabled and active |

---

## Known limitations

### Consumption prediction is averaged, not model-based

The planner predicts house load from a weighted average of 1, 3, 7, and 14-day
historical consumption per clock-hour. This works well for regular households
but may under- or over-predict when:

- An EV charges on an irregular schedule.
- Seasonal load shifts (e.g. heating vs. cooling) haven't had time to appear in the lookback window.
- Spike days (e.g. a party) pull the average up permanently.

The spike-aware blending algorithm mitigates outliers but does not eliminate them.
A Kalman-filter-based predictor is planned as a future improvement.

### Prices are assumed known for the full horizon

The planner treats all `price_points` as equally reliable. In practice:

- Today's prices are firm (EDS publishes by ~13:00).
- Tomorrow's prices arrive around 13:00 CET and are typically available before the evening planning run.
- Day +2 prices (72-hour horizon) may be unavailable or estimated.

Missing price data is surfaced in `data_quality` and triggers `Degraded` mode,
but the planner proceeds using `0.0` as a fallback — which means it cannot
meaningfully optimise slots where prices are absent.

### PV forecast is a point estimate

Solcast provides a single `pv_estimate` per hour. There is no confidence interval
or worst-case/best-case distinction exposed to the planner. The confidence decay
factor (10 %/day) is a conservative heuristic, not a calibrated uncertainty model.

### No intra-day re-planning of past slots

Slots marked `time_passed` are frozen. If the morning plan assumed 5 kWh of PV
that didn't materialise (cloudy day), the afternoon plan starts fresh from the
current SoC but does not retroactively account for the morning shortfall.

### Grid export throttle is a binary threshold

The `export_min_price` threshold turns grid export on or off below a price level.
There is no proportional throttle or ramp — the switch is instantaneous.

### Battery round-trip efficiency is a fixed percentage

`battery_conversion_loss_pct` is a static configuration value. Real lithium
batteries have efficiency curves that depend on charge rate, temperature, and SoC
level. Using a conservative value (e.g. 10–15 %) compensates partially for this.

### Single-zone tariff model

The planner applies a single import price and export price per slot. It does not
model time-of-use (TOU) tariffs with multiple simultaneous price components (e.g.
capacity tariffs, network fees, or spot + fixed-premium structures). These can be
factored in manually by adjusting the import price values fed to the planner.
