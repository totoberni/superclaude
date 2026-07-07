---
name: mem-health
description: "Score memory DB health /100. 6 DB-aware criteria + v3 trigger checks."
category: memory
user-invocable: true
argument-hint: "[--quick | --verbose]"
allowed-tools: Read, Bash, Glob, Grep
---

# Memory Health Assessment (/100)

Read-only assessment of the v3 memory DB at `~/.claude/agent-memory/.memory.db`
(table `memories` + FTS5 + vec0 shadows). Never modifies anything.

**v3 (DB-aware)**: memory is no longer a tree of `MEMORY.md`/`ltm.md`/`mtm.md` files
with per-file LINE budgets — it is a hybrid-search SQLite store. There is no line-count
to measure, so each of the 6 criteria is sourced from a DB-health signal (see the
SEMANTIC SUBSTITUTION column). The 100-pt weighting and the `SCORE: <int>/100` contract
are unchanged.

**Mode**: $ARGUMENTS (default: normal. `--quick` skips the near-dup scan. `--verbose` appends the per-tier signal table)

## Scoring

| # | Criterion | Pts | Measurement | SEMANTIC SUBSTITUTION (old MD criterion) |
|---|-----------|-----|-------------|------------------------------------------|
| 1 | Right-sized rows | 20 | `(rows - oversized) / rows * 20`; oversized = `LENGTH(text) > OVERSIZE_BYTES` **AND not budget-exempt** (denominator stays all rows) | was "line budgets" (per-file LOC vs LINE cap) |
| 2 | FTS index cohesion | 20 | full 20 iff `memories == memories_fts_docsize`, else proportional | was "no broken refs" (MD paths resolve on disk) |
| 3 | No near-dup rows | 20 | `(rows - redundant) / rows * 20`; near-dup = token-Jaccard `>= DUP_SIM_MIN` between two bodies (skip if `--quick`, award 20) | was "no cross-cell dupes" (bullet/row text across files) |
| 4 | vec store soundness | 15 | full 15 iff `memories_vec` present (vec0 virtual table), else 0 | was "load-order paths valid" (agent-def paths) |
| 5 | Metadata complete | 15 | `(rows - malformed) / rows * 15`; malformed = empty `name`/`description`/`type` | was "entry formatting" (table-rows/bullets present) |
| 6 | Staleness managed | 10 | `(rows - stale) / rows * 10`; stale = `updated < now-STALE_DAYS`. **N/A (excluded from score) when the corpus edit-span is below STALE_DAYS** — see below | was "archives manageable" (archive/ dir sizes) |

### Budget-exempt marker (criterion 1)

A row opts OUT of the oversize penalty iff it carries a **line-anchored** `<!-- budget-exempt` marker — the marker must BEGIN a line (start of the body, or immediately after a newline). A loose mid-prose mention (e.g. a row that merely *describes* the marker) does NOT exempt it. The denominator stays `COUNT(*) FROM memories` (exempt rows are still part of the corpus); only the oversized-numerator and the v3 oversized trigger skip them. The exact SQL predicate the runner uses:

```sql
(text LIKE '<!-- budget-exempt%' OR text LIKE '%' || char(10) || '<!-- budget-exempt%')
```

The per-criterion detail surfaces how many rows were skipped (`; exempt: N`).

### N/A renormalization (criterion 6)

The final score is **renormalized over only the criteria that were actually scored**. The runner accumulates earned points alongside a `MAXPTS` total (each scored criterion adds its max; an N/A criterion adds neither), then `SCORE = round(earned / MAXPTS * 100)`. When all 6 criteria apply, `MAXPTS == 100` and this is identical to a straight sum.

Criterion 6 goes **N/A** when `(julianday(MAX(updated)) - julianday(MIN(updated))) < STALE_DAYS`: a corpus whose entire edit-history is shorter than the staleness window structurally cannot contain a stale row, so awarding the full 10 would be a false free pass (the classic uniform-age freshly-migrated case). When N/A, the 10 pts are excluded from BOTH numerator and denominator (the remaining criteria carry honest weight — the score correctly stops being inflated), the row prints `| 6 | Staleness managed | N/A | edit-span <X>d < <STALE_DAYS>d — unmeasurable until timestamps diverge |`, and the v3 staleness trigger is suppressed. Force the scored branch for testing with `MEM_STALE_DAYS=0` (threshold 0 ⇒ span is never below it ⇒ criterion 6 scores numerically again).

## DB-health thresholds (replace MD line budgets)

These are the tunable constants behind the criteria above (env-overridable; kept in sync
with `scan-mem-matrix.sh`). They are the DB analogue of the old per-tier LINE budgets.

| Constant | Default | Env override | Meaning |
|----------|---------|--------------|---------|
| `OVERSIZE_BYTES` | 8000 | `MEM_OVERSIZE_BYTES` | a single memory body bigger than this is "fat" (consolidation candidate) — the DB analogue of a per-file LOC overage |
| `STALE_DAYS` | 60 | `MEM_STALE_DAYS` | `updated` older than this = staleness candidate |
| `DUP_SIM_MIN` | 0.82 | `MEM_DUP_SIM_MIN` | token-Jaccard at/above which two bodies count as near-duplicate |
| `CORPUS_BYTES_MAX` | 1500000 | `MEM_CORPUS_BYTES_MAX` | total corpus bytes above which the consolidation trigger fires |

> CAVEAT (criterion 6 / staleness): right after a bulk migration every row shares one
> `updated` timestamp, so the stale count would read 0 (a false perfect 10/10) even for
> genuinely old content. The runner handles this structurally: when the corpus edit-span
> is below `STALE_DAYS`, criterion 6 is reported **N/A** and excluded from the renormalized
> score (see "N/A renormalization" above) rather than awarded a free 10. Once organic
> writes spread the `updated` timestamps past `STALE_DAYS`, the criterion becomes scorable.
> The `memory_db.py stats` row counts are the ground truth to sanity-check against.

## Procedure

## Implementation (canonical runner)

`bash ~/.claude/scripts/mem-health.sh $ARGUMENTS` is the authoritative deterministic
implementation of all 6 criteria below (it calls `scan-mem-matrix.sh` for Step 0 and queries
the DB directly for every scored signal — no MD files read). It supports `--quick` (skips the
near-dup scan, awards criterion 3 full) and `--verbose` (appends the per-tier signal table),
prints a per-criterion breakdown, prints the v3-trigger lines informationally (they do NOT
change the 6-criterion /100), and emits a final `SCORE: <int>/100` line. Run it and present
its output. The criteria and threshold tables above document what the script implements.

Fast path: bash + sqlite3 CLI only — NO python, NO embedding model — so it stays fail-safe
under `set -uo pipefail` and degrades (not crashes) when sqlite3 or the DB is absent.

**Memory storage (v3)**: all memory is DB-resident at `~/.claude/agent-memory/.memory.db`.
Use `memory_db.py stats` to confirm DB population before interpreting health scores — a low
score may indicate the DB needs an init/sync rather than that memories are genuinely missing.
`stats` is also the authoritative `memories == fts == vec` triple check (it loads `sqlite_vec`,
which plain sqlite3 cannot):

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py stats
```

### Step 0: Collect (DB signals)
```bash
bash ~/.claude/scripts/scan-mem-matrix.sh --budgets
```
Emits per-tier rows/bytes/biggest/fat/stale + an FTS-cohesion + vec-presence integrity line.
This replaces the old per-file LOC scan; the denominator for ratio criteria is `COUNT(*) FROM memories`.

### Step 1: Right-sized rows (20 pts)
`(rows - oversized) / rows * 20`, where oversized = `LENGTH(text) > OVERSIZE_BYTES` AND the row is
NOT budget-exempt (line-anchored `<!-- budget-exempt` marker — see "Budget-exempt marker" above).
DB analogue of the old per-file LINE budget: a single fat body is the consolidation signal the LOC
cap used to give. The denominator stays all rows; exempt rows are reported via `; exempt: N`.

### Step 2: FTS index cohesion (20 pts)
Full 20 iff `COUNT(*) FROM memories == COUNT(*) FROM memories_fts_docsize`, else proportional.
DB analogue of "no broken refs": every content row must have exactly one indexed doc.
`memories_fts` is external-content (`content='memories'`) so its `COUNT(*)` is vacuous —
`memories_fts_docsize` is the falsifiable check (mirrors `super-health.sh`).

### Step 3: No near-duplicate rows (20 pts)
Skip if `--quick` (award 20). Near-dup = two bodies whose lowercased word-set token-Jaccard is
`>= DUP_SIM_MIN`. Each near-dup PAIR implicates ~1 redundant row; `(rows - redundant) / rows * 20`.
DB analogue of "no cross-cell dupes" — surfaces consolidation candidates for `/lt-mem`.

> IDs (M-1, W-2, etc.) are project-scoped; the heuristic compares whole-body token sets, not IDs,
> so same-numbered IDs in different rows do NOT false-trigger.

### Step 4: vec store soundness (15 pts)
Full 15 iff `memories_vec` exists in `sqlite_master`, else 0. It is a vec0 VIRTUAL table — plain
sqlite3 cannot `COUNT(*)` it, so PRESENCE is the fail-safe check. DB analogue of "load-order paths
valid": the vector-search plumbing is wired. For the actual row count, use `memory_db.py stats`.

### Step 5: Metadata complete (15 pts)
`(rows - malformed) / rows * 15`, where malformed = empty/NULL `name`, `description`, or `type`.
DB analogue of "entry formatting": a row missing the metadata that hybrid search + `list` rely on
is the equivalent of a malformed MD cell.

### Step 6: Staleness managed (10 pts, or N/A)
`(rows - stale) / rows * 10`, where stale = `updated < datetime('now','-STALE_DAYS days')`. DB
analogue of "archives manageable": old, possibly low-value rows should be consolidated/pruned, not
pile up. **N/A and excluded from the renormalized score when the corpus edit-span
`MAX(updated) - MIN(updated)` is below `STALE_DAYS`** (see "N/A renormalization" above) — a uniform-age
corpus cannot have stale rows, so it earns neither the points nor a free pass. The v3 staleness
trigger is suppressed while N/A.

## Output
```
## Memory Health Report (mode: <mode>)
| # | Criterion | Score | Detail |
| 1 | Right-sized rows   | NN/20 | ...; exempt: N |
| 2 | FTS index cohesion | NN/20 | ... |
| 3 | No near-dup rows   | NN/20 | ... |
| 4 | vec store soundness| NN/15 | ... |
| 5 | Metadata complete  | NN/15 | ... |
| 6 | Staleness managed  | NN/10 OR N/A | ... |
### v3 Triggers (informational — not scored)
Scored NN/MM pts (renormalized to /100[; KK pts N/A excluded])
SCORE: NN/100
```
The penultimate line surfaces the renormalization basis (earned over scorable MAXPTS); when a
criterion is N/A the excluded points are named so the dropped denominator is never silent.
`--verbose` additionally appends the per-tier signal table from `scan-mem-matrix.sh`.

## v3 Triggers (informational — appended by the runner if any fire)

Re-expressed against DB signals (parallel to the old MD-corpus triggers):

| Trigger | Threshold | Action |
|---------|-----------|--------|
| Corpus too large | total `SUM(LENGTH(text))` > `CORPUS_BYTES_MAX` (default 1.5 MB) | Run `/lt-mem --quick all` |
| Oversized rows present | any **non-exempt** row `LENGTH(text) > OVERSIZE_BYTES` (budget-exempt rows excluded) | `/lt-mem --compact` to consolidate |
| FTS index desync | `memories != memories_fts_docsize` | Rebuild via `memory_db.py init` |
| Stale rows accumulating | stale fraction > 10% (**suppressed when criterion 6 is N/A**) | `/lt-mem` archive pass |

If none fire: "v3 Triggers: None. DB within thresholds."
