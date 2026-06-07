#!/usr/bin/env bash
# Scan the agent-memory DB and emit per-tier health signals + DB integrity.
# Usage: scan-mem-matrix.sh [--budgets] [--json]
# Default: human-readable table
#
# v3 REWRITE (DB-aware): the memory matrix is no longer a tree of MD files with
# per-file LINE budgets — it is the hybrid-search SQLite store at
# ~/.claude/agent-memory/.memory.db (table `memories` + FTS5 + vec0 shadows).
# There is no line-count to measure, so the historical per-file LOC/budget columns
# are REPLACED by the closest DB-health signals:
#   - rows-per-tier            (GROUP BY tier — the "how full is each cell" signal)
#   - oversized rows           (length(text) > OVERSIZE_BYTES — the "cell too fat,
#                               consider consolidation" signal that the old per-file
#                               LINE budget used to force)
#   - potential staleness      (updated older than STALE_DAYS — the "old + maybe
#                               low-value" signal; see CAVEAT below)
#   - FTS index cohesion       (memories == memories_fts_docsize — the "search index
#                               aligned with content" signal; vec presence checked
#                               via sqlite_master, see CAVEAT below)
#
# SEMANTIC SUBSTITUTION MAP (old MD concept -> new DB signal):
#   per-file LOC                 -> length(text) per row (bytes)
#   per-file LINE budget (OVER)  -> OVERSIZE_BYTES per-row threshold (FAT)
#   total corpus LOC             -> total bytes across all rows
#   "cell populated"             -> row exists for that (tier,agent)
#   MEMORY.md / ltm.md / mtm.md  -> tier+agent columns; no file path involved
#
# CAVEATS (documented so downstream readers do not over-trust the numbers):
#   - STALENESS is only meaningful once memories accrue ORGANIC writes. Immediately
#     after a bulk migration every row shares one `updated` timestamp, so the stale
#     count will read 0 even for genuinely old content. Treat 0 as "not yet
#     informative", not "definitively fresh".
#   - vec0 (`memories_vec`) is a VIRTUAL table: plain sqlite3 CANNOT
#     `SELECT COUNT(*)` from it ("no such module: vec0"). We assert FTS cohesion on
#     `memories_fts_docsize` (the falsifiable check) and verify vec PRESENCE via
#     sqlite_master only — mirroring scripts/super-health.sh. For the authoritative
#     memories==fts==vec triple, use `memory_db.py stats` (loads sqlite_vec).
#
# Fail-safe: missing sqlite3 / missing DB / unreadable DB -> emit a single skip row,
# exit 0. Never crash a caller (mem-health.sh consumes this).

set -uo pipefail   # NEVER set -e — a missing tool must degrade, not abort

MEM_DIR="${HOME}/.claude/agent-memory"
MDB="${MEMORY_DB_PATH:-$MEM_DIR/.memory.db}"

# ── Tunable DB-health thresholds (semantic replacements for MD line budgets) ──
# OVERSIZE_BYTES: a single memory body above this is "fat" — the DB analogue of the
# old per-file OVER flag. Chosen from the live corpus: median body is well under
# ~4 KB while a deliberate long-form handoff/retrospective runs 8-100 KB. 8000 keeps
# ordinary entries clear and flags only the genuinely large bodies worth consolidating.
OVERSIZE_BYTES="${MEM_OVERSIZE_BYTES:-8000}"
# STALE_DAYS: a row whose `updated` is older than this is a staleness CANDIDATE
# (old + possibly low-value). See CAVEAT — only informative after organic writes.
STALE_DAYS="${MEM_STALE_DAYS:-60}"

format=${1:-table}

# sqlite3 helper — echoes empty on any failure, never aborts the script.
q() { sqlite3 "$MDB" "$1" 2>/dev/null || true; }

# Preconditions: degrade gracefully when the DB cannot be inspected.
db_unavailable_reason() {
  command -v sqlite3 >/dev/null 2>&1 || { echo "sqlite3 absent"; return; }
  [ -f "$MDB" ] || { echo "DB not built ($MDB)"; return; }
  sqlite3 "$MDB" "SELECT 1;" >/dev/null 2>&1 || { echo "invalid SQLite"; return; }
  echo ""  # available
}

emit_table() {
  local reason; reason="$(db_unavailable_reason)"
  printf "TIER\tROWS\tBYTES\tBIGGEST\tFAT(>%s)\tSTALE(>%sd)\n" "$OVERSIZE_BYTES" "$STALE_DAYS"
  if [ -n "$reason" ]; then
    printf "(skip: %s)\t0\t0\t0\t0\t0\n" "$reason"
    return 0
  fi
  local now_iso oversize stale
  now_iso=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)
  # Per-tier aggregate rows. NULL-safe sums via COALESCE so empty tiers print 0.
  q "SELECT tier,
            COUNT(*),
            COALESCE(SUM(LENGTH(text)),0),
            COALESCE(MAX(LENGTH(text)),0),
            COALESCE(SUM(CASE WHEN LENGTH(text) > $OVERSIZE_BYTES THEN 1 ELSE 0 END),0),
            COALESCE(SUM(CASE WHEN updated < datetime('now','-$STALE_DAYS days') THEN 1 ELSE 0 END),0)
     FROM memories
     GROUP BY tier
     ORDER BY COUNT(*) DESC;" \
  | while IFS='|' read -r tier rows bytes biggest fat stale; do
      [ -n "$tier" ] || tier="(null)"
      printf "%s\t%s\t%s\t%s\t%s\t%s\n" "$tier" "$rows" "$bytes" "$biggest" "$fat" "$stale"
    done
  # TOTAL summary row (corpus-wide — replaces the old total-LOC budget line).
  q "SELECT 'TOTAL',
            COUNT(*),
            COALESCE(SUM(LENGTH(text)),0),
            COALESCE(MAX(LENGTH(text)),0),
            COALESCE(SUM(CASE WHEN LENGTH(text) > $OVERSIZE_BYTES THEN 1 ELSE 0 END),0),
            COALESCE(SUM(CASE WHEN updated < datetime('now','-$STALE_DAYS days') THEN 1 ELSE 0 END),0)
     FROM memories;" \
  | while IFS='|' read -r tag rows bytes biggest fat stale; do
      printf "%s\t%s\t%s\t%s\t%s\t%s\n" "$tag" "$rows" "$bytes" "$biggest" "$fat" "$stale"
    done
  # FTS index cohesion (the real integrity assertion) + vec presence.
  local rows idx vec_present
  rows=$(q "SELECT COUNT(*) FROM memories;"); [[ "$rows" =~ ^[0-9]+$ ]] || rows=0
  idx=$(q "SELECT COUNT(*) FROM memories_fts_docsize;"); [[ "$idx" =~ ^[0-9]+$ ]] || idx=0
  vec_present=$(q "SELECT COUNT(*) FROM sqlite_master WHERE name='memories_vec';")
  [[ "$vec_present" =~ ^[0-9]+$ ]] || vec_present=0
  if [ "$idx" -eq "$rows" ] && [ "$vec_present" -ge 1 ]; then
    printf "INTEGRITY\trows=%s\tfts_idx=%s\tvec=present\tOK\t-\n" "$rows" "$idx"
  else
    local vtxt="present"; [ "$vec_present" -ge 1 ] || vtxt="MISSING"
    printf "INTEGRITY\trows=%s\tfts_idx=%s\tvec=%s\tDESYNC\t-\n" "$rows" "$idx" "$vtxt"
  fi
}

emit_json() {
  local reason; reason="$(db_unavailable_reason)"
  if [ -n "$reason" ]; then
    printf '{"available":false,"reason":"%s","tiers":[]}' "$reason"
    return 0
  fi
  local rows idx vec_present total_rows total_bytes
  rows=$(q "SELECT COUNT(*) FROM memories;"); [[ "$rows" =~ ^[0-9]+$ ]] || rows=0
  idx=$(q "SELECT COUNT(*) FROM memories_fts_docsize;"); [[ "$idx" =~ ^[0-9]+$ ]] || idx=0
  vec_present=$(q "SELECT COUNT(*) FROM sqlite_master WHERE name='memories_vec';")
  [[ "$vec_present" =~ ^[0-9]+$ ]] || vec_present=0
  total_rows="$rows"
  total_bytes=$(q "SELECT COALESCE(SUM(LENGTH(text)),0) FROM memories;"); [[ "$total_bytes" =~ ^[0-9]+$ ]] || total_bytes=0
  printf '{"available":true,'
  printf '"oversize_bytes":%s,"stale_days":%s,' "$OVERSIZE_BYTES" "$STALE_DAYS"
  printf '"integrity":{"rows":%s,"fts_index":%s,"vec_present":%s,"cohesive":%s},' \
    "$rows" "$idx" "$vec_present" \
    "$([ "$idx" -eq "$rows" ] && [ "$vec_present" -ge 1 ] && echo true || echo false)"
  printf '"total":{"rows":%s,"bytes":%s},' "$total_rows" "$total_bytes"
  printf '"tiers":['
  local first=1
  while IFS='|' read -r tier nrows bytes biggest fat stale; do
    [ -n "$tier" ] || continue
    [ -z "$first" ] && printf ','
    printf '{"tier":"%s","rows":%s,"bytes":%s,"biggest":%s,"fat":%s,"stale":%s}' \
      "$tier" "$nrows" "$bytes" "$biggest" "$fat" "$stale"
    first=
  done < <(q "SELECT tier,
                     COUNT(*),
                     COALESCE(SUM(LENGTH(text)),0),
                     COALESCE(MAX(LENGTH(text)),0),
                     COALESCE(SUM(CASE WHEN LENGTH(text) > $OVERSIZE_BYTES THEN 1 ELSE 0 END),0),
                     COALESCE(SUM(CASE WHEN updated < datetime('now','-$STALE_DAYS days') THEN 1 ELSE 0 END),0)
              FROM memories GROUP BY tier ORDER BY COUNT(*) DESC;")
  printf ']}'
}

case "$format" in
  --budgets|table)
    emit_table
    ;;
  --json)
    emit_json
    ;;
  *)
    echo "Unknown format: $format" >&2
    exit 1
    ;;
esac
