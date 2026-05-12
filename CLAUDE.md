# Claude Code Instructions for HSEM

This document provides practical guidance for Claude Code (Claude-powered coding assistant) when
working with the HSEM repository.

**Note:** This is a quick reference guide. For comprehensive rules, constraints, and standards,
please refer to `AGENTS.md`.

## Quick Start

1. **Read AGENTS.md first** — Understand the project's constraints, security rules, and Home
   Assistant compliance requirements
2. **Verify Python 3.13** — Ensure you're using Python 3.13 (see `.python-version`)
3. **Create a feature branch** — Use format: `feat/<issue-number>-<description>`
4. **Make focused changes** — Solve one issue at a time
5. **Run ruff before committing** — Format AND lint before any commit
6. **Run all checks** — Linting, formatting, tests, type checking
7. **Submit PR for review** — Do not merge without explicit permission

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
   - Run linting and formatting before committing
   - Include type hints for all public functions
   - Write docstrings for all public modules, classes, and functions
   - Write tests for new functionality
   - Follow PEP 8 and PEP 257

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

## Development Workflow

```bash
# 1. Ensure you're on Python 3.13
python --version  # Should show 3.13.x

# 2. Create a feature branch
git checkout -b feat/<issue-number>-<description>

# 3. Make your changes and write tests

# 4. Format code with ruff (REQUIRED)
ruff format .

# 5. Lint and auto-fix
ruff check . --fix

# 6. Run tests
pytest tests/

# 7. Verify changes
git status

# 8. Commit with conventional commit format
git commit -m "feat(scope): description - Fixes #<ISSUE_NUMBER>"

# 9. Push and create PR (do not merge)
git push origin feat/<issue-number>-<description>

# 10. If the PR already exists and you make further commits, update it
gh pr edit <PR_NUMBER> --title "<type>(scope): updated title" --body "$(cat pr_body.md)"
```

### Keeping an Open PR Up to Date

Whenever you push additional commits to a branch that already has an open PR:

1. **Update the PR title** if the scope or description has changed.
2. **Update the PR body** to reflect all changes made so far — new files, behaviour changes,
   additional tests, and any newly satisfied acceptance criteria.
3. Tick off completed items in any checklist inside the PR description.
4. Never leave the PR description stale after follow-up commits.

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
# Step 1: Format code with ruff (MUST be done)
ruff format .

# Step 2: Lint and auto-fix
ruff check . --fix

# Step 3: Run tests
pytest tests/

# Step 4: Type checking
mypy custom_components tests

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

- **Target**: Python 3.13 (required - see `.python-version`)
- **Syntax**: Use modern Python 3.13+ syntax (union operator `|`, walrus operator, etc.)
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

✅ Focus on the specific issue assigned ✅ Use Python 3.13 and pass all ruff quality checks ✅ Write
clear, maintainable code with type hints ✅ Include tests for new features and behavior changes ✅
Format and lint code before every commit (`ruff format .` then `ruff check . --fix`) ✅ Ask for
clarification if requirements are unclear ✅ Document complex logic with comments ✅ Keep commits
atomic and focused ✅ Reference `AGENTS.md` for comprehensive rules

## What to Avoid

❌ Submitting a PR without running `ruff format .` first ❌ Ignoring ruff linting warnings or errors
❌ Using Python versions other than 3.13 ❌ Refactoring unrelated code ❌ Changing planner or safety
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
- **Python version issues**: Ensure you're using Python 3.13 from `.python-version`
- **Ruff format errors**: Run `ruff check . --fix` to auto-fix most issues
- **Unclear requirements**: Stop and ask for clarification before implementing
- **Design decisions**: Refer to `docs/` directory for architecture notes
- **Code quality**: When in doubt, run the full pre-commit checklist
