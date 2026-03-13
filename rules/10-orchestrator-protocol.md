# Orchestrator Protocol

Plans and state live in one of two locations:

| Pattern | Location | Used By |
|---------|----------|---------|
| Superclaude | `~/.claude/plans/<name>/` | Cross-project orchestration (VPS migration, etc.) |
| In-project | `<project>/.orchestrator/` | Standalone projects (cash_synch_proto, etc.) |

Superclaude pattern keeps orchestration OUT of project repos. See `12-agent-hierarchy.md` for multi-agent protocol.

## Startup (Orchs)

1. Read YOUR state file first (`state-<X>.md` for named orchs, `state.md` for single)
2. Read plan.md for phase requirements (READ ONLY — Meta owns plan.md)
3. Check `25-context-management.md` for session limits
4. Check mistakes.md before debugging

## Source of Truth

- `plan.md` = SOT for what to build (Meta-owned)
- `state*.md` = SOT for current progress (orch-owned during execution)
- Per-orch state files (`state-p1.md`, etc.) prevent write conflicts in multi-orch
- Master `state.md` updated by Meta ONLY when no orchs active
- If SOT conflicts with code, SOT wins — ask the user if unclear

## Decision Logging

DEC-NNN format: decision, rationale, alternatives, date.
Location: state.md Decisions section or `~/.claude/plans/<name>/decisions.md`

## Gotchas/Mistakes

After fixing a new issue, record it:
- Superclaude: `~/.claude/agent-memory/shared/projects/<project>.md` (canonical)
- In-project: `docs/gotchas.md` or `.orchestrator/mistakes.md`
- Tool patterns: `~/.claude/rules/20-tool-conventions.md`
