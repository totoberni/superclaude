# Context Management Protocol

Applies to ALL agents (orchs, workers). Meta is exempt from time limits but NOT from context hygiene.

## Enforcement Layers

| Layer | Mechanism | What It Does |
|-------|-----------|-------------|
| **Hard** | `~/.claude/hooks/pre-compact.sh` | Snapshots state files before compaction |
| **Hard** | `~/.claude/hooks/session-timer.sh` | Enforces orch session time limits (warn → grace → block) + PID-liveness GC |
| **Hard** | `~/.claude/hooks/session-cleanup.sh` | SessionEnd: deletes timer files on normal exit |
| **Soft** | Self-compact protocol (below) | Agent-driven context-window monitoring + proactive stash |
| **Soft** | This rule file | Good checkpointing habits |

## Session Lifecycle Manager

3-layer system preventing zombie processes and orphaned timer files:

| Layer | Hook/Script | Trigger | Handles |
|-------|-------------|---------|---------|
| 1 | `hooks/session-cleanup.sh` | `SessionEnd` | Normal exits (`/exit`, Ctrl+C, `/clear`) — immediate file cleanup |
| 2 | `hooks/session-timer.sh` Phase 2 GC | `PreToolUse` (every tool call) | Abnormal exits — PID dead → clean files; PID stopped (T) → kill + clean |
| 3 | `scripts/session-reaper.sh` | Manual / cron | Batch cleanup — `--dry-run` to preview, `--all` to also kill stale active sessions |

Timer files per session in `~/.claude/session-timers/`:
- `<session_id>.start` — epoch timestamp
- `<session_id>.agent` — agent name
- `<session_id>.pid` — claude process PID (for liveness checks)
- `<session_id>.override` — timer bypass (created manually by the user)
- `<session_id>.context-warned` — one-shot flag for memory footprint warning

## Periodic Checkpointing (Orchs)

After each task (or every ~15 tool calls):
1. Update state file (`state-<X>.md`)
2. Commit if a logical batch is done

## Post-Directive Retrospective (Orchs)

After completing any directive that involved code changes or took >5 minutes:
1. Write RPT to reports.md **first** (most critical artifact — Meta and the user need this)
2. Run `/mistake <project>` and `/good-idea <project>` (promotes learnings to shared project memory)
3. Update state file

This is separate from the grace-period shutdown procedure. Retrospectives at normal completion ensure learnings reach `~/.claude/agent-memory/shared/projects/<project>.md` where ALL future orchs benefit — even if the session ends gracefully without hitting the timer.

## Context Hygiene (All Agents)

Minimize context consumption so you can work longer before compaction:

- **Targeted reads**: use `offset`/`limit` for large files (>200 lines). Never read a full large file unless required.
- **Head-limited searches**: use `head_limit` on Grep. Don't dump 500 matches when 10 suffice.
- **Concise output**: lead with the answer, not the reasoning. Summarize multi-step results in 2-3 lines.
- **Avoid re-reads**: if you already read a file this session, don't read it again unless it changed.
- **Batch operations**: group related git/file operations. Don't alternate read-edit-read-edit on the same file.

## Self-Compact Protocol (All Agents)

Context compaction can happen at any time when the conversation grows large. Auto-compact gives no warning — context is silently truncated. Proactive stashing prevents information loss.

### Triggers — Stash When ANY Is True

| Signal | Threshold | What To Do |
|--------|-----------|------------|
| **Tool call count** | ~50+ calls in this session | Stash now — you're deep |
| **Large file reads** | 3+ files over 300 lines each read in full | Stash — context is bloated |
| **Multi-task session** | Completed 2+ distinct tasks | Stash between tasks |
| **Worker delegation** | Before spawning a complex worker (w-debugger, w-refactorer) | Stash — worker output will consume context |
| **Gut check** | You sense the session is getting long | Stash — better early than late |

### Stash Procedure

Write recovery context to `~/.claude/agent-memory/instance/<your-agent-name>/MEMORY.md` (or via your root symlink `~/.claude/agent-memory/<your-agent-name>/MEMORY.md`):

```markdown
## Recovery Context (auto-stash)
- **Directive**: DIR-NNN ref
- **Progress**: which tasks done, which in progress
- **Current task**: exact task ID + what you were doing
- **Uncommitted work**: files modified but not committed
- **Next steps**: concrete actions (not vague)
- **Key findings**: decisions made, gotchas discovered, important context
```

Then update your state file (`state-<X>.md`) with current task progress.

**Critical**: the stash is incremental — append/update, don't overwrite prior recovery context. If you already have useful content in MEMORY.md, preserve it and add the new recovery section.

### After Compaction — Recovery

The `pre-compact.sh` hook auto-snapshots state files. After compaction, resume in this order:

1. `~/.claude/agent-memory/_system/_compact-snapshots/` (latest snapshot)
2. Your `MEMORY.md` recovery context
3. Your state file
4. Your latest report
5. Your directive

## Session Time Limit (Orchs)

| Time | Phase | Behavior |
|------|-------|----------|
| **45 min** | Warning | Non-blocking. Start wrapping up current task. |
| **48 min** | Grace period | Only shutdown ops allowed (5 min window). See shutdown procedure. |
| **53 min** | Hard block | ALL tool calls blocked. Session is over. |

- Override: `touch ~/.claude/session-timers/<session_id>.override`
- Meta exempt

**At the 45-min warning**: do NOT start new long-running work (especially parallel test batches). Finish current atomic task, then start shutdown.

**During grace period (48-53 min)** — execute the shutdown procedure in order:
1. Commit outstanding work
2. Update state file + write RPT to reports.md
3. Run `/mistake <project>` and `/good-idea <project>` (captures learnings to shared memory)
4. Write recovery context to MEMORY.md

## Context Estimation Response

When the context estimation hook (05-context-check.sh) fires a memory footprint warning, respond by agent type:

| Agent | Action |
|-------|--------|
| **Orch / Scaffolder** | Run `/nudge <parent meta>` requesting `/mem-health` on their behalf |
| **Meta** | Request the user's permission to run `/mem-health` (meta cannot self-authorize health checks) |
| **Worker** | Report to spawning orch, which escalates if needed |

The warning is informational — don't stop current work. Address it at the next natural break point.

## Efficiency

- Commit in logical batches, not one file at a time
- Prefer delegation over doing everything yourself — workers get fresh context
- If a task can be split into 2 sessions cleanly, stash and let the next session handle it
