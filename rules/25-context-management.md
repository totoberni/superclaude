# Context Management Protocol

Applies to ALL agents (meta, workers).

## Enforcement Layers

| Layer | Mechanism | What It Does |
|-------|-----------|-------------|
| **Hard** | The pre-compact snapshot hook | Snapshots state files before compaction |
| **Soft** | The context-check hook | Warns when the memory footprint grows (see § Context Estimation Response) |
| **Soft** | Self-compact protocol (below) | Agent-driven context-window monitoring + proactive stash |
| **Soft** | This rule file | Good checkpointing habits |

## Periodic Checkpointing

After each task (or every ~15 tool calls):
1. Update the state file (`state.md`)
2. Commit if a logical batch is done (subject to the `/git` toggle)

## Post-Task Retrospective

After completing any task that involved code changes or took >5 minutes:
1. Write the report **first** (most critical artifact — Meta and the user need this)
2. Run `/mistake <project>` and `/good-idea <project>` (promotes learnings to shared project memory)
3. Update state file

Retrospectives at normal completion ensure learnings reach the DB (`shared-projects` tier) where ALL future sessions benefit.

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

Stash via `/remember` (upserts to the DB). Write a recovery memory with content structured as:

```
- **Task**: plan / task ref
- **Progress**: which tasks done, which in progress
- **Current task**: exact task ID + what you were doing
- **Uncommitted work**: files modified but not committed
- **Next steps**: concrete actions (not vague)
- **Key findings**: decisions made, gotchas discovered, important context
```

Then update your state file (`state.md`) with current task progress.

**Critical**: the stash is incremental — use /remember to upsert (the DB handles de-dup). Include a descriptive name slug like `<agent>-recovery-<date>` so `mem search '<agent> recovery context'` retrieves it.

### After Compaction — Recovery

The pre-compact snapshot hook auto-snapshots state files. After compaction, resume in this order:

1. `~/.claude/agent-memory/_system/_compact-snapshots/` (latest snapshot)
2. Recovery context: `mem search '<agent> recovery context current state'` then `mem get --name <slug>`
3. Your state file
4. Your latest report
5. Your plan (`plan.md`) or dispatch prompt

## Context Estimation Response

When the context-check hook fires a memory footprint warning, respond by agent type:

| Agent | Action |
|-------|--------|
| **Meta** | Request the user's permission to run `/mem-health` (meta cannot self-authorize health checks) |
| **Worker** | Report to the spawning agent, which escalates if needed |

The warning is informational — don't stop current work. Address it at the next natural break point.

## Efficiency

- Commit in logical batches, not one file at a time
- Prefer delegation over doing everything yourself — workers get fresh context
- If a task can be split into 2 sessions cleanly, stash and let the next session handle it

## Worker Absorption (Swarm-First)

**SOT**: `~/.claude/rules/13-worker-first-mandate.md`. Workers absorb context — spawning a w-* no longer triggers a "stash" (delegated context is freed). Stash triggers (above) apply to non-delegating long sequences only.

For the mandate + matrix + battle-tested patterns, see 13-rule.
