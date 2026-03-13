---
name: sanitize-mem
description: "Audit and sanitize superclaude memory files: fix refs, dedup, move misplaced learnings, purge stale artifacts."
user-invocable: true
disable-model-invocation: true
argument-hint: "[scope: 'all' | 'project <name>' | 'agent <name>' | 'comms' | 'timers']"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

# Memory Sanitization

Audit and sanitize superclaude memory files for: $ARGUMENTS (default: all)

## What This Skill Does

Finds and fixes: misplaced learnings, duplicate content across files, stale references, incorrect paths, session-specific cruft that shouldn't persist, and orphaned artifacts.

## Scope Map

| Scope | What It Covers |
|-------|---------------|
| `all` | Full sweep of everything below |
| `project <name>` | `~/.claude/agent-memory/shared/projects/<name>.md` + all orch memories referencing it |
| `agent <name>` | `~/.claude/agent-memory/<name>/MEMORY.md` only |
| `comms` | All `~/.claude/comms/*/` directories |
| `timers` | `~/.claude/session-timers/` cleanup |

## Procedure

### 1. Inventory (read-only)

Read all files in scope. Build a mental map of what each file contains.

**File locations:**
- Agent memories: `~/.claude/agent-memory/*/MEMORY.md`
- Shared project memories: `~/.claude/agent-memory/shared/projects/*.md`
- Cross-project wins: `~/.claude/agent-memory/shared/wins.md`
- Compact snapshots: `~/.claude/agent-memory/_compact-snapshots/*.md`
- Comms: `~/.claude/comms/*/` (directives, bootstrap, reports, escalations per orch)
- Session timers: `~/.claude/session-timers/`
- Plans/state: `~/.claude/plans/*/`

### 2. Detect Issues

Check each file for these problems (in priority order):

| Priority | Issue | Detection | Fix |
|----------|-------|-----------|-----|
| P0 | **Wrong file** — project learnings in agent-specific memory | Content about a project's codebase/patterns in `agent-memory/<agent>/MEMORY.md` instead of `shared/projects/<project>.md` | Move to shared project file |
| P1 | **Duplicates** — same learning in 2+ files | Same concept described with similar words in different files | Keep in canonical location, replace with reference in others |
| P2 | **Stale refs** — paths/branches/statuses that changed | File paths that don't exist, branches that were merged, statuses that are outdated | Update or remove |
| P3 | **Session cruft** — timestamps, commit hashes, "current task" that's done | Session-specific details that won't help future sessions | Remove (keep only reusable learnings) |
| P4 | **Orphaned artifacts** — consumed snapshots, expired timers | Files in `_compact-snapshots/` or `session-timers/` with no active session | Delete |

### 3. Canonical Location Rules

| Content Type | Canonical Location | Other Files Should |
|-------------|-------------------|-------------------|
| Project-specific gotchas/patterns | `shared/projects/<project>.md` | Reference with path link |
| Agent operational protocol | `agent-memory/<agent>/MEMORY.md` | Not duplicate rules/ content |
| Cross-project wins | `shared/wins.md` | Reference, not duplicate |
| Tool patterns | `rules/20-tool-conventions.md` | Reference, not duplicate |
| Test quality standards | `agent-memory/orch/MEMORY.md` (base orch) | Inherit from base orch |

### 4. Execute Fixes

For each issue found:
1. Read the source and target files
2. If moving content: add to canonical file first, then replace with reference in source
3. If deduplicating: keep the more detailed version, add `## References` link in the other
4. If purging: verify the content is truly stale before deleting

### 5. Report

Present findings in this format:

```
## Sanitization Report

### Scope: [all | project X | agent Y | ...]

| # | Issue | Type | File | Action Taken |
|---|-------|------|------|-------------|
| 1 | ... | P0/P1/P2/P3/P4 | path | Moved/Deduped/Updated/Removed |

### Summary
- Files audited: N
- Issues found: N
- Issues fixed: N
- Files modified: [list]
- Files deleted: [list]
```

## Key Principle

**Retain lessons, remove noise.** Every line in a memory file should help a future agent session. If it wouldn't, cut it. If it's useful but in the wrong place, move it.
