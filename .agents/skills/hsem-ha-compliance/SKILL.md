---
name: hsem-ha-compliance
description: Activate when making changes that touch Home Assistant integration surfaces — config flows, entities, translations, services, device info, async patterns, or platform setup.
---

# HSEM Home Assistant Compliance Checklist

Activate this skill when your change touches any of these HA integration surfaces:
- Config flow (`config_flow.py`, `flows/`)
- Entity classes and platforms
- Translations (`translations/en.json`)
- Services (`services.yaml`)
- Device info, unique IDs
- Async patterns, setup/unload
- Dependencies, `manifest.json`

## Section 1 — Development Checklist (HA Silver/Gold)

### Dependencies
- [ ] No new third-party deps in `manifest.json` without justification
- [ ] Dev-only deps go in `pyproject.toml`
- [ ] All `manifest.json` requirements pinned to exact versions (`"pkg==1.2.3"`)
- [ ] `REQUIREMENTS` constant is deprecated — use `manifest.json` only

### API & Async
- [ ] All device/API-specific code lives in a third-party PyPI library; HA code only interacts with library objects
- [ ] All `hass.async_create_task()` calls have error handling (no fire-and-forget without `try/except`)
- [ ] No blocking calls (`time.sleep`, `requests`, `open()`) on the event loop — offload via `hass.async_add_executor_job()`

### Config Flow
- [ ] Every `async_step_*` returns a proper dict
- [ ] No state leaks between steps
- [ ] `async_migrate_entry` handles version bumps
- [ ] Voluptuous schemas present for all configuration validation
- [ ] Default parameters in schema, not in `setup()`
- [ ] Use generic keys from `homeassistant.const` where possible
- [ ] No `customize` dependency — never depend on users adding things to `customize`

### Translations
- [ ] Every user-facing string has a key in `translations/en.json`
- [ ] Field labels, errors, aborts, boolean/switch fields all present
- [ ] Both `config` and `options` steps updated for `huawei_solar` if applicable

### Entities
- [ ] Correct MRO: mixins before base — `CoordinatorEntity, RestoreEntity, SensorEntity`
- [ ] No bare `Entity`
- [ ] Every entity has `unique_id` (stable, uses config entry ID)
- [ ] Every entity has `device_info` with `identifiers={(DOMAIN, entry.entry_id)}`

### Services
- [ ] Registered in `async_setup_entry`, removed in `async_unload_entry`
- [ ] All have voluptuous schemas

### Platform Communication
- [ ] Share data via `hass.data[DOMAIN]`
- [ ] Notify platforms of updates via `homeassistant.helpers.dispatcher`
- [ ] Prefix all custom event names with the domain name

## Section 2 — Style Guidelines

- [ ] File headers: every `.py` file starts with a docstring describing what the file does
- [ ] Import order: standard library → third-party → `homeassistant.*` → `custom_components.hsem.*`
- [ ] Alphabetical ordering for constants and list/dict content
- [ ] Use HA constants: `CONF_NAME`, `UnitOfEnergy`, `PERCENTAGE`, `Platform`, `STATE_ON`/`STATE_OFF`
- [ ] No hardcoded strings: no `"on"`, `"off"`, `"kWh"`, `"%"`, `"unknown"` — use HA constants
- [ ] Logging: use `%`-formatting, no component name in message, no period at end, restrict `_LOGGER.info`
- [ ] Type hints: every public function, use `| None` (not `Optional`), `@override` on overrides
- [ ] Docstrings: Google-style, summary line (imperative, period)
- [ ] Planner code uses `HSEM_LOGGER` from `utils/logger.py`; non-planner may use `logging.getLogger(__name__)`

## Section 3 — PR Scope Rules

- [ ] Single platform per PR
- [ ] No feature creep — only what the issue requires
- [ ] No mixed cleanups — don't mix refactors with features
- [ ] One issue per PR
- [ ] No unmerged dependencies

## Section 4 — Testing

- [ ] Use mock-based approach (`MagicMock`/`AsyncMock`) — compatible with Windows
- [ ] Config flow tests: fresh install, reconfigure, abort-on-duplicate, validation errors
- [ ] Entity tests: assert `unique_id`, `device_info`, `native_value`, `native_unit_of_measurement`, `state`, `extra_state_attributes`
- [ ] Float comparisons: `pytest.approx()` in tests, epsilon guard in production

## Section 5 — Unload & Cleanup

- [ ] Every listener, timer, task created in setup is cancelled/removed in teardown
- [ ] `hass.data[DOMAIN]` popped on unload
