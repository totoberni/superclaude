#!/bin/bash
# mem-health.sh — deterministic implementation of the /mem-health rubric.
# Canonical runner behind skills/mem-health/SKILL.md (6 weighted criteria, 100 pts).
#
# v3 REWRITE (DB-aware): memory is no longer a tree of MD files with per-file LINE
# budgets — it is the hybrid-search SQLite store at ~/.claude/agent-memory/.memory.db
# (table `memories` + FTS5 `memories_fts` + vec0 `memories_vec`, plus the FTS doc-index
# shadow `memories_fts_docsize`). There is NO line-count to measure, so each of the 6
# criteria is re-expressed against the closest DB-health signal. The 100-pt weighting
# and the "SCORE: <int>/100" final-line contract are UNCHANGED.
#
# SEMANTIC SUBSTITUTION MAP (old MD criterion -> new DB signal). Each is COMMENTED at
# its criterion block below:
#   1 Line budgets        -> Right-sized rows   : rows whose LENGTH(text) <= OVERSIZE_BYTES
#   2 No broken refs       -> FTS index cohesion : memories == memories_fts_docsize
#   3 No cross-cell dupes  -> No near-dup rows   : text-similarity heuristic across rows
#   4 Load-order paths     -> vec store soundness: memories_vec present (vec0 virtual table)
#   5 Entry formatting     -> Metadata complete  : non-empty name + description + type
#   6 Archives manageable  -> Staleness managed  : fraction of rows not past STALE_DAYS
#
# Usage:
#   bash mem-health.sh [--quick|--verbose]
#     --quick   : skip the near-duplicate scan (criterion 3 auto-awards full 20)
#     --verbose : print the per-tier signal table (from scan-mem-matrix.sh)
#
# Contract: prints a per-criterion breakdown, and as its FINAL stdout line,
# exactly: "SCORE: <int>/100".  Exit code is ALWAYS 0 (reporting tool, not a gate).
#
# The v3-trigger lines are INFORMATIONAL ONLY — printed, but they do NOT change the
# 6-criterion /100 score (matches the SKILL.md contract).
#
# Determinism: same DB content => identical score.
# Defensive: a missing DB / missing sqlite3 yields graceful handling for THAT check
# (noted), never a crash. vec0 `memories_vec` is VIRTUAL — plain sqlite3 cannot
# COUNT(*) it, so its PRESENCE is asserted via sqlite_master (mirrors super-health.sh).

set -uo pipefail   # NEVER set -e

CLAUDE="${CLAUDE_DIR:-$HOME/.claude}"
MEM="$CLAUDE/agent-memory"
MDB="${MEMORY_DB_PATH:-$MEM/.memory.db}"
SCAN="$CLAUDE/scripts/scan-mem-matrix.sh"

# ── Tunable DB-health thresholds (semantic replacements for MD line budgets) ──
# Keep in sync with scan-mem-matrix.sh; env overrides apply to both.
OVERSIZE_BYTES="${MEM_OVERSIZE_BYTES:-8000}"   # body bigger than this = "fat" row
STALE_DAYS="${MEM_STALE_DAYS:-60}"             # updated older than this = stale candidate
DUP_SIM_MIN="${MEM_DUP_SIM_MIN:-0.82}"         # cosine-ish token-overlap to call a near-dup

MODE="normal"
for arg in "$@"; do
  case "$arg" in
    --quick)   MODE="quick" ;;
    --verbose) MODE="verbose" ;;
  esac
done

pct_points() {
  local ok="$1" total="$2" max="$3"
  [ "$total" -gt 0 ] 2>/dev/null || { echo 0; return; }
  echo $(( ok * max / total ))
}

# sqlite3 helper — empty on any failure, never aborts.
q() { sqlite3 "$MDB" "$1" 2>/dev/null || true; }

# ── Budget-exempt marker predicate (Q3) ──────────────────────────────────────
# A row opts OUT of the oversize penalty (criterion 1 + Trigger B) iff it carries
# a LINE-ANCHORED `<!-- budget-exempt` marker: the marker must BEGIN a line — at
# the very start of the body OR immediately after a newline. A loose substring is
# NOT an exemption (prose that merely MENTIONS the marker, e.g. "use the
# `<!-- budget-exempt -->` marker", must still count as fat). Built once here so
# C1, the fat-list, and Trigger B share one definition (DRY).
#   char(10) = '\n'. The two LIKE branches cover (a) first-line and (b) after-\n.
# NOTE: the literal pattern is constructed below with char() codes ONLY inside
# the SQL string — never interpolated from shell — so it is immune to shell
# history-expansion mangling of '!'.
EXEMPT_PRED="(text LIKE '<!-- budget-exempt%' OR text LIKE '%' || char(10) || '<!-- budget-exempt%')"

# ── Renormalizing scorer (Q4) ────────────────────────────────────────────────
# SCORE accumulates earned points; MAXPTS accumulates the max of every criterion
# that was actually SCORED. A criterion that is N/A (unmeasurable on this corpus)
# adds to NEITHER — it is excluded from both numerator and denominator. The final
# score is renormalized: round(SCORE / MAXPTS * 100). When all 6 criteria apply
# MAXPTS == 100 and this is identical to the old straight sum. `add_score` is the
# single accrual point so the two accumulators can never drift.
SCORE=0
MAXPTS=0
add_score() {   # add_score <earned> <max>  — record a SCORED criterion
  SCORE=$((SCORE + $1))
  MAXPTS=$((MAXPTS + $2))
}
echo "## Memory Health Report (mode: $MODE)"
echo ""

# ── DB availability gate. If we cannot inspect the DB, every criterion scores 0
#    with a clear reason (mirrors the old "no memory dir" early-out). Exit 0.
DB_REASON=""
if ! command -v sqlite3 >/dev/null 2>&1; then
  DB_REASON="sqlite3 absent"
elif [ ! -f "$MDB" ]; then
  DB_REASON="memory DB not built ($MDB)"
elif ! sqlite3 "$MDB" "SELECT 1;" >/dev/null 2>&1; then
  DB_REASON="memory DB is not valid SQLite"
fi
if [ -n "$DB_REASON" ]; then
  echo "memory DB unavailable: $DB_REASON — all criteria score 0."
  echo ""
  echo "| # | Criterion | Score | Detail |"
  echo "|---|-----------|-------|--------|"
  echo "| 1 | Right-sized rows | 0/20 | $DB_REASON |"
  echo "| 2 | FTS index cohesion | 0/20 | $DB_REASON |"
  echo "| 3 | No near-dup rows | 0/20 | $DB_REASON |"
  echo "| 4 | vec store soundness | 0/15 | $DB_REASON |"
  echo "| 5 | Metadata complete | 0/15 | $DB_REASON |"
  echo "| 6 | Staleness managed | 0/10 | $DB_REASON |"
  echo ""
  echo "SCORE: 0/100"
  exit 0
fi

# Step 0: honor the SKILL.md "Collect" step — run scan-mem-matrix.sh (informational;
# we query the DB directly below for the scored signals).
if [ -x "$SCAN" ] || [ -f "$SCAN" ]; then
  bash "$SCAN" --budgets >/dev/null 2>&1 || true
fi

TOTAL_ROWS=$(q "SELECT COUNT(*) FROM memories;"); [[ "$TOTAL_ROWS" =~ ^[0-9]+$ ]] || TOTAL_ROWS=0

echo "| # | Criterion | Score | Detail |"
echo "|---|-----------|-------|--------|"

# ─────────────────────────────────────────────────────────────
# Criterion 1: Right-sized rows — 20 pts — (rows - oversized)/rows*20
#   SEMANTIC SUBSTITUTION: old "line budgets" measured per-file LOC against a LINE
#   cap. In a DB there is no file/line — the analogous "this cell is too fat,
#   consolidate it" signal is a single row whose body exceeds OVERSIZE_BYTES.
#   A right-sized corpus has few/no oversized bodies.
# ─────────────────────────────────────────────────────────────
# Q3: only NON-EXEMPT oversized rows are penalized. A row carrying a line-anchored
# `<!-- budget-exempt` marker is excluded from c1_over and from the fat list, but
# the denominator stays TOTAL_ROWS (exempt rows still count as part of the corpus).
# c1_exempt is surfaced so the skipped rows are visible, not silently dropped.
c1_over=$(q "SELECT COUNT(*) FROM memories WHERE LENGTH(text) > $OVERSIZE_BYTES AND NOT $EXEMPT_PRED;")
[[ "$c1_over" =~ ^[0-9]+$ ]] || c1_over=0
c1_exempt=$(q "SELECT COUNT(*) FROM memories WHERE LENGTH(text) > $OVERSIZE_BYTES AND $EXEMPT_PRED;")
[[ "$c1_exempt" =~ ^[0-9]+$ ]] || c1_exempt=0
c1_big_list=$(q "SELECT name FROM memories WHERE LENGTH(text) > $OVERSIZE_BYTES AND NOT $EXEMPT_PRED ORDER BY LENGTH(text) DESC LIMIT 5;" | tr '\n' ',' | sed 's/,$//')
if [ "$TOTAL_ROWS" -eq 0 ]; then
  c1=0; c1_detail="no rows in DB"
else
  c1=$(pct_points "$(( TOTAL_ROWS - c1_over ))" "$TOTAL_ROWS" 20)
  c1_detail="$(( TOTAL_ROWS - c1_over ))/$TOTAL_ROWS within ${OVERSIZE_BYTES}B${c1_big_list:+; fat: $c1_big_list}${c1_exempt:+; exempt: $c1_exempt}"
fi
add_score "$c1" 20
echo "| 1 | Right-sized rows | $c1/20 | $c1_detail |"

# ─────────────────────────────────────────────────────────────
# Criterion 2: FTS index cohesion — 20 pts — all-or-nothing on index alignment
#   SEMANTIC SUBSTITUTION: old "no broken refs" verified MD paths resolved on disk.
#   The DB analogue of "every pointer is live" is: the FTS5 search index has exactly
#   one indexed document per content row. memories_fts is EXTERNAL-CONTENT
#   (content='memories') so COUNT(*) on it is vacuous; memories_fts_docsize holds one
#   row per INDEXED doc and desyncs if a write bypassed the triggers. The real,
#   falsifiable assertion is memories == memories_fts_docsize (mirrors super-health.sh).
# ─────────────────────────────────────────────────────────────
c2_idx=$(q "SELECT COUNT(*) FROM memories_fts_docsize;"); [[ "$c2_idx" =~ ^[0-9]+$ ]] || c2_idx=0
if [ "$TOTAL_ROWS" -gt 0 ] && [ "$c2_idx" -eq "$TOTAL_ROWS" ]; then
  c2=20; c2_detail="fts index aligned (rows=$TOTAL_ROWS, fts_idx=$c2_idx)"
else
  # Partial credit proportional to alignment so a small desync isn't a total zero.
  if [ "$TOTAL_ROWS" -gt 0 ]; then
    aligned=$c2_idx; [ "$aligned" -gt "$TOTAL_ROWS" ] && aligned=$TOTAL_ROWS
    c2=$(pct_points "$aligned" "$TOTAL_ROWS" 20)
  else
    c2=0
  fi
  c2_detail="fts DESYNC (rows=$TOTAL_ROWS, fts_idx=$c2_idx) — run memory_db.py init/sync"
fi
add_score "$c2" 20
echo "| 2 | FTS index cohesion | $c2/20 | $c2_detail |"

# ─────────────────────────────────────────────────────────────
# Criterion 3: No near-duplicate rows — 20 pts — (rows - dups)/rows*20
#   SEMANTIC SUBSTITUTION: old "no cross-cell duplicates" compared bullet/table lines
#   across MD files. The DB analogue is near-duplicate ROWS: two memories whose bodies
#   are token-near-identical (a candidate to consolidate via lt-mem). Deterministic
#   token-overlap (Jaccard on lowercased word sets) — no python, no embeddings needed.
#   Skip + award 20 if --quick.
# ─────────────────────────────────────────────────────────────
if [ "$MODE" = "quick" ]; then
  c3=20; c3_detail="skipped (--quick) — awarded full"
else
  tmp_rows=$(mktemp "${TMPDIR:-/tmp}/mem-health-dup.XXXXXX" 2>/dev/null)
  if [ -z "$tmp_rows" ]; then
    c3=20; c3_detail="tempfile unavailable — awarded full (defensive)"
  else
    # Dump id<TAB>normalized-token-bag per row. Normalize in awk: lowercase, strip
    # non-alphanumerics, collapse whitespace. Cap each bag so the O(n^2) compare stays
    # cheap (DB is ~10^2 rows; this is fine).
    q "SELECT id, REPLACE(REPLACE(REPLACE(LOWER(text), char(10), ' '), char(9), ' '), char(13), ' ') FROM memories;" \
      | awk -F'|' '{ id=$1; $1=""; bag=$0; gsub(/[^a-z0-9 ]/," ",bag); gsub(/ +/," ",bag); print id"\t"bag }' \
      > "$tmp_rows"
    dups=$(awk -F'\t' -v simmin="$DUP_SIM_MIN" '
      { ids[NR]=$1; txt[NR]=$2 }
      END {
        n=NR; d=0
        for (i=1; i<=n; i++) {
          # tokenize row i into a set
          ni=split(txt[i], wi, " ")
          delete seti
          ci=0
          for (k=1;k<=ni;k++){ w=wi[k]; if(length(w)>=3 && !(w in seti)){ seti[w]=1; ci++ } }
          if (ci==0) continue
          for (j=i+1; j<=n; j++) {
            nj=split(txt[j], wj, " ")
            inter=0; cj=0
            delete setj
            for (k=1;k<=nj;k++){ w=wj[k]; if(length(w)>=3 && !(w in setj)){ setj[w]=1; cj++; if(w in seti) inter++ } }
            if (cj==0) continue
            uni=ci+cj-inter
            if (uni>0) { jac=inter/uni; if (jac>=simmin) d++ }
          }
        }
        print d+0
      }' "$tmp_rows")
    rm -f "$tmp_rows"
    [[ "$dups" =~ ^[0-9]+$ ]] || dups=0
    if [ "$TOTAL_ROWS" -eq 0 ]; then
      c3=20; c3_detail="no rows to compare (full)"
    else
      # Each near-dup PAIR implicates ~1 redundant row; clamp to total.
      red=$dups; [ "$red" -gt "$TOTAL_ROWS" ] && red=$TOTAL_ROWS
      c3=$(pct_points "$(( TOTAL_ROWS - red ))" "$TOTAL_ROWS" 20)
      c3_detail="$dups near-dup pair(s) (Jaccard>=$DUP_SIM_MIN) across $TOTAL_ROWS rows"
    fi
  fi
fi
add_score "$c3" 20
echo "| 3 | No near-dup rows | $c3/20 | $c3_detail |"

# ─────────────────────────────────────────────────────────────
# Criterion 4: vec store soundness — 15 pts — all-or-nothing
#   SEMANTIC SUBSTITUTION: old "load-order paths valid" verified agent-def file paths.
#   The DB analogue of "the structural plumbing is wired" is: the vector-search shadow
#   table memories_vec EXISTS. It is a vec0 VIRTUAL table — plain sqlite3 cannot
#   COUNT(*) it ("no such module: vec0"), so PRESENCE via sqlite_master is the correct,
#   fail-safe check (mirrors super-health.sh). For the authoritative
#   memories==fts==vec row triple, use `memory_db.py stats` (it loads sqlite_vec).
# ─────────────────────────────────────────────────────────────
c4_vec=$(q "SELECT COUNT(*) FROM sqlite_master WHERE name='memories_vec';")
[[ "$c4_vec" =~ ^[0-9]+$ ]] || c4_vec=0
if [ "$c4_vec" -ge 1 ]; then
  c4=15; c4_detail="memories_vec present (vec0); full triple via memory_db.py stats"
else
  c4=0; c4_detail="memories_vec MISSING — vector search unavailable; rebuild via memory_db.py init"
fi
add_score "$c4" 15
echo "| 4 | vec store soundness | $c4/15 | $c4_detail |"

# ─────────────────────────────────────────────────────────────
# Criterion 5: Metadata complete — 15 pts — well-formed/total*15
#   SEMANTIC SUBSTITUTION: old "entry formatting" checked MD entries had table-rows /
#   bullets. The DB analogue of "well-formed entry" is: the row carries the metadata
#   that hybrid search and listing rely on — a non-empty name, description, and type.
#   A row missing any of these is the DB equivalent of a malformed MD cell.
# ─────────────────────────────────────────────────────────────
c5_bad=$(q "SELECT COUNT(*) FROM memories WHERE name IS NULL OR TRIM(name)='' OR description IS NULL OR TRIM(description)='' OR type IS NULL OR TRIM(type)='';")
[[ "$c5_bad" =~ ^[0-9]+$ ]] || c5_bad=0
if [ "$TOTAL_ROWS" -eq 0 ]; then
  c5=0; c5_detail="no rows"
else
  c5=$(pct_points "$(( TOTAL_ROWS - c5_bad ))" "$TOTAL_ROWS" 15)
  c5_detail="$(( TOTAL_ROWS - c5_bad ))/$TOTAL_ROWS have name+description+type"
fi
add_score "$c5" 15
echo "| 5 | Metadata complete | $c5/15 | $c5_detail |"

# ─────────────────────────────────────────────────────────────
# Criterion 6: Staleness managed — 10 pts — (rows - stale)/rows*10
#   SEMANTIC SUBSTITUTION: old "archives manageable" capped the size of archive/ dirs.
#   The DB has no archive dir; the lifecycle analogue is "old, possibly low-value rows
#   are being consolidated/pruned, not piling up". stale = updated older than STALE_DAYS.
#   Q4 — N/A GUARD: staleness is UNMEASURABLE when the whole corpus's edit-history
#   spans less than STALE_DAYS. A corpus whose (MAX(updated) - MIN(updated)) < STALE_DAYS
#   CANNOT structurally contain a stale row, so awarding the full 10 would be a false
#   free pass (the no-rubber-stamp failure mode: a uniform-age freshly-migrated corpus
#   collecting 10/10). When N/A we award nothing AND exclude the 10 from MAXPTS, so the
#   remaining criteria carry honest weight and the final score renormalizes over 90.
#   The edit-span is the falsifiable gate; force the scored branch with MEM_STALE_DAYS=0.
# ─────────────────────────────────────────────────────────────
c6_na=0
c6_span_days=$(q "SELECT COALESCE(julianday(MAX(updated)) - julianday(MIN(updated)), 0) FROM memories;")
# Round the float span to an integer day count for display + comparison (bash has no
# float compare). awk does the <STALE_DAYS test on the raw float to avoid truncation
# bias near the boundary (a 59.9d span must still read < 60).
c6_span_disp=$(awk -v s="$c6_span_days" 'BEGIN{ printf "%.1f", (s+0=="" ? 0 : s) }')
c6_span_lt=$(awk -v s="$c6_span_days" -v thr="$STALE_DAYS" 'BEGIN{ print ((s+0) < (thr+0)) ? 1 : 0 }')
if [ "$TOTAL_ROWS" -eq 0 ]; then
  c6=0; c6_detail="no rows"
  add_score "$c6" 10
  echo "| 6 | Staleness managed | $c6/10 | $c6_detail |"
elif [ "$c6_span_lt" -eq 1 ]; then
  c6_na=1
  # N/A: award nothing, contribute nothing to MAXPTS (do NOT call add_score).
  echo "| 6 | Staleness managed | N/A | edit-span ${c6_span_disp}d < ${STALE_DAYS}d — unmeasurable until timestamps diverge |"
else
  c6_stale=$(q "SELECT COUNT(*) FROM memories WHERE updated < datetime('now','-$STALE_DAYS days');")
  [[ "$c6_stale" =~ ^[0-9]+$ ]] || c6_stale=0
  c6=$(pct_points "$(( TOTAL_ROWS - c6_stale ))" "$TOTAL_ROWS" 10)
  c6_detail="$c6_stale/$TOTAL_ROWS older than ${STALE_DAYS}d (edit-span ${c6_span_disp}d)"
  add_score "$c6" 10
  echo "| 6 | Staleness managed | $c6/10 | $c6_detail |"
fi

# ── Optional per-tier detail (verbose) — delegated to scan-mem-matrix.sh ──
if [ "$MODE" = "verbose" ]; then
  echo ""
  echo "### Per-tier DB signals (from scan-mem-matrix.sh)"
  echo '```'
  bash "$SCAN" --budgets 2>/dev/null || echo "(scan unavailable)"
  echo '```'
fi

# ── v3 Triggers (INFORMATIONAL ONLY — do NOT affect the score) ──
#    Re-expressed against DB signals (parallel to the old MD-corpus triggers).
echo ""
echo "### v3 Triggers (informational — not scored)"
fired=0
# Trigger A: total corpus bytes large (analogue of old ">2000 lines corpus").
# 1.5 MB chosen as a generous ceiling for prose-memory; above it, consolidate.
TOTAL_BYTES=$(q "SELECT COALESCE(SUM(LENGTH(text)),0) FROM memories;"); [[ "$TOTAL_BYTES" =~ ^[0-9]+$ ]] || TOTAL_BYTES=0
CORPUS_BYTES_MAX="${MEM_CORPUS_BYTES_MAX:-1500000}"
if [ "$TOTAL_BYTES" -gt "$CORPUS_BYTES_MAX" ]; then
  echo "- Corpus ${TOTAL_BYTES}B (>${CORPUS_BYTES_MAX}B): run /lt-mem --quick all"
  fired=$((fired + 1))
fi
# Trigger B: oversized rows present (analogue of old per-file OVER). c1_over is
# already exempt-aware (Q3): line-anchored budget-exempt rows do NOT fire this.
if [ "$c1_over" -gt 0 ]; then
  echo "- $c1_over oversized row(s) (>${OVERSIZE_BYTES}B): consolidate via /lt-mem --compact${c1_exempt:+ ($c1_exempt exempt, skipped)}"
  fired=$((fired + 1))
fi
# Trigger C: FTS desync (analogue of structural breakage).
if [ "$TOTAL_ROWS" -gt 0 ] && [ "$c2_idx" -ne "$TOTAL_ROWS" ]; then
  echo "- FTS index desync (rows=$TOTAL_ROWS, fts_idx=$c2_idx): rebuild via memory_db.py init"
  fired=$((fired + 1))
fi
# Trigger D: stale rows accumulating (analogue of >10% duplication / archive overflow).
# Q4: SUPPRESSED when criterion 6 is N/A — if the edit-span is below STALE_DAYS no row
# can be stale, so the percentage is meaningless and must not fire.
if [ "$TOTAL_ROWS" -gt 0 ] && [ "$c6_na" -eq 0 ]; then
  stale_pct=$(( c6_stale * 100 / TOTAL_ROWS ))
  if [ "$stale_pct" -gt 10 ]; then
    echo "- ${stale_pct}% rows stale (>${STALE_DAYS}d): run /lt-mem archive pass"
    fired=$((fired + 1))
  fi
fi
[ "$fired" -eq 0 ] && echo "v3 Triggers: None. DB within thresholds."

echo ""
# Q4 renormalization: the score is earned-points over the points that were actually
# SCORABLE (MAXPTS), rescaled to /100. N/A criteria contributed to neither SCORE nor
# MAXPTS, so they neither penalize nor inflate. When all 6 criteria apply MAXPTS==100
# and FINAL==SCORE (identical to the pre-Q4 behavior). Half-up rounding via awk.
# Defensive: MAXPTS==0 (only if EVERY criterion went N/A, impossible today since C1-C5
# always score) yields 0 rather than a divide-by-zero.
if [ "$MAXPTS" -gt 0 ]; then
  FINAL=$(awk -v s="$SCORE" -v m="$MAXPTS" 'BEGIN{ printf "%d", int((s / m * 100) + 0.5) }')
else
  FINAL=0
fi
[[ "$FINAL" =~ ^[0-9]+$ ]] || FINAL=0
[ "$FINAL" -lt 0 ] && FINAL=0
[ "$FINAL" -gt 100 ] && FINAL=100
# Q4(b) blind-axis cap: when any criterion is N/A (MAXPTS<100) an entire health axis
# is UNMEASURABLE, so perfect health cannot be CERTIFIED — cap below 100 and flag,
# rather than letting renormalization reach 100 on the measurable axes alone.
# Env-overridable. (A uniform-age corpus must not be able to look perfectly healthy.)
INCOMPLETE_CAP="${MEM_INCOMPLETE_CAP:-97}"
if [ "$MAXPTS" -lt 100 ] && [ "$FINAL" -gt "$INCOMPLETE_CAP" ]; then
  FINAL="$INCOMPLETE_CAP"
fi
# Surface the renormalization basis so a dropped denominator is never silent.
echo "Scored ${SCORE}/${MAXPTS} pts (renormalized to /100$([ "$MAXPTS" -lt 100 ] && echo "; $((100 - MAXPTS)) pts N/A excluded; capped at ${INCOMPLETE_CAP} — incomplete assessment"))."
echo "SCORE: $FINAL/100"
exit 0
