# Custom GitHub Copilot Instructions

## Repository Memory (Read First)

**Always read `.github/memories.md` before starting any work.**

It contains:
- Module responsibility map for all planner and utils files
- Canonical patterns you must use (never re-invent)
- MILP variable vector layout (8*n)
- File size limits and oversized files
- Cycle cost formula with the mandatory 2x denominator
- File organization patterns (by responsibility, not by theme)
- Huawei entity wiring protocol
- Logging and testing rules

## HA Development Compliance (Read Before PR)

**Always read `.rules/ha-development-rules.md` before creating a PR.**

It contains the complete Home Assistant development compliance checklist:
- Dependency management and requirement pinning
- Async patterns, config flow, and voluptuous schemas
- Translations, entity base classes, and device info
- Style guidelines (import order, docstrings, logging, type hints)
- PR scope rules and testing requirements
- Quality gates: `./scripts/quality.sh lint`, `./scripts/quality.sh typing`, `./scripts/quality.sh quality`, `./scripts/quality.sh test`

---

## Standard Issue-Solving Workflow

When asked to solve a GitHub issue, always follow these steps in order:

0. **Checkout main and pull latest**
   ```bash
   git checkout main
   git pull
   ```
1. **Read the GitHub issue** — Understand the problem fully before touching any code.
2. **Read `.github/memories.md`** — Check if the issue touches a known pattern or canonical helper.
3. **Create a branch** using the issue prefix and a short slug.
   - Format: `<type>/<issue-number>-<slug>` — e.g., `fix/444-milp-cycle-cost`
4. **Understand the relevant code** — Search and read the affected files before making changes.
5. **Implement the smallest safe fix** — No unrelated changes, no broad refactors.
6. **Update documentation** — Update every docs/ file that describes the changed behaviour
   (planner guide, spec, config flow reference, memories.md, README, etc.).
7. **Add or update regression tests** — Cover the bug or new behavior.
8. **Run the relevant tests** — `pytest tests/` or the targeted test file.
9. **Run lint/type + quality checks** — all four must pass before opening a PR:
   - `./scripts/quality.sh lint` — ruff format + ruff check
   - `./scripts/quality.sh typing` — mypy type checking
   - `./scripts/quality.sh quality` — pyright + vulture
   - `./scripts/quality.sh test` — pytest with coverage
10. **Report a summary** including:
   - Issue title
   - Branch name
   - Files changed
   - What changed and why
   - Tests added or updated
   - Test and lint results
11. **Create a pull request** linked to the issue using `Fixes #<ISSUE_NUMBER>` in the description.
12. **Keep the PR up to date** — after every follow-up commit on a branch that already has an open
    PR, update both the PR title and description to reflect the current state of all changes made.
    Tick off any completed acceptance criteria in the PR checklist.
    - Use `gh pr edit <n> --title ... --body-file <path>` (or the `update_pull_request`
      MCP tool when available). Always pass the body via `--body-file`, never as an
      inline shell argument.

## GitHub Operations — `gh` CLI (Primary), MCP Tools (When Available)

- **The `gh` CLI IS available and authenticated in the devcontainer** (`/usr/bin/gh`,
  installed via devcontainer features). Use it for GitHub API operations: PRs, issues,
  reviews, branches, releases.
- **GitHub MCP tools are not present in every session.** Never assume they exist. When
  both are available either path is fine; when they are not, `gh` is the only path.

| Operation | `gh` command | MCP tool (when available) |
|---|---|---|
| Create a PR | `gh pr create --base main --title ... --body-file -` | `create_pull_request` |
| Update a PR (title/body) | `gh pr edit <n> --title ... --body-file -` | `update_pull_request` |
| Read a PR / diff / comments | `gh pr view <n> --json ...` / `gh pr diff <n>` | `pull_request_read` |
| Review a PR | `gh pr review <n>` | `pull_request_review_write` |
| Create / update / close issues | `gh issue create` / `gh issue edit` / `gh issue close` | `issue_write` / `issue_read` |
| List / search issues & PRs | `gh issue list` / `gh search issues` | `list_issues` / `search_issues` |
| Merge a PR | `gh pr merge <n>` | `merge_pull_request` |
| Create / list branches | `git push -u origin <branch>` | `create_branch` / `list_branches` |

- **Multiline bodies:** pass `--body-file <path>`, or `--body-file -` and pipe a
  heredoc. Do not inline a long markdown body as a single shell argument.
- **Prefer `rtk gh ...`** — RTK filters `gh` output and cuts 26-87 % of the tokens.
- **Local git is still fine** for `git add` / `git commit` / `git checkout` / `git push`.

## Planner Specification Rule (Mandatory)
- **Always read `docs/planner-spec.md` before touching any planner code** — engine, cost
  function, SoC simulation, candidate generation, slot population, or safety gates.
- **Every planner change must satisfy all spec invariants**: energy balance per slot, SoC bounds,
  cost identity (`winner.cost == final_output.cost`), terminal-SoC accounting, and safety gates.
- **Update `docs/planner-spec.md`** when a change intentionally alters planner semantics.
  Spec and implementation must never diverge silently.
- **Add or update tests** covering the affected invariants for every planner change.
- A planner PR is not done until: spec is consistent, invariant tests pass, and lint is clean.
- See `AGENTS.md` → **Planner Specification** for the full compliance checklist.

## Documentation Update Rule (Mandatory)
- **All documentation that describes the changed behaviour must be updated in the same PR.**
  This includes, but is not limited to:
  - `docs/planner-guide.md` — planner inputs, outputs, cost function, scenarios
  - `docs/planner-spec.md` — specification invariants and formulas
  - `docs/config-flow-reference.md` — config/options flow step tables
  - `docs/ev-charge-plan-setup.md` — EV planned load setup guide
  - `.github/memories.md` — canonical patterns, module map, open issues
  - `README.md` — user-facing feature descriptions and links
- **Check every docs/ file before closing a PR** — if a file describes something you changed,
  update it. Stale documentation causes confusion and bugs.
- **A PR is not done until all affected docs are consistent with the implementation.**

## Huawei Solar Sensor Rule (Mandatory)
- **Always use entities exposed by `wlcrs/huawei_solar`** for every inverter/battery value.
- Never hard-code numeric battery constants — always source from the live HA entity.
- If a value is needed but not yet wired into HSEM, add it through the full stack:
  `const.py` → `flows/huawei_solar.py` → **`translations/en.json`** (both `config` and
  `options` `huawei_solar` steps) → `models/sensor_config.py` →
  `custom_sensors/config_reader.py` → `custom_sensors/state_collector.py` →
  `models/live_state.py` → `coordinator.py`
- **Always check `docs/huawei_entities.md` first** for the verified list of available HA entities
  before searching the upstream `wlcrs/huawei_solar` repo or guessing an entity ID.
- See `AGENTS.md` → **Huawei Solar Sensor Usage Rule** for the full wiring protocol.

## Canonical Helpers (Mandatory)

These helpers exist — never re-implement them inline:

- **`clamp_efficiency(pct)`** in `utils/misc.py` — converts efficiency % to fraction
- **`calculate_recommended_threshold(...)`** in `utils/misc.py` — discharge threshold with real parameters, never use `cycle_cost * 0.30` as proxy
- **`DISCHARGE_RECS`** and **`CHARGE_RECS`** in `utils/recommendations.py` — canonical frozensets, never redefine locally
- **`HSEM_LOGGER`** in `utils/logger.py` — use for all planner logging, never `logging.getLogger(__name__)`

## File Size Rule (Mandatory)

- **Hard limit: 30 KB AND 1000 lines per file** across the entire codebase.
  Both limits must be satisfied.  A file under 30 KB but over 1000 lines still
  needs splitting.
- If a file exceeds either limit, split it before adding more features.
- Check before every PR:
  ```bash
  # Lines
  find custom_components/hsem -name '*.py' -exec sh -c 'l=$(wc -l < "$1"); [ "$l" -gt 1000 ] && echo "$l $1"' _ {} \;
  # Size
  find custom_components/hsem -name '*.py' -exec sh -c 's=$(wc -c < "$1"); [ "$s" -gt 30720 ] && echo "$s $1"' _ {} \;
  ```

## Issue-Solving Rules
- Always read `AGENTS.md` and `CLAUDE.md` before starting any issue work.
- Solve **one issue only** per branch and PR.
- Do **not** refactor unrelated code.
- Keep behavior unchanged unless the issue explicitly states the current behavior is unsafe or wrong.
- Prefer small, reviewable changes.
- Add tests for every bug fix or new feature.
- Do **not** skip tests unless the repo has no working test setup — if so, explain exactly why.
- Do **not** close the issue manually. Link the PR using `Fixes #ISSUE_NUMBER`.
- Ask the user before making any broad architectural changes.

## Solve One Issue Per Branch
- Each branch should solve **one** issue from the GitHub issue tracker.
- Use the branch naming convention: `<type>/<issue-number>-<description>`
- Examples: `feat/123-add-feature`, `fix/456-resolve-bug`, `chore/789-update-docs`
- Do not combine multiple issues in a single branch or PR.

## Conventional Commits
- Always use [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/) for commit messages and pull request titles.
- Format: `<type>(<scope>): <description>`
- Types: `feat`, `fix`, `chore`, `docs`, `refactor`, `perf`, `test`, `ci`
- Scopes should be specific to the domain being changed (e.g., `sensor`, `flow`, `config`)
- Always include `Fixes #<ISSUE_NUMBER>` in the PR description

## Code Quality
- All code MUST use safe and secure coding practices.
- All code MUST be fully optimized for performance and maintainability.
- Avoid clear passwords, hardcoded secrets, and common security gaps.
- Follow PEP 8 and the project's style guide. See `CODE_QUALITY_STANDARDS.md` for full standards.
- Write type hints for all function parameters and return types.
- Include docstrings for all public modules, classes, functions, and methods.
- **Never use `==` or `!=` to compare floating-point values.** In production code use an epsilon
  guard (`abs(x) > 1e-9` instead of `x != 0`). In tests always use `pytest.approx()`.
- Run `./scripts/quality.sh lint` before every commit.
- Run `./scripts/quality.sh typing` after lint — type checking.
- Run `./scripts/quality.sh quality` after typing — static analysis.
- Run `./scripts/quality.sh test` to run the full test suite with coverage before opening a PR.

## Write Modular Code
- Break code into modules and components for easy reuse.
- Maximize code reuse (DRY principle).
- Minimize technical debt.

## Python Instructions
- Use snake_case for variable and function names.
- Use CamelCase for class names.
- Include type hints for function parameters and return types.
- Write docstrings following PEP 257 conventions.
- Use f-strings for formatting instead of .format() or %.
- Prefer duck-typing tests (hasattr) over isinstance checks.
- Use modern Python 3.9+ syntax.
- Use the union operator (|) for type unions instead of typing.Union.
- Use pathlib for path operations instead of os.path.
- Explicitly set encoding='utf-8' when using open() in text mode.
- Prefer argparse over optparse.
- Use itertools for common iterable operations.
- When creating log statements, never use runtime string formatting — use `%` placeholders and the `extra` argument.

## Always Provide File Names
- Always provide the complete file path in responses.
- Help users understand where code changes should be placed.

## Do Not
- Do not refactor planner or safety logic unless solving a specific issue that requires it.
- Do not change runtime behavior unless specifically requested.
- Do not fix unrelated bugs in the same PR.
- Do not reformat the entire codebase unless required by tooling setup.
- Do not generate code without understanding the context first.
- Do not redefine `DISCHARGE_RECS`, `CHARGE_RECS`, or `clamp_efficiency()` locally — import from canonical locations.
- Do not use `cycle_cost * 0.30` as a threshold proxy — use `calculate_recommended_threshold()`.
- Do not use `break` in slot iteration loops unless the loop is explicitly ordered and early exit is provably correct.

<!-- rtk-instructions v2 -->
# RTK — Token-Optimized CLI

**rtk** is a CLI proxy that filters and compresses command outputs, saving 60-90% tokens.

## Rule

Always prefix shell commands with `rtk`:

```bash
# Instead of:              Use:
git status                 rtk git status
git log -10                rtk git log -10
cargo test                 rtk cargo test
docker ps                  rtk docker ps
kubectl get pods           rtk kubectl get pods
```

## Meta commands (use directly)

```bash
rtk gain              # Token savings dashboard
rtk gain --history    # Per-command savings history
rtk discover          # Find missed rtk opportunities
rtk proxy <cmd>       # Run raw (no filtering) but track usage
```
<!-- /rtk-instructions -->