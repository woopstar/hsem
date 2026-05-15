# HSEM Static Quality Checks

This document describes the static quality tools available in HSEM and how to run them locally.

## Tools

| Tool | Purpose | Config |
|------|---------|--------|
| **Pyright** | Type checking (CI-friendly Pylance equivalent) | `pyrightconfig.json` |
| **Vulture** | Dead-code and unused-symbol detection | `vulture_whitelist.py` |
| **mypy** | Legacy type checking | `pyproject.toml [tool.mypy]` |
| **ruff** | Linting and formatting | `pyproject.toml [tool.ruff]` |
| **black** | Code formatting | `tox.ini [testenv:lint]` |
| **isort** | Import sorting | `tox.ini [testenv:lint]` |

## Local Commands

### Run all lint/format checks (isort + black + ruff)

```bash
tox -e lint
```

### Run Pyright type checker

```bash
python -m pyright
```

This reads `pyrightconfig.json` and checks `custom_components/hsem` and `tests/`.

### Run Vulture dead-code detector

```bash
python -m vulture custom_components/hsem tests vulture_whitelist.py --min-confidence 80
```

The `vulture_whitelist.py` file suppresses false positives for Home Assistant lifecycle
methods (e.g. `async_setup_entry`, config flow steps) that Vulture would otherwise flag as
unused because they are called dynamically by HA.

### Run all quality checks (Pyright + Vulture)

```bash
tox -e quality
```

### Run tests

```bash
pytest tests/
```

## Pyright Configuration

`pyrightconfig.json` is set to `typeCheckingMode: "basic"` — a safe starting point for an
HA integration that uses many dynamic patterns.  Do **not** upgrade to `strict` mode without
first resolving the known false-positive list.

### Severity levels

| Rule | Level | Reason |
|------|-------|--------|
| `reportMissingTypeStubs` | none | HA stubs are incomplete |
| `reportUnknownMemberType` | none | HA uses `Any` extensively |
| `reportUnknownVariableType` | none | HA uses `Any` extensively |
| `reportUnknownArgumentType` | none | HA uses `Any` extensively |
| `reportTypedDictNotRequiredAccess` | none | HA flow results use TypedDict with all-optional keys |

### Known remaining warnings (~156 total)

Most remaining warnings fall into two categories that are **HA framework limitations**,
not bugs in HSEM:

1. **`CoordinatorEntity` generic invariance** (~38 warnings in `custom_sensors/*.py`):
   All HSEM sensors inherit from `CoordinatorEntity[HSEMDataUpdateCoordinator]` but HA's
   generic `CoordinatorEntity[DataUpdateCoordinator[dict[str, Any]]]` is invariant.
   These are safe at runtime; the correct fix is to wait for HA to widen the generic.

2. **Test mock patterns** (~100 warnings in `tests/`):
   Tests use partial mocks, `MagicMock`, and stub objects that don't have full type
   annotations.  These are safe and intentional.

## Vulture Whitelist

`vulture_whitelist.py` documents all HA dynamic entry points.  **Before deleting any function
that Vulture flags**, check whether it belongs to one of these categories:

- HA integration lifecycle: `async_setup_entry`, `async_unload_entry`, `async_migrate_entry`
- Config/options flow steps: `async_step_user`, `async_step_init`, etc.
- Diagnostics: `async_get_config_entry_diagnostics`
- Platform setup: `async_setup_entry` in `sensor.py`, `select.py`, `switch.py`, `time.py`
- Entity properties used by HA: `device_info`, `native_value`, `is_on`, etc.

If in doubt, **add to the whitelist** rather than deleting.

## CI Integration

Pyright and Vulture run in CI as the `quality` job in `.github/workflows/lint-and-test.yml`.
Both are currently set to `continue-on-error: true` (staged rollout) to avoid blocking
PRs until the warning baseline is fully resolved.

**Next steps to harden CI:**
1. Resolve the remaining `CoordinatorEntity` invariance warnings (requires HA framework fix
   or a type-ignore comment on each `super().__init__()` call).
2. Set `continue-on-error: false` in the CI workflow once the warning count is zero.
3. Add `tox -e quality` to the local pre-commit checklist.
