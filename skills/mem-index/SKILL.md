---
name: mem-index
description: "Use when browsing the memory DB by tier or type, with stats."
model: haiku
category: memory
user-invocable: true
argument-hint: "[--tier instance|class|shared-projects|shared-global|archive] [--type feedback|project|reference|user] [--stats]"
allowed-tools: Read, Write, Bash, Grep, Glob
---

# Memory Index

Browse the superclaude memory DB (v3). Memory is DB-resident: all tiers (instance, class,
shared-projects, shared-global, archive) are indexed in `~/.claude/agent-memory/.memory.db`. The legacy per-directory `MEMORY.md`
index files are deprecated as the authoritative source — the DB is the single index.

**Arguments**: $ARGUMENTS

## Procedure

### Step 1: Show DB stats (always run first)

```bash
mem stats
```

Prints the three row totals, then per-tier counts, per-type counts, and the last-updated timestamp:

```
memories : N
fts rows : N
vec rows : N
tiers    : instance=A, shared-global=B, shared-projects=C
types    : feedback=W, project=X, reference=Y, user=Z
updated  : <ISO timestamp>
```

### Step 2: List entries

List all entries (optionally filtered):

```bash
# All entries
mem list

# Filter by tier: instance | class | shared-projects | shared-global | archive
# (`shared` and `global` are accepted as input aliases for the two shared tiers)
mem list --tier shared-projects

# Filter by type: feedback | project | reference | user
mem list --type project

# Both filters combined
mem list --tier shared-projects --type feedback
```

Each row shows: `name  tier=<tier>  type=<type>  path`. There is no id and no description column;
use `mem get --name <name>` for the body.

### Step 3: Inspect a specific entry

```bash
# By name (slug)
mem get --name <name>

# By numeric id
mem get --id <N>

# Rendered HTML (for rich text / markdown notation)
mem get --name <name> --html
```

## Output Format

```
## Memory Index

### DB Stats
- Total entries: N (fts rows: N, vec rows: N)
- Tiers: instance=A, class=B, shared-projects=C, shared-global=D
- Types: feedback=W, project=X, reference=Y, user=Z
- Last updated: <timestamp>

### Entries (scope: <tier> / <type>)
| name | tier | type | path |
|------|------|------|------|
| feedback_testing_real_db | shared-projects | feedback | shared-projects/_/feedback_testing_real_db.md |
| ...  | ...  | ...  | ...  |
```

## Tiered Model (preserved in DB columns)

The tiered memory architecture is unchanged; tiers are now DB columns, not directory paths:

| Tier | Old path | DB `tier` value | Accepted input alias |
|------|----------|-----------------|----------------------|
| Instance | `agent-memory/instance/<name>/` | `instance` | none |
| Class | `agent-memory/class/<class>/` | `class` | none |
| Shared/project | `agent-memory/shared/projects/` | `shared-projects` | `shared` |
| Global | `agent-memory/shared/global/` | `shared-global` | `global` |
| Archive | none | `archive` | none |

The aliases are normalised on input only: the stored value, and what `mem list` and `mem stats` print, is always the canonical long name.

## When to Run

- When you need to browse what memories exist without doing a keyword search
- After bulk memory promotion via `/lt-mem` (verify entries appeared in DB)
- Manually when you want an overview of memory coverage by tier or type

## Cross-References

- Memory access protocol: `~/.claude/rules/12-agent-hierarchy.md` § Memory Access
- Memory hygiene: `/mem-health`, `/memory-prune`, `/lt-mem`
- Search: `/memory-search` (keyword + semantic queries against the same DB)
