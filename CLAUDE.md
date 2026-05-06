# Claude Code Instructions for HSEM

This document provides guidance for Claude Code (Claude-powered coding assistant) when working with the HSEM repository.

## Core Principles

1. **Solve One Issue Per Session**
   - Focus on a single GitHub issue at a time
   - Do not combine multiple issues in one session
   - Use clear branch naming: `<type>/<issue-number>-<description>`

2. **Avoid Broad Refactors**
   - Do not perform large refactors unrelated to the issue at hand
   - Do not reformat entire directories unless required for tooling setup
   - Keep changes focused and minimal

3. **Preserve Runtime Behavior**
   - Do not change planner logic or safety features
   - Do not modify configuration loading unless specifically requested
   - Do not change entity behavior unless it's the issue being fixed

4. **Follow Code Quality Standards**
   - Run linting and formatting before committing: `ruff check . && ruff format .`
   - Ensure type hints are present for all public functions
   - Include docstrings for all public modules and functions
   - Write tests for new functionality

## Development Workflow

1. **Start Fresh**
   - Create a new branch from `main`
   - Use conventional commit format
   - Reference the issue number in commits and PR description

2. **Test Thoroughly**
   - Run `pytest` to execute tests
   - Run `tox` to test across Python versions
   - Run `ruff check .` to find linting issues
   - Run `ruff format .` to apply formatting

3. **Commit and Push**
   - Make atomic commits with clear messages
   - Use format: `<type>(<scope>): <description>`
   - Include `Fixes #<ISSUE_NUMBER>` in PR description

## What to Avoid

- ❌ Refactoring planner.py or safety-related modules without explicit issue
- ❌ Reformatting code unrelated to the changes you're making
- ❌ Adding new dependencies without justification
- ❌ Changing logging levels or output formats
- ❌ Modifying configuration loading or schema validation
- ❌ Large PRs that address multiple unrelated issues
- ❌ Committing without running checks first

## What to Do

- ✅ Focus on the specific issue assigned
- ✅ Write clear, maintainable code
- ✅ Include tests for new features
- ✅ Run all checks before committing
- ✅ Ask for clarification if requirements are unclear
- ✅ Document complex logic with comments
- ✅ Keep commits atomic and focused

## Type Hints and Documentation

Always include:
- Type hints for function parameters and return types
- Docstrings for public functions, classes, and modules
- Comments explaining complex logic
- Example usage in docstrings where helpful

Example:
```python
def calculate_consumption_prediction(
    historical_data: list[float],
    weights: dict[str, float],
) -> float:
    """
    Calculate predicted consumption based on historical data and weights.

    Args:
        historical_data: List of historical consumption values
        weights: Dictionary of time window weights

    Returns:
        Predicted consumption value
    """
    return sum(val * weights.get(str(i), 0) for i, val in enumerate(historical_data))
```

## Python Version and Style

- Target: Python 3.13
- Use modern Python 3.9+ syntax (union operator `|`, walrus operator, etc.)
- Follow PEP 8 and PEP 257
- Use f-strings for string formatting
- Prefer pathlib over os.path
- Use type annotations from the `typing` module appropriately

## Testing

- Write tests for all new functions and features
- Use pytest as the test framework
- Place tests in the `tests/` directory
- Use meaningful test names that describe what is being tested
- Run `pytest tests/` to verify tests pass

## Pre-commit Checks

Before committing, run these commands:

```bash
# Check for linting issues
ruff check .

# Format code
ruff format .

# Run tests
pytest tests/

# Type checking
mypy custom_components tests
```

Or use pre-commit hook:
```bash
pre-commit run --all-files
```
