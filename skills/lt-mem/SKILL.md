---
name: lt-mem
description: "Memory DB consolidation: re-tier mature entries, prune stale, merge near-dups; propagate to peer when replicated (Step 8)."
category: memory
user-invocable: true
disable-model-invocation: true
argument-hint: "[--quick | --complete] [--compact | --skip-compact] [--sanitize] [--dry-run] [project <name> | all]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

# /lt-mem -- Long-Term Memory Consolidation

Consolidate the v3 memory DB (`~/.claude/agent-memory/.memory.db`): re-tier mature entries
upward through the logical horizons, mark stale ones, consolidate oversized/near-duplicate
rows, sanitize misplaced/duplicate entries, and clean compact snapshots.

**v3 (DB-aware)**: memory is a hybrid-search SQLite store, not a tree of MD files. The
short/mid/long-term horizons are now **DB-expressed via the `tier` column**, not directories.
Promote/consolidate/archive are **DB operations** (`memory_db.py upsert`/`search`/`get`/`prune`),
not file moves. The one exception is the deliberate MD archive-tombstone and the
`--complete` checkpoint file — those are intentional MD-WRITES, preserved (see the
`MD-WRITE (preserved)` notes); only MD-READ-for-measurement logic was removed.

## Logical Horizons → DB tier mapping

The skill's short/mid/long-term horizons map onto DB tiers as follows. Promotion moves an
entry ONE horizon up per run (this is the existing `project → class → global` hierarchy,
now expressed as tier re-assignment via upsert):

| Horizon | DB tier(s) | Meaning | Promotes to |
|---------|-----------|---------|-------------|
| **short-term** | `instance` (agent-scoped, ordered `updated DESC` for "recent") | raw per-agent working memory; first landing spot | mid-term |
| **mid-term** | `class` + `shared-projects` | consolidated cross-session / cross-project knowledge that outlived being transient | long-term |
| **long-term** | `shared-global` (+ `rules/*.md` flat files for Occ≥3 universal rules) | canonical cross-project wins | (terminal) |

> Rationale: this preserves the skill's original promotion ladder while expressing each rung
> as a `tier` value the DB already uses. "Recent" within short-term = `ORDER BY updated DESC`
> (no separate recency store needed). A more granular mapping (e.g. splitting `class` and
> `shared-projects` into distinct horizons) was considered and rejected as over-engineering —
> both are "consolidated but not yet global", so they share the mid-term rung.

**Arguments**: $ARGUMENTS (default: `--quick all`)

## Modes & Flags

| Mode/Flag | Scope | Cost | Use when |
|-----------|-------|------|----------|
| `--quick` | Targeted project or all (single pass) | ~10 min | After an orch wave, end of day |
| `--complete` | Cell-by-cell with checkpointing | ~25 min | Monthly deep clean, project milestone |
| `--compact` | Run compaction procedure (formerly /compact-mem) | additive | When cells are over-budget |
| `--sanitize` | Run sanitization P0/P1 fixes (formerly /sanitize-mem) | additive | When refs/dupes are suspected |
| `--dry-run` | Show what WOULD be modified (renames, archives, deletes, compactions) without performing them | read-only | Preview a complete/quick run before committing |

**Targeting**: `/lt-mem project <project>` processes only project cells across all 3 rows. `/lt-mem all` scans everything.

**`--dry-run` output**: prints a unified plan listing every promotion (source row -> dest tier), every archive (row + reason), every delete (snapshot path), every consolidation (rows merged + before/after byte size), and every sanitization fix (P0/P1 only — see Sanitization section). NO DB writes and NO file writes occur. Use to preview before a destructive `--complete` run.

## Procedure

### Step 1: Scan

Collect per-tier DB health signals (rows, bytes, oversized "fat" rows, stale candidates, FTS/vec integrity):

```bash
bash ~/.claude/scripts/scan-mem-matrix.sh --budgets
```

Then enumerate rows per horizon for classification:

```bash
HF_HUB_OFFLINE=1 ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py list --tier instance         # short-term
HF_HUB_OFFLINE=1 ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py list --tier class             # mid-term
HF_HUB_OFFLINE=1 ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py list --tier shared-projects   # mid-term
HF_HUB_OFFLINE=1 ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py list --tier shared-global     # long-term
```

For each row, classify:

| Entry state | Criteria | Action |
|-------------|----------|--------|
| **Promotable** | Has `[PROMOTE]` tag in body, OR Occ>=3, OR same pattern in 2+ projects (find via `memory_db.py search`) | Re-tier upward (Step 2) |
| **Stale** | References deleted files/orchs, completed work, resolved gotchas, OR `updated` past `STALE_DAYS` with low signal | Archive (Step 3) |
| **Oversized / near-dup** | `LENGTH(text) > OVERSIZE_BYTES` (from the scan's `fat` count) OR flagged near-dup by `/mem-health` OR surfaced by hybrid `memory_db.py similar` (Step 7) | Consolidate (Step 7) |
| **Active** | Currently actionable | Keep in place |

### Step 2: Promote (re-tier one horizon up)

Horizon ladder: `instance (short) -> {class, shared-projects} (mid) -> shared-global (long)`.
An entry promotes ONE horizon per /lt-mem run. Promotion = re-assign the `tier` IN PLACE via
the `retier` subcommand (a single in-place UPDATE of tier+path; the row, its FTS shadow, and
its precomputed embedding are all preserved — no dup, no re-embed, no delete).

> CRITICAL — use `retier`, not `upsert`, to promote: `memory_db.py retier (--name X | --id N)
> --tier <TARGET>` re-tiers the row IN PLACE. It rewrites the path to `<TARGET>/<tail>` for you
> (stripping the old leading tier segment), so there is NO `--path` to remember and NO way to
> accidentally fork a duplicate. This removes the old footgun entirely: the previous mechanism
> was `upsert --path <EXISTING-PATH> --tier <new>`, and OMITTING `--path` silently created a
> SECOND row instead of moving the existing one. `retier` has no such trap. Still pass the
> LITERAL stored tier string — `--tier shared-global` (not `--tier global`). `retier` guards
> against a no-op (errors if the row is already in TARGET) and REJECTS `--tier archive` (use the
> `archive` subcommand for that — archive needs an origin-encoded reversible path; see Step 3).
> Fetch the slug/description first via `memory_db.py get --name <slug>` (or `list`) if unsure.

For each promotable entry (mid-term tiers are `class` and `shared-projects`; long-term is `shared-global`):

1. **short -> mid** (`instance` -> `shared-projects`): an `instance` row whose content is project knowledge (not agent-operational) seen in 2+ contexts → `retier --name <slug> --tier shared-projects`. (Or `--tier class` if it's an agent-class pattern.) The body marker `[PROMOTED->mid]` is no longer needed for a re-tier (the single row simply moves; there is no source copy to deprecate).
2. **mid -> long** (`class`/`shared-projects` -> `shared-global`): a mid-tier row with a `[PROMOTE]` tag or seen across 2+ projects/classes → `retier --name <slug> --tier shared-global` (see format below).
3. **Occ>=3 any tier**: Auto-promote to `rules/20-tool-conventions.md` (Edit tool) as a universal rule (this remains a flat-file rule, by design). Mark the source body as `[PROMOTED->rules]`.

Promotion format for long-term (`shared-global`) — the PRIMARY mechanism is the in-place `retier`
(no dup, no re-embed). Search first to confirm you are promoting the right row, then re-tier it:

```bash
# Search to confirm the row to promote (and detect any sibling near-dup first)
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py \
  search '<pattern summary>' -k 3

# Re-tier IN PLACE — one UPDATE, no --path needed, no duplicate possible.
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py \
  retier --name <slug-kebab-case> --tier shared-global
```

> When promotion is genuinely a RESHAPE (you are rewriting the body, not merely moving it), use
> `upsert` to write the reshaped content and `prune` the source after confirmation — see the
> note below. For a pure horizon move, prefer `retier`. (`retier` does not change `type`; if the
> promoted row needs a different `--type`, follow the `retier` with an in-place `upsert` reusing
> the NEW path it printed.)

Promotion format for rules:
```markdown
## <Pattern Title>

- <Concise rule>
- Source: <which project/class promoted this, occurrence count>
```

**Important**: Never `prune` a source row in the same step as promotion. When promoting by
copying to a NEW slug (rather than re-tiering in place), write the promoted entry, mark the
source body, present to the user for confirmation, and `prune --name <source>` only after the
user approves (or if `--auto` is passed for non-destructive consolidations). When re-tiering
IN PLACE via `retier`, there is no separate source to delete — the single row simply moves
horizons (and no `--path` is involved, so a duplicate cannot occur).

### Step 3: Archive (re-tier, NOT prune)

There is no `archive/` subdir in a DB — "archive" means the stale ROW leaves the active
recall horizon while staying queryable and keeping its embedding.

**PRIMARY mechanism — DB re-tier (`archive` subcommand)**: re-tier the row to `tier='archive'`
IN PLACE. The row, its text, FTS shadow, and its precomputed vector are all preserved (no dup,
no re-embed, no delete) — it simply leaves the default search/recall set. `search` and
`list` exclude `tier='archive'` unless `--all` or `--tier archive` is given, and a `--unarchive`
restores it losslessly. This is the default: stale ≠ gone; a hard-won lesson stays recoverable
AND semantically searchable.

```bash
# Re-tier to archive (preserves row + embedding; default search no longer surfaces it)
HF_HUB_OFFLINE=1 ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py archive --name <slug>
# Reverse if it turns out to still matter:
HF_HUB_OFFLINE=1 ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py archive --name <slug> --unarchive
# Confirm it left the active horizon but is still queryable:
HF_HUB_OFFLINE=1 ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py search '<its topic>' -k 5            # absent
HF_HUB_OFFLINE=1 ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py search '<its topic>' --tier archive -k 5  # present
```

**SECONDARY — MD tombstone (preserved, optional)**: optionally also write a tombstone capturing
the row's body to `~/.claude/agent-memory/_system/_archive/<slug>-<date>.md`, as a durable
off-DB record. This is no longer required for recoverability (the re-tiered row stays in the DB),
but remains available for a true paper-trail of a removed lesson and is preserved by design.

**Hard prune (`prune`) — last resort only**: `prune` permanently deletes the row + its FTS/vec
shadows. Reserve it for genuine noise (e.g. an accidental/empty row), and only after user
confirmation per Constraints. For ordinary staleness, prefer `archive` so the vector survives.

Find stale rows via the scan's `stale` count + targeted listing:
```bash
# rows past the staleness horizon (updated older than STALE_DAYS)
sqlite3 ~/.claude/agent-memory/.memory.db \
  "SELECT name, tier, updated FROM memories WHERE updated < datetime('now','-60 days') ORDER BY updated;"
```

Stale criteria:
- References files that don't exist on disk (broken refs — verify with `test -e` on paths in the body)
- Gotcha for a resolved issue (project has moved past it)
- Mistake with Occ=1, older than 60 days (`STALE_DAYS`), and no related active work
- Orch instance memory for a decommissioned orch (`instance` row whose agent is a dead orch)

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

**Mode**: --quick | --complete (+ --compact | --sanitize | --dry-run flags)
**Scope**: <project name | all>
**Rows scanned**: N (per `memory_db.py stats`)

### Promotions (re-tiered)
| # | Entry | From horizon (tier) | To horizon (tier) | Status |
|---|-------|---------------------|-------------------|--------|
| 1 | <slug> | mid (shared-projects) | long (shared-global) | PROMOTED |

### Archives (re-tiered to `archive`)
| # | Entry | From tier | Reason | Mechanism |
|---|-------|-----------|--------|-----------|
| 1 | <slug> | shared-projects | broken ref | archive (re-tier; +optional tombstone) |

### Snapshot Cleanup
- Deleted: N snapshots (M KB freed)
- Retained: N (active sessions)

### DB Health Post-Consolidation
| Signal | Before | After |
|--------|--------|-------|
| total rows | N | N |
| oversized (fat) rows | N | N |
| stale rows | N | N |
| FTS/vec integrity | OK/DESYNC | OK/DESYNC |
```
(Capture Before/After by running `bash ~/.claude/scripts/scan-mem-matrix.sh --budgets` at the start and end.)

### Step 6: Checkpoint (--complete mode only)

**MD-WRITE (preserved — flagged for owner review)**: write progress to
`~/.claude/agent-memory/_system/lt-mem-checkpoint.md`. This is a deliberate resumable-state
file (an off-DB checkpoint that survives a session crash), not measurement logic, so it is
intentionally retained.
> NOTE TO OWNER: this checkpoint is an MD file by design. If you prefer the checkpoint live in
> the DB too, it could be an `instance`-tier row (e.g. `--name lt-mem-checkpoint --agent meta`).
> Left as MD for now — see OPEN QUESTIONS.

```markdown
# LT-Mem Checkpoint
- Started: <timestamp>
- Mode: --complete
- Scope: <target>
- Tiers processed: [list]
- Tiers remaining: [list]
- Promotions: N
- Archives: N
- Snapshots cleaned: N
```

Resume from checkpoint on next `--complete` invocation. Delete checkpoint when all horizons done.

### Step 7: Consolidation Pass (default unless `--skip-compact`)

After promote/archive completes, run the Consolidation Procedure below on the rows that
received promoted content (and any row still flagged oversized/near-dup per Step 1). Use the
same scope flags as the parent invocation.

**Skip conditions** (DO NOT consolidate if ANY applies):
- User's invocation included `--skip-compact`
- User explicitly asked to skip consolidation in the prompt
- The Step 5 DB Health table reports zero oversized + zero near-dup rows (nothing to consolidate)

## Consolidation Procedure (formerly /compact-mem)

Compress oversized row bodies and merge near-duplicate rows. Run via `--compact` flag or as
Step 7 of a normal /lt-mem run.

### Row size targets (replace per-file LINE budgets)

There is no per-file LINE budget in a DB. The unit is the `text` body of a row, measured in
BYTES (`LENGTH(text)`). A row above its tier's target is a candidate to compress (trim prose,
drop dead timestamps) or to split/merge. These targets are the DB analogue of the old per-tier
LINE budgets — converted at a rough ~60 bytes/line so the same intent (density per horizon)
carries over. They are advisory consolidation triggers, not hard gates.

| Horizon | DB tier | Byte target (≈ old LINE budget) |
|---------|---------|--------------------------------|
| long | `shared-global` (agent NULL) | ~3600 B (≈60 lines) |
| mid | `shared-projects` (agent NULL) | ~3600 B (≈60 lines) |
| mid | `class` MTM (agent = class name) | ~2400 B (≈40 lines) |
| mid | `class` project-scoped (agent = class) | ~1800 B (≈30 lines) |
| short | `instance` meta (agent = meta) | ~4800 B (≈80 lines) |
| short | `instance` orch (agent = orch/<name>) | ~2400 B (≈40 lines) |
| short | `instance` other (agent = <name>) | ~1800 B (≈30 lines) |

> These per-tier byte targets are softer than the single global `OVERSIZE_BYTES` (8000 B) that
> `/mem-health` scores on. `OVERSIZE_BYTES` flags only egregiously fat rows (a hard health
> signal); these per-tier targets are the finer-grained consolidation hints lt-mem uses to keep
> each horizon dense. Both are advisory — neither blocks a write.

### C1. Measure
Use the scan from Step 1 (per-tier rows/bytes/biggest/fat). For ad-hoc invocation:
```bash
bash ~/.claude/scripts/scan-mem-matrix.sh --budgets
# Find the specific oversized rows to target (largest first):
sqlite3 ~/.claude/agent-memory/.memory.db \
  "SELECT name, tier, LENGTH(text) AS bytes FROM memories ORDER BY bytes DESC LIMIT 20;"
```

**Near-duplicate detection (hybrid `similar`)** — for each row that looks like a merge
candidate (a fat row, or one whose topic recurs), ask the DB for its nearest neighbours by the
hybrid metric (semantic cosine + lexical Jaccard). High `cos` AND high `jac` ⇒ likely duplicate;
high `cos` with low `jac` ⇒ same topic in different words (a relation pure FTS misses):
```bash
HF_HUB_OFFLINE=1 ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py \
  similar --name <slug> -k 8
```
This reuses the row's precomputed embedding (no re-embed). The `/mem-similar` skill wraps the
same call. **PROPOSE every merge for human review — NEVER auto-merge** (see C2).

### C2. Compress / merge (in order of aggressiveness)

Edit the row body, then re-`upsert` IN PLACE (same `--path`) so the change re-embeds:

| Technique | When | Transform |
|-----------|------|-----------|
| Prose to bullet | Paragraphs in a body | "When X because Y, do Z" -> `- X -> Y. Fix: Z` |
| Merge near-dup rows | 2+ rows same topic (from `/mem-health`, or hybrid `memory_db.py similar` per C1) | PROPOSE the merge for human review (never auto-merge); once approved, keep the richer row, fold in the unique facts, then `archive` the other(s) — `prune` only if confirmed noise |
| Table to inline | <3-row table in a body | Single-row table -> `Key: value` |
| Promote to ref | Detail duplicated in another row | Full text -> `See: [[other-slug]]` |
| Drop timestamps | Static items | Remove dates that add no value |

To rewrite a row's body in place (preserves its tier/path, re-embeds the new text):
```bash
HF_HUB_OFFLINE=1 ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py \
  upsert --tier <existing-tier> --type <existing-type> \
  --name <existing-slug> --description "<existing-desc>" \
  --path <existing-path> --text-file <compressed-body.md>
```

### C3. Keep/Cut Rules

**KEEP**: gotchas/fix recipes, canonical paths, active orch state, DEC-NNN, wins/mistakes content.

**CUT**: "Updated: [date]" on static items, verbose prose (one-liner suffices), completed TODOs, session-specific commit hashes.

**ARCHIVE stale rows**: per Step 3 — re-tier to `tier='archive'` via `memory_db.py archive --name <slug>` (preserves the row + its embedding, leaves the active horizon). `prune` only as a last resort for genuine noise.

### C4. Verify

1. `bash ~/.claude/scripts/scan-mem-matrix.sh --budgets` — fat count dropped, FTS/vec still OK
2. `memory_db.py stats` — `memories == fts == vec` (re-embedding kept the shadows aligned)
3. Spot-check a compressed row via `memory_db.py get --name <slug>` — every gotcha still has its fix recipe; `[[links]]` resolve to real slugs

### C5. Report (consolidation summary)

```
| Signal | Before | After |
|--------|--------|-------|
| total bytes | N | N |
| fat rows (>OVERSIZE_BYTES) | N | N |
| near-dup pairs | N | N |
Rows merged: [list of pruned slugs -> kept slug]
Integrity: memories==fts==vec [OK | DESYNC]
```

**Principle**: Density over length. One lesson per line beats a paragraph. Future agents need facts, not narratives.

### C6. Structural DB compaction (FTS + vec0 + VACUUM)

Row-level ops (compress/archive/prune) shrink content but NOT the file. Two structural bloat
sources accrue independently: (1) **FTS5 fragmentation** — every upsert/re-tier is an FTS
delete+insert, creating a new index segment; (2) **vec0 logical deletion** — `sqlite-vec`
zeroes slots rather than freeing chunk space, and its default `chunk_size=1024` over-allocates
for small corpora. Plain `VACUUM` reclaims SQLite free pages but cannot repack vec0 chunk BLOBs.

**One command fixes both**:
```bash
HF_HUB_OFFLINE=1 ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py compact
```
Sequence: FTS5 `optimize` → vec0 rebuild (DROP + recreate at `chunk_size=256`, re-inserting
every vector at `rowid=memories.id`) → `VACUUM` → self-verify (integrity_check=ok, row counts
unchanged, zero orphan memories, KNN self-match byte-identical). Rolls back the vec rebuild on
any error.

**Safety discipline**: structural compaction is more invasive than row ops. Always back up first
(`cp ~/.claude/agent-memory/.memory.db <backup>`) and prefer testing on the copy
(`memory_db.py compact --db <copy>`) before running live — especially after large prune waves.
The command is transactional + self-verifying, but a backup is the cardinal-rule safety net.

**When**: after a heavy consolidation round (many prunes/compressions/re-tiers), not on every
quick run. Measured impact: 4.1 MB → 1.83 MB (~56% reduction) with zero search-quality loss.

**Peer note**: compaction is file-level and does NOT propagate via Step 8's row-level sync. Run
it on the peer separately after Step 8 completes:
```bash
ssh <peer> 'HF_HUB_OFFLINE=1 ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py compact'
```

**Optional ongoing hygiene**: `PRAGMA auto_vacuum=INCREMENTAL; VACUUM;` once, then
`PRAGMA incremental_vacuum;` cheaply reclaims free pages between full compactions.

### Step 8: Propagate to peer (replicated 2-DB setup)

A `/lt-mem` consolidation writes only to the local machine's DB. On a replicated 2-machine setup
(e.g. WSL + a peer host), the consolidating machine is the source of truth; the peer must inherit all
compactions, re-tiers, archives, and prunes — else a later peer-side sync can resurrect
stale/fat copies.

Run immediately after the consolidation pass:

```bash
HF_HUB_OFFLINE=1 ~/.claude/.venv/bin/python ~/.claude/scripts/memory/claude_mem_sync.py \
  --peer-host <peer> --auto newer
```

Use `--auto newer`, NOT `--auto safe`: the tidied rows are newer post-consolidation, so `newer`
lets them win cleanly; `--auto safe` keeps-both and would re-add `-conflict-<peer>` copies of
every compressed/archived row, undoing the tidy. Unless the owner specifies a different SOT or
direction (e.g. the peer initiated the consolidation), the consolidating machine always pushes
as source of truth.

> Sync mechanics — SSH transport, conflict resolution, row merging — live in
> `scripts/memory/claude_mem_sync.py` and `scripts/cockpit/`; do not duplicate them here.

## Sanitization (P0/P1 — formerly /sanitize-mem)

Audit memory files for misplaced learnings (P0) and cross-file duplicates (P1). Run via `--sanitize` flag. Lower priorities (P2 stale refs, P3 session cruft, P4 orphans) are now manual — handle as needed during normal /lt-mem archiving.

### S1. Inventory (read-only)

Query the DB to enumerate all entries in scope. Build a mental map of what each tier contains.

```bash
# List entries by tier (repeat for each tier needed)
HF_HUB_OFFLINE=1 ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py list --tier instance
HF_HUB_OFFLINE=1 ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py list --tier shared-projects
HF_HUB_OFFLINE=1 ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py list --tier shared-global
HF_HUB_OFFLINE=1 ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py list --tier class

# Or search across tiers for specific content
HF_HUB_OFFLINE=1 ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py search "<topic>" -k 8
```

Fetch full body of any entry via `memory_db.py get --name <slug>` (or `get --id <n>`).

### S2. Detect P0/P1 Issues

| Priority | Issue | Detection | Fix |
|----------|-------|-----------|-----|
| **P0** | **Wrong tier** — project learnings in agent-specific memory | Content about a project's codebase/patterns in a row at tier=`instance` (agent=<agent>) instead of tier=`shared-projects` | Re-tier in place to `shared-projects` via `retier --name <slug> --tier shared-projects` (no `--path`, no dup) |
| **P1** | **Duplicates** — same learning in 2+ rows | Same concept in different rows (use the `/mem-health` near-dup flag or `memory_db.py search`) | Keep the richer row, `prune` the others (or reduce them to a `[[link]]`) |

(P2 stale refs, P3 session cruft, P4 orphans are no longer auto-handled — surface them in the /lt-mem report's "manual review" appendix.)

### S3. Canonical Location Rules

| Content Type | Canonical DB tier/agent | Other entries should |
|-------------|------------------------|---------------------|
| Project-specific gotchas/patterns | `shared-projects` (agent NULL) | Reference, not duplicate |
| Agent operational protocol | `instance` (agent = <agent>) | Not duplicate rules/ content |
| Cross-project wins | `shared-global` (agent NULL) | Reference, not duplicate |
| Tool patterns | `rules/20-tool-conventions.md` (flat file) | Reference, not duplicate |
| Test quality standards | `instance` (agent = orch, base orch) | Inherit from base orch |

### S4. Execute P0/P1 Fixes

For each issue found:
1. Fetch the source and target entries from the DB:
   ```bash
   HF_HUB_OFFLINE=1 ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py get --name <source-slug>
   HF_HUB_OFFLINE=1 ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py get --name <target-slug>
   ```
   (Use `search "<topic>"` first if the slug is not known.)
2. If moving content (P0): `retier --name <slug> --tier <canonical-tier>` — moves the single misplaced row in place (no `--path`, no dup, embedding preserved). There is no separate source to update; the one row simply changes tier.
3. If deduplicating (P1): keep the more detailed version, update the other entry to a short reference

### S5. Sanitization Report

Append to /lt-mem report:

```
## Sanitization Findings (P0/P1)

| # | Issue | Type | Row (slug/tier) | Action Taken |
|---|-------|------|-----------------|-------------|
| 1 | ... | P0/P1 | <slug> @ <tier> | Re-tiered/Pruned |

### Manual Review (P2-P4 — not auto-fixed)
| # | Issue | Type | Row (slug/tier) | Recommended Action |
|---|-------|------|-----------------|--------------------|
```

**Key Principle**: Retain lessons, remove noise. Every row body should help a future agent session.

## Constraints

- Stale rows are RE-TIERED via `archive` (preserves row + embedding), NOT pruned — `archive` is non-destructive and needs no confirmation; only hard `prune` (genuine noise) requires user confirmation unless `--auto`
- Near-dup merges are PROPOSED for human review (via hybrid `similar`); NEVER auto-merge rows
- Destructive ops (`prune` a source after a confirmed promotion/merge) require user confirmation unless `--auto` flag
- Process at most 5 rows per session in --complete mode
- Re-tier IN PLACE via `retier` (no `--path`, no possible dup) wherever possible — only create a new slug + `prune` the old when the content is genuinely being reshaped, not merely moved
- Never `prune` a source row before its promoted/re-tiered copy is confirmed written
- ALWAYS run the consolidation pass (Step 7) after promote/archive unless `--skip-compact` or zero oversized + zero near-dup rows remain
- The deliberate MD-WRITES — the `_system/_archive/` tombstone (Step 3) and the `--complete` checkpoint file (Step 6) — are preserved by design and flagged for owner review; do NOT remove them
- `--dry-run`: NO DB writes and NO file writes anywhere. Output the unified plan only.
- After any consolidation, sync to the peer DB via Step 8 — consolidating machine is SOT; use `--auto newer` (NOT `--auto safe`), unless the owner specifies otherwise
- Structural compaction (`memory_db.py compact`) is transactional + self-verifying with rollback; always back up the DB and test on a copy before running live after large prune waves; it does NOT propagate via Step 8 — run it on the peer separately
