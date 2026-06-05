# HSEM Compliance Checklist — Read Before Any Code Change

This checklist enforces Home Assistant development guidelines from:
- https://developers.home-assistant.io/docs/creating_component_code_review
- https://developers.home-assistant.io/docs/development_guidelines
- Validated in issue #491

Apply these rules to **every** PR, regardless of scope.

---

## Pre-Flight

```bash
git checkout main
git pull
cat .github/memories.md
```

Branch naming: `<type>/<issue-number>-<slug>`

---

## Section 1 — Development Checklist (HA Silver/Gold)

- [ ] **Dependencies**: No new third-party deps in `manifest.json` without justification. Dev-only deps go in `pyproject.toml`.
- [ ] **Requirement pinning**: All `manifest.json` requirements must be pinned to exact versions (`"pkg==1.2.3"`). The `REQUIREMENTS` constant is deprecated — use `manifest.json` only.
- [ ] **API isolation**: All device/API-specific code lives in a third-party PyPI library. Home Assistant code only interacts with library objects, never makes direct HTTP calls (`requests.get(...)`).
- [ ] **Async patterns**: All `hass.async_create_task()` calls have error handling. No fire-and-forget without `try/except`. No blocking calls (`time.sleep`, `requests`, `open()`) on the event loop — offload via `hass.async_add_executor_job()`.
- [ ] **Config flow**: Every `async_step_*` returns a proper dict. No state leaks between steps. `async_migrate_entry` handles version bumps.
- [ ] **Voluptuous schemas**: Present for all configuration validation. Default parameters in schema, not in `setup()`. Use generic keys from `homeassistant.const` where possible. If using `PLATFORM_SCHEMA` with `EntityComponent`, import base from `homeassistant.helpers.config_validation`.
- [ ] **No `customize` dependency**: Never depend on users adding things to `customize` to configure behavior.
- [ ] **Translations**: Every user-facing string (field labels, errors, aborts) has a key in `translations/en.json`. Boolean/switch fields especially.
- [ ] **Entity base classes**: Correct MRO — mixins before base: `CoordinatorEntity, RestoreEntity, SensorEntity`. No bare `Entity`.
- [ ] **Services**: Registered in `async_setup_entry`, removed in `async_unload_entry`. All have voluptuous schemas.
- [ ] **Device info**: Every entity has `unique_id` (stable, uses config entry ID) and `device_info` with `identifiers={(DOMAIN, entry.entry_id)}`.
- [ ] **Unload**: Every listener, timer, task created in setup is cancelled/removed in teardown. `hass.data[DOMAIN]` popped.
- [ ] **Platform communication**: Share data via `hass.data[DOMAIN]`. Notify platforms of updates via `homeassistant.helpers.dispatcher`.
- [ ] **Event names**: Prefix all custom event names with the domain name (e.g., `hsem_person` not `person`).

---

## Section 2 — Style Guidelines

- [ ] **File headers**: Every `.py` file starts with a docstring describing what the file does. Example: `"""Support for HSEM battery planner."""`
- [ ] **Import order**: Standard library → third-party → `homeassistant.*` → `custom_components.hsem.*`
- [ ] **Alphabetical ordering**: Constants and the content of lists/dictionaries should be in alphabetical order.
- [ ] **HA constants**: Always check [`homeassistant/const.py`](https://github.com/home-assistant/core/blob/dev/homeassistant/const.py) before defining your own. Use `CONF_NAME`, `UnitOfEnergy`, `PERCENTAGE`, `Platform`, `STATE_ON`/`STATE_OFF`, etc.
- [ ] **No hardcoded strings**: No `"on"`, `"off"`, `"kWh"`, `"%"`, `"unknown"` — use HA constants.
- [ ] **String formatting**: Prefer f-strings (`f"{value}"`) over `%` or `.format()`. **Exception**: logging uses `%`-formatting to avoid evaluating the string when the log level is suppressed.
- [ ] **Logging rules**: No component/platform name in log messages (added automatically). No period at the end of log messages. Never log API keys, tokens, usernames, or passwords. Restrict `_LOGGER.info` — use `_LOGGER.debug` for non-user-facing details.
- [ ] **Type hints**: Every public function has parameter and return type annotations. Use `| None` (Python 3.10+), not `Optional[...]`.
- [ ] **Type narrowing**: Use `assert` inside `TYPE_CHECKING` blocks (not at runtime) to help the type checker narrow types.
- [ ] **Docstrings**: Google-style. Summary line (imperative, period) → blank line → description → Args → Returns → Raises. Omit types from docstring when already in type annotations. Private methods: one-line summary is fine.
- [ ] **Comments**: Full sentences ending with a period.
- [ ] **`@override`**: Every method that overrides a base class method has `@override` decorator.
- [ ] **Config migration**: `async_migrate_entry` present if `CONFIG_VERSION > 1`.
- [ ] **Logging**: Planner code uses `HSEM_LOGGER` from `utils/logger.py`. Non-planner code may use `logging.getLogger(__name__)`.

---

## Section 3 — PR Scope Rules

- [ ] **Single platform**: Limit to one platform per PR.
- [ ] **No feature creep**: Do not add features not directly needed by the issue.
- [ ] **No mixed cleanups**: Do not mix refactors/cleanups with feature work in one PR.
- [ ] **One issue per PR**: Do not solve multiple issues in a single PR.
- [ ] **No unmerged dependencies**: Do not submit PRs that depend on other unmerged work.
- [ ] **Sequential PRs**: For dependent work, branch `next` off `current`, rebase after merge, then submit.

---

## Section 4 — Testing

- [ ] **Fixtures**: Use mock-based approach (`MagicMock`/`AsyncMock`) — compatible with Windows.
- [ ] **Config flow tests**: Cover fresh install, reconfigure, abort-on-duplicate, validation errors.
- [ ] **Entity tests**: Assert `unique_id`, `device_info`, `native_value`, `native_unit_of_measurement`, `state`, `extra_state_attributes`.
- [ ] **Float comparisons**: Always use `pytest.approx()` in tests, epsilon guard (`abs(x) > 1e-9`) in production.

---

## Section 5 — Type Checking

- [ ] `disable_error_code` is **empty** in `pyproject.toml`. Never add new suppressions.
- [ ] `mypy` passes with 0 errors on all source files.
- [ ] No `# type: ignore` without a comment justifying why.

---

## Canonical Helpers — Never Re-Invent

| Helper | Location | Use for |
|---|---|---|
| `clamp_efficiency(pct)` | `utils/misc.py` | Efficiency % → fraction |
| `calculate_recommended_threshold(...)` | `utils/misc.py` | Discharge threshold |
| `DISCHARGE_RECS` / `CHARGE_RECS` | `utils/recommendations.py` | Recommendation checks |
| `HSEM_LOGGER` | `utils/logger.py` | Planner logging |

---

## Quality Gates (All Four Must Pass)

```bash
tox -e lint      # isort + black + ruff format + ruff check
tox -e typing    # mypy — 0 errors
tox -e quality   # pyright + vulture — 0 errors
tox -e py314     # pytest with coverage
```

---

## Commit & PR

- Conventional Commits: `<type>(<scope>): <description>`
- PR description: summary, files changed, what changed and why, tests, tox results
- Include `Fixes #<ISSUE_NUMBER>` if applicable
- Never merge without explicit permission
