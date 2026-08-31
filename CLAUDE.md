# Claude Code Instructions for HSEM

This document provides practical guidance for Claude Code (Claude-powered coding assistant) when
working with the HSEM repository.

**Note:** This is a quick reference guide. For comprehensive rules, constraints, and standards,
please refer to `AGENTS.md`.

## Quick Start

1. **Read AGENTS.md first** — Understand the project's constraints, security rules, and Home
   Assistant compliance requirements
2. **Verify Python 3.14** — Ensure you're using Python 3.14 (see `.python-version`)
3. **Create a feature branch** — Use format: `feat/<issue-number>-<description>`
4. **Make focused changes** — Solve one issue at a time
5. **Run quality checks** — `./scripts/quality.sh lint`, `./scripts/quality.sh typing`, `./scripts/quality.sh quality`, `./scripts/quality.sh test`
6. **Submit PR for review** — Do not merge without explicit permission

## Core Principles

1. **One Issue Per Session**

   - Focus on a single GitHub issue at a time
   - Do not combine multiple issues in one session
   - Reference issue number in commits and PR description

2. **Preserve Existing Behavior**

   - Do not refactor unrelated code
   - Do not modify planner logic or safety features unless specifically requested
   - Do not reformat entire directories unless required for tooling setup
   - Keep changes focused and minimal

3. **Code Quality**
   - Run `./scripts/quality.sh lint` before committing (ruff format + ruff check + prettier)
   - Run `./scripts/quality.sh typing` after lint (mypy type checking)
   - Run `./scripts/quality.sh quality` after typing (pyright + vulture)
   - Run `./scripts/quality.sh test` to run tests with coverage before opening a PR
   - Include type hints for all public functions
   - Write docstrings for all public modules, classes, and functions
   - Write tests for new functionality
   - Follow PEP 8 and PEP 257
   - See `CODE_QUALITY_STANDARDS.md` for full standards and conventions

## Utility Function Centralization Rule

**CRITICAL: Check for Code Duplication FIRST**

When implementing a utility or helper function:

1. **Search first**: Check if similar functionality exists in `utils/misc.py` or other utils modules
2. **If used 2+ times**: The function MUST live in utils, NOT in multiple modules
3. **Never duplicate**: Create the function in the appropriate utils module, then import it
   everywhere
4. **DRY Principle**: Do not repeat utility logic across multiple files

**Example of WRONG approach (creates duplicates):**

- Create `_convert_months_to_int()` in `flows/months.py`
- Create `_convert_month_list_to_int()` in `working_mode_sensor.py`
- ❌ Result: Two functions doing the same thing in different places

**Example of CORRECT approach:**

- Create `convert_months_to_int()` in `utils/misc.py` (centralized, public)
- Import it in `flows/months.py`:
  `from custom_components.hsem.utils.misc import convert_months_to_int`
- Import it in `working_mode_sensor.py`:
  `from custom_components.hsem.utils.misc import convert_months_to_int`
- ✅ Result: Single source of truth, easier to maintain

**Common mistake to avoid:**

- Don't create private versions (`_function_name`) in multiple modules thinking they're isolated
- Private functions should still be centralized if used in 2+ places

## Floating-Point Comparisons

**Never use `==` or `!=` to compare floating-point values.**

- In production code: use an epsilon guard instead of exact equality.
  - `abs(x) > 1e-9` instead of `x != 0`
  - `abs(a - b) < 1e-9` instead of `a == b`
- In tests: always use `pytest.approx()` for any assertion involving `float`.
  - `assert result == pytest.approx(0.0)` ✅
  - `assert result == 0.0` ❌
- Integer-valued comparisons (`== 0` on a sum of `int` weights) are fine; only float literals
  and float-typed variables are subject to this rule.

## Planner Specification Compliance (Mandatory)

**Before touching any planner code**, read `docs/planner-spec.md` — it is the single source
of truth for planner semantics.

Rules:

1. **Read the spec first** — applies to engine, cost function, SoC simulation, candidate
   generation, slot population, and safety gates.
2. **Verify consistency** — every change must satisfy the invariants listed under
   _Invariants for tests_ in the spec (energy balance, SoC bounds, cost identity,
   terminal-SoC accounting, safety gate behaviour).
3. **Update the spec** when a change intentionally alters planner semantics. Spec and
   implementation must never diverge silently.
4. **Add or update tests** covering the affected invariants for every planner change.
5. **Definition of Done** for planner work: spec updated (if needed) + invariant tests passing.

Quick checklist before opening a planner PR:

- [ ] `docs/planner-spec.md` read and understood
- [ ] Energy balance holds for every slot
- [ ] SoC stays within configured bounds
- [ ] `winner.cost == final_output.cost` (no post-selection mutation)
- [ ] Terminal SoC affects cost (emptying the battery is not free)
- [ ] No-action baseline includes normal PV/battery self-consumption
- [ ] Read-only / degraded / dry-run gates block hardware writes
- [ ] Spec updated if semantics changed
- [ ] Tests added or updated

## Development Workflow

```bash
# 1. Ensure you're on Python 3.14
python --version  # Should show 3.14.x

# 2. Create a feature branch
git checkout -b feat/<issue-number>-<description>

# 3. Make your changes and write tests

# 4. Format and lint (REQUIRED)
./scripts/quality.sh lint
# 5. Type check (REQUIRED)
./scripts/quality.sh typing
# 6. Quality checks (REQUIRED)
./scripts/quality.sh quality
# 7. Run tests (REQUIRED)
./scripts/quality.sh test

# 8. Verify changes
git status

# 9. Commit with conventional commit format
git commit -m "feat(scope): description - Fixes #<ISSUE_NUMBER>"

# 10. Push the branch (local git over SSH)
git push origin feat/<issue-number>-<description>

# 11. Create the PR with the `gh` CLI, passing the body via --body-file:
#     gh pr create --base main --title "feat(scope): ..." --body-file pr.md
```

> **GitHub operations: use the `gh` CLI.** It is installed and authenticated in the
> devcontainer (`/usr/bin/gh`). GitHub MCP tools are not present in every session, so
> never assume they exist — check, and fall back to `gh`. Prefer `rtk gh ...` to cut
> output tokens. Pass PR/issue bodies via `--body-file`, never as an inline shell
> argument. Local `git` (add/commit/checkout/push) is unchanged.

### Keeping an Open PR Up to Date

Whenever you push additional commits to a branch that already has an open PR:

1. **Update the PR title** if the scope or description has changed.
2. **Update the PR body** to reflect all changes made so far — new files, behaviour changes,
   additional tests, and any newly satisfied acceptance criteria.
3. Tick off completed items in any checklist inside the PR description.
4. Never leave the PR description stale after follow-up commits.
5. **Use `gh pr edit <n> --title ... --body-file <path>`** (or the `update_pull_request`
   MCP tool when the session has it). Always pass the body via `--body-file` so multiline
   markdown survives intact — never inline it as a single shell argument.

## Home Assistant Compliance

Ensure your changes follow Home Assistant integration standards:

- **Architecture**: Use config entries, setup/unload flows, and platform forwarding patterns
- **Entities**: Implement entity model conventions (state, availability, device info, unique IDs,
  naming)
- **Data Updates**: Use `DataUpdateCoordinator` for periodic polling when needed
- **Configuration**: Maintain `config_flow`, diagnostics, and translations as needed
- **Quality**: Target at least Silver quality, aim for Gold
- **Tests**: Add tests for setup flows, coordinator behavior, and entity state handling

See `AGENTS.md` → **Home Assistant Compliance** section for detailed requirements.

## Pre-commit Checklist

**REQUIRED before every commit:**

```bash
# Step 1: Format and lint (MUST be done)
./scripts/quality.sh lint

# Step 2: Type checking
./scripts/quality.sh typing

# Step 3: Quality checks (pyright + vulture)
./scripts/quality.sh quality

# Step 4: Run tests with coverage
./scripts/quality.sh test

# Step 5: Verify no unintended changes
git status

# Step 6: Use pre-commit hooks (optional, but recommended)
pre-commit run --all-files
```

**If any of these checks fail, fix them before committing. Do not submit a PR with formatting or
linting issues.**

## Type Hints and Documentation

Always include type hints and docstrings:

```python
def calculate_consumption_prediction(
    historical_data: list[float],
    weights: dict[str, float],
) -> float:
    """
    Calculate predicted consumption based on historical data and weights.

    Args:
        historical_data: List of historical consumption values in kWh.
        weights: Dictionary mapping time windows to weight factors.

    Returns:
        Predicted consumption value in kWh.

    Raises:
        ValueError: If historical_data is empty or weights contain invalid values.
    """
    if not historical_data:
        raise ValueError("historical_data cannot be empty")
    return sum(val * weights.get(str(i), 0) for i, val in enumerate(historical_data))
```

## Python Version and Style

- **Target**: Python 3.14 (required - see `.python-version`)
- **Syntax**: Use modern Python 3.14+ syntax (union operator `|`, walrus operator, etc.)
- **Style**: Follow PEP 8 and PEP 257 (enforced by ruff)
- **Formatting**: Use f-strings for string formatting
- **Paths**: Prefer `pathlib` over `os.path`
- **Type Annotations**: Use explicit type hints in function signatures
- **Code Quality**: Write code that passes ruff checks without warnings

## Testing

Write tests for all new functions and features:

```bash
# Run all tests
pytest tests/

# Run with coverage
pytest tests/ --cov=hsem --cov-report=html

# Run specific test file
pytest tests/test_module.py

# Run specific test
pytest tests/test_module.py::test_function_name
```

**Test guidelines:**

- Use pytest as the test framework
- Place tests in the `tests/` directory following the same structure as the source
- Use meaningful test names that describe what is being tested
- Test edge cases: missing data, unavailable entities, invalid values
- Test async and concurrent operations for race conditions

## What to Do

✅ Focus on the specific issue assigned ✅ Use Python 3.14 and pass all ruff quality checks ✅ Write
clear, maintainable code with type hints ✅ Include tests for new features and behavior changes ✅
Format and lint code before every commit (`ruff format .` then `ruff check . --fix`) ✅ Ask for
clarification if requirements are unclear ✅ Document complex logic with comments ✅ Keep commits
atomic and focused ✅ Reference `AGENTS.md` for comprehensive rules

## What to Avoid

❌ Submitting a PR without running `./scripts/quality.sh all` first ❌ Ignoring lint warnings or errors
❌ Using Python versions other than 3.14 ❌ Refactoring unrelated code ❌ Changing planner or safety
features without explicit issue ❌ Reformatting code outside your changes ❌ Adding new dependencies
without justification ❌ Changing logging levels or sensitive output ❌ Modifying configuration
without issue requirement ❌ Committing secrets, API keys, or credentials ❌ Merging PRs without
explicit permission

## Security Considerations

- Never commit credentials, API keys, or tokens
- Never log sensitive information in plaintext
- Load secrets from environment variables or secure storage
- Document required environment variables
- See `AGENTS.md` → **Security Constraints** for details

## Need Help?

- **General rules and constraints**: See `AGENTS.md`
- **Home Assistant requirements**: See `AGENTS.md` → **Home Assistant Compliance**
- **Python version issues**: Ensure you're using Python 3.14 from `.python-version`
- **Lint/format errors**: Run `./scripts/quality.sh lint` to auto-fix most issues
- **Unclear requirements**: Stop and ask for clarification before implementing
- **Design decisions**: Refer to `docs/` directory for architecture notes
- **Code quality**: When in doubt, run the full pre-commit checklist

<!-- rtk-instructions v2 -->

# RTK (Rust Token Killer) - Token-Optimized Commands

## Golden Rule

**Always prefix commands with `rtk`**. If RTK has a dedicated filter, it uses it. If not, it passes through unchanged. This means RTK is always safe to use.

**Important**: Even in command chains with `&&`, use `rtk`:

```bash
# ❌ Wrong
git add . && git commit -m "msg" && git push

# ✅ Correct
rtk git add . && rtk git commit -m "msg" && rtk git push
```

## RTK Commands by Workflow

### Build & Compile (80-90% savings)

```bash
rtk cargo build         # Cargo build output
rtk cargo check         # Cargo check output
rtk cargo clippy        # Clippy warnings grouped by file (80%)
rtk tsc                 # TypeScript errors grouped by file/code (83%)
rtk lint                # ESLint/Biome violations grouped (84%)
rtk prettier --check    # Files needing format only (70%)
rtk next build          # Next.js build with route metrics (87%)
```

### Test (60-99% savings)

```bash
rtk cargo test          # Cargo test failures only (90%)
rtk go test             # Go test failures only (90%)
rtk jest                # Jest failures only (99.5%)
rtk vitest              # Vitest failures only (99.5%)
rtk playwright test     # Playwright failures only (94%)
rtk pytest              # Python test failures only (90%)
rtk rake test           # Ruby test failures only (90%)
rtk rspec               # RSpec test failures only (60%)
rtk test <cmd>          # Generic test wrapper - failures only
```

### Git (59-80% savings)

```bash
rtk git status          # Compact status
rtk git log             # Compact log (works with all git flags)
rtk git diff            # Compact diff (80%)
rtk git show            # Compact show (80%)
rtk git add             # Ultra-compact confirmations (59%)
rtk git commit          # Ultra-compact confirmations (59%)
rtk git push            # Ultra-compact confirmations
rtk git pull            # Ultra-compact confirmations
rtk git branch          # Compact branch list
rtk git fetch           # Compact fetch
rtk git stash           # Compact stash
rtk git worktree        # Compact worktree
```

Note: Git passthrough works for ALL subcommands, even those not explicitly listed.

### GitHub (26-87% savings)

```bash
rtk gh pr view <num>    # Compact PR view (87%)
rtk gh pr checks        # Compact PR checks (79%)
rtk gh run list         # Compact workflow runs (82%)
rtk gh issue list       # Compact issue list (80%)
rtk gh api              # Compact API responses (26%)
```

### JavaScript/TypeScript Tooling (70-90% savings)

```bash
rtk pnpm list           # Compact dependency tree (70%)
rtk pnpm outdated       # Compact outdated packages (80%)
rtk pnpm install        # Compact install output (90%)
rtk npm run <script>    # Compact npm script output
rtk npx <cmd>           # Compact npx command output
rtk prisma              # Prisma without ASCII art (88%)
rtk uv run <cmd>        # Compact uv project command output
```

### Files & Search (60-75% savings)

```bash
rtk ls <path>           # Tree format, compact (65%)
rtk read <file>         # Code reading with filtering (60%)
rtk grep <pattern>      # Search grouped by file (75%). Format flags (-c, -l, -L, -o, -Z) run raw.
rtk find <pattern>      # Find grouped by directory (70%)
```

### Analysis & Debug (70-90% savings)

```bash
rtk err <cmd>           # Filter errors only from any command
rtk log <file>          # Deduplicated logs with counts
rtk json <file>         # JSON structure without values
rtk deps                # Dependency overview
rtk env                 # Environment variables compact
rtk summary <cmd>       # Smart summary of command output
rtk diff                # Ultra-compact diffs
```

### Infrastructure (85% savings)

```bash
rtk docker ps           # Compact container list
rtk docker images       # Compact image list
rtk docker logs <c>     # Deduplicated logs
rtk kubectl get         # Compact resource list
rtk kubectl logs        # Deduplicated pod logs
```

### Network (65-70% savings)

```bash
rtk curl <url>          # Compact HTTP responses (70%)
rtk wget <url>          # Compact download output (65%)
```

### Meta Commands

```bash
rtk gain                # View token savings statistics
rtk gain --history      # View command history with savings
rtk discover            # Analyze Claude Code sessions for missed RTK usage
rtk proxy <cmd>         # Run command without filtering (for debugging)
rtk init                # Add RTK instructions to CLAUDE.md
rtk init --global       # Add RTK to ~/.claude/CLAUDE.md
```

## Token Savings Overview

| Category         | Commands                       | Typical Savings |
| ---------------- | ------------------------------ | --------------- |
| Tests            | vitest, playwright, cargo test | 90-99%          |
| Build            | next, tsc, lint, prettier      | 70-87%          |
| Git              | status, log, diff, add, commit | 59-80%          |
| GitHub           | gh pr, gh run, gh issue        | 26-87%          |
| Package Managers | pnpm, npm, npx                 | 70-90%          |
| Files            | ls, read, grep, find           | 60-75%          |
| Infrastructure   | docker, kubectl                | 85%             |
| Network          | curl, wget                     | 65-70%          |

Overall average: **60-90% token reduction** on common development operations.

<!-- /rtk-instructions -->
