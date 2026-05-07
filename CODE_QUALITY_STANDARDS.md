# Code Quality Standards for HSEM

This document defines code quality standards tailored for agentic coding and AI-assisted development. All contributions must adhere to these standards to maintain code consistency, security, and maintainability.

## Overview

HSEM uses **ruff** as the single source of truth for code quality. All formatting, linting, and style decisions are automated and enforced through ruff. This ensures:

- **Consistency**: All code follows identical formatting rules
- **Determinism**: Code quality is reproducible and predictable
- **Reduced Review Friction**: Automated checks catch issues before review
- **Agentic Reliability**: AI agents can reliably produce code that passes checks

## Core Quality Principles

### 1. **Ruff is Non-Negotiable**

Before every commit:
```bash
ruff format .        # Format all code
ruff check . --fix   # Auto-fix linting issues
```

These must pass without warnings or errors.

### 2. **Type Hints Required**

All public functions, methods, and class attributes must have explicit type hints:

```python
def calculate_production(
    solar_output: float,
    efficiency: float,
) -> float:
    """Calculate system production."""
    return solar_output * efficiency
```

**Why**: Type hints catch errors early, enable better IDE support, and make code intent clear for agents.

### 3. **Docstrings for Public APIs**

All public modules, classes, functions, and methods must have docstrings:

```python
def predict_consumption(
    history: list[float],
    forecast: list[float],
) -> float:
    """
    Predict energy consumption for the next period.
    
    Args:
        history: Historical consumption values in kWh.
        forecast: Weather forecast data for prediction.
    
    Returns:
        Predicted consumption in kWh.
    
    Raises:
        ValueError: If inputs are empty or invalid.
    """
    if not history or not forecast:
        raise ValueError("Inputs cannot be empty")
    return sum(history) / len(history)
```

### 4. **Python 3.13+ Features**

Always use modern Python syntax:

| Old | New | Reason |
|-----|-----|--------|
| `typing.Union[int, str]` | `int \| str` | More readable, native to Python 3.10+ |
| `if x is not None` | `if x` (with type guard) | Cleaner, less boilerplate |
| `.format()` | f-strings | Better performance and readability |
| `os.path.join()` | `pathlib.Path()` | Object-oriented, chainable |

### 5. **No Technical Debt**

Code must not:
- Introduce unused imports or variables
- Create dead code branches
- Add commented-out code blocks
- Leave TODO comments without issue references
- Exceed cyclomatic complexity thresholds

### 6. **Error Handling is Explicit**

Never silently ignore errors:

```python
# ❌ Bad: Silent failure
try:
    value = parse_sensor_data(raw_data)
except Exception:
    pass

# ✅ Good: Explicit error handling
try:
    value = parse_sensor_data(raw_data)
except ValueError as e:
    logger.error("Invalid sensor data: %s", e)
    raise
except SensorUnavailableError:
    logger.warning("Sensor currently unavailable")
    return None
```

### 7. **Security by Default**

- Never log credentials, tokens, or sensitive data
- Use environment variables for secrets (never hardcoded)
- Validate all inputs, especially from external sources
- Document security assumptions in comments

### 8. **Testing is Part of the Definition of Done**

Every feature change must include tests:

```bash
# Run tests before any commit
pytest tests/ -v

# Verify coverage
pytest tests/ --cov=custom_components.hsem --cov-report=html
```

### 9. **Comments Explain Why, Not What**

Code should be self-documenting. Comments explain non-obvious decisions:

```python
# ❌ Bad: Comments explain what the code does
# Loop through sensors
for sensor in sensors:
    # Get the value
    value = sensor.get_value()

# ✅ Good: Comments explain why
# We process sensors in order to maintain consistent ordering
# across multiple coordinator updates (see issue #123)
for sensor in sensors:
    value = sensor.get_value()
```

### 10. **Files Should Be Focused**

- One class per file (or closely related classes)
- Modules should have a single, clear responsibility
- Maximum 500 lines per file (consider refactoring if larger)
- Related functionality grouped in the same directory

## Pre-commit Quality Checklist

Before every commit, run:

```bash
# 1. Format code
ruff format .

# 2. Lint and auto-fix issues
ruff check . --fix

# 3. Type checking
mypy custom_components tests

# 4. Run all tests
pytest tests/ -v

# 5. Verify no unintended changes
git status

# 6. Check coverage doesn't decrease
pytest tests/ --cov=custom_components.hsem --cov-report=term-missing
```

If any step fails, fix the issues before committing.

## CI/CD Enforcement

The following checks run on every PR:

| Check | Tool | Purpose |
|-------|------|---------|
| Formatting | `ruff format --check` | Ensures consistent code style |
| Linting | `ruff check` | Catches bugs, style issues, complexity |
| Type Checking | `mypy` | Catches type errors and unsafe code |
| Tests | `pytest` | Verifies functionality |
| Coverage | `coverage` | Ensures new code is tested |

**All checks must pass before merge.**

## Python Version

- **Target**: Python 3.13
- Check `.python-version` for the required version
- Use `pyenv` or `asdf` to manage multiple Python versions locally

## Ruff Configuration

The project's ruff configuration (in `pyproject.toml`) enforces:

- **Line Length**: 100 characters
- **Complexity**: Favor small, focused functions
- **Imports**: Sorted using isort rules
- **Comprehensions**: Prefer built-in methods over explicit loops
- **Upgrades**: Use latest Python 3.13+ features

See `pyproject.toml` for the full ruff configuration.

## Common Ruff Violations and Fixes

### E501: Line Too Long

```bash
# Run ruff format to fix automatically
ruff format .
```

### F841: Local variable assigned but never used

```python
# ❌ Bad
result = calculate_value()  # Never used

# ✅ Good
result = calculate_value()
return result
```

### C901: Function too complex

```python
# ❌ Bad: Multiple nested conditionals
def process_data(sensor_data):
    if condition1:
        if condition2:
            if condition3:
                # complex logic

# ✅ Good: Refactored into smaller functions
def process_data(sensor_data):
    if not _validate_data(sensor_data):
        return None
    return _extract_value(sensor_data)

def _validate_data(data):
    # isolated logic

def _extract_value(data):
    # isolated logic
```

### UP009: Using PEP 585 generics

```python
# ❌ Bad (Python 3.8 style)
from typing import List, Dict
def process(items: List[int]) -> Dict[str, int]:
    pass

# ✅ Good (Python 3.13 style)
def process(items: list[int]) -> dict[str, int]:
    pass
```

## For AI Agents

When working with this codebase, ensure:

1. **Always run the pre-commit checklist** before creating a commit
2. **Never submit a PR** that hasn't passed ruff format and check
3. **Ask for clarification** if code quality requirements conflict with implementation needs
4. **Reference issues** for any technical decisions or trade-offs
5. **Write tests alongside features** — testing is not optional
6. **Document non-obvious patterns** in comments and docstrings

## Continuous Improvement

Code quality standards evolve. When you encounter patterns or issues not addressed here:

1. Document the pattern
2. Add it to ruff configuration if it should be automated
3. Update this document
4. Reference the change in related issues

## Questions?

- **Ruff Documentation**: https://docs.astral.sh/ruff/
- **Python Style Guide**: https://peps.python.org/pep-0008/
- **Type Hints**: https://peps.python.org/pep-0484/
- **Project Issues**: Check GitHub issues for architectural decisions
