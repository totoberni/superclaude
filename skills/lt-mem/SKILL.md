---
name: lt-mem
description: "Consolidate memory: promote mature entries, archive stale, clean snapshots."
category: memory
user-invocable: true
disable-model-invocation: true
argument-hint: "[--quick | --complete] [project <name> | all]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

# /lt-mem -- Long-Term Memory Consolidation

Consolidate the memory matrix: promote mature entries upward, archive stale ones, and clean compact snapshots.

**Arguments**: $ARGUMENTS (default: `--quick all`)

## Modes

| Mode | Scope | Cost | Use when |
|------|-------|------|----------|
| `--quick` | Targeted project or all (single pass) | ~10 min | After an orch wave, end of day |
| `--complete` | Cell-by-cell with checkpointing | ~25 min | Monthly deep clean, project milestone |

**Targeting**: `/lt-mem project <project>` processes only <PROJECT> cells across all 3 rows. `/lt-mem all` scans everything.

## Procedure

### Step 1: Scan

Collect all populated cells in the memory matrix:

```bash
MEM="$HOME/.claude/agent-memory"
echo "=== Row 1: Shared ==="
wc -l "$MEM"/shared/global/ltm.md "$MEM"/shared/projects/*.md 2>/dev/null
echo "=== Row 2: Class ==="
wc -l "$MEM"/class/*/mtm.md 2>/dev/null
echo "=== Row 3: Instance ==="
wc -l "$MEM"/instance/*/MEMORY.md 2>/dev/null
```

For each cell, classify every entry:

| Entry state | Criteria | Action |
|-------------|----------|--------|
| **Promotable** | Has `[PROMOTE]` tag, OR Occ>=3, OR same pattern in 2+ projects | Promote upward |
| **Stale** | References deleted files/orchs, completed work, resolved gotchas | Archive |
| **Active** | Currently actionable | Keep in place |

### Step 2: Promote

Promotion hierarchy: `project -> class -> global`. An entry promotes ONE level per /lt-mem run.

For each promotable entry:

1. **Project -> Class**: Entry in `shared/projects/<project>.md` seen in 2+ projects -> write to `class/<class>/mtm.md`. Mark source with `[PROMOTED->class]`, remove after confirmation.
2. **Class -> Global**: Entry in `class/<class>/mtm.md` with `[PROMOTE]` tag or seen in 2+ classes -> write to `shared/global/ltm.md`. Mark source with `[PROMOTED->global]`, remove after confirmation.
3. **Occ>=3 any tier**: Auto-promote to `rules/20-tool-conventions.md` as a universal rule. Mark source as `[PROMOTED->rules]`.

Promotion format for global LTM:
```markdown
| CW-<N> | <Pattern> | <One-liner from source> | <Source projects/classes> |
```

Promotion format for rules:
```markdown
## <Pattern Title>

- <Concise rule>
- Source: <which project/class promoted this, occurrence count>
```

**Important**: Never delete source entries in the same step as promotion. Write the promoted entry, mark the source, present to the user for confirmation. Delete sources only after the user approves (or if `--auto` flag is passed for non-destructive consolidations).

### Step 3: Archive

For each stale entry:
1. Create tombstone in cell's `archive/` subdir: `archive/<cell-name>-<date>.md`
2. Remove from active cell
3. Log in report

Stale criteria:
- References files that don't exist on disk (broken refs)
- Gotcha for a resolved issue (project has moved past it)
- Mistake with Occ=1, older than 60 days, and no related active work
- Orch instance memory for a decommissioned orch (move to `_system/_archive/`)

### Step 4: Clean Compact Snapshots

Primary method (session-matching):

```bash
SNAP_DIR="$HOME/.claude/agent-memory/_system/_compact-snapshots"
TIMER_DIR="$HOME/.claude/session-timers"

for snap in "$SNAP_DIR"/*.md; do
  [ -f "$snap" ] || continue
  SNAP_SESSION=$(basename "$snap" .md | sed 's/compact-[0-9]*-[0-9]*-//')
  ACTIVE=false
  for sf in "$TIMER_DIR"/*.start; do
    [ -f "$sf" ] || continue
    SID=$(basename "$sf" .start)
    echo "$SID" | grep -q "^${SNAP_SESSION}" && ACTIVE=true && break
  done
  if [ "$ACTIVE" = false ]; then
    rm "$snap"
    echo "Cleaned: $(basename "$snap")"
  fi
done
```

Fallback (date-based): delete all snapshots from the current day when no sessions are active:
```bash
find "$SNAP_DIR" -name "*.md" -mtime 0 -delete 2>/dev/null
```

### Step 5: Report

```markdown
## /lt-mem Report

**Mode**: --quick | --complete
**Scope**: <project name | all>
**Cells scanned**: N

### Promotions
| # | Entry | From | To | Status |
|---|-------|------|-----|--------|
| 1 | <pattern> | shared/projects/X.md | class/orch/mtm.md | PROMOTED |

### Archives
| # | Entry | Cell | Reason |
|---|-------|------|--------|
| 1 | <pattern> | shared/projects/X.md | broken ref |

### Snapshot Cleanup
- Deleted: N snapshots (M KB freed)
- Retained: N (active sessions)

### Cell Health Post-Consolidation
| Cell | Before | After | Budget | Status |
|------|--------|-------|--------|--------|
```

### Step 6: Checkpoint (--complete mode only)

Write progress to `~/.claude/agent-memory/_system/lt-mem-checkpoint.md`:

```markdown
# LT-Mem Checkpoint
- Started: <timestamp>
- Mode: --complete
- Scope: <target>
- Cells processed: [list]
- Cells remaining: [list]
- Promotions: N
- Archives: N
- Snapshots cleaned: N
```

Resume from checkpoint on next `--complete` invocation. Delete checkpoint when all cells done.

### Step 7: Chain into `/compact-mem` (MANDATORY unless `--skip-compact`)

`/lt-mem` and `/compact-mem` are designed as a pair. `/lt-mem` promotes/archives; `/compact-mem` re-budgets the resulting cells. Running `/lt-mem` alone leaves cells that may be over-budget after promotions have been merged in.

**Default behaviour**: after Step 5 (report) and Step 6 (checkpoint), execute `/compact-mem <same scope>` automatically. Use the same scope flags: if the user invoked `/lt-mem --complete project example-project`, chain into `/compact-mem project example-project`. If the user invoked `/lt-mem all`, chain into `/compact-mem all`.

**Skip conditions** (DO NOT chain if ANY applies):
- User's invocation included `--skip-compact`
- User explicitly asked to skip compaction in the prompt
- The Step 5 Cell Health table reports ALL cells within budget (no compaction needed)

**Report format when chained**: append a "Compaction follow-up" section to the /lt-mem report, then invoke `/compact-mem` and merge its report at the end.

## Constraints

- Destructive ops (delete source after promotion, archive cleanup >90 days) require the user confirmation unless `--auto` flag
- Process at most 5 cells per session in --complete mode
- Never create new memory directories (archive/ subdirs are OK via `mkdir -p`)
- Never delete source entries before promoted copy is confirmed written
- ALWAYS chain into `/compact-mem` after Step 6 unless a skip condition applies (see Step 7)
