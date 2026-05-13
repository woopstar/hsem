# AGENTS.md — Home Assistant Solar Energy Management

This document is intended for AI coding agents (e.g., OpenAI Copilot, Claude Code) working in this
repository. It defines setup, constraints, workflow, safety rules, and quality expectations for HSEM
(Home Assistant Solar Energy Management) development.

Agents must follow this document strictly.

## Agent Objectives

- Implement and maintain HSEM features for Home Assistant.
- Keep changes minimal, isolated, and testable.
- Prefer deterministic, explicit implementations over implicit or heuristic behavior.
- Never fabricate missing technical details.
- Maintain compatibility with Home Assistant Silver quality standards and work toward Gold.

## No-Assumption Rule (Facts Only)

If required technical details are missing, the agent MUST:

- Locate the information inside this repository, Home Assistant documentation, or referenced
  dependencies, or
- Explicitly request clarification before implementing a dependent solution.

The agent must NOT:

- Invent API endpoints or protocols
- Guess authentication or service flows
- Introduce undocumented environment variables
- Fabricate integration requirements or constraints

If there is uncertainty, stop and request clarification.

## Repository Structure

- `hsem/` — Core integration and business logic
- `tests/` — Unit and integration tests
- `docs/` — Architecture documentation and design decisions
- `scripts/` — Development utilities and testing scripts
- `.github/` — GitHub Actions workflows and configurations

## Environment & Setup

The agent must use the exact versions defined in the project configuration files.

### Requirements

- Runtime: Python 3.13 (required - see `.python-version`)
- Follow versions specified in `requirements.txt` and/or `setup.py`

## Huawei Solar Sensor Usage Rule (Mandatory)

**Every hardware value consumed or written by HSEM MUST use the entity exposed by the
[`wlcrs/huawei_solar`](https://github.com/wlcrs/huawei_solar) Home Assistant integration.**

The agent MUST:

1. **Before using any battery/inverter value**, check `docs/huawei_entities.md` in this
   repository first — it is the canonical, verified list of every entity exposed by the
   `wlcrs/huawei_solar` integration on this installation.  Only fall back to searching
   `number.py`, `sensor.py`, and `select.py` in that repository when you need a register name
   or an entity that is not yet listed in `docs/huawei_entities.md`.
2. **If the entity already exists in HSEM** (in `flows/huawei_solar.py`, `sensor_config.py`,
   `config_reader.py`, `state_collector.py`, and `live_state.py`): re-use it — never hard-code
   the value.
3. **If the entity exists in `wlcrs/huawei_solar` but is NOT yet wired into HSEM**: add it through
   the full stack in this order:
   - `const.py` — add a default entity-id string under `DEFAULT_CONFIG_VALUES`
   - `flows/huawei_solar.py` — add to the schema and validation
   - `translations/en.json` — add `data` label and `data_description` for the new field in
     **both** `config.step.huawei_solar` and `options.step.huawei_solar`
   - `models/sensor_config.py` — add the `str | None` field
   - `custom_sensors/config_reader.py` — read from config entry
   - `custom_sensors/state_collector.py` — read the HA entity state
   - `models/live_state.py` — add the field to `LiveState`
   - `coordinator.py` — pass to `PlannerInput` (if planner-relevant)
4. **Never use a fixed numeric constant** for a value that the inverter reports (e.g. max SoC,
   charge cutoff, rated capacity).  Always source it from the live entity.

**Key entity mappings** (register name → HA entity id pattern):

| Register / source | Entity | Meaning |
|---|---|---|
| `STORAGE_CHARGING_CUTOFF_CAPACITY` | `number.batteries_end_of_charge_soc` | Max SoC during charging (90-100 %) |
| `STORAGE_GRID_CHARGE_CUTOFF_STATE_OF_CHARGE` | `number.batteries_grid_charge_cutoff_soc` | Max SoC when charging **from grid** |
| `STORAGE_DISCHARGING_CUTOFF_CAPACITY` | `number.batteries_end_of_discharge_soc` | Min SoC floor |
| `STORAGE_MAXIMUM_CHARGING_POWER` | `number.batteries_maximum_charging_power` | Max charge power (W) |
| `STORAGE_MAXIMUM_DISCHARGING_POWER` | `number.batteries_maximum_discharging_power` | Max discharge power (W) |
| `STORAGE_STATE_OF_CAPACITY` | `sensor.batteries_state_of_capacity` | Current SoC (%) |
| `STORAGE_RATED_CAPACITY` | `sensor.batteries_rated_capacity` | Nameplate capacity (Wh) |
| `STORAGE_WORKING_MODE_SETTINGS` | `select.batteries_working_mode` | Working mode select |
| `STORAGE_EXCESS_PV_ENERGY_USE_IN_TOU` | `select.batteries_excess_pv_energy_use_in_tou` | Excess PV use mode in TOU |
| `STORAGE_HUAWEI_LUNA2000_TOU_…_PERIODS` | `sensor.batteries_tou_charging_and_discharging_periods` | TOU period schedule |
| `HuaweiSolarActivePowerControlModeEntity` | `sensor.inverter_active_power_control` | Active power / export control mode |

**Always check `docs/huawei_entities.md` first** before searching the upstream repo or guessing
an entity ID. If a new entity is confirmed to exist in HA, add it to `docs/huawei_entities.md`
as part of the same PR that wires it into HSEM.

## HSEM Development Rules

Solar energy systems must be treated as external hardware interfaces.

The agent must:

- Avoid changes that require physical hardware validation unless:
  - Proper mocks are provided, or
  - A clear manual test plan is included.
- Model energy flows and power calculations conservatively and explicitly.
- Ensure network calls include reasonable timeouts.
- Avoid infinite retry loops.
- Handle disconnections and sensor unavailability gracefully.
- Document assumptions about sensor data accuracy and availability.

If credentials, API keys, or tokens are required:

- Never commit them.
- Never log them in plaintext.
- Always load them from environment variables or secure storage.
- Document required environment variables clearly.

## Logging & Error Handling

- Never log credentials, tokens, certificates, or sensitive identifiers.
- Prefer structured errors and logging where applicable.
- Fail explicitly rather than silently ignoring errors.
- Surface actionable error messages that help users understand and resolve issues.
- ALWAYS test for race conditions in relevant async/concurrent flows before considering a change
  complete.

## Code Standards

- Follow existing project formatting and naming conventions.
- Do not introduce large refactors in the same change as functional modifications unless explicitly
  requested.
- Keep commits small and focused.
- Avoid introducing new dependencies unless justified and discussed with the user.
- Apply Python style rules as defined in `pyproject.toml` and `tox.ini`.
- Run `tox -e lint` locally before committing (runs isort, black, ruff format, ruff check).
- See `CODE_QUALITY_STANDARDS.md` for full quality rules and conventions.
- **Never use `==` or `!=` to compare floating-point values.** In production code use an epsilon
  guard (e.g. `abs(x) > 1e-9` instead of `x != 0`). In tests always use `pytest.approx()`.

### Utility Function Centralization (No Duplication Rule)

Utility and helper functions must NEVER be duplicated across modules. Follow these rules:

**Rule: If a utility function is used in 2 or more modules, it belongs in `utils/`**

1. **Before writing any utility function**, search existing code:

   - Check `utils/misc.py` for similar functions
   - Check other `utils/*.py` modules
   - Search for regex patterns that might match the functionality

2. **If found**: Import and reuse the existing function

   - Never create a duplicate with a different name
   - Never create a local version in your module

3. **If NOT found AND will be used 2+ times**: Create in utils

   - Add to `utils/misc.py` (or appropriate utils module)
   - Use public name (no leading underscore for functions meant to be reused)
   - Document with proper docstring
   - Import in all locations that need it

4. **If a one-off helper** that's ONLY used in one module:
   - Can be private (`_function_name()`) in that module
   - But if needs grow, refactor to utils immediately

**Real Example - Month Conversion (Anti-pattern):**

## Home Assistant Compliance

The integration MUST comply with Home Assistant integration standards and developer guidelines.

The agent must:

- Follow Home Assistant architecture patterns for config entries, setup/unload flows, and platform
  forwarding.
- Implement entities according to Home Assistant entity model conventions (state, availability,
  device info, unique IDs, and naming).
- Use `DataUpdateCoordinator` where periodic or shared polling is required.
- Provide and maintain `config_flow`, diagnostics/repair handling (when relevant), and translations.
- Keep `manifest.json` and supported features aligned with Home Assistant requirements.
- Ensure changes maintain at least Home Assistant Silver quality expectations, and move toward Gold
  where feasible.
- Add or update tests for behavior changes, especially setup flows, coordinator behavior, and entity
  state handling.

## Git Workflow

Branch naming convention:

```
feat/<issue-number>-<description>    - for introducing new features
fix/<issue-number>-<description>     - for fixing bugs
chore/<issue-number>-<description>   - for repository and code chores
docs/<issue-number>-<description>    - for documentation updates
refactor/<issue-number>-<description> - for code refactoring
```

All new branches MUST be based on the default branch (typically `main` or `master`), unless the user
explicitly instructs otherwise.

All code changes MUST start from a dedicated branch following the naming convention above.

The agent must NEVER push directly to the default branch and NEVER merge directly without explicit
user permission.

Before creating a commit, the agent MUST report the result of:

- `git status`
- Any local linting or formatting checks
- Relevant test runs for the change

## Pull Request Guidelines

**REQUIRED: Code Quality Before Submission**

Before submitting a PR, the agent MUST:

- Run `tox -e lint` to format and lint all code (runs isort, black, ruff format, and ruff check)
- Run all tests locally: `pytest tests/`
- Verify `git status` shows only intended changes
- Commit changes with: `git commit -m "<type>(<scope>): <description>"`

Each PR should include:

- A clear, descriptive title following Conventional Commits format
- A description of changes made
- Test strategy (automated test coverage or manual testing plan)
- Known limitations or open questions
- Any required configuration changes
- Reference to related GitHub issues using `Fixes #<issue-id>` if applicable

The agent must NOT merge a PR without explicit user permission.

### Keeping an Open PR Up to Date

If a PR already exists for the current branch and work continues on it, the agent MUST update the
PR after every meaningful commit:

- **Title** — keep it accurate to the current scope using Conventional Commits format.
- **Description** — reflect every change made since the PR was opened: new files, updated logic,
  additional tests, and any acceptance criteria that were added or completed.
- **Checklist** — tick off acceptance criteria that are now satisfied.
- Use `gh pr edit --body-file <file>` to apply updates — write the PR body to a
  temporary file first and pass the path via `--body-file`. **Never** use `--body "..."`
  with an inline multiline string: this corrupts content in PowerShell (newlines become
  `∙` and backticks become `\x5c`). Delete the temp file after the command succeeds.
- Do NOT leave the PR description stale after follow-up commits.

Before merging any PR, the agent MUST ensure:

- All required CI/status checks are green/passing (including lint checks)
- Code review requirements are met (if applicable)
- Tests are passing locally and in CI

When a branch is merged, it should also be deleted locally and remotely after confirming changes are
available in the default branch.

## Security Constraints

The agent must NOT:

- Introduce telemetry without explicit approval
- Send user data to third-party services
- Add undocumented network endpoints
- Disable encryption for convenience
- Commit secrets or hardcoded credentials

All cloud endpoints or external integrations must be clearly documented.

## Testing Requirements

- Write unit tests for new logic and behavior changes.
- Test edge cases: missing sensors, unavailable entities, invalid data types, empty datasets.
- Document test scenarios and expected behaviors.
- Test concurrent or async operations for race conditions before marking changes complete.
- Include pytest or unittest fixtures for common test scenarios.

## When in Doubt

The agent must stop and request clarification regarding:

- Energy calculation logic or assumptions
- Home Assistant integration architecture decisions
- CI/CD expectations or tool configuration
- Required vs. optional features or breaking changes

## Definition of Done

A change is considered complete when:

- All relevant tests pass locally and in CI
- New behavior is covered by tests (where feasible)
- Code follows project style and conventions (enforced by isort, black, and ruff)
- **All lint checks pass** (`tox -e lint` — runs isort, black, ruff format, ruff check)
- Documentation is updated if configuration, API, or user-facing changes are made
- No secrets are committed
- All linting and formatting checks pass (`tox -e lint` and CI)
- The implementation adheres strictly to the No-Assumption Rule
- The change aligns with Home Assistant integration standards
- Code quality is enhanced (no technical debt introduced)
