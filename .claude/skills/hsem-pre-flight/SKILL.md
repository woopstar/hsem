---
name: hsem-pre-flight
description: Run before starting any HSEM code change. Fetch origin, create a dedicated git worktree and feature branch, read repository memory and relevant docs.
---

# HSEM Pre-Flight — Start Any Code Change

Activate this skill **before writing any code** — when the user asks to fix a bug, implement a feature, or make any change to the HSEM codebase.

## Step 1: Create a Worktree and Feature Branch

The main checkout at `/workspaces/hsem` is **shared** between agent sessions (Claude
Code, Zed agents, Copilot) running in the same devcontainer. Never run `git checkout`,
`git switch`, `git pull`, `git rebase` or `git reset` there: it moves the branch, index
and working tree under whichever other session is using it (issue #1122,
woopstar/open_spot_forecast#55). Give every task its own worktree instead:

```bash
git -C /workspaces/hsem fetch origin
git -C /workspaces/hsem worktree add /workspaces/worktrees/hsem-<issue> \
  -b <type>/<issue>-<slug> origin/main
cd /workspaces/worktrees/hsem-<issue>
```

- Branch from `origin/main` unless the user explicitly says otherwise. A 6.3.x
  backport branches from `origin/v6.3.0-hotfix` into its own worktree, e.g.
  `/workspaces/worktrees/hsem-<issue>-hotfix`.
- `/workspaces/worktrees` is the `hsem-worktrees` volume (see `.devcontainer/README.md`).
  No per-worktree setup is needed: Python deps are container-wide and pre-commit hooks
  live in the common git dir.
- Do **all** work from the worktree: edits, `./scripts/quality.sh`, commits, pushes and
  `gh` commands. `scripts/quality.sh` keeps separate mypy, ruff and pytest caches per
  worktree.
- If `worktree add` fails because the branch is already checked out elsewhere, another
  session probably owns it. Inspect that worktree (`git worktree list`,
  `git -C <path> status`) before touching it, and never remove one with uncommitted
  changes.
- The git stash stack is shared by every worktree. Don't use `git stash`; commit
  work in progress instead.

Branch naming: see Step 4.

## Step 2: Read Repository Memory

Read `.github/memories.md`. Pay special attention to:

- Module responsibility map (planner, ML, utils layers)
- Canonical patterns (clamp_efficiency, DISCHARGE_RECS, calculate_recommended_threshold, HSEM_LOGGER)
- MILP variable vector layout (8\*n base, growing with EV co-optimisation)
- File size limits (30 KB hard limit in planner/ and utils/)
- Cycle cost formula with mandatory 2x denominator
- File organization patterns (by responsibility, not by theme)
- Huawei entity wiring protocol
- Testing and logging rules

## Step 3: Read Any Issue Being Solved

If this is issue-driven work, read the full GitHub issue before touching any code.

## Step 4: Branch Naming

The branch is created by `git worktree add -b` in Step 1. Format:
`<type>/<issue-number>-<slug>`

| Type       | Use for                  |
| ---------- | ------------------------ |
| `feat`     | New features             |
| `fix`      | Bug fixes                |
| `chore`    | Repository/code chores   |
| `docs`     | Documentation updates    |
| `refactor` | Code refactoring         |
| `perf`     | Performance improvements |
| `test`     | Test additions/updates   |
| `ci`       | CI/CD changes            |

Examples: `fix/444-milp-cycle-cost`, `feat/123-add-solar-forecast`

All branches MUST be based on `origin/main` unless the user explicitly instructs otherwise.

## Step 5: Identify Relevant Documentation

Based on the change type, read these docs before touching code:

| Change touches                                                                                     | Must read                       |
| -------------------------------------------------------------------------------------------------- | ------------------------------- |
| Planner engine, cost function, SoC simulation, candidate generation, slot population, safety gates | `docs/planner-spec.md`          |
| Huawei Solar sensors                                                                               | `docs/huawei_entities.md`       |
| Config/options flow                                                                                | `docs/config-flow-reference.md` |
| EV charging                                                                                        | `docs/ev-charge-plan-setup.md`  |
| Planner inputs/outputs                                                                             | `docs/planner-guide.md`         |

## Step 6: Understand the Affected Code

Search and read the relevant source files. Do not guess file paths — use `grep` and `glob` to locate them.

## Reminder: One Issue Per Branch

Solve **one issue only** per branch and PR. Do not combine multiple issues. Do not refactor unrelated code.
