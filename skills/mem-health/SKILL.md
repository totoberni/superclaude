---
name: mem-health
description: "Score memory matrix health /100. 6 criteria + v3 trigger checks."
category: memory
user-invocable: true
disable-model-invocation: true
argument-hint: "[--quick | --verbose]"
allowed-tools: Read, Bash, Glob, Grep
---

# Memory Health Assessment (/100)

Read-only assessment of the `~/.claude/agent-memory/` matrix. Never modifies anything.

**Mode**: $ARGUMENTS (default: normal. `--quick` skips dedup. `--verbose` shows per-entry detail)

## Scoring Criteria

| # | Criterion | Points | Weight |
|---|-----------|--------|--------|
| 1 | Line budgets respected | /20 | `cells_in_budget / total_populated_cells * 20` |
| 2 | No broken references | /20 | `(total_entries - broken) / total_entries * 20` |
| 3 | No cross-cell duplicates | /20 | `(total_entries - duplicates) / total_entries * 20` |
| 4 | Load-order paths valid | /15 | `valid_refs / total_refs * 15` (full 15 if no refs exist) |
| 5 | Proper entry formatting | /15 | `formatted / total * 15` |
| 6 | Archives manageable | /10 | `>50 entries = 0, 20-50 = 5, <20 = 10` (weighted avg across all archive dirs) |

## Procedure

### Step 0: Collect All Cells

```bash
MEM="$HOME/.claude/agent-memory"

echo "=== Row 1: Shared ==="
wc -l "$MEM"/shared/global/ltm.md "$MEM"/shared/projects/*.md 2>/dev/null

echo "=== Row 2: Class ==="
wc -l "$MEM"/class/*/mtm.md 2>/dev/null

echo "=== Row 3: Instance ==="
wc -l "$MEM"/instance/*/MEMORY.md 2>/dev/null
```

Exclude empty cells (only template headers, no real entries) from all scoring denominators. A cell is "populated" if it has content beyond headers — check for table rows (`| ... |`) or bullet entries below section headers.

### Step 1: Line Budgets (20 pts)

Check each populated cell against its budget:

| Row | Cell Pattern | Budget |
|-----|-------------|--------|
| 1 | `shared/global/ltm.md` | 60 |
| 1 | `shared/projects/<project>.md` | 60 each |

| 2 | `class/<class>/mtm.md` | 40 each |
| 3 | `instance/meta/MEMORY.md` | 80 |
| 3 | `instance/orch*/MEMORY.md`, `instance/o-*/MEMORY.md` | 40 each |
| 3 | `instance/*/MEMORY.md` (other) | 30 each |

**Score**: `cells_in_budget / total_populated_cells * 20` (round down)

### Step 2: Broken References (20 pts)

Scan each populated cell for path references:
- Lines containing `~/.claude/` or backtick-quoted absolute paths
- Lines referencing files like `<project>.md`, `mtm.md`, `ltm.md` with implied paths

For each reference, verify the target exists (`test -f` or `test -d`). Expand `~` to `$HOME`.

**Score**: `(total_entries - broken_refs) / total_entries * 20`

If no path references exist, award full 20.

### Step 3: Cross-Cell Duplicates (20 pts)

**Skip if `--quick`** (award full 20 and note "skipped").

Compare entries across cells at different tiers. Duplication = same concept appearing in 2+ cells (exact match OR semantic overlap — same pattern described differently).

Focus on:
- Wins tables: same `Pattern` column across `ltm.md`, `class/*/mtm.md`, `shared/projects/*.md`
- Mistakes tables: same `Summary` across cells
- Gotchas: same concept in multiple files

**Score**: `(total_entries - duplicate_count) / total_entries * 20`

Deduplication is expected between tiers (project -> class -> global promotion). Flag only redundant copies where the lower-tier entry adds no context beyond the higher-tier version.

### Step 4: Load-Order Paths (15 pts)

Check agent definitions for inline load-order paths referencing memory files:

```bash
grep -r "agent-memory" "$HOME/.claude/agents/"*.md 2>/dev/null
```

Also check if any v3 manifest files exist (`class/*/manifest.md` or similar). Verify each referenced path resolves.

**Score**: `valid_refs / total_refs * 15`. If no refs exist yet, full 15.

### Step 5: Entry Formatting (15 pts)

Check that entries follow the matrix formatting conventions:

| Cell Type | Required Format |
|-----------|----------------|
| Wins (ltm.md, mtm.md, projects) | Table row: `\| # \| Pattern \| Detail/One-liner \| Source/Reusable \|` |
| Mistakes (all cells) | Table row: `\| # \| Summary \| Prevention Rule \| Occ \|` |
| Gotchas (all cells) | Bullet: `- **keyword**: description` |
| Instance MEMORY.md | Free-form (always passes) |

Count entries with correct format vs total entries.

**Score**: `formatted / total * 15`

### Step 6: Archives Manageable (10 pts)

Count entries in each `archive/` subdir across the matrix:

```bash
MEM="$HOME/.claude/agent-memory"
for d in "$MEM"/shared/global/archive "$MEM"/shared/projects/archive "$MEM"/class/*/archive "$MEM"/instance/*/archive; do
  [ -d "$d" ] && echo "$(ls "$d" 2>/dev/null | wc -l) $d"
done
```

Per-archive scoring: `>50 files = 0, 20-50 = 5, <20 = 10`. Weighted average across all existing archive dirs. If no archive dirs have content, full 10.

## Output Format

```
## Memory Health Report

**Score: NN/100**

### Criteria Breakdown
| # | Criterion | Score | Detail |
|---|-----------|-------|--------|
| 1 | Line budgets | NN/20 | X/Y cells in budget |
| 2 | Broken references | NN/20 | X broken refs found |
| 3 | Cross-cell duplicates | NN/20 | X duplicates across Y cells |
| 4 | Load-order paths | NN/15 | X/Y refs valid |
| 5 | Entry formatting | NN/15 | X/Y entries properly formatted |
| 6 | Archives manageable | NN/10 | X archive dirs, avg Y entries |

### Cell Detail
| Row | Cell | Lines | Budget | Status | Issues |
|-----|------|-------|--------|--------|--------|
| 1 | shared/global/ltm.md | 21 | 60 | OK | -- |
| 1 | shared/projects/example-project.md | 38 | 60 | OK | -- |
| ... | ... | ... | ... | ... | ... |

### Recommendations
- [actionable items if score < 100]
```

## v3 Trigger Checks

After scoring, check these v3 criteria and append if ANY fires:

| Trigger | Measurement | Threshold | v3 Feature |
|---------|------------|-----------|------------|
| Corpus size | Sum of all populated cell line counts | >2,000 lines | /lt-mem skill (DIR-035) |
| Class bloat | Any single `class/*/mtm.md` | >40 lines | class/projects/ tier split |
| Class growth | Count of class cells >30 lines | 3+ cells | Standalone manifest files |
| Cross-cell duplication | Duplication % from criterion 3 | >10% | /lt-mem promotion logic |

Output format when trigger fires:
```
### v3 Triggers
v3 TRIGGER: [description]. Recommend activating [feature].
See ~/.claude/agent-memory/shared/projects/superclaude.md for spec.
Prompt the user for approval before proceeding.
```

If no triggers fire: `### v3 Triggers\nNone. All metrics within v2 thresholds.`

## Constraints

- **Read-only** — never modify any file
- **Fast** — target <30 seconds for full scan
- **Deterministic** — same state = same score, always
- **Exclude empty cells** — template-only files (just headers, no entries) don't count in denominators
