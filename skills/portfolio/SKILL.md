---
name: portfolio
description: "Cross-orch dashboard: active orchs, status, escalations"
model: haiku
category: orchestration
user-invocable: true
disable-model-invocation: true
argument-hint: "[--orch] [--worker] [--meta] [--scaf] [--all]"
allowed-tools: Read, Glob, Grep, Bash
---

# /portfolio — Cross-Orch Dashboard

Parse `$ARGUMENTS` to determine mode (default: `--orch`):
- `--orch` or empty — Active orchs table
- `--worker` — Currently spawned workers
- `--meta` — Meta state (registry, pending escalations, plan statuses)
- `--scaf` — Scaf campaign progress
- `--all` — Everything combined (compact)

## Data Sources

- Registry: `~/.claude/comms/meta-registry.md`
- Reports (latest RPT per orch): broker query — `kind='RPT' GROUP BY from_agent HAVING ts=MAX(ts)`
- Escalations (pending ESC): broker query — `kind='ESC' AND read_at IS NULL`
- Plans: `~/.claude/plans/*/plan.md`, `~/.claude/plans/*/state*.md` (agents read `plan.md`; for human browser viewing, point to the rendered `plan.html` in the same dir)
- Scaf directives (latest DIR to scaf): broker query — `kind='DIR' AND to_agent LIKE '@scaf%'`
- Broker DB: `~/.claude/comms/.broker.db`; fall back to flat files only if broker unavailable

## Mode: --orch (default)

1. Read `~/.claude/comms/meta-registry.md` — extract Active orchs table
2. Query broker for latest RPT per orch + unanswered ESC count:
   ```bash
   DB="$HOME/.claude/comms/.broker.db"
   sqlite3 -header -column "$DB" "
     SELECT
       from_agent AS orch,
       MAX(CASE WHEN kind='RPT' THEN seq END) AS last_rpt,
       datetime(MAX(CASE WHEN kind='RPT' THEN ts END),'unixepoch') AS rpt_time,
       CAST((strftime('%s','now') - MAX(CASE WHEN kind='RPT' THEN ts END)) / 60 AS INTEGER) AS age_min,
       SUM(CASE WHEN kind='ESC' AND read_at IS NULL THEN 1 ELSE 0 END) AS unanswered_esc
     FROM messages
     WHERE kind IN ('RPT','ESC')
     GROUP BY from_agent
     ORDER BY MAX(ts) DESC;
   "
   ```
3. Compute age from RPT timestamp vs now (age_min column above)
4. Output:

```
| Orch | Project | DIR | Last RPT | Status | Age | Escalation |
|------|---------|-----|----------|--------|-----|------------|
| o-x-1| workspace   | 012 | RPT-015  | DONE   | 2h  | --         |
| o-y-1| <project>  | 008 | RPT-010  | ACTIVE | 15m | ESC-003    |
```

If no active orchs: `No active orchs. Use /handoff --commission to create one.`

## Mode: --worker

List active workers by checking `~/.claude/session-timers/*.agent` for `w-*` prefix agents. Show: `| Worker | Parent Orch | PID | Age |`. If none: `No active workers.`

## Mode: --meta

1. Read `~/.claude/comms/meta-registry.md` (full Active + Archive tables)
2. Count pending ESCs from broker:
   ```bash
   DB="$HOME/.claude/comms/.broker.db"
   sqlite3 "$DB" "SELECT COUNT(*) AS pending_esc FROM messages WHERE kind='ESC' AND read_at IS NULL;"
   ```
3. List plan statuses from `~/.claude/plans/*/plan.md` (Phase header)
4. Output: registry summary, pending escalation count, plan phase summary

## Mode: --scaf

1. Query broker for scaf directives:
   ```bash
   DB="$HOME/.claude/comms/.broker.db"
   sqlite3 -header -column "$DB" "SELECT seq, datetime(ts,'unixepoch') AS t, substr(body,1,80) AS preview FROM messages WHERE kind='DIR' AND to_agent LIKE '@scaf%' ORDER BY ts DESC LIMIT 20;"
   ```
2. Output status table from directive index (DIR | Title | Status)
3. Show next pending directive (no matching RPT for the latest seq)

## Mode: --all

Run all modes above sequentially, with `---` separator. Compact: 1 line per entity.

## Constraints

- **READ-ONLY**: never modify registry, comms, or plan files
- Handle missing files gracefully — show `--` for unavailable data
- Keep output compact — truncate long summaries to one line
