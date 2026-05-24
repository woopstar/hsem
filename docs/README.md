# HSEM Documentation

> **Home Assistant Solar Energy Management (HSEM)** — version 5.1.0

---

## Quick reference

| Document | Description |
|---|---|
| [Architecture Overview](hsem-architecture-overview.md) | System context, layered architecture, module map, planning pipeline |
| [Planner Specification](hsem-planner-spec.md) | **Normative** — all planner invariants, rules, and constraints |
| [Planner Technical Guide](hsem-planner-guide.md) | How the planner works with worked examples |
| [Cost Function Math](hsem-cost-function-math.md) | Complete mathematical formulation of the 8-term cost function |
| [Energy Accounting](hsem-energy-accounting.md) | Physical energy flow model, SoC simulation, efficiency math |
| [Candidate Generation](hsem-candidate-generation.md) | How candidates are generated, assumptions, partial-SoC, MILP |
| [Consumption Prediction](hsem-consumption-prediction.md) | Weighted-average model, IQR outlier detection, spike suppression |
| [Safety Modes](hsem-safety-modes.md) | Degraded mode, read-only gate, write-verify applier, runtime resolver |
| [Price Interval Semantics](hsem-price-interval-semantics.md) | EDS price scaling, eds_share conversion factor |
| [Services Reference](hsem-services-reference.md) | All 4 HSEM services with examples |
| [Sensors Reference](hsem-sensors-reference.md) | All sensor, select, switch, and time entities |
| [Config Flow Reference](hsem-config-flow-reference.md) | Every config/options flow step and field |
| [EV Charge Plan Setup](ev-charge-plan-setup.md) | EV planned load configuration guide |
| [Forecast Accuracy Tracking](forecast-accuracy-tracking.md) | Forecast vs actual tracking system |
| [Huawei Entities](huawei_entities.md) | Canonical HA entity ID reference |
| [Quality Checks](quality-checks.md) | Static quality tools and CI configuration |

---

## Key files

| File | Purpose |
|---|---|
| `hsem-planner-spec.md` | **Read this first** before touching any planner code |
| `hsem-planner-guide.md` | Worked examples for 6 common scenarios |
| `huawei_entities.md` | Verified HA entity IDs — never guess |
| `hsem-architecture-overview.md` | Module responsibility map and dependency graph |

---

## For developers

1. **Always read `hsem-planner-spec.md`** before modifying planner code
2. **Always check `huawei_entities.md`** before using a battery/inverter value
3. Run `tox -e lint` before every commit
4. Run `tox -e quality` after lint
5. Run `pytest tests/` before every PR
6. See `AGENTS.md` and `CLAUDE.md` for full development rules
