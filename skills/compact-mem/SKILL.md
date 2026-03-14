---
name: compact-mem
description: "Compress memory files: shorten prose, merge entries, enforce budgets."
category: memory
user-invocable: true
disable-model-invocation: true
argument-hint: "[scope: 'all' | 'agent <name>' | 'comms <orch-name>']"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

# Memory Compaction

Compress and tighten superclaude memory files for: $ARGUMENTS (default: all)

## What This Skill Does

Reduces memory file sizes while preserving all useful information. Shortens verbose prose, merges related entries, enforces line budgets, and archives completed orch communications.

## Line Budgets (Memory Matrix)

### Row 1: Shared (cross-agent)

| Cell | Path | Max Lines |
|------|------|-----------|
| Global LTM | `shared/global/ltm.md` | 60 |
| Project memory | `shared/projects/<project>.md` | 60 each |

### Row 2: Class (per agent type)

| Cell | Path | Max Lines |
|------|------|-----------|
| Class MTM | `class/<class>/mtm.md` | 40 |
| Class project | `class/projects/<class>/<project>.md` | 30 each |

### Row 3: Instance (per agent)

| Cell | Path | Max Lines |
|------|------|-----------|
| Meta | `instance/meta/MEMORY.md` | 80 |
| Orch/named-orch | `instance/<orch>/MEMORY.md` | 40 |
| Other (workers, scaf) | `instance/<name>/MEMORY.md` | 30 |

## Procedure

### 1. Measure Current State

Scan all three rows of the memory matrix:

```bash
MEM="$HOME/.claude/agent-memory"
echo "=== Row 1: Shared ==="
wc -l "$MEM"/shared/global/ltm.md "$MEM"/shared/projects/*.md 2>/dev/null
echo "=== Row 2: Class ==="
wc -l "$MEM"/class/*/mtm.md "$MEM"/class/projects/*/*.md 2>/dev/null
echo "=== Row 3: Instance ==="
wc -l "$MEM"/instance/*/MEMORY.md 2>/dev/null
```

Flag any file over its budget.

### 2. Compression Techniques (in order)

| Technique | When to Use | Example |
|-----------|-------------|---------|
| **Prose → bullet** | Paragraphs explaining a pattern | "When X happens because of Y, do Z" → `- X → Y. Fix: Z` |
| **Merge related** | 2+ entries about the same topic | 3 test isolation entries → 1 section with 3 sub-bullets |
| **Table → inline** | Table with <3 rows | Single-row table → `Key: value` |
| **Promote to reference** | Detail that exists in another file | Full explanation → `See: <path>` |
| **Archive completed** | Orch comms for finished orchs | Move reports.md content to summary, wipe directives/bootstrap |
| **Drop timestamps** | Dates on items that won't change | `(2026-03-08 04:15)` → remove if the date adds no value |

### 3. Compaction Rules

**KEEP (always):**
- Patterns that prevent mistakes (gotchas, fix recipes)
- Canonical file locations and references
- Active pipeline state and orch status
- Decisions with rationale (DEC-NNN)
- Wins and mistakes tables (compressed)

**CUT (always):**
- "Updated: [date]" on items that won't be re-dated
- Verbose explanations when a one-liner suffices
- Session-specific commit hashes (unless needed for rollback)
- Duplicate section headers with no content
- TODOs that have been completed

**ARCHIVE (for completed orchs):**
- `comms/<orch>/directives.md` → wipe (bootstrap has the summary)
- `comms/<orch>/bootstrap.md` → keep header only (role + repo + branch)
- `comms/<orch>/reports.md` → keep final RPT only (drop intermediate reports)
- `comms/<orch>/escalations.md` → wipe if all resolved

**ARCHIVE (for memory matrix cells):**
- Move stale entries to cell-specific `archive/` subdir (e.g., `shared/global/archive/`, `class/<class>/archive/`)
- Archive dirs already exist for seeded class cells
- Instance memories: archive to `instance/<name>/archive/` (create if needed)
- Archived content keeps its filename + date suffix: `ltm-2026-03.md`

### 4. Verify Post-Compaction

After compacting, verify:
1. `wc -l` — all files within budget (use cell-specific budgets from tables above)
2. No broken references — grep for paths mentioned in compacted files, verify they exist
3. No lost lessons — every gotcha/mistake still has its fix recipe
4. Run `/memory-prune all` to confirm no new issues introduced

### 5. Report

```
## Compaction Report

### Before / After

| Row | Cell | Before | After | Budget | Status |
|-----|------|--------|-------|--------|--------|
| 1 | shared/global/ltm.md | N | N | 60 | OK/OVER |
| 1 | shared/projects/X.md | N | N | 60 | OK/OVER |
| 2 | class/orch/mtm.md | N | N | 40 | OK/OVER |
| 3 | instance/meta/MEMORY.md | N | N | 80 | OK/OVER |
| ... | ... | ... | ... | ... | ... |

### Total savings: N lines removed across M files

### Archived
- [list of entries moved to archive/ subdirs]
- [list of completed orch comms archived]

### Verification
- Broken references: [none | list]
- Missing patterns: [none | list]
```

## Key Principle

**Density over length.** A 30-line file with one lesson per line beats a 100-line file with prose. Future agents need facts, not narratives. Compress aggressively — if a pattern matters, it survives compression. If it doesn't survive, it wasn't important.
