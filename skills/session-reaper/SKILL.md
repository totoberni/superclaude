---
name: session-reaper
description: "Monitor and clean Claude Code sessions: list, kill zombies, check."
category: orchestration
user-invocable: true
argument-hint: "[status|kill|history|cron] [--dry-run] [--all]"
allowed-tools: Read, Bash, Glob
---

# Session Reaper

Manage Claude Code session lifecycle. Shows process health, kills zombies, reviews session history.

**Subcommand**: $ARGUMENTS (default: `status`)

## Subcommands

### `status` (default)

Show a dashboard of all claude processes, memory usage, and session health.

1. Run `~/.claude/scripts/session-status.sh` to get structured data
2. Check for memory pressure alert at `~/.claude/session-timers/memory-alert`
3. Present as a table:

```
## Session Dashboard

### Active Processes
| PID | State | RSS | Agent | Elapsed | Session |
|-----|-------|-----|-------|---------|---------|

### Meta Sessions
| PID | Age | RSS | Status |
|-----|-----|-----|--------|

If meta count exceeds cap (2): "X meta sessions exceed cap of 2 — run `/session-reaper kill` to clean"

### Health
- Total memory: X MB / 8192 MB threshold
- Memory pressure: OK / ALERT
- Zombie count: N
- Meta sessions: N active (cap: 2)
- Cron reaper: active / inactive
- Last reaper run: [timestamp from reaper.log]

### Timer Files
| Session | Agent | Started | PID Alive? |
|---------|-------|---------|------------|
```

### `kill`

Kill zombie (stopped) processes. Supports `--dry-run` and `--all` flags.

1. If `--dry-run` is in $ARGUMENTS: run `~/.claude/scripts/session-reaper.sh --dry-run`
2. If `--all` is in $ARGUMENTS: run `~/.claude/scripts/session-reaper.sh --all`
3. Otherwise: run `~/.claude/scripts/session-reaper.sh`
4. Show the output directly — the reaper already formats nicely

### `history`

Show recent session history.

1. Read `~/.claude/session-timers/session-history.log` (last 30 entries)
2. Read `~/.claude/session-timers/cleanup.log` (last 10 entries)
3. Present as:

```
## Session History (last 30)

| Time | Agent | Duration | Exit Reason |
|------|-------|----------|-------------|

## Recent Cleanup Events (last 10)
[raw log lines]
```

### `cron`

Show cron job status and recent reaper log.

1. Run `crontab -l` to verify the reaper cron is installed
2. Read `~/.claude/session-timers/reaper.log` (last 20 lines)
3. If no cron entry found, tell the user how to install it:
   ```
   echo '*/30 * * * * ~/.claude/scripts/session-reaper.sh >> ~/.claude/session-timers/reaper.log 2>&1' | crontab -
   ```
