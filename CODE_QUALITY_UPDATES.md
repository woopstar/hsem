# Code Quality Standards Update Summary

This document summarizes the recent updates to HSEM's code quality standards, focused on Python 3.13 and agentic coding best practices.

## What Changed

### 1. Python Version: Now 3.13 Required

**Files Updated:**
- `AGENTS.md` — Updated Python requirement to 3.13
- `CLAUDE.md` — Updated Python requirement to 3.13 and added verification check
- `.python-version` — Already set to 3.13.5
- `pyproject.toml` — Target version: py313
- `tox.ini` — All environments use Python 3.13

**Action for Developers:**
```bash
# Verify you're using Python 3.13
python --version

# If not, install Python 3.13 and set it locally
# Using pyenv:
pyenv install 3.13.5
pyenv local 3.13.5
```

### 2. Ruff is the Single Source of Truth

**Files Updated:**
- `tox.ini` — Replaced black, isort, flake8, pylint with ruff
- `requirements_lint.txt` — Now only lists ruff and mypy
- `AGENTS.md` — Added mandatory ruff pre-commit requirements
- `CLAUDE.md` — Added ruff formatting as step 1 of workflow
- `CODE_QUALITY_STANDARDS.md` — New document (detailed below)

**What This Means:**
- All code formatting is handled by `ruff format`
- All linting is handled by `ruff check`
- Import sorting is built into ruff
- Complexity checks are built into ruff
- No more debates about code style — ruff decides

**Pre-commit Workflow (REQUIRED):**
```bash
ruff format .        # Step 1: Format
ruff check . --fix   # Step 2: Lint and auto-fix
pytest tests/        # Step 3: Tests
mypy ...             # Step 4: Type checking
```

### 3. Mandatory Ruff Format Before PR

**Files Updated:**
- `AGENTS.md` — Added "REQUIRED: Code Quality Before Submission" section
- `CLAUDE.md` — Emphasized ruff format in Quick Start and "What to Avoid"
- `CODE_QUALITY_STANDARDS.md` — Documented as non-negotiable

**What This Means:**
- Every PR must show evidence of running `ruff format .`
- Every commit should be preceded by ruff format
- PRs that haven't been formatted will be rejected by CI

### 4. New Document: CODE_QUALITY_STANDARDS.md

**Purpose:** Comprehensive guide to code quality for agentic coding

**Includes:**
- Core quality principles (10 principles)
- Type hints requirements
- Docstring standards
- Python 3.13+ feature guidance
- Pre-commit checklist
- CI/CD enforcement rules
- Common ruff violations and fixes
- Specific guidance for AI agents

**Key Principle for Agents:**
> "Always run the pre-commit checklist before creating a commit. Never submit a PR that hasn't passed ruff format and check."

## Files Modified

| File | Changes |
|------|---------|
| `AGENTS.md` | Python 3.13 requirement, ruff format mandate, Definition of Done updates |
| `CLAUDE.md` | Python 3.13 verification, ruff workflow, What to Avoid list |
| `tox.ini` | Replaced old linters with ruff, added format environment |
| `requirements_lint.txt` | Now only ruff and mypy |
| `CODE_QUALITY_STANDARDS.md` | **NEW** — Comprehensive quality guide |

## Quick Reference

### Before You Commit

```bash
# 1. Format code
ruff format .

# 2. Lint and auto-fix
ruff check . --fix

# 3. Type check
mypy custom_components tests

# 4. Test
pytest tests/ -v

# 5. Verify
git status
```

### Before You Submit a PR

```bash
# Ensure all of the above pass, plus:
pytest tests/ --cov=custom_components.hsem --cov-report=html
# Review coverage report to ensure new code is tested
```

### Python Version Check

```bash
python --version  # Must show 3.13.x
```

## Why These Changes?

### For Code Quality
- **Ruff** is faster and more reliable than multiple tools
- **Python 3.13** uses latest features, better type hinting support
- **Automated checks** reduce manual review burden
- **Type hints** catch bugs earlier

### For Agentic Development
- **Deterministic checks** make AI code contributions reliable and predictable
- **Ruff format first** ensures agents can't produce non-compliant code
- **Type hints required** make code intent clear to AI systems
- **Docstrings required** provide context for AI code generation
- **Pre-commit checklist** is straightforward for agents to follow

## CI/CD Changes

GitHub Actions will now run:
1. `ruff format --check` — Verify formatting is correct
2. `ruff check` — Verify linting passes
3. `mypy` — Verify type checking passes
4. `pytest` — Verify tests pass
5. `coverage` — Verify coverage meets threshold

All must pass before merge.

## Next Steps for Developers

1. **Read** `CODE_QUALITY_STANDARDS.md` for detailed guidance
2. **Update Python** to 3.13 if you haven't already
3. **Install tools** from updated `requirements_lint.txt`
4. **Run pre-commit checklist** before next commit
5. **Refer back** to these docs when in doubt

## Questions?

See the relevant documentation:
- **General rules**: `AGENTS.md`
- **Quick reference**: `CLAUDE.md`
- **Detailed standards**: `CODE_QUALITY_STANDARDS.md`
- **Ruff docs**: https://docs.astral.sh/ruff/
- **Python style guide**: https://peps.python.org/pep-0008/
