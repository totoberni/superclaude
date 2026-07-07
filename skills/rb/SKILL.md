---
name: rb
description: "Refresh bootstrap.md from state + directive + pitfalls"
category: workflow
user-invocable: true
argument-hint: "<orch-name>"
allowed-tools: Read, Write, Bash, Glob, Grep
---

# /rb — Refresh Bootstrap

Generate fresh `bootstrap.md` for `$ARGUMENTS` (orch name) from current state, directives, and project pitfalls.

## Procedure

### 1. Resolve Orch Context

1. Query broker for the latest DIR addressed to `$ARGUMENTS`:
   ```bash
   DB="$HOME/.claude/comms/.broker.db"
   sqlite3 -header -column "$DB" "SELECT seq, datetime(ts,'unixepoch') AS t, body FROM messages WHERE kind='DIR' AND to_agent='@$ARGUMENTS' ORDER BY ts DESC LIMIT 1;"
   ```
2. The broker `body` column above holds the full directive text (handoff dual-writes the entire directive into the broker), so use it directly — no flat-file read needed
3. Identify the project from the directive or registry (`~/.claude/comms/meta-registry.md`)

### 2. Gather State

Read in parallel:
- `~/.claude/plans/<project>/state-$ARGUMENTS.md` (or `state.md`) — current progress
- Project gotchas + top mistakes: `HF_HUB_OFFLINE=1 ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py search '<project> gotchas mistakes' -k 8` or `list --tier shared-projects`
- Recovery context: injected at session start; for full handoff run `memory_db.py search '<orch-name> recovery context current state'` / `get --name <slug>`

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
<from `memory_db.py search '<agent> recovery context current state'` (then `get --name <slug>`) if found, otherwise "Fresh session — no prior context">

## Environment
- CWD: `~/projects/workspace/` (never cd into project dirs)
- Git: `git -C <repo-path> <cmd>`
```

### 5. Confirm

Output: `Bootstrap refreshed for <orch-name>. Start with: claude --agent <orch-name>`
