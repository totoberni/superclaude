---
name: mistake
description: "Record mistakes and promote recurring patterns to prevention rules."
category: memory
user-invocable: true
argument-hint: "[project-name or 'all']"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

# Mistake Retrospective

Record mistakes and patterns from this session.

**Scope**: $ARGUMENTS (project name, or "all" for cross-project)

## Procedure

### 1. Detect Agent Class + Gather Evidence

Infer class: `o-`/`orch` → `orch`, `scaf` → `scaf`, `meta` → `meta`, `w-<type>-N` → `w-<type>`. Check if the class tier has entries (`HF_HUB_OFFLINE=1 ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py list --tier class` filtered to your class) — enables dual-write to class tier.

Read in parallel:
- `git -C <repo> log --oneline -20` + `git reflog --oneline -30` — look for reverts, fixups
- Recent orch RPTs and ESCs (look for BLOCKED, retries):
  ```bash
  DB="$HOME/.claude/comms/.broker.db"
  sqlite3 -header -column "$DB" "SELECT kind, from_agent, seq, datetime(ts,'unixepoch') AS t, substr(body,1,80) AS preview FROM messages WHERE kind IN ('RPT','ESC') ORDER BY ts DESC LIMIT 30;"
  ```
  Or semantic search: `HF_HUB_OFFLINE=1 ~/.claude/.venv/bin/python ~/.claude/scripts/memory/comms_db.py search "BLOCKED retry failed"`

### 2. Tag Mistakes

Tag each: `[FAILURE]` (what didn't work + why), `[GOTCHA]` (counterintuitive trap), `[PATTERN]` (recurring, 2+ times).

### 3. Scope + Dedup

| Scope | DB tier | Type | Who Writes |
|-------|---------|------|------------|
| Project mistake/gotcha | `shared` (--agent <project>) | feedback / project | **Meta only** |
| Class-level | `class` (--agent <class>) | feedback | Any agent of that class |
| Universal tool pattern | `rules/20-tool-conventions.md` (Edit tool) | — | Via promotion |
| Agent operational | `instance` (--agent <agent>) | feedback | That agent |

**Orchs**: `shared` tier writes are sandbox-denied. Write to `class` tier (primary) + `instance` tier (secondary). Meta promotes via `/lt-mem`.

Search the DB for duplicates before recording: `memory_db.py search '<summary>' -k 3`. If already recorded, upsert with updated body (increment Occurrences in the text).

### 4. Record

Storage uses the v3 memory DB CLI. Env prefix for every call: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1`.
CLI: `~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py`

**For each mistake, search first, then upsert:**

```bash
# 1. Search to find an existing entry to update (increment occurrence) vs creating new
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py \
  search '<mistake summary in a few words>' -k 3
```

If a matching entry exists, reuse its `--name` slug (upsert updates in place). Otherwise derive a new kebab-case slug.

**Project mistakes/gotchas** (meta only — tier=shared, type=feedback for process gotchas / type=project for project-specific bugs):

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py \
  upsert --tier shared --type feedback \
  --name <slug-kebab-case> \
  --description "<one-line summary>" \
  --agent <project-name> \
  --text-stdin <<'EOF'
| M-<N> | <Phase> | <What Went Wrong> | <Root Cause> | <Fix> | <Prevention> | 1 |
EOF
```

**Class dual-write** (tier=class, type=feedback; --agent=<class-name>):

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py \
  upsert --tier class --type feedback \
  --name <slug-kebab-case> \
  --description "<one-line summary>" \
  --agent <class-name> \
  --text-stdin <<'EOF'
| <ID> | <Summary> [<project>] | <Prevention Rule> | 1 |
EOF
```

Each `upsert` prints `upserted id=N` on success.

**Universal tool patterns**: still appended to `rules/20-tool-conventions.md` via Edit tool — that file is not in the DB:
```markdown
## <Pattern Title>
- <Concise rule>
- WRONG: `<example>` | RIGHT: `<example>`
```

### 5. Check Promotion

If `Occurrences >= 2` (same pattern, different contexts):
1. Promote to `rules/20-tool-conventions.md` via Edit tool
2. Upsert a global-tier entry marking the pattern: `upsert --tier global --type feedback --name <slug> ...`
3. Update the source DB entries to note `[PROMOTED->rules]` in the body

### 6. Report

```
## Retrospective Summary
| # | Mistake | Category | Recorded In | Occ | Promoted? |
|---|---------|----------|-------------|-----|-----------|

### Patterns Promoted to Rules
- [list or "None"]
### Recommendations
- [process improvements]
```

## Storage
- All mistake/gotcha writes go through `~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py upsert` with `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1`.
- Tier mapping: project-scoped mistake → `--tier shared --agent <project>`; class-level → `--tier class --agent <class>`.
- Type mapping: process gotchas / agent behavior mistakes → `--type feedback`; project-specific bugs / codebase gotchas → `--type project`.
- Write scopes: rule 12. Class writes are layer 2 only. Promotion to global via `/lt-mem`.
- Rules promotion (`rules/20-tool-conventions.md`) still uses Edit tool directly — that file is not in the DB.
