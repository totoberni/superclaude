---
name: rb
description: "Refresh bootstrap.md from state + directive + pitfalls"
category: workflow
user-invocable: true
argument-hint: "<orch-name>"
allowed-tools: Read, Write, Glob, Grep
---

# /rb — Refresh Bootstrap

Generate fresh `bootstrap.md` for `$ARGUMENTS` (orch name) from current state, directives, and project pitfalls.

## Procedure

### 1. Resolve Orch Context

1. Read `~/.claude/comms/$ARGUMENTS/directives.md` — find latest active DIR (last PENDING or IN_PROGRESS in status table)
2. Read the directive file referenced in the index
3. Identify the project from the directive or registry (`~/.claude/comms/meta-registry.md`)

### 2. Gather State

Read in parallel:
- `~/.claude/plans/<project>/state-$ARGUMENTS.md` (or `state.md`) — current progress
- `~/.claude/agent-memory/shared/projects/<project>.md` — Gotchas + top Mistakes
- `~/.claude/agent-memory/$ARGUMENTS/MEMORY.md` — recovery context (if exists)

### 3. Auto-Select Pitfalls

From the project memory file, select top 3-5 pitfalls by priority:
1. Gotchas section entries (always relevant)
2. Mistakes with Occurrences >= 2 (recurring)
3. Most recent mistakes (likely still relevant)

### 4. Generate Bootstrap

Write to `~/.claude/comms/$ARGUMENTS/bootstrap.md`:

```markdown
# Bootstrap — <orch-name>

## Identity
You are **<orch-name>**, an orchestrator for <project>.
- **Comms**: `~/.claude/comms/<orch-name>/`
- **State**: `~/.claude/plans/<project>/state-<orch-name>.md`
- **Plan**: `~/.claude/plans/<project>/plan.md`

## Active Directive: DIR-NNN — <title>
<directive summary: 3-5 lines>

## Current Progress
<from state file: completed tasks, current task, blockers>

## Known Pitfalls
<auto-selected 3-5 items, numbered>

## Recovery Context
<from MEMORY.md if exists, otherwise "Fresh session — no prior context">

## Environment
- CWD: `~/projects/workspace/` (never cd into project dirs)
- Git: `git -C <repo-path> <cmd>`
```

### 5. Confirm

Output: `Bootstrap refreshed for <orch-name>. Start with: claude --agent <orch-name>`
