---
name: mem-index
description: "Browse the v3 memory DB: list entries by tier/type and show DB stats."
model: haiku
category: memory
user-invocable: true
disable-model-invocation: true
argument-hint: "[--tier instance|shared|class|global] [--type feedback|project|reference|user] [--stats]"
allowed-tools: Read, Write, Bash, Grep, Glob
---

# Memory Index

Browse the superclaude memory DB (v3). Memory is DB-resident: all tiers (instance, shared,
class, global) are indexed in `~/.claude/agent-memory/.memory.db`. The legacy per-directory `MEMORY.md`
index files are deprecated as the authoritative source — the DB is the single index.

**Arguments**: $ARGUMENTS

## Procedure

### Step 1: Show DB stats (always run first)

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py stats
```

Prints total rows, per-tier counts, per-type counts, and last-updated timestamp.

### Step 2: List entries

List all entries (optionally filtered):

```bash
# All entries
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py list

# Filter by tier: instance | shared | class | global
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py list --tier shared

# Filter by type: feedback | project | reference | user
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py list --type project

# Both filters combined
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py list --tier shared --type feedback
```

Each row shows: `id | name | tier | type | description (truncated)`.

### Step 3: Inspect a specific entry

```bash
# By name (slug)
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py get --name <name>

# By numeric id
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py get --id <N>

# Rendered HTML (for rich text / markdown notation)
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py get --name <name> --html
```

## Output Format

```
## Memory Index

### DB Stats
- Total entries: N
- Tiers: instance=A, shared=B, class=C, global=D
- Types: feedback=W, project=X, reference=Y, user=Z
- Last updated: <timestamp>

### Entries (scope: <tier> / <type>)
| id | name | tier | type | description |
|----|------|------|------|-------------|
| 1  | feedback_testing_real_db | shared | feedback | Integration tests must hit real DB… |
| …  | …    | …    | …    | …           |
```

## Tiered Model (preserved in DB columns)

The tiered memory architecture is unchanged; tiers are now DB columns, not directory paths:

| Tier | Old path | DB `tier` value |
|------|----------|-----------------|
| Instance | `agent-memory/instance/<name>/` | `instance` |
| Class | `agent-memory/class/<class>/` | `class` |
| Shared/project | `agent-memory/shared/projects/` | `shared` |
| Global | `agent-memory/shared/global/` | `global` |

## When to Run

- When you need to browse what memories exist without doing a keyword search
- After bulk memory promotion via `/lt-mem` (verify entries appeared in DB)
- Before `/super-health --complete` audit (ensures DB is populated)
- Manually when you want an overview of memory coverage by tier or type

## Cross-References

- Memory access protocol: `~/.claude/rules/12-agent-hierarchy.md` § Memory Access
- Memory hygiene: `/mem-health`, `/memory-prune`, `/lt-mem`
- Search: `/memory-search` (keyword + semantic queries against the same DB)
