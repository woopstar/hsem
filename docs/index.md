# HSEM Documentation

> **Home Assistant Solar Energy Management (HSEM)** — version 5.1.0

---

## Quick reference

| Document | Description |
|---|---|
| [Home](home.md) | User-facing overview: features, FAQ, working modes, battery schedules, excess export, consumption sensors |
| [Battery Charging Economics](battery-charging-economics.md) | How to calculate the minimum charging price for a battery schedule |
| [Architecture Overview](architecture-overview.md) | System context, layered architecture, module map, planning pipeline |
| [Planner Specification](planner-spec.md) | **Normative** — all planner invariants, rules, and constraints |
| [Planner Technical Guide](planner-guide.md) | How the planner works with worked examples |
| [Cost Function Math](cost-function-math.md) | Complete mathematical formulation of the 8-term cost function |
| [Energy Accounting](energy-accounting.md) | Physical energy flow model, SoC simulation, efficiency math |
| [Candidate Generation](candidate-generation.md) | How candidates are generated, assumptions, partial-SoC |
| [MILP Optimization](milp-optimization.md) | Full LP formulation, variable layout, constraints, and solver pipeline |
| [Consumption Prediction](consumption-prediction.md) | Weighted-average model, IQR outlier detection, spike suppression |
| [Safety Modes](safety-modes.md) | Degraded mode, read-only gate, write-verify applier, runtime resolver |
| [Price Scaling](price-scaling.md) | EDS price scaling, eds_share conversion factor |
| [Services Reference](services-reference.md) | All 4 HSEM services with examples |
| [Sensors Reference](sensors-reference.md) | Complete entity reference: all sensor, select, switch, number, and time entities |
| [Dashboard Setup](dashboard-setup.md) | Step-by-step ApexCharts dashboard with full YAML, layout reference, and troubleshooting |
| [Config Flow Reference](config-flow-reference.md) | Every config/options flow step and field |
| [EV Charge Plan Setup](ev-charge-plan-setup.md) | EV planned load configuration guide |
| [EV Optimal Charging Template](ev-optimal-charging-template.md) | Legacy Home Assistant template sensor for cost-optimal EV charging |
| [Forecast Accuracy Tracking](forecast-accuracy-tracking.md) | Forecast vs actual tracking system |
| [Huawei Entities](huawei_entities.md) | Canonical HA entity ID reference |
| [Troubleshooting Guide](troubleshooting-guide.md) | Diagnose and fix common problems: missing data, wrong prices, write failures, battery behaviour |
| [Quality Checks](quality-checks.md) | Static quality tools and CI configuration |

---

## Key files

| File | Purpose |
|---|---|
| `planner-spec.md` | **Read this first** before touching any planner code |
| `planner-guide.md` | Worked examples for 6 common scenarios |
| `huawei_entities.md` | Verified HA entity IDs — never guess |
| `architecture-overview.md` | Module responsibility map and dependency graph |

---

## For developers

1. **Always read `planner-spec.md`** before modifying planner code
2. **Always check `huawei_entities.md`** before using a battery/inverter value
3. Use Mermaid for architecture and flow diagrams; do not use ASCII/Markdown box diagrams
4. Use math equations (`$$ ... $$`) for formulas rather than plain text/code-block formulas
5. Run `tox -e lint` before every commit
6. Run `tox -e quality` after lint
7. Run `pytest tests/` before every PR
8. See `AGENTS.md` and `CLAUDE.md` for full development rules
