# Custom GitHub Copilot Instructions

## Repository Memory (Read First)

**Always read `.github/memories.md` before starting any work.**

It contains:
- Module responsibility map for all planner and utils files
- Canonical patterns you must use (never re-invent)
- MILP variable vector layout (6*n)
- File size limits and oversized files
- Cycle cost formula with the mandatory 2x denominator
- All open refactor and bug issues (#439–#447)
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
- Quality gates: `tox -e lint`, `tox -e typing`, `tox -e quality`, `tox -e py314`

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
   - `tox -e lint` — isort + black + ruff format + ruff check
   - `tox -e typing` — mypy type checking
   - `tox -e quality` — pyright + vulture
   - `tox -e py314` — pytest with coverage
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
    - Use `gh pr edit <PR_NUMBER> --title "..." --body-file <file>` — write the PR body
      to a temp file first, pass it with `--body-file`, then delete the file.
    - **Never** pass a multiline body inline via `--body "..."`: PowerShell corrupts the
      content (newlines become `∙` characters; backticks become `\x5c` escapes).

## Planner Specification Rule (Mandatory)
- **Always read `docs/hsem-planner-spec.md` before touching any planner code** — engine, cost
  function, SoC simulation, candidate generation, slot population, or safety gates.
- **Every planner change must satisfy all spec invariants**: energy balance per slot, SoC bounds,
  cost identity (`winner.cost == final_output.cost`), terminal-SoC accounting, and safety gates.
- **Update `docs/hsem-planner-spec.md`** when a change intentionally alters planner semantics.
  Spec and implementation must never diverge silently.
- **Add or update tests** covering the affected invariants for every planner change.
- A planner PR is not done until: spec is consistent, invariant tests pass, and lint is clean.
- See `AGENTS.md` → **Planner Specification** for the full compliance checklist.

## Documentation Update Rule (Mandatory)
- **All documentation that describes the changed behaviour must be updated in the same PR.**
  This includes, but is not limited to:
  - `docs/hsem-planner-guide.md` — planner inputs, outputs, cost function, scenarios
  - `docs/hsem-planner-spec.md` — specification invariants and formulas
  - `docs/hsem-config-flow-reference.md` — config/options flow step tables
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

- **Hard limit: 30 KB per file** in `planner/` and `utils/`.
- If a file exceeds 30 KB, split it before adding more features.
- Check file size before every PR: `wc -c custom_components/hsem/planner/*.py`

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
- Run `tox -e lint` before every commit (isort + black + ruff format + ruff check in one command).
- Run `tox -e typing` after lint — mypy type checking.
- Run `tox -e quality` after typing (pyright + vulture static checks).
- Run `tox -e py314` to run the full test suite with coverage before opening a PR.

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
