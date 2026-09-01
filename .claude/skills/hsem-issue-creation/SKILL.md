---
name: hsem-issue-creation
description: Activate when creating a new GitHub issue for the HSEM repository. Ensures every issue has a Conventional Commits title, a detailed description, a proposed solution, acceptance criteria, a tailored fix prompt, and labels.
---

# HSEM Issue Creation

Activate this skill whenever a new GitHub issue is being created for this repository —
whether reporting a bug, filing an enhancement, or capturing a chore/tech-debt item.

## Step 1: Investigate Before Drafting

Read the relevant code (and `docs/planner-spec.md` / `AGENTS.md` if the area applies)
before writing the issue. An issue drafted without reading the code produces a vague
description and an unusable "solution" — grep for the symptom, find the responsible
file(s), and cite concrete `file:line` references in the body.

## Step 2: Title — Conventional Commits

Format: `<type>(<scope>): <description>`

Types: `feat`, `fix`, `chore`, `docs`, `refactor`, `perf`, `test`, `ci`

Scope should name the specific domain (e.g. `sensor`, `ocpp`, `planner`, `config`,
`milp`). Keep the description short and imperative — it becomes the branch slug and,
eventually, the commit/PR title.

Examples:

```
fix(ocpp): status sensor misleadingly shows 'disconnected' when unconfigured
feat(planner): add temperature-adaptive charge rate
chore(quality): enforce prettier for markdown in CI
```

## Step 3: Body — Required Sections

Every issue body MUST contain these four sections, in this order:

```markdown
## Description

<Detailed explanation of the problem or request: what happens, why it's wrong or
missing, where it lives in the code (file:line references), and root cause if known.
For enhancements, explain the user-visible gap being filled.>

## Possible Solution

<A concrete, plausible approach to fix/implement this — specific enough to guide
implementation, but not a full diff. Name the functions/files likely to change.
If there are multiple viable approaches, note the trade-off briefly and recommend one.>

## Acceptance Criteria

- [ ] <Specific, testable condition that must hold once this is fixed>
- [ ] <Another condition — cover edge cases, not just the happy path>
- [ ] Tests added/updated covering the above
- [ ] `./scripts/quality.sh all` passes

## Suggested Prompt

\`\`\`
<A ready-to-run prompt tailored to this specific issue — enough context that a fresh
Claude Code session with no prior conversation could act on it directly: the file(s)
involved, the expected behavior change, and a pointer to run the pre-flight skill and
quality gates before opening a PR. Do not write a generic "fix issue #N" placeholder.>
\`\`\`
```

Do not omit or reorder these sections. Do not pad the description with sections the
repo doesn't use elsewhere (no "Environment", "Steps to Reproduce", etc. unless the
issue genuinely needs them as sub-content within Description).

## Step 4: Labels

Always attach at least one label. Check current labels first — they do change:

```bash
rtk gh label list
```

Pick from the repo's existing taxonomy (see below); do not invent new labels without
asking the user first. At minimum, apply:

1. **One type label** — `bug` or `enhancement` (or `chore` for maintenance-only work).
2. **One `area:*` label** matching the affected subsystem, if one fits:
   `area:planner`, `area:inverter`, `area:forecast`, `area:config`,
   `area:home-assistant`, `area:diagnostics`, `area:safety`, `area:tests`,
   `area:docs`, `area:refactor`.
3. Optional topical labels when relevant: `ocpp`, `ev-charging`, `sensors`,
   `services`, `logging`, `dashboard`, `config`, `dependencies`, `breaking-change`.
4. Optional priority label if urgency is known: `priority:p0` (must fix before real
   hardware control) or `priority:p1` (core architecture/planner improvements).

Skip `type:bug` / `type:feature` unless the user's convention has shifted to those —
`bug` / `enhancement` are the labels actually in use on current open issues.

## Step 5: Create the Issue

Draft the body in a temp markdown file and pass it via `--body-file` — never inline a
multiline body as a shell argument.

```bash
gh issue create \
  --title "fix(ocpp): status sensor misleadingly shows 'disconnected' when unconfigured" \
  --body-file /tmp/issue-body.md \
  --label bug --label area:diagnostics --label ocpp
```

Prefer `rtk gh issue create` for the token-savings passthrough.

## Step 6: Verify

```bash
rtk gh issue view <number>
```

Confirm the title parses as Conventional Commits, all four body sections are present,
and the labels applied match the taxonomy.
