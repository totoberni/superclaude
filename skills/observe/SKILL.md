---
name: observe
description: "Multi-agent observability dashboard (agents/activity/costs)"
category: orchestration
user_invocable: true
arg: "subcommand: agents (default) | activity | costs"
---

# /observe — Multi-Agent Observability

Read-only dashboard for monitoring active agent sessions, recent activity, and costs.

## Usage

- `/observe` or `/observe agents` — Active session table
- `/observe activity` — Recent reports/escalations timeline
- `/observe costs` — Session cost summary (last 24h)

## Subcommand: agents (default)

Build a table of active sessions from `~/.claude/session-timers/`.

**Data sources per session_id**:
- `.agent` — agent name
- `.pid` — process ID (verify alive with `kill -0 $PID 2>/dev/null`)
- `.start` — epoch timestamp (compute age: `$(( $(date +%s) - start ))`)
- `.calls` — tool call count (may not exist for all sessions)
- `.tdd` — TDD cycle score (may not exist — DIR-019 dependency)

**Steps**:
1. List unique session IDs: `ls ~/.claude/session-timers/*.agent | sed 's/.*\///' | sed 's/\.agent//'`
2. For each session ID, read `.agent`, `.pid`, `.start`, `.calls` (if exists), `.tdd` (if exists)
3. Check PID liveness: `kill -0 $PID 2>/dev/null` — mark dead sessions with `DEAD` status
4. Compute age in minutes: `$(( ($(date +%s) - start) / 60 ))` min
5. Check RSS (if alive): `ps -o rss= -p $PID 2>/dev/null` (convert to MB)
6. Output table:

```
| Agent | Session | PID | Status | Age | Calls | TDD | RSS |
|-------|---------|-----|--------|-----|-------|-----|-----|
| meta  | abc123  | 4521| ALIVE  | 12m | 34    | --  | 89M |
| o-x-1 | def456  | 4590| ALIVE  | 8m  | 22    | 3/5 | 76M |
| scaf  | ghi789  | 0   | DEAD   | 45m | --    | --  | --  |
```

**Handle gracefully**: missing `.calls`, `.tdd`, `.start` files — show `--` for missing data.

## Subcommand: activity

Show the last 5 entries from each active orch's `reports.md` and `escalations.md`.

**Steps**:
1. List comms directories: `ls -d ~/.claude/comms/*/` (exclude README.md)
2. For each dir with a `reports.md`, extract the last 5 `## RPT-NNN` or `## ESC-NNN` headers + their **Status** line
3. Sort by timestamp (from **Time** field), newest first
4. Output as timeline:

```
[14:32] RPT-042 (o-example-project-1) — DONE: Test suite green, 47/47 passing
[14:28] ESC-005 (o-example) — BLOCKED: Need the user's approval for schema change
[14:15] RPT-041 (o-example-project-1) — IN_PROGRESS: 3/7 tests fixed
```

**Handle gracefully**: empty reports/escalations files, comms dirs without reports.

## Subcommand: costs

Parse JSONL session files for the last 24h if available.

**Steps**:
1. Check for JSONL session logs at `~/.claude/sessions/` or `~/.claude/projects/*/sessions/`
2. If found: parse entries from last 24h, sum input/output tokens, estimate cost
3. If not found: output:
   ```
   Session cost data not available. Install ccusage for cost tracking:
   https://github.com/ryoppippi/ccusage
   ```

## Constraints

- **READ-ONLY**: never modify timer files, comms files, or any session data
- Handle missing files gracefully — agents may not have all timer file types
- Keep output compact — truncate long summaries to one line
