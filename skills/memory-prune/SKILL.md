---
name: memory-prune
description: "Scan memory matrix for stale or broken entries. Advisory only."
category: memory
user-invocable: true
disable-model-invocation: true
argument-hint: "[scope: 'all' | 'shared' | 'class' | 'instance']"
allowed-tools: Read, Bash, Glob, Grep
---

# Memory Prune

Scan memory matrix cells for stale, broken, or obsolete entries. **Advisory only** — presents
findings for human decision. Deletion is explicit and per-entry via the DB CLI.

Memory is DB-resident (v3): all tiers are indexed in `~/.claude/agent-memory/.memory.db`.
Find candidates with `memory_db.py search` or `memory_db.py list`, then remove confirmed
stale entries with `memory_db.py prune --name <name>`.

**Scope**: $ARGUMENTS (default: all)

## What Gets Flagged

| Category | Criteria | Action |
|----------|----------|--------|
| **Broken path** | Entry references a file/dir that doesn't exist | Flag for removal |
| **Stale mistake** | Mistake with Occ=1, older than 30 days | Flag for review |
| **Resolved gotcha** | Gotcha for an issue that's been fixed | Flag for archival |
| **Oversized tier** | Tier row count exceeds budget (see `/mem-health`) | Flag for /lt-mem --compact |
| **Dead project** | Project memory for a project with no recent commits (>60 days) | Flag for archival |

## Tier Budgets (unchanged — now row counts in DB)

| Tier | DB filter | Budget |
|------|-----------|--------|
| Global LTM | `--tier global` | 60 rows |
| Project memories | `--tier shared --type project` | 60 rows each project |
| Class MTM | `--tier class` | 40 rows per class |
| Instance | `--tier instance` | 80 (meta), 40 (orch), 30 (other) |

## Procedure

### 1. Enumerate All Entries

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py list
```

Filter by tier or type to narrow scope:

```bash
# Shared project memories only
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py list \
  --tier shared --type project
```

### 2. Search for Candidates by Symptom

Use semantic + keyword search to surface likely-stale entries:

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py \
  search '<topic or symptom>' -k 10 --mode hybrid
```

Use `--mode fts` for exact strings (e.g., a deleted file path, a resolved error message).

### 3. Inspect Candidates

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py get --name <name>
```

Read the full entry text. Apply staleness criteria from the table above.

### 4. Scan for Broken Paths

For each suspicious entry's `text`, verify any `~/.claude/` or backtick-quoted paths still
exist with `test -f`/`test -d`. Flag entries whose referenced paths are gone.

### 5. Check Dead Projects

```bash
git -C <repo> log -1 --format=%ci 2>/dev/null
```

Flag project-type entries where the repo has no commits in >60 days.

### 6. Present Findings

```
## Memory Prune Report

### Flagged Entries (N total)
| # | name | tier | type | Reason | Recommendation |
|---|------|------|------|--------|----------------|
| 1 | feedback_workaround_foo | shared | feedback | Path /foo/bar gone | prune |
| 2 | project_vps_migration | shared | project | Repo silent >60 days | archive |
| … | … | … | … | … | … |

### Action Required
- Confirm each entry above, then prune confirmed stale entries (see below)
- Run `/lt-mem --compact` on oversized tiers
- Archive dead project memories if confirmed inactive
```

### 7. Remove Confirmed Stale Entries

After human confirmation — remove one entry at a time:

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py prune --name <name>
```

Or by numeric id:

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py prune --id <N>
```

## Key Principle

**Never delete automatically.** Memory entries may look stale but encode hard-won lessons.
Present findings with context so the user (or Meta) can make informed decisions. When in
doubt, keep. `memory_db.py prune` is only invoked after explicit human sign-off.
