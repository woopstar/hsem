---
name: hsem-pr-workflow
description: Activate when creating, updating, or managing a pull request for the HSEM repository. Covers conventional commits, PR description, quality gates, and merge rules.
---

# HSEM Pull Request Workflow

Activate this skill when:
- Creating a new pull request
- Updating an existing PR after follow-up commits
- Preparing to merge a PR

## Pre-PR Checklist

Before opening a PR, all four quality gates must pass:

```bash
tox -e lint      # isort + black + ruff format + ruff check
tox -e typing    # mypy — 0 errors
tox -e quality   # pyright + vulture — 0 errors
tox -e py314     # pytest with coverage
```

Verify: `git --no-optional-locks status` shows only intended changes.

## Documentation Update

Before opening a PR, check and update ALL documentation that describes changed behavior:

- [ ] `docs/planner-guide.md` — if planner inputs/outputs/cost function changed
- [ ] `docs/planner-spec.md` — if planner semantics changed
- [ ] `docs/config-flow-reference.md` — if config/options flow steps changed
- [ ] `docs/ev-charge-plan-setup.md` — if EV planned load changed
- [ ] `.github/memories.md` — if canonical patterns or module map changed
- [ ] `README.md` — if user-facing features changed
- [ ] `docs/huawei_entities.md` — if new Huawei entities wired
- [ ] `translations/en.json` — if user-facing strings changed

**A PR is not done until all affected docs are consistent with the implementation.**

## Commit Messages — Conventional Commits

Format: `<type>(<scope>): <description>`

Types: `feat`, `fix`, `chore`, `docs`, `refactor`, `perf`, `test`, `ci`

Scopes should be specific to the domain: `sensor`, `flow`, `config`, `planner`, `milp`, etc.

Examples:
```
fix(planner): correct cycle cost denominator — Fixes #444
feat(sensor): add temperature-adaptive charge rate — Fixes #123
```

## Creating a PR

### PR Title
Must follow Conventional Commits format: `<type>(<scope>): <description>`

### PR Description Must Include
- Summary of changes
- Branch name
- Files changed
- What changed and why
- Tests added or updated
- Test and lint results
- Known limitations or open questions
- Any required configuration changes
- `Fixes #<ISSUE_NUMBER>` (if applicable)

### PR Scope Rules
- [ ] Single platform per PR
- [ ] No feature creep
- [ ] No mixed cleanups with features
- [ ] One issue per PR
- [ ] No unmerged dependencies

## Keeping an Open PR Up to Date

After every follow-up commit on a branch that already has an open PR:

1. **Update the PR title** if the scope or description has changed
2. **Update the PR body** to reflect ALL changes made so far
3. **Tick off** completed items in any checklist inside the PR description
4. **Never leave the PR description stale** after follow-up commits

### How to Update a PR

Always use a temp file for the body — never pass multiline body inline:

```bash
# Write PR body to a temp file first
cat > /tmp/pr_body.md << 'EOF'
<markdown body here>
EOF

# Edit the PR
gh pr edit <PR_NUMBER> --title "<type>(scope): updated title" --body-file /tmp/pr_body.md

# Clean up
rm /tmp/pr_body.md
```

**Never use `--body "..."` with inline multiline text** — PowerShell corrupts newlines and backticks.

## Merge Rules

Before merging ANY PR:
- [ ] All four quality gates pass (`tox -e lint`, `tox -e typing`, `tox -e quality`, `tox -e py314`)
- [ ] All CI/status checks are green
- [ ] Code review requirements are met (if applicable)
- [ ] Tests passing locally and in CI
- [ ] All documentation updated and consistent

**Never merge without explicit user permission.**

After merge, delete the branch locally and remotely.

## PR Review Request

If requesting a Copilot code review:
```bash
# Use the request_copilot_review tool
```

## Definition of Done

A PR is complete when:
- [ ] All tests pass locally and in CI
- [ ] New behavior is covered by tests
- [ ] Code follows project style and conventions
- [ ] All lint/type/quality checks pass
- [ ] Documentation is updated
- [ ] No secrets committed
- [ ] No technical debt introduced
- [ ] PR description is accurate and complete
