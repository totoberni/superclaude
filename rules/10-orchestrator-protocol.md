# Orchestrator Protocol

Plans and state live in one of two locations:

| Pattern | Location | Used By |
|---------|----------|---------|
| Superclaude | `~/.claude/plans/<name>/` | Cross-project orchestration (VPS migration, etc.) |
| In-project | `<project>/.orchestrator/` | Standalone projects (cash_synch_proto, etc.) |

Superclaude pattern keeps orchestration OUT of project repos. See `12-agent-hierarchy.md` for the hierarchy and write scopes.

## Startup

1. Read the state file first (`state.md`)
2. Read plan.md for phase requirements (Meta owns plan.md; workers READ ONLY)
3. Check `25-context-management.md` for context hygiene
4. Check mistakes.md before debugging

## Source of Truth

- `plan.md` = SOT for what to build (Meta-owned)
- `state.md` = SOT for current progress (Meta-owned)
- If SOT conflicts with code, SOT wins — ask the user if unclear
- `plan.html` is the human-rendered VIEW; `plan.md` remains the agent-facing SOT (agents read `plan.md`). Regenerate `plan.html` via `scpy plan/render_plan.py <plan.md>` after editing `plan.md`.

## Decision Logging

DEC-NNN format: decision, rationale, alternatives, date.
Location: state.md Decisions section or `~/.claude/plans/<name>/decisions.md`

## Gotchas/Mistakes

After fixing a new issue, record it:
- Superclaude: `mem search '<project> gotchas mistakes'` or `mem list --tier shared-projects` (use /mistake or /good-idea to write)
- In-project: `docs/gotchas.md` or `.orchestrator/mistakes.md`
- Tool patterns: `~/.claude/rules/20-tool-conventions.md`

## Memory Hygiene

Memory entries have a lifecycle: **relevant → stale → archived**.

- **Workers**: when a Gotcha is resolved during your task, note it in your final report (`Resolved: <gotcha summary>`)
- **Meta**: run `/memory-prune` before planning new work on inactive projects (>30 days since last activity)
- **All agents**: prefer updating existing entries over adding new ones. Check for duplicates before writing.

**Staleness signals**: entry references deleted files, Mistake with Occ=1 older than 30 days, project with no commits in 60 days.

**Archive, don't delete**: move stale entries to the cell's `archive/` subdir. Hard-won lessons may become relevant again.
