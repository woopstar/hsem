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
./scripts/quality.sh lint     # ruff format + ruff check
./scripts/quality.sh typing   # mypy — 0 errors
./scripts/quality.sh quality  # pyright + vulture — 0 errors
./scripts/quality.sh test     # pytest with coverage
```

Or run all at once:
```bash
./scripts/quality.sh all
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

Types: `feat`, `fix`, `docs`, `refactor`, `perf`, `test`, `ci`

Scopes should be specific to the domain: `sensor`, `flow`, `config`, `planner`, `milp`, etc.

Examples:
```
fix(planner): correct cycle cost denominator — Fixes #444
feat(sensor): add temperature-adaptive charge rate — Fixes #123
```

## GitHub Operations — Use MCP Tools, Never the `gh` CLI (Mandatory)

- **The `gh` CLI is NOT available in the devcontainer.** Do not shell out to `gh` for any
  GitHub operation (PRs, issues, reviews, branches, releases, etc.).
- **Always use the GitHub MCP tools** instead:
  - Create a PR → `create_pull_request`
  - Update a PR (title/body/draft/reviewers) → `update_pull_request`
  - Read a PR / diff / files / reviews / comments → `pull_request_read`
  - Review a PR → `pull_request_review_write` / `add_comment_to_pending_review`
  - Create / update / close issues → `issue_write` / `issue_read`
  - List / search issues & PRs → `list_issues` / `search_issues` / `search_pull_requests`
  - Merge a PR → `merge_pull_request`
  - Create / list branches → `create_branch` / `list_branches`
  - Push files to a branch → `push_files`
- **Local git is still fine** for `git add` / `git commit` / `git checkout` / `git push`
  (pushing the branch over SSH). Only the *GitHub API* operations (PRs, issues, reviews)
  must go through the MCP tools.

## Creating a PR

### PR Title
Must follow Conventional Commits format: `<type>(scope): <description>`

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

Use the **`update_pull_request` MCP tool** — pass the full markdown body as the `body`
argument. The MCP tool handles multiline content safely (no temp file, no shell escaping).

**Never** shell out to `gh pr edit` — the `gh` CLI is not available in the devcontainer.

## Merge Rules

Before merging ANY PR:
- [ ] All four quality gates pass (`./scripts/quality.sh all`)
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