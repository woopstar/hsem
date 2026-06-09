# Architecture Decision Records (ADR)

This directory contains Architecture Decision Records (ADRs) for the HSEM project.

## What is an ADR?

An Architecture Decision Record is a short document that captures an important architectural or design decision, including the context, the decision itself, the consequences, and the alternatives that were considered (and rejected). ADRs help new contributors (and future maintainers) understand *why* the system works the way it does — not just *how*.

## ADR conventions

| Convention | Rule |
|---|---|
| **Naming** | `ADR-NNN-title-with-hyphens.md` |
| **Status** | `Accepted` (retrospective decisions are marked as such) |
| **Scope** | One decision per ADR — no multi-decision records |
| **Layout** | Context → Decision → Consequences → Alternatives Considered → Related |
| **Updates** | When a decision changes, supersede with a new ADR (don't edit old ADRs) |

## Index

| ADR | Title | Area | Status |
|---|---|---|---|
| [ADR-001](ADR-001-planner-extraction.md) | Pure-Python Planner Extraction | Architecture, planner layer | Accepted |
| [ADR-002](ADR-002-slot-model.md) | Slot Model | Planner engine, data model | Accepted |
| [ADR-003](ADR-003-cost-scoring.md) | Cost Scoring Architecture | Cost function, candidate selection | Accepted |
| [ADR-004](ADR-004-inverter-safety.md) | Inverter Safety — Layered Hardware Write Protection | Safety, hardware interface | Accepted |
| [ADR-005](ADR-005-forecast-confidence.md) | Forecast Confidence | Forecast handling, diagnostics | Accepted |

## How to add a new ADR

1. Determine the next ADR number (find the highest `ADR-NNN` in this directory and increment).
2. Use the template below.
3. Ensure the new ADR links to related ADRs and implementation files.
4. Add it to the index table above.

### Template

```markdown
# ADR-NNN: Short Title in Title Case

**Status:** [Proposed | Accepted | Deprecated | Superseded]

**Date:** YYYY-MM-DD

**Deciders:** [list of people involved]

---

## Context

What is the issue that motivated this decision? What constraints or forces are at play?

## Decision

What is the change that we're proposing and/or doing? Use clear, specific language.

### Detailed design (if applicable)

Architecture diagrams, module lists, configuration structures, or API contracts.

## Consequences

### Positive

What advantages does this decision bring?

### Negative

What trade-offs, costs, or risks does this decision introduce?

### Mitigations

How are the negative consequences addressed?

## Alternatives Considered

### Option A: [short name]

*Description of the alternative.*

**Rejected because:** [reason]

### Option B: [short name]

*Description of the alternative.*

**Rejected because:** [reason]

---

## Related

- Links to other ADRs
- Links to implementation files
- Links to GitHub issues
```



## Relationship to other documentation

- **`docs/planner-spec.md`** — the canonical planner specification. ADRs explain *why* design decisions were made; the spec documents *what* the planner must do.
- **`docs/architecture-overview.md`** — high-level architecture overview. ADRs provide the reasoning behind the architecture.
- **`.github/memories.md`** — repository memory for AI agents. Key ADR conclusions are reflected there.