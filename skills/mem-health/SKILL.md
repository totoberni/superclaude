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

Read-only assessment of `~/.claude/agent-memory/` matrix. Never modifies anything.

**Mode**: $ARGUMENTS (default: normal. `--quick` skips dedup. `--verbose` shows per-entry detail)

## Scoring

| # | Criterion | Pts | Measurement |
|---|-----------|-----|-------------|
| 1 | Line budgets | 20 | `cells_in_budget / total_populated * 20` |
| 2 | No broken refs | 20 | `(entries - broken) / entries * 20` |
| 3 | No cross-cell dupes | 20 | `(entries - dupes) / entries * 20` (skip if `--quick`, award 20) |
| 4 | Load-order paths valid | 15 | `valid / total * 15` (full 15 if none) |
| 5 | Entry formatting | 15 | `formatted / total * 15` |
| 6 | Archives manageable | 10 | Per-archive: >50=0, 20-50=5, <20=10. Weighted avg. |

## Line Budgets

| Cell | Budget |
|------|--------|
| `shared/global/ltm.md` | 60 |
| `shared/projects/*.md` | 60 each |
| `class/*/mtm.md` | 40 each |
| `instance/meta/MEMORY.md` | 80 |
| `instance/orch*/MEMORY.md`, `instance/o-*/MEMORY.md` | 40 each |
| Other instance | 30 each |

## Procedure

### Step 0: Collect
```bash
MEM="$HOME/.claude/agent-memory"
wc -l "$MEM"/shared/global/ltm.md "$MEM"/shared/projects/*.md 2>/dev/null
wc -l "$MEM"/class/*/mtm.md 2>/dev/null
wc -l "$MEM"/instance/*/MEMORY.md 2>/dev/null
```
Exclude empty cells (only headers, no table rows or bullets) from denominators.

### Step 1: Line Budgets (20 pts)
Check each populated cell against budget table above.

### Step 2: Broken References (20 pts)
Scan cells for `~/.claude/` paths. Expand `~` to `$HOME`. Verify with `test -f`/`test -d`. Full 20 if no refs.

**Exclusions**: Skip `_system/_archive/` (archived files are expected to reference deleted resources) and `_system/_compact-snapshots/` (ephemeral). Only scan active cells: `shared/`, `class/`, `instance/`.

### Step 3: Cross-Cell Duplicates (20 pts)
Skip if `--quick` (award 20). Compare Wins/Mistakes/Gotchas **content** across tiers. Flag redundant copies where lower-tier adds no context beyond higher-tier.

**Important**: IDs (M-1, W-2, etc.) are project-scoped, not global. <PROJECT> M-1 ≠ VPS M-1. Do NOT flag same-numbered IDs in different project files as duplicates — compare the actual summary text instead.

### Step 4: Load-Order Paths (15 pts)
```bash
grep -r "agent-memory" "$HOME/.claude/agents/"*.md 2>/dev/null
```
Verify each path resolves. Skip template patterns (`<...>`). Full 15 if none.

### Step 5: Entry Formatting (15 pts)
- Wins/Mistakes: table rows `| ... |`
- Gotchas: bullets `- **keyword**: description`
- Instance MEMORY.md: free-form (always passes)

### Step 6: Archives (10 pts)
```bash
for d in "$MEM"/shared/*/archive "$MEM"/class/*/archive "$MEM"/instance/*/archive; do
  [ -d "$d" ] && echo "$(ls "$d" 2>/dev/null | wc -l) $d"
done
```

## Output
```
## Memory Health Report
**Score: NN/100**
| # | Criterion | Score | Detail |
| Cell Detail |
| Row | Cell | Lines | Budget | Status | Issues |
### Recommendations
```

## v3 Triggers (append if any fire)

| Trigger | Threshold | Action |
|---------|-----------|--------|
| Corpus >2000 lines | Count active cells only: `find $MEM/{shared,class,instance} -name "*.md"`. Exclude `_system/` (ephemeral snapshots + archives — cleaned by pre-compact.sh retention + /lt-mem) | Run `/lt-mem --quick all` |
| Any class mtm >40 lines | Single file | class/projects/ tier split |
| 3+ class cells >30 lines | Count | Standalone manifests |
| Cross-cell duplication >10% | From criterion 3 | `/lt-mem` promotion logic |

If none fire: "v3 Triggers: None. All within v2 thresholds."
