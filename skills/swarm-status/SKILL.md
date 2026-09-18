---
name: swarm-status
description: "Use when viewing a live snapshot of in-flight workers and reviewer queue"
model: haiku
category: delegation
user-invocable: true
argument-hint: "[--all | --bg-only | --ephemeral-only]"
allowed-tools: Read, Bash, Glob
---

# Swarm Status

Lightweight visibility into currently-active worker dispatches. Use when you only need "what's running now?" — sub-second response.

**Mode**: $ARGUMENTS (default: `--all`)

## What This Shows

| Source | What |
|--------|------|
| `~/.claude/agents/_ephemeral/` | Ephemeral autocommissioned worker `.md` files currently active (auto-cleaned on task done) |
| `~/.claude/comms/_spawns.log`, `_spawns-rich.log` (if they exist) | Recent worker spawn events from the spawn-log hooks |
| `~/.claude/comms/_outcomes.log` (if exists) | Worker outcome events from the spawn-log hooks; a spawn with no matching outcome is still in flight |

## Procedure

### Step 1: Ephemeral agents currently registered

Glob `~/.claude/agents/_ephemeral/*.md`. If nothing matches, report "(no ephemeral agents currently registered)".

### Step 2: Recent spawn events (last 50)

Read the last 50 lines of `~/.claude/comms/_spawns.log` (and `_spawns-rich.log` for the task description). If the file is absent, report "(no spawn log yet: the spawn-log hooks are not deployed)".

### Step 3: In-flight workers

Read the last 50 lines of `~/.claude/comms/_outcomes.log`. A spawn from Step 2 with no matching outcome entry is treated as still in flight (background reviewers included). If the file is absent, report "(no outcome log yet)".

### Step 4: Format output

Compact table:

```
EPHEMERAL AGENTS (N)
- name | spawned-at | task-summary

BG REVIEWERS (N)
- agent | spawned-at

IN-FLIGHT WORKERS (N)
- agent-type | spawned-at | task-summary
```

## Modes

- `--all` (default): all 3 sections
- `--bg-only`: only BG reviewers
- `--ephemeral-only`: only ephemeral agents

## When to Use

- Before dispatching another swarm batch (avoid >5 cap including BG)
- When `/autocommission` lifecycle feels unclear (which ephemerals exist?)
- Debugging stuck-looking session

## Constraints

- NEVER spawn anything — read-only diagnostic
- NEVER kill processes — that's the user's call
- If a worker appears "stuck" (spawned >2hr ago with no outcome): report it to the user; see `/recover-truncated` for the recovery flow

## Cross-References

- `~/.claude/skills/autocommission/SKILL.md` (reads from same `_ephemeral/` dir)
