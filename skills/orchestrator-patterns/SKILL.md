---
name: orchestrator-patterns
description: "Templates and conventions for orchestrated projects."
category: meta
user-invocable: true
argument-hint: "[project-name]"
---

# Orchestrator Patterns

Templates and conventions for orchestrated projects. Two patterns exist:

## Pattern Selection

| Context | Pattern | Location |
|---------|---------|----------|
| Project managed by superclaude agents (`meta`, `orch-*`) | **Superclaude** | `~/.claude/plans/<project>/` |
| Standalone project (no superclaude involvement) | **In-project** | `<repo>/.orchestrator/` |

**Rule**: Never create `.orchestrator/` inside a repo that is superclaude-managed. The SOT for all agent infrastructure is `~/.claude/`.

## Superclaude Pattern

```
~/.claude/plans/<project>/
  plan.md          # Implementation plan — SOT for what to build (Meta-owned)
  state*.md        # Mutable state — updated by orch during execution (Orch-owned)
  context.md       # Project spec — pointers to repo, key files
  decisions.md     # Executive decision log (DEC-NNN format)
  reference/       # Frozen reference docs

# Wins, Mistakes, Gotchas — accessible to ALL orch instances (shared-projects tier)
  # Query: memory_db.py search "<project> gotchas mistakes" -k 8

~/.claude/comms/<orch-name>/
  directives.md    # Meta → Orch (DIR-NNN)
  bootstrap.md     # Meta → Orch (cold-start context)
  reports.md       # Orch → Meta (RPT-NNN)
  escalations.md   # Orch → Meta/the user (ESC-NNN)
```

## In-Project Pattern (standalone only)

```
.orchestrator/
  context.md       # Project specification — never modified by agents
  plan.md          # Implementation plan — source of truth for what to build
  state.md         # Mutable session state — updated by orchestrator each cycle
  decisions.md     # Executive decision log (DEC-NNN format)
  mistakes.md      # Per-worker error patterns and prevention rules
```

## State File Template

```markdown
# State — [Project Name]

**Updated:** YYYY-MM-DD HH:MM
**Active Plan:** plan.md
**Session:** [brief description of current session goal]

## Current Phase
- **Phase:** N — [description]
- **Status:** [in-progress | blocked | complete]

## Active Workers
| Worker | Task | Status | Branch |
|--------|------|--------|--------|
| ... | ... | ... | ... |

## Human Gates Pending
- [ ] [description of decision needed from the user]

## Decisions Made This Session
- DEC-NNN: [brief summary]

## Commit History
| Commit | Description | Phase |
|--------|-------------|-------|
| ... | ... | ... |
```

## Decision Log Template

```markdown
# Decisions — [Project Name]

## DEC-NNN — [Title]
**When:** YYYY-MM-DD
**Context:** [what situation prompted this decision]
**Decision:** [what was decided]
**Rationale:** [why this choice]
**Alternatives rejected:** [what else was considered and why not]
```

Key principles: see rules 10 + 12 (auto-loaded) and `orch.md` § Delegating to Workers.
