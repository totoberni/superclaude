---
name: memory-prune
description: "Use when scanning memory for stale or broken entries; advisory."
category: memory
user-invocable: true
argument-hint: "[scope: 'all' | 'shared' | 'class' | 'instance']"
allowed-tools: Read, Bash, Glob, Grep
---

# Memory Prune

Scan memory matrix cells for stale, broken, or obsolete entries. **Advisory only** — presents
findings for human decision. Deletion is explicit and per-entry via the DB CLI.

Memory is DB-resident (v3): all tiers are indexed in `~/.claude/agent-memory/.memory.db`.
Find candidates with `mem search` or `mem list`, then remove confirmed
stale entries with `mem prune --name <name>`.

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
| Global LTM | `--tier shared-global` | 60 rows |
| Project memories | `--tier shared-projects --type project` | 60 rows each project |
| Class MTM | `--tier class` | 40 rows per class |
| Instance | `--tier instance` | 80 (meta), 30 (workers and other) |

## Procedure

### 1. Enumerate All Entries

```bash
mem list
```

Filter by tier or type to narrow scope:

```bash
# Shared project memories only
mem list --tier shared-projects --type project
```

### 2. Search for Candidates by Symptom

Use semantic + keyword search to surface likely-stale entries:

```bash
mem search "<topic or symptom>" -k 10 --mode hybrid
```

Use `--mode fts` for exact strings (e.g., a deleted file path, a resolved error message).

### 3. Inspect Candidates

```bash
mem get --name <name>
```

Read the full entry text. Apply staleness criteria from the table above.

### 4. Scan for Broken Paths

For each suspicious entry's `text`, verify any `~/.claude/` or backtick-quoted paths still
exist (check each with the Glob or Read tool). Flag entries whose referenced paths are gone.

### 5. Check Dead Projects

```bash
git -C <repo> log -1 --format=%ci
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
mem prune --name <name>
```

Or by numeric id:

```bash
mem prune --id <N>
```

## Key Principle

**Never delete automatically.** Memory entries may look stale but encode hard-won lessons.
Present findings with context so the user (or Meta) can make informed decisions. When in
doubt, keep. `mem prune` is only invoked after explicit human sign-off.

## Self-check

memory-prune is a ONE-SHOT advisory scan: a single invocation runs the seven steps above and presents flagged candidates for human decision. The Step 7 human-confirmation gate before any deletion is NEVER replaced by an automated approval.

Before presenting the Step 6 report, run ONE optional adversarial false-positive pass over the flagged table: re-examine each stale/broken/dead candidate against fuller context (recent references, active-project signals, near-duplicate coverage) to catch a memory that looks stale but is still load-bearing. This is a single self-review pass, not a review loop; the human still confirms every deletion at Step 7.
