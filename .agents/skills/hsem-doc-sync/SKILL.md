---
name: hsem-doc-sync
description: Activate after every code change to verify all documentation files that describe the changed behaviour are updated and consistent with the implementation.
---

# HSEM Documentation Sync — Keep Docs in Sync with Code

Activate this skill **after making code changes** and **before opening a PR**. Stale documentation causes confusion and bugs — every doc that describes changed behaviour must be updated in the same PR.

## Documentation Files — Check Every One

When behaviour changes, check **every** file below. If it describes something you changed, update it:

| File | When to check |
|------|---------------|
| `docs/planner-guide.md` | Planner inputs, outputs, cost function, or scenarios changed |
| `docs/planner-spec.md` | Planner semantics, invariants, formulas, or safety gates changed |
| `docs/config-flow-reference.md` | Config/options flow steps changed |
| `docs/ev-charge-plan-setup.md` | EV planned load setup changed |
| `docs/huawei_entities.md` | New Huawei entities wired or existing ones changed |
| `.github/memories.md` | Canonical patterns, module map, open issues, or architectural decisions changed |
| `README.md` | User-facing features, descriptions, or links changed |
| `translations/en.json` | Any user-facing string added, changed, or removed |

## Documentation Rules

### Spec-Implementation Consistency (Highest Priority)

- `docs/planner-spec.md` and the planner implementation **must never diverge silently**
- If a change intentionally alters planner semantics, update the spec in the same commit
- The spec is the source of truth — code must match it exactly

### Memories.md

- Module responsibility map must reflect current file layout
- Canonical patterns must be accurate
- Open issue numbers must be up to date
- New architectural decisions must be recorded

### Translations

- Every user-facing string (field labels, errors, aborts) must have a key in `translations/en.json`
- Boolean/switch fields must have translation entries
- For `huawei_solar` fields: update **both** `config.step.huawei_solar` and `options.step.huawei_solar`

### README.md

- User-facing feature descriptions must be accurate
- Links must resolve correctly
- Setup/configuration instructions must reflect current flows

## Verification Checklist

Before opening a PR:

- [ ] Read every docs file listed above
- [ ] For each file: does it describe something I changed? If yes, update it
- [ ] `docs/planner-spec.md` consistent with planner implementation
- [ ] `.github/memories.md` module map matches current file layout
- [ ] `translations/en.json` has entries for all new/changed user-facing strings
- [ ] No stale or misleading documentation remains

## Anti-Patterns to Avoid

- ❌ Updating the planner code but not `docs/planner-spec.md`
- ❌ Adding a new config field but not `docs/config-flow-reference.md`
- ❌ Changing a feature but leaving old behavior in `README.md`
- ❌ Adding a user-facing string but skipping `translations/en.json`
- ❌ Wiring a new Huawei entity but not `docs/huawei_entities.md`
- ❌ Recording a pattern in code but not in `.github/memories.md`
