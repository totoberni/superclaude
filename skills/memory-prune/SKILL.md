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

Scan memory matrix cells for stale, broken, or obsolete entries. **Advisory only** — presents findings for human decision, never auto-deletes.

**Scope**: $ARGUMENTS (default: all)

## What Gets Flagged

| Category | Criteria | Action |
|----------|----------|--------|
| **Broken path** | Entry references a file/dir that doesn't exist | Flag for removal |
| **Stale mistake** | Mistake with Occ=1, older than 30 days | Flag for review |
| **Resolved gotcha** | Gotcha for an issue that's been fixed | Flag for archival |
| **Oversized cell** | File exceeds its line budget | Flag for /compact-mem |
| **Dead project** | Project memory for a project with no recent commits (>60 days) | Flag for archival |

## Scan Targets

### Row 1: Shared (cross-agent)

| Cell | Path | Budget |
|------|------|--------|
| Global LTM | `shared/global/ltm.md` | 60 lines |
| Project memory | `shared/projects/<project>.md` | 60 lines each |

### Row 2: Class (per agent type)

| Cell | Path | Budget |
|------|------|--------|
| Class MTM | `class/<class>/mtm.md` | 40 lines |
| Class project | `class/projects/<class>/<project>.md` | 30 lines each |

### Row 3: Instance (per agent)

| Cell | Path | Budget |
|------|------|--------|
| Instance MEMORY.md | `instance/<name>/MEMORY.md` | 80 (meta), 40 (orch), 30 (other) |

## Procedure

### 1. Collect All Cells

```bash
MEM="$HOME/.claude/agent-memory"
# Row 1
wc -l "$MEM"/shared/global/ltm.md "$MEM"/shared/projects/*.md 2>/dev/null
# Row 2
wc -l "$MEM"/class/*/mtm.md "$MEM"/class/projects/*/*.md 2>/dev/null
# Row 3
wc -l "$MEM"/instance/*/MEMORY.md 2>/dev/null
```

### 2. Check Line Budgets

Flag any cell exceeding its budget (see tables above).

### 3. Scan for Broken Paths

For each memory file, extract path references (lines matching `~/.claude/` or backtick-quoted paths). Verify each exists. Flag missing.

### 4. Scan for Stale Entries

- **Mistakes**: grep for `Occ=1` or `| 1 |` entries. Check if the date is >30 days old.
- **Gotchas**: check if referenced files/patterns still exist in the codebase.
- **Projects**: `git -C <repo> log -1 --format=%ci 2>/dev/null` — flag if >60 days.

### 5. Present Findings

```
## Memory Prune Report

### Budget Status
| Cell | Lines | Budget | Status |
|------|-------|--------|--------|
| shared/projects/example-project.md | 38 | 60 | OK |
| ... | ... | ... | ... |

### Flagged Entries (N total)
| # | Cell | Entry | Reason | Recommendation |
|---|------|-------|--------|----------------|
| 1 | shared/projects/X.md | "workaround for bug Y" | Path /foo/bar gone | Remove |
| ... | ... | ... | ... | ... |

### Action Required
- Review flagged entries above
- Run `/compact-mem` on oversized cells
- Archive dead project memories if confirmed inactive
```

## Key Principle

**Never delete automatically.** Memory entries may look stale but encode hard-won lessons. Present findings with context so the user (or Meta) can make informed decisions. When in doubt, keep.
