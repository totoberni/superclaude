---
name: good-idea
description: "Record effective solutions and patterns for reuse across sessions."
category: memory
user-invocable: true
argument-hint: "[project-name or 'all']"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

# Good Idea Retrospective

Record effective solutions and patterns from this session.

**Scope**: $ARGUMENTS (project name, or "all" for cross-project)

## Procedure

### 1. Detect Agent Class + Gather Evidence

Infer class: `o-`/`orch` → `orch`, `scaf` → `scaf`, `meta` → `meta`, `w-<type>-N` → `w-<type>`. Check if the class tier has entries (`HF_HUB_OFFLINE=1 ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py list --tier class` filtered to your class) — enables dual-write to class tier.

Read in parallel:
- `git -C <repo> log --oneline -20` + `git diff --stat HEAD~5..HEAD`
- Recent orch RPTs (look for DONE, smooth completions):
  ```bash
  DB="$HOME/.claude/comms/.broker.db"
  sqlite3 -header -column "$DB" "SELECT from_agent, seq, datetime(ts,'unixepoch') AS t, substr(body,1,80) AS preview FROM messages WHERE kind='RPT' ORDER BY ts DESC LIMIT 20;"
  ```
  Or semantic search: `HF_HUB_OFFLINE=1 ~/.claude/.venv/bin/python ~/.claude/scripts/memory/comms_db.py search "DONE completed smooth"`
- Plan state (`~/.claude/plans/*/state*.md`) — tasks completed faster than expected

### 2. Identify + Tag Wins

Tag each win: `[WORKING_SOLUTION]` (confirmed pattern), `[DECISION]` (design choice + reasoning), `[PREFERENCE]` (style/convention).

Categories: tool usage, architecture, delegation, process, code pattern.

### 3. Scope + Dedup

| Scope | DB tier | Type | Who Writes |
|-------|---------|------|------------|
| Project win | `shared` (--agent <project>) | project / feedback | **Meta only** |
| Cross-project | `global` | project | Meta only |
| Class-level | `class` (--agent <class>) | project / feedback | Any agent of that class |
| Universal tool | `rules/20-tool-conventions.md` (Edit tool) | — | Via promotion |

**Orchs**: `shared` tier writes are sandbox-denied. Write to `class` tier (primary) + `instance` tier (secondary). Meta promotes via `/lt-mem`.

Search the DB for duplicates before recording: `memory_db.py search '<summary>' -k 3`. Skip or update if already present.

### 4. Record

Storage uses the v3 memory DB CLI. Env prefix for every call: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1`.
CLI: `~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py`

**For each win, search first, then upsert:**

```bash
# 1. Search to find an existing entry to update vs creating a new one
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py \
  search '<win summary in a few words>' -k 3
```

If a matching entry exists, reuse its `--name` slug. Otherwise derive a new kebab-case slug.

**Project wins** (meta only — tier=shared, scoped to project; technical wins → type=project, process wins → type=feedback):

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py \
  upsert --tier shared --type project \
  --name <slug-kebab-case> \
  --description "<one-line summary>" \
  --agent <project-name> \
  --text-stdin <<'EOF'
| W-<N> | <Phase> | <What Worked> | <Why> | Reusable? Yes |
EOF
```

**Cross-project wins** (tier=global, type=project or reference):

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py \
  upsert --tier global --type project \
  --name <slug-kebab-case> \
  --description "<one-line summary>" \
  --text-stdin <<'EOF'
| CW-<N> | <Pattern> | <Source Projects> |
EOF
```

**Class dual-write** (tier=class, type=feedback or project; --agent=<class-name>):

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py \
  upsert --tier class --type project \
  --name <slug-kebab-case> \
  --description "<one-line summary>" \
  --agent <class-name> \
  --text-stdin <<'EOF'
| <ID> | <Pattern> | <One-liner> [<project>] | <Source> |
EOF
```

Each `upsert` prints `upserted id=N` on success.

### 5. Check Promotion

If `Reusable? = Yes` and seen in 2+ projects:
1. Upsert to global tier: `upsert --tier global --type project --name <cw-slug> ...` (see Step 4 Cross-project block)
2. If tool pattern → promote to `rules/20-tool-conventions.md` via Edit tool
3. Mark source entries as `Promoted`

### 6. Report

```
## Good Ideas Summary
| # | Win | Category | Recorded In | Reusable? | Promoted? |
|---|-----|----------|-------------|-----------|-----------|

### Patterns Promoted to Rules
- [list or "None"]
```

## Storage
- All writes go through `~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py upsert` with `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1`.
- Tier mapping: project-scoped win → `--tier shared --agent <project>`; cross-project → `--tier global`; class-level → `--tier class --agent <class>`; instance → `--tier instance`.
- Type mapping: technical wins → `--type project`; process/convention wins → `--type feedback`.
- Write scopes: rule 12. Class writes are layer 2 only. Promotion to global via `/lt-mem`.
- Rules promotion (`rules/20-tool-conventions.md`) still uses Edit tool directly — that file is not in the DB.

## Loop integration (converge)

This skill is a ONE-SHOT retrospective producer: it records a win or pattern into memory, then finishes. It is not an iterate-produce-review loop and never seals itself under `/goal` (see `/converge`).

**Optional single self-check** (before the Step 4 DB write, or a Step 5 promotion): re-examine the classification once, fresh. Is the win category correct, is the tier/scope (shared vs class vs global) the right audience, is a promotion claim to a wider tier genuinely warranted rather than assumed. One pass on the classification before the write, not a review round.

**Conductor context** (per `/converge`): loop orchestration, dispatching producers, invoking `/review-dispatch`, printing the `/goal` block, is a conductor concern. This skill does not drive a loop and holds no conductor role.
