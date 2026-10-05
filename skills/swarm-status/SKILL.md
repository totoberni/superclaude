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
| `~/.claude/comms/_outcomes.log` (if exists) | Launch/outcome rows from the spawn-log hooks; a `LAUNCHED` row (background dispatch) with no `STOP` row for its agent_id in `_spawns.log` is still in flight |

## Procedure

### Step 1: Ephemeral agents currently registered

Glob `~/.claude/agents/_ephemeral/*.md`. If nothing matches, report "(no ephemeral agents currently registered)".

### Step 2: Recent spawn events (last 50)

Read the last 50 lines of `~/.claude/comms/_spawns.log` (and `_spawns-rich.log` for the task description). If the file is absent, report "(no spawn log yet: the spawn-log hooks are not deployed)".

### Step 3: In-flight workers

A background dispatch (`run_in_background`) returns at LAUNCH, so its `_outcomes.log` row says nothing about completion; the real completion is the `STOP` row in `_spawns.log` (`ts  ""  type  STOP  agent_id ...`: col4 == `STOP`, agent_id in col5). An agent is IN FLIGHT iff both hold:

- its `_outcomes.log` row (last 24 h) has outcome `LAUNCHED` (agent_id = column 7), OR is a legacy 6-column `OK` row whose last column contains `tasks\\<agentId>.output` (agent_id extracted by regex), AND
- `_spawns.log` has no `STOP` row with that agent_id in col5.

Rows with any other outcome are foreground calls that already completed: never in flight. Do not rely on the last 50 lines (the logs have thousands of lines). Run this exact command and use its output as the IN-FLIGHT list:

```bash
~/.claude/.venv/Scripts/python.exe - <<'PY'
import os, re
from datetime import datetime, timedelta, timezone
comms = os.path.expanduser("~/.claude/comms")
fmt, utc = "%Y-%m-%dT%H:%M:%SZ", timezone.utc
now = datetime.now(utc)
def rows(name):
    try:
        with open(os.path.join(comms, name), encoding="utf-8", errors="replace") as fh:
            return [l.rstrip("\n").split("\t") for l in fh if l.strip() and not l.startswith("#")]
    except OSError:
        return None
spawns, outcomes = rows("_spawns.log"), rows("_outcomes.log")
if spawns is None or outcomes is None:
    raise SystemExit("(no spawn/outcome log yet)")
stopped = {c[4] for c in spawns if len(c) >= 5 and c[3] == "STOP"}
legacy = re.compile(r"tasks\\+([A-Za-z0-9_-]+)\.output")
seen, found = set(), []
for c in outcomes:
    if len(c) < 6:
        continue
    try:
        ts = datetime.strptime(c[0], fmt).replace(tzinfo=utc)
    except ValueError:
        continue
    if now - ts > timedelta(hours=24):
        continue
    if c[4] == "LAUNCHED" and len(c) >= 7:
        aid = c[6] or "?"
    elif c[4] == "OK" and len(c) == 6 and legacy.search(c[5]):
        aid = legacy.search(c[5]).group(1)
    else:
        continue
    if aid in stopped or aid in seen:
        continue
    seen.add(aid)
    mins = int((now - ts).total_seconds() // 60)
    found.append(f"{aid} | {c[2]} | {c[3]} | {c[0]} | {mins} min" + (" | POSSIBLY STUCK" if mins > 120 else ""))
print(f"IN-FLIGHT ({len(found)})")
print("\n".join(found))
PY
```

An `aid` of `?` means a launch whose agent_id was not captured: it cannot be matched to a STOP row, so report it as "launched, completion unverifiable". If a log file is absent, report "(no outcome log yet)".

### Step 4: Format output

Compact table:

```
EPHEMERAL AGENTS (N)
- name | spawned-at | task-summary

BG REVIEWERS (N)
- agent | spawned-at

IN-FLIGHT WORKERS (N)
- agent_id | agent-type | description | launched-at | elapsed-min [POSSIBLY STUCK if >120]
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
- If a worker appears "stuck" (in flight per Step 3 for >120 min, i.e. launched with no STOP row): report it to the user; see `/recover-truncated` for the recovery flow

## Cross-References

- `~/.claude/skills/autocommission/SKILL.md` (reads from same `_ephemeral/` dir)
