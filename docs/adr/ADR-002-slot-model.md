# ADR-002: Slot Model

**Status:** Accepted · **Date:** 2026-05-08 · **Scope:** Planner engine, all scheduling layers

---

## Context

The HSEM planner operates over a discretised time horizon. Every planning decision
— charge, discharge, export, idle — must be assigned to a specific time interval
with precise energy accounting. The data structure that represents one atomic time
interval in the horizon is called a **slot**.

The slot model must satisfy several conflicting requirements:

1. **Deterministic energy accounting** — Every slot must carry enough fields to
   independently verify energy balance (load = PV + battery + grid) without
   cross-referencing other slots.

2. **Flexible granularity** — The planner supports 15-minute and 60-minute slot
   widths. The slot model must work identically at both resolutions.

3. **Layered recommendation assignment** — Three independent layers (planner engine,
   EV labelling, runtime resolver) may assign or override a slot's recommendation.
   The slot must preserve this provenance.

4. **Multi-day horizon** — Horizons span up to 48 hours (192 × 15-min slots). The
   slot model must handle missing future-day data gracefully without crashing.

5. **Planner independence** — The slot model lives in the pure-Python planner layer
   (no Home Assistant imports). It must be fully serialisable for test assertions
   and debug logging.

---

## Decision

We adopt an **explicit per-slot record** model. Every slot in the horizon is a
`PlannedSlot` dataclass (or equivalent typed dict) with the following field groups:

### Temporal identity

- `start_time` / `end_time` — timezone-aware datetime range
- `duration_hours` — cached float for power→energy conversion
- `slot_index` — ordinal position in the horizon [0..n-1]

### Forecast data (immutable after population)

- `estimated_house_consumption_kwh` — weighted historical average
- `solcast_pv_estimate_kwh` — Solcast forecast (decayed for future days)
- `import_price_kwh` / `export_price_kwh` — full currency/kWh rate
- `tariff_cost_kwh` — optional grid tariff
- `ev_planned_load_kwh` — extra EV AC load not in house load
- `ev_accounted_load_kwh` — EV AC load already in house load
- `ev_total_planned_load_kwh` — sum of both EV fields

### Planning decisions (mutable during pipeline)

- `recommendation` — `Recommendations` enum value (or `None`)
- `batteries_charged_kwh` — energy stored by charging in this slot
- `batteries_discharged_kwh` — energy removed by discharging in this slot
- `grid_import_kwh` / `grid_export_kwh` — grid energy flows
- `pv_used_kwh` — PV energy consumed locally (includes battery/PV/EV)
- `estimated_battery_soc_before_kwh` / `estimated_battery_soc_after_kwh`
- `estimated_battery_soc_before_pct` / `estimated_battery_soc_after_pct`

### Derived / diagnostic

- `estimated_net_consumption_kwh` — `house_load + ev_planned - pv`
- `time_passed` — `True` if slot's end time is in the past
- `is_planned_load_slot` — `True` if EV load is scheduled here

### Key design rules

#### 1. No nullable energy fields at planner output
Every energy field is `0.0` for slots that do not participate (past slots, idle
recommendations). This eliminates the need for null guards in cost calculators
and SoC simulation.

#### 2. Recommendation is the sole control signal
The SoC simulator reads only the slot's `recommendation` to decide energy flows.
No other field drives behaviour — this keeps the simulation deterministic and
testable.

#### 3. Past-slot sentinel
After time passes, `recommendation` is set to `time_passed`, all energy fields
are zeroed, and `estimated_battery_soc_after_kwh` is set to `0.0`. The cost
function skips these slots entirely.

#### 4. Dedicated fields, not derived
`estimated_net_consumption_kwh` is populated explicitly (with PV confidence decay
applied) rather than computed on-the-fly. This ensures consistency between the
planner that schedules around it and the sensors that display it.

#### 5. Energy units only
All power limits are converted to per-slot energy caps at the planner boundary.
The slot model never stores kW values — only kWh.

---

## Consequences

### Positive

1. **Auditability** — Every energy flow is independently recorded. Two planners
   processing the same input produce byte-identical slot arrays.

2. **Testability** — A slot array can be constructed manually in a unit test and
   fed directly into the cost function or SoC simulator without HA infrastructure.

3. **Layered safety** — The mutable recommendation field allows three independent
   layers to assign and override without corrupting other fields.

4. **Granularity independence** — All formulas use `duration_hours` as a multiplier.
   The same slot population code works for 15-min and 60-min slots unchanged.

5. **Diagnostically rich** — Every Home Assistant sensor attribute is populated
   from slot fields with no additional computation.

### Negative

1. **Field count** — A `PlannedSlot` has ~20 fields, which is large relative to a
   simpler (start, end, energy) tuple. This increases memory per slot and
   serialisation volume.

2. **Population cost** — Building the full slot array requires iterating all fields
   for every slot. For 192 × 15-min slots this is negligible (< 1 ms), but the
   code is more verbose than a lazy-evaluation approach.

3. **Duplication of time series** — Several fields (price, PV, load) are repeated
   per slot instead of stored once in a shared time series. This is acceptable for
   n ≤ 192 slots but would not scale to thousands.

4. **Mutation risk** — Because slots are mutable, a bug in one pipeline step can
   corrupt fields that other steps depend on. We mitigate this by strictly ordering
   slot population before SoC simulation, and simulation before cost scoring.

---

## Alternatives Considered

### A. Lazy / index-based model
Store all time-series data in parallel arrays (price[0..n-1], load[0..n-1], etc.)
and compute slot views on demand.

**Rejected because:**
- Each pipeline step (schedule, simulate, score) would need to carry the full index
  around, making function signatures fragile.
- Derived fields (net consumption) would be recomputed in every step, risking
  inconsistency with PV confidence decay.
- Test readability suffers — slot-level assertions require array indexing.

### B. Event-sourced state machine
Model the battery as a state machine and slots as transitions. Reconstruct the
slot array from the transition log.

**Rejected because:**
- Over-engineered for the current requirements. The slot count (≤ 192) makes
  reconstruction overhead irrelevant.
- Auditability suffers — there is no single "slot n" object to inspect in
  a debugger or log.
- Three-layer recommendation assignment becomes complex when state transitions
  are first-class objects.

### C. Single energy model per slot
Store only `net_kwh = PV - load` per slot and derive all battery/grid flows
from that single number.

**Rejected because:**
- Loss of information — you cannot distinguish "grid import for battery" from
  "grid import for house" after simplification.
- Terminal SoC accounting requires knowing battery energy removed, not just net.
- Diagnostic sensor values (battery charged/discharged per slot) would require
  reverse computation, which is fragile and imprecise.

---

## Related

- ADR-001: Planner Extraction (slot model is the core data type passing through
  the extracted planner boundary)
- ADR-003: Cost Scoring (cost function reads every energy field from the slot)
- `docs/hsem-planner-spec.md` — Slot definition in Core concepts
- `models/planner_outputs.py` — `PlannedSlot` implementation