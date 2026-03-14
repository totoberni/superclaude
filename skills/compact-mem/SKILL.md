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

Compress memory files for: $ARGUMENTS (default: all)

## Line Budgets

| Row | Cell | Path Pattern | Max |
|-----|------|-------------|-----|
| 1 | Global LTM | `shared/global/ltm.md` | 60 |
| 1 | Project | `shared/projects/<project>.md` | 60 |
| 2 | Class MTM | `class/<class>/mtm.md` | 40 |
| 2 | Class project | `class/projects/<class>/<project>.md` | 30 |
| 3 | Meta | `instance/meta/MEMORY.md` | 80 |
| 3 | Orch | `instance/<orch>/MEMORY.md` | 40 |
| 3 | Other | `instance/<name>/MEMORY.md` | 30 |

## Procedure

### 1. Measure

```bash
MEM="$HOME/.claude/agent-memory"
wc -l "$MEM"/shared/global/ltm.md "$MEM"/shared/projects/*.md 2>/dev/null
wc -l "$MEM"/class/*/mtm.md "$MEM"/class/projects/*/*.md 2>/dev/null
wc -l "$MEM"/instance/*/MEMORY.md 2>/dev/null
```

### 2. Compress (in order of aggressiveness)

| Technique | When | Transform |
|-----------|------|-----------|
| Prose → bullet | Paragraphs | "When X because Y, do Z" → `- X → Y. Fix: Z` |
| Merge related | 2+ entries same topic | 3 entries → 1 with sub-bullets |
| Table → inline | <3 rows | Single-row table → `Key: value` |
| Promote to ref | Detail in another file | Full text → `See: <path>` |
| Drop timestamps | Static items | Remove dates that add no value |

### 3. Keep/Cut/Archive Rules

**KEEP**: gotchas/fix recipes, canonical paths, active orch state, DEC-NNN, wins/mistakes tables.

**CUT**: "Updated: [date]" on static items, verbose prose (one-liner suffices), completed TODOs, session-specific commit hashes.

**ARCHIVE completed orchs**: directives.md → wipe; bootstrap.md → header only; reports.md → final RPT only; escalations.md → wipe if resolved. Move stale memory entries to cell `archive/` subdir.

### 4. Verify

1. `wc -l` — all within budget
2. Grep paths in compacted files — verify targets exist
3. Every gotcha/mistake still has its fix recipe

### 5. Report

```
| Row | Cell | Before | After | Budget | Status |
|-----|------|--------|-------|--------|--------|
Total savings: N lines across M files
Archived: [list]
Verification: [broken refs | none]
```

**Principle**: Density over length. One lesson per line beats a paragraph. Future agents need facts, not narratives.
