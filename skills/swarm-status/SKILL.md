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

Lightweight visibility into currently-active worker dispatches. Use INSTEAD of `/super-health` when you only need "what's running now?" — sub-second response.

**Mode**: $ARGUMENTS (default: `--all`)

## What This Shows

| Source | What |
|--------|------|
| `~/.claude/agents/_ephemeral/` | Ephemeral autocommissioned worker `.md` files currently active (auto-cleaned on task done) |
| `~/.claude/comms/_spawns.log` (if exists) | Recent worker spawn events from `45-spawn-log.sh` hook |
| Process tree | Background `claude --agent w-*` processes (currently running BG reviewers, etc.) |

## Procedure

### Step 1: Ephemeral agents currently registered

```bash
ls -la ~/.claude/agents/_ephemeral/ 2>/dev/null | grep -E '^-.*\.md$' || echo "(no ephemeral agents currently registered)"
```

### Step 2: Recent spawn events (last 50)

```bash
log=~/.claude/comms/_spawns.log
[ -f "$log" ] && tail -50 "$log" || echo "(no spawn log yet — 45-spawn-log.sh not deployed)"
```

### Step 3: Live BG processes

```bash
# Find background claude --agent w-* processes
pgrep -af 'claude --agent' || echo "(no live agent processes)"
```

### Step 4: Format output

Compact table:

```
EPHEMERAL AGENTS (N)
- name | spawned-at | task-summary

BG REVIEWERS (N)
- agent | parent-PID | spawned-at

LIVE AGENT PROCESSES (N)
- agent-type | PID | started-at
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
- If a process appears "stuck" (running >2hr): suggest `/super-health --quick` for deeper diagnosis

## Cross-References

- `/super-health` for full health audit
- `/nudge` for cross-agent status probe
- `~/.claude/skills/autocommission/SKILL.md` (reads from same `_ephemeral/` dir)
