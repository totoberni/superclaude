---
name: remember
description: "Meta context save/load: cheaper than compaction"
category: memory
user-invocable: true
disable-model-invocation: true
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
   ```bash
   HF_HUB_OFFLINE=1 ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py search "meta recovery context current state" -k 3
   ```
   Then fetch the full body of the top hit: `memory_db.py get --name <slug>` (or `get --id <n>`). Present the Recovery Context section + any permanent memories.
2. **Registry**: Read `~/.claude/comms/meta-registry.md` — list active orchs with status
3. **Compact snapshot**: If the recovered entry references a snapshot slug, fetch it: `memory_db.py get --name <slug>`

Output format:
```
## Recovered Context
- **Last session**: <focus from recovery context>
- **Active orchs**: <count> (<names>)
- **Pending actions**: <from recovery context>
- **Unfinished**: <what was interrupted>
```

## Mode: --save

Write a structured recovery snapshot via the v3 memory DB CLI. **Search first** to detect an existing entry to update vs creating new.

Env prefix for every call: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1`.
CLI: `~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py`

```bash
# 1. Search for an existing recovery-context entry to update
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py \
  search 'meta recovery context session focus' -k 3

# 2. Upsert (tier=instance for session recovery state; type=user for agent operational context)
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py \
  upsert --tier instance --type user \
  --name meta-recovery-context \
  --description "Meta agent session recovery context (<YYYY-MM-DD HH:MM>)" \
  --agent meta \
  --text-stdin <<'EOF'
## Recovery Context (<YYYY-MM-DD HH:MM>)
- **Session focus**: [1 line summary]
- **Decisions made**: [DEC refs, directive amendments]
- **Active orchs**: [1-liner status per orch]
- **Pending actions**: [concrete next steps]
- **the user's preferences**: [feedback-worthy items from this session]
- **Unfinished**: [what got interrupted, exact point]
EOF
```

Each `upsert` prints `upserted id=N` on success. Because `--name meta-recovery-context` is stable, repeated saves update in place (no accumulation of stale snapshots).

**Constraints**:
- Keep body under 30 lines (dense — one fact per line)
- `--name` is stable (`meta-recovery-context`) so re-saves overwrite the prior entry

## Mode: --deep

Load everything from default mode, PLUS (in parallel):

4. Each active orch's latest RPT (from broker):
   ```bash
   DB="$HOME/.claude/comms/.broker.db"
   sqlite3 -header -column "$DB" "SELECT from_agent, seq, datetime(ts,'unixepoch') AS t, substr(body,1,120) AS preview FROM messages WHERE kind='RPT' GROUP BY from_agent HAVING ts=MAX(ts) ORDER BY ts DESC;"
   ```
   Or semantic search: `HF_HUB_OFFLINE=1 ~/.claude/.venv/bin/python ~/.claude/scripts/memory/comms_db.py search "RPT status progress"`
5. Each active orch's state: `~/.claude/plans/*/state-*.md`
6. Active plan files: `~/.claude/plans/*/plan.md`
7. Pending escalations (unanswered ESCs):
   ```bash
   sqlite3 -header -column "$DB" "SELECT from_agent, seq, datetime(ts,'unixepoch') AS t, substr(body,1,80) AS preview FROM messages WHERE kind='ESC' AND read_at IS NULL ORDER BY ts ASC;"
   ```

Present as a structured dashboard after loading.

## Constraints

- **--save is write; default and --deep are read-only**
- Never overwrite unrelated DB entries — use stable `--name` slugs so upsert updates in place
- Keep output concise — this skill exists to SAVE context window, not consume it
