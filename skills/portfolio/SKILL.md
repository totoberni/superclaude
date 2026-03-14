---
name: portfolio
description: "Cross-orch dashboard: active orchs, status, escalations"
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
- Reports: `~/.claude/comms/*/reports.md` (latest RPT per orch)
- Escalations: `~/.claude/comms/*/escalations.md` (pending ESC)
- Plans: `~/.claude/plans/*/plan.md`, `~/.claude/plans/*/state*.md`
- Scaf directives: `~/.claude/comms/scaf*/directives.md`
- File timestamps for age calculation

## Mode: --orch (default)

1. Read `~/.claude/comms/meta-registry.md` — extract Active orchs table
2. For each active orch, read its latest RPT from `~/.claude/comms/<name>/reports.md` (last `## RPT-NNN` entry — extract Status + Time)
3. Check `~/.claude/comms/<name>/escalations.md` for unanswered ESCs (no `**Answer**` line)
4. Compute age from RPT timestamp vs now
5. Output:

```
| Orch | Project | DIR | Last RPT | Status | Age | Escalation |
|------|---------|-----|----------|--------|-----|------------|
| o-x-1| workspace   | 012 | RPT-015  | DONE   | 2h  | --         |
| o-y-1| example-project  | 008 | RPT-010  | ACTIVE | 15m | ESC-003    |
```

If no active orchs: `No active orchs. Use /handoff --commission to create one.`

## Mode: --worker

List active workers by checking `~/.claude/session-timers/*.agent` for `w-*` prefix agents. Show: `| Worker | Parent Orch | PID | Age |`. If none: `No active workers.`

## Mode: --meta

1. Read `~/.claude/comms/meta-registry.md` (full Active + Archive tables)
2. Count pending ESCs across all `~/.claude/comms/*/escalations.md`
3. List plan statuses from `~/.claude/plans/*/plan.md` (Phase header)
4. Output: registry summary, pending escalation count, plan phase summary

## Mode: --scaf

1. Read `~/.claude/comms/scaf/directives.md` + `~/.claude/comms/scaf2/directives.md` (if exists)
2. Output status table from directive index (DIR | Title | Status)
3. Show next pending directive

## Mode: --all

Run all modes above sequentially, with `---` separator. Compact: 1 line per entity.

## Constraints

- **READ-ONLY**: never modify registry, comms, or plan files
- Handle missing files gracefully — show `--` for unavailable data
- Keep output compact — truncate long summaries to one line
