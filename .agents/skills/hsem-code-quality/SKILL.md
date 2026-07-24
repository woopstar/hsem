---
name: hsem-code-quality
description: Activate before every commit and PR to run the full HSEM quality gate pipeline — lint, typing, quality checks, and tests.
---

# HSEM Code Quality — Pre-Commit & Pre-PR Gates

Activate this skill **before every commit** and **before opening any PR**.

## Four Quality Gates — All Must Pass

Run these in order. If any fails, fix the issues before proceeding.

### Gate 1: Lint (`tox -e lint`)

```bash
tox -e lint
```

Runs: isort → black → ruff format → ruff check

This auto-formats and checks for style violations. Fix any remaining lint errors.

### Gate 2: Type Checking (`tox -e typing`)

```bash
tox -e typing
```

Runs: mypy type checking. Must pass with **0 errors**.

Rules:
- `disable_error_code` is empty in `pyproject.toml` — never add new suppressions
- No `# type: ignore` without a comment justifying why

### Gate 3: Quality (`tox -e quality`)

```bash
tox -e quality
```

Runs: pyright + vulture static checks. Must pass with **0 errors**.

### Gate 4: Tests (`tox -e py314`)

```bash
tox -e py314
```

Runs: pytest with coverage on Python 3.14.

For faster iteration during development:
```bash
pytest tests/                          # all tests
pytest tests/test_module.py            # specific file
pytest tests/test_module.py::test_fn   # specific test
```

## File Size Check

Hard limit: **30 KB per file** in `planner/` and `utils/`. Check before PR:
```bash
wc -c custom_components/hsem/planner/*.py
wc -c custom_components/hsem/utils/*.py
```

If a file exceeds 30 KB, split it before adding more features.

## Pre-Commit Hook (Optional but Recommended)

```bash
pre-commit run --all-files
```

## Code Standards Quick Reference

- **Float comparisons**: epsilon guard in production (`abs(x) > 1e-9`), `pytest.approx()` in tests
- **Type hints**: every public function has parameter and return type annotations; use `| None`, not `Optional[...]`
- **Docstrings**: Google-style for public modules, classes, functions, methods
- **`@override`**: every method that overrides a base class method
- **Import order**: standard library → third-party → `homeassistant.*` → `custom_components.hsem.*`
- **String formatting**: f-strings for runtime, `%`-formatting for logging
- **Encoding**: `open(file, encoding='utf-8')` in text mode
- **Paths**: `pathlib` over `os.path`
- **Modular code**: DRY principle, break into components

## Verify Before Commit

```bash
git --no-optional-locks status
```

Only intended changes should appear. If any of the four gates fail, fix them before committing. Do not submit a PR with formatting, linting, typing, or test failures.
