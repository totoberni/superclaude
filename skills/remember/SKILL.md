---
name: remember
description: "Use when saving or loading meta context, cheaper than compaction"
category: memory
user-invocable: true
argument-hint: "[--save] [--deep]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

# /remember — Meta Context Preservation

Parse `$ARGUMENTS` to determine mode:
- empty (default) — Load recovery context
- `--save` — Write structured recovery snapshot
- `--deep` — Load everything (full state reconstruction)

## Mode: default (load)

Load context in order, presenting a concise summary after each:

1. **Recovery context**: Query the DB for the latest meta recovery entry:
   ```
   mem search "meta recovery context current state" -k 3
   ```
   Then fetch the full body of the top hit: `mem get --name <slug>` (or `mem get --id <n>`). Present the Recovery Context section + any permanent memories.
2. **Compact snapshot**: If the recovered entry references a snapshot slug, fetch it: `mem get --name <slug>`

Output format:
```
## Recovered Context
- **Last session**: <focus from recovery context>
- **Active workers**: <count> (<names>)
- **Pending actions**: <from recovery context>
- **Unfinished**: <what was interrupted>
```

## Mode: --save

Write a structured recovery snapshot via the v3 memory DB CLI. **Search first** to detect an existing entry to update vs creating new.

CLI: `mem` (on PATH; identical in Git Bash and PowerShell).

1. Search for an existing recovery-context entry to update:

```
mem search "meta recovery context session focus" -k 3
```

2. Write the body to `~/.claude/tmp/meta-recovery-context.md` with the Write tool:

```markdown
## Recovery Context (<YYYY-MM-DD HH:MM>)
- **Session focus**: [1 line summary]
- **Decisions made**: [DEC refs, plan amendments]
- **Active workers**: [1-liner status per running worker or swarm]
- **Pending actions**: [concrete next steps]
- **the user's preferences**: [feedback-worthy items from this session]
- **Unfinished**: [what got interrupted, exact point]
```

3. Upsert (tier=instance for session recovery state; type=user for agent operational context):

```
mem upsert --tier instance --type user --name meta-recovery-context --description "Meta agent session recovery context (<YYYY-MM-DD HH:MM>)" --agent meta --text-file ~/.claude/tmp/meta-recovery-context.md
```

Each `upsert` prints `upserted id=N` on success. Because `--name meta-recovery-context` is stable, repeated saves update in place (no accumulation of stale snapshots).

**Constraints**:
- Keep body under 30 lines (dense — one fact per line)
- `--name` is stable (`meta-recovery-context`) so re-saves overwrite the prior entry

## Mode: --deep

Load everything from default mode, PLUS (in parallel):

3. Plan state: `~/.claude/plans/*/state*.md`
4. Active plan files: `~/.claude/plans/*/plan.md`

Present as a structured dashboard after loading.

## Constraints

- **--save is write; default and --deep are read-only**
- Never overwrite unrelated DB entries — use stable `--name` slugs so upsert updates in place
- Keep output concise — this skill exists to SAVE context window, not consume it
