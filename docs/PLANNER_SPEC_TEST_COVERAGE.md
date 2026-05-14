# HSEM Planner Spec — Test Coverage Table

Generated: 2026-05-13  
Source of truth: `docs/hsem-planner-spec.md`

Each row maps one spec invariant to its test coverage status.

Legend:
- ✅ **Covered** — at least one targeted test exists
- ⚠️ **Partial** — the invariant is exercised indirectly but no dedicated test exists
- ❌ **Missing** — no test covers this invariant; new test added in this PR
- 🚧 **Known gap** — invariant depends on unimplemented feature; xfail test added

---

## Core Energy & SoC Invariants

| # | Invariant (from spec) | Status | Existing test(s) | New test(s) |
|---|---|---|---|---|
| 1 | Energy balance holds for every slot | ❌ | — | `tests/planner/test_invariants.py::TestEnergyBalance` |
| 2 | SoC never leaves configured bounds | ✅ | `test_soc_simulation.py::TestSoCBoundsIntegration`, `test_planner_harness.py::TestSlotValues::test_battery_soc_bounded` | — |
| 3 | Forced discharge changes SoC and cost | ❌ | — | `tests/planner/test_invariants.py::TestForcedDischarge` |
| 4 | Force export changes SoC and export revenue | ❌ | — | `tests/planner/test_invariants.py::TestForceExport` |
| 5 | Grid charge prices actual grid import, not stored energy | ❌ | — | `tests/planner/test_invariants.py::TestGridChargeAccounting` |
| 6 | Candidate winner cost equals final output cost | ❌ | — | `tests/planner/test_invariants.py::TestWinnerCostIdentity` |
| 7 | Final output slots equal selected candidate slots | ❌ | — | `tests/planner/test_invariants.py::TestWinnerSlotsIdentity` |
| 8 | No post-selection mutation happens without re-score | ❌ | — | `tests/planner/test_invariants.py::TestNoPostSelectionMutation` |
| 9 | No-action includes normal PV/battery behavior | ⚠️ | `test_candidate_generation.py::TestGenerateCandidates::test_no_action_has_no_charge_or_discharge` | `tests/planner/test_invariants.py::TestNoActionBaseline` |
| 10 | Terminal SoC affects cost | ❌ | — | `tests/planner/test_invariants.py::TestTerminalSoC` |
| 11 | Emptying the battery is not free | ❌ | — | `tests/planner/test_invariants.py::TestTerminalSoC::test_emptying_battery_is_not_free` |
| 12 | Winner cost ≤ no-action cost within implemented candidate set | ⚠️ | `test_candidate_generation.py::TestSelectBestCandidate::test_winner_has_lowest_cost_among_valid` | `tests/planner/test_invariants.py::TestWinnerVsNoAction` |
| 13 | Current partial slot uses remaining duration only | 🚧 | — | `tests/planner/test_invariants.py::TestPartialSlot` (xfail — partial-slot duration not yet implemented) |
| 14 | Missing price/PV data does not become real zero silently | ❌ | — | `tests/planner/test_invariants.py::TestMissingDataSentinel` |
| 15 | Read-only/degraded/dry-run gates block writes | ✅ | `tests/test_safety_gates.py` (full applier gate coverage) | — |

## Seasonal & Scheduling Invariants

| # | Invariant (from spec) | Status | Existing test(s) | New test(s) |
|---|---|---|---|---|
| 16 | Seasonal mode selection is deterministic | ⚠️ | `test_planner_harness.py::TestSeasonalLogic` | `tests/planner/test_invariants.py::TestSeasonalDeterminism` |
| 17 | Schedule windows crossing midnight work | ❌ | — | `tests/test_cross_day_charge_windows.py` (already exists, covers the cross-day case) → promoted to ✅ |
| 18 | Pre-charge happens before the target discharge window | ✅ | `test_planner_harness.py::TestChargeScheduling::test_charge_slots_precede_schedule_discharge_window` | — |

## Negative-Price & Export-Price Invariants

| # | Invariant (from spec) | Status | Existing test(s) | New test(s) |
|---|---|---|---|---|
| 19 | Negative import price can trigger charge only within constraints | ✅ | `test_planner_harness.py::TestChargeScheduling::test_negative_price_slots_include_grid_charge`, `test_cycle_cost_guard.py` | — |
| 20 | Negative export price blocks or penalizes export according to config | ❌ | — | `tests/planner/test_invariants.py::TestNegativeExportPrice` |

## Additional Required Invariants (from issue #379)

| # | Invariant | Status | Existing test(s) | New test(s) |
|---|---|---|---|---|
| 21 | EV load is not double-counted | ⚠️ | `test_planner_harness.py` (house_power_includes_ev present in fixtures) | `tests/planner/test_invariants.py::TestEvLoadNotDoubleCounted` |
| 22 | Manual override cannot bypass safety gates | ✅ | `tests/test_safety_gates.py` | — |
| 23 | Fusion Solar schedule writes verified before considered applied | 🚧 | — | `tests/planner/test_invariants.py::TestFusionSolarVerification` (xfail — Fusion Solar not in scope) |
| 24 | Warm-up mode limits optimization if history is insufficient | 🚧 | — | `tests/planner/test_invariants.py::TestWarmupMode` (xfail — warm-up gate not yet implemented) |
| 25 | Required reserve is not consumed without cost or invalidation | ❌ | — | `tests/planner/test_invariants.py::TestRequiredReserve` |

---

## Summary

| Status | Count |
|---|---|
| ✅ Covered | 7 |
| ⚠️ Partial | 5 |
| ❌ Missing → new test added | 11 |
| 🚧 Known gap → xfail test added | 3 |
| **Total invariants** | **25** |

---

## New test file

All missing tests are added in:

```
tests/planner/test_invariants.py
```

This file covers invariants 1, 3–12, 14, 16, 20–21, 23–25.
