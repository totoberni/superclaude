---
name: remember
description: "Meta context save/load: cheaper than compaction"
category: memory
user-invocable: true
disable-model-invocation: true
argument-hint: "[--save] [--deep]"
allowed-tools: Read, Write, Edit, Glob, Grep
---

# /remember — Meta Context Preservation

Parse `$ARGUMENTS` to determine mode:
- empty (default) — Load recovery context
- `--save` — Write structured recovery snapshot
- `--deep` — Load everything (full state reconstruction)

## Mode: default (load)

Load context in order, presenting a concise summary after each:

1. **MEMORY.md**: Read `~/.claude/agent-memory/meta/MEMORY.md` — present Recovery Context section + any permanent memories
2. **Registry**: Read `~/.claude/comms/meta-registry.md` — list active orchs with status
3. **Compact snapshot**: If MEMORY.md references a snapshot, read from `~/.claude/agent-memory/_compact-snapshots/` (latest by timestamp)

Output format:
```
## Recovered Context
- **Last session**: <focus from recovery context>
- **Active orchs**: <count> (<names>)
- **Pending actions**: <from recovery context>
- **Unfinished**: <what was interrupted>
```

## Mode: --save

Write structured snapshot to `~/.claude/agent-memory/meta/MEMORY.md`. **Append/update** the Recovery Context section — do NOT overwrite existing permanent memories.

Structure to write:
```markdown
## Recovery Context (<YYYY-MM-DD HH:MM>)
- **Session focus**: [1 line summary]
- **Decisions made**: [DEC refs, directive amendments]
- **Active orchs**: [1-liner status per orch]
- **Pending actions**: [concrete next steps]
- **the user's preferences**: [feedback-worthy items from this session]
- **Unfinished**: [what got interrupted, exact point]
```

**Constraints**:
- Keep Recovery Context under 30 lines (MEMORY.md has 200-line cap)
- Use Edit tool to replace existing Recovery Context section (not append duplicates)
- Preserve all other MEMORY.md content (index entries, permanent memories)

## Mode: --deep

Load everything from default mode, PLUS (in parallel):

4. Each active orch's latest RPT: `~/.claude/comms/*/reports.md` (last `## RPT-NNN`)
5. Each active orch's state: `~/.claude/plans/*/state-*.md`
6. Active plan files: `~/.claude/plans/*/plan.md`
7. Pending escalations: `~/.claude/comms/*/escalations.md` (unanswered ESCs)

Present as a structured dashboard after loading.

## Constraints

- **--save is write; default and --deep are read-only** (except MEMORY.md for --save)
- Never overwrite permanent memory entries — only update Recovery Context section
- Keep output concise — this skill exists to SAVE context window, not consume it
