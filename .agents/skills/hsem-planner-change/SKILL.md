---
name: hsem-planner-change
description: Activate when making any change to the HSEM planner layer — engine, cost function, SoC simulation, candidate generation, slot population, MILP optimizer, or safety gates.
---

# HSEM Planner Change — Spec Compliance & Invariants

Activate this skill when touching **any file under `custom_components/hsem/planner/`**: `engine.py`, `cost_function.py`, `soc_simulation.py`, `candidate_generator.py`, `candidate_selector.py`, `slot_population.py`, `charge_scheduler.py`, `discharge_scheduler.py`, `milp_optimizer.py`, `ev_planner.py`, or safety gates.

## Step 1: Read the Planner Specification

**Always read `docs/planner-spec.md` before touching any planner code.** This is the single source of truth for planner semantics.

## Step 2: Verify These Invariants for Every Planner Change

Every planner change must satisfy ALL of these invariants:

- [ ] **Energy balance per slot** — energy in equals energy out for every time slot
- [ ] **SoC bounds** — battery SoC never leaves configured min/max bounds
- [ ] **Cost identity** — `winner.cost == final_output.cost` (no post-selection mutation)
- [ ] **Slot identity** — `winner.slots == final_output.slots`
- [ ] **Terminal SoC accounting** — terminal SoC affects cost; emptying the battery is not free
- [ ] **No-action baseline** — includes normal PV/battery self-consumption
- [ ] **Safety gates** — read-only, degraded, and dry-run gates block hardware writes
- [ ] **Forced discharge/export** — changes SoC and cost/revenue correctly
- [ ] **Grid charge prices** — actual grid import, not stored energy
- [ ] **Float comparisons** — use epsilon guard (`abs(x) > 1e-9`) in production, `pytest.approx()` in tests
- [ ] **MILP variable vector** — 8*n base, growing to 8n + 2n·E + E with EV co-optimisation
- [ ] **Cycle cost formula** — uses mandatory 2x denominator

## Step 3: Update the Spec If Semantics Change

If a change intentionally alters planner semantics (formulas, invariants, safety gates, behavior), **update `docs/planner-spec.md` in the same PR**. Spec and implementation must never diverge silently.

## Step 4: Add or Update Tests

Add tests covering the affected invariants. Every planner change must have regression test coverage for:
- Energy balance
- SoC bounds
- Cost function correctness
- Terminal SoC handling
- Safety gate behavior

## Step 5: Do Not Break in Slot Loops

Never use `break` in slot iteration loops unless the loop is explicitly ordered and early exit is provably correct.

## Step 6: Use HSEM_LOGGER for Planner Logging

```python
from custom_components.hsem.utils.logger import HSEM_LOGGER
# Never use logging.getLogger(__name__) in planner code
```

When creating log statements, never use runtime string formatting — use `%` placeholders and the `extra` argument.

## Step 7: Check File Size

Hard limit: 30 KB per file in `planner/` and `utils/`. Check before PR:
```bash
wc -c custom_components/hsem/planner/*.py
```

## Definition of Done for Planner Work

- [ ] `docs/planner-spec.md` read and understood
- [ ] All invariants verified
- [ ] Spec updated if semantics changed
- [ ] Tests added or updated
- [ ] Spec and implementation are consistent
- [ ] Lint, typing, quality, and test checks pass
