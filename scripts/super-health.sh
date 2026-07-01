#!/bin/bash
# super-health.sh — deterministic aggregator behind skills/super-health/SKILL.md.
# Combines 7 component health scores into a single weighted /100 + letter grade.
#
# Usage:
#   bash super-health.sh [--quick|--standard|--deep|--complete]
#
# Tiers:
#   --quick     (default) hook-health --quick + skill + mem + settings + sessions
#               + comms + infra-test.sh (default, not --full)
#   --standard  hook-health --standard + all quick checks
#   --deep      hook-health --deep + infra-test.sh --full + all standard checks
#   --complete  deep score PLUS a printed note that the model must run the Step-4
#               5-agent post-hoc audit (per SKILL.md). This script NEVER spawns agents.
#
# Contract: parses each component's "SCORE:" line, multiplies by weight, sums,
# rounds to nearest int, prints the component table + grade, and as its FINAL
# stdout line, exactly: "SCORE: <int>/100".  Exit code is ALWAYS 0.
#
# ════════════════════════════════════════════════════════════════════════════
#  EXTENSIBILITY CONTRACT (read before editing):
#  To add a new component later (e.g. a v3 memory-DB or render-pipeline check):
#    1. Add one entry to the WEIGHTS table below: WEIGHTS[<key>]=<fraction>
#       and append <key> to COMPONENT_ORDER (controls table row order).
#    2. Add one `score_<key>` function that prints a final "SCORE: <int>/100" line.
#    3. Map <key> -> "Human Label" in COMPONENT_LABEL.
#    4. Re-balance the WEIGHTS fractions so they sum to 1.00 (the script asserts this).
#  No other code changes are needed — the aggregation loop is component-agnostic.
# ════════════════════════════════════════════════════════════════════════════

set -uo pipefail   # NEVER set -e (a failed component must yield 0, not abort the run)

CLAUDE="${CLAUDE_DIR:-$HOME/.claude}"
SCRIPTS="$CLAUDE/scripts"
# Prefer the superclaude venv (runs with no shell activation); fall back to system python3.
PYTHON="$CLAUDE/.venv/bin/python"; [ -x "$PYTHON" ] || PYTHON="$(command -v python3 2>/dev/null || true)"

# ── Args / tier ──
TIER="quick"
for arg in "$@"; do
  case "$arg" in
    --quick)    TIER="quick" ;;
    --standard) TIER="standard" ;;
    --deep)     TIER="deep" ;;
    --complete) TIER="complete" ;;
  esac
done
# --complete reuses the --deep scoring path; the only difference is a printed note.
EFFECTIVE_TIER="$TIER"
[ "$TIER" = "complete" ] && EFFECTIVE_TIER="deep"

# ════════════════════════════════════════════════════════════════════════════
#  WEIGHTS — single source of truth. Must sum to 1.00. (DRY: rebalance here only.)
# ════════════════════════════════════════════════════════════════════════════
declare -A WEIGHTS=(
  [hook]=0.18
  [skill]=0.13
  [mem]=0.18
  [settings]=0.12
  [session]=0.05
  [comms]=0.10
  [regression]=0.12
  [subsystems]=0.12
)
# Row order for the report table.
COMPONENT_ORDER=(hook skill mem settings session comms regression subsystems)
declare -A COMPONENT_LABEL=(
  [hook]="Hook health"
  [skill]="Skill health"
  [mem]="Memory health"
  [settings]="Settings + agents"
  [session]="Sessions"
  [comms]="Comms (HCOM broker)"
  [regression]="Regression tests"
  [subsystems]="Subsystems (v3)"
)

# ── Shared helper: extract the final "SCORE: N/100" integer from a blob ──
extract_score() {
  # stdin = component output; echoes the integer (0 if none found, clamped 0..100)
  local n
  n=$(grep -oE 'SCORE: *[0-9]+/100' | tail -1 | grep -oE '[0-9]+' | head -1)
  [ -z "$n" ] && n=0
  [ "$n" -lt 0 ] 2>/dev/null && n=0
  [ "$n" -gt 100 ] 2>/dev/null && n=100
  echo "$n"
}

# ════════════════════════════════════════════════════════════════════════════
#  COMPONENT SCORERS — each prints a final "SCORE: <int>/100" line on stdout.
#  hook/skill/mem delegate to the sibling scripts (the scoring SOT).
#  settings/sessions/comms port the inline bash from super-health/SKILL.md.
#  regression runs infra-test.sh and converts its pass-rate to /100.
# ════════════════════════════════════════════════════════════════════════════

score_hook() {
  local s="$SCRIPTS/hook-health.sh"
  if [ ! -f "$s" ]; then echo "hook-health.sh missing"; echo "SCORE: 0/100"; return; fi
  case "$EFFECTIVE_TIER" in
    quick)            bash "$s" --quick ;;
    standard)         bash "$s" --standard ;;
    deep)             bash "$s" --deep ;;
  esac
}

score_skill() {
  local s="$SCRIPTS/skill-health.sh"
  if [ ! -f "$s" ]; then echo "skill-health.sh missing"; echo "SCORE: 0/100"; return; fi
  bash "$s" all
}

score_mem() {
  local s="$SCRIPTS/mem-health.sh"
  if [ ! -f "$s" ]; then echo "mem-health.sh missing"; echo "SCORE: 0/100"; return; fi
  local mh_out mh_score
  mh_out=$(bash "$s")
  mh_score=$(printf '%s\n' "$mh_out" | extract_score)

  # ── Memory search-store integrity facet (v3) — mirrors score_comms' .comms.db ──
  # In ADDITION to mem-health.sh's scoring, validate the FTS5+vec search store at
  # agent-memory/.memory.db (built by scripts/memory/*). Fast path only: bash +
  # sqlite3 CLI, NO python, NO embedding model. memories_vec is a vec0 VIRTUAL
  # table — plain sqlite3 CANNOT `SELECT COUNT(*) FROM memories_vec` (errors
  # "no such module: vec0"), so its PRESENCE is checked via sqlite_master only and
  # cohesion is asserted on memories==memories_fts (both plain FTS5-backed tables).
  #   sqlite3 ABSENT → graceful: no penalty (cannot inspect).
  #   db ABSENT      → N/A: no penalty (fail-safe for pre-memory-DB systems).
  #   HEALTHY        → no penalty (cap stays 100).
  #   BROKEN         → genuine deduction: cap the whole mem component at 50.
  # MEMORY_DB_PATH overrides the path (used by the failing-path test against a
  # throwaway COPY — the real .memory.db is never opened for write by this tool).
  local MDB="${MEMORY_DB_PATH:-$CLAUDE/agent-memory/.memory.db}"
  local mdb_detail mdb_cap=100
  if ! command -v sqlite3 >/dev/null 2>&1; then
    mdb_detail="sqlite3 absent (skip)"
  elif [ ! -f "$MDB" ]; then
    mdb_detail="not built (skip)"
  elif ! sqlite3 "$MDB" "SELECT 1;" >/dev/null 2>&1; then
    mdb_detail="invalid SQLite -> CAP 50"
    mdb_cap=50
  else
    local mdb_tables mdb_rows mdb_idx t miss_tbl=""
    mdb_tables=$(sqlite3 "$MDB" "SELECT name FROM sqlite_master WHERE type IN ('table','view');" 2>/dev/null)
    # Required tables: the three core (memories, memories_fts, memories_vec) PLUS
    # the FTS5 doc-index shadow (memories_fts_docsize) that the count-cohesion check
    # below reads. memories_vec is a vec0 VIRTUAL table; its presence shows in
    # sqlite_master even though plain sqlite3 cannot SELECT COUNT(*) from it.
    for t in memories memories_fts memories_vec memories_fts_docsize; do
      printf '%s\n' "$mdb_tables" | grep -qx "$t" || miss_tbl="$miss_tbl $t"
    done
    mdb_rows=$(sqlite3 "$MDB" "SELECT COUNT(*) FROM memories;" 2>/dev/null)
    # Count-cohesion uses memories vs the FTS5 doc-index shadow, NOT COUNT(*) FROM
    # memories_fts: memories_fts is an EXTERNAL-CONTENT FTS5 table (content='memories'),
    # so its COUNT(*) always proxies back to memories and can NEVER diverge — comparing
    # those two would be a structurally vacuous (flat-pass) check. memories_fts_docsize
    # holds one row per INDEXED document and goes stale if the index desyncs from the
    # content table (e.g. a write that bypassed the AFTER-INSERT/DELETE triggers), so
    # memories==memories_fts_docsize is the real, falsifiable integrity assertion.
    mdb_idx=$(sqlite3 "$MDB" "SELECT COUNT(*) FROM memories_fts_docsize;" 2>/dev/null)
    [[ "$mdb_rows" =~ ^[0-9]+$ ]] || mdb_rows=0
    [[ "$mdb_idx"  =~ ^[0-9]+$ ]] || mdb_idx=0
    if [ -n "$miss_tbl" ]; then
      mdb_detail="missing table(s)$miss_tbl -> CAP 50"
      mdb_cap=50
    elif [ "$mdb_rows" -le 0 ]; then
      mdb_detail="memories empty (rows=$mdb_rows) -> CAP 50"
      mdb_cap=50
    elif [ "$mdb_idx" -ne "$mdb_rows" ]; then
      mdb_detail="fts-index!=memories (idx=$mdb_idx, rows=$mdb_rows) -> CAP 50"
      mdb_cap=50
    else
      mdb_detail="healthy (rows=$mdb_rows, fts-index=$mdb_idx)"
    fi
  fi
  # ── Memory corpus-health facet (Q6) ──────────────────────────────────────
  # AFTER the integrity facet above (which gives mdb_cap), measure two prongs of
  # day-to-day corpus health and fold them into ONE bounded penalty. This is
  # distinct from the integrity cap (which answers "is the search store STRUCTURALLY
  # sound") — the corpus facet answers "is the corpus an OPERATIONALLY healthy size
  # and shape, or is it bloating / slowing down". Both prongs are always surfaced.
  #   (a) SEARCHABILITY: row_count (sqlite COUNT — always) + measured search latency
  #       (ONE real memory_db.py search, timed — DEEP MODE ONLY so the fast default
  #       stays python-free; fast mode prints "latency: not measured (fast mode)").
  #   (b) FOOTPRINT: MIN/AVG/MAX(LENGTH(text)) over NON-EXEMPT rows (sqlite — always),
  #       reusing the SAME line-anchored budget-exempt predicate as mem-health.sh Q3
  #       so a legitimately-exempt giant (e.g. a pinned active-context row) does not
  #       distort the max.
  #
  # Reference caps — "normal-operation reference points calibrated to the live corpus"
  # (135 rows, non-exempt max≈101KB, avg≈4KB, cold-CLI search≈1.3s as of 2026-06).
  # Each is ENV-OVERRIDABLE so the failing-path tests can drive a single prong over
  # its ref without touching the DB, and so caps can be retuned as the corpus grows.
  local ROW_REF="${MEM_ROW_COUNT_REF:-600}"
  local AVG_REF="${MEM_AVG_BYTES_REF:-8000}"
  local MAX_REF="${MEM_MAX_BYTES_REF:-50000}"      # over NON-EXEMPT rows
  local LAT_REF_MS="${MEM_SEARCH_LATENCY_REF_MS:-5000}"

  # The line-anchored exempt predicate (identical semantics to mem-health.sh Q3).
  # Constructed with char() codes inside the SQL ONLY (never interpolated from the
  # shell) so a literal '!' cannot be mangled by shell history expansion.
  local EXEMPT_SQL="(text LIKE '<!-- budget-exempt%' OR text LIKE '%' || char(10) || '<!-- budget-exempt%')"

  local corpus_penalty=0 corpus_detail lat_str="not measured (fast mode)"
  if ! command -v sqlite3 >/dev/null 2>&1; then
    corpus_detail="sqlite3 absent — corpus facet skipped (no penalty)"
  elif [ ! -f "$MDB" ] || ! sqlite3 "$MDB" "SELECT 1;" >/dev/null 2>&1; then
    corpus_detail="db unavailable — corpus facet skipped (no penalty)"
  else
    local row_count fp_min fp_avg fp_max
    row_count=$(sqlite3 "$MDB" "SELECT COUNT(*) FROM memories;" 2>/dev/null)
    # Footprint over NON-EXEMPT rows only.
    fp_min=$(sqlite3 "$MDB" "SELECT COALESCE(MIN(LENGTH(text)),0) FROM memories WHERE NOT $EXEMPT_SQL;" 2>/dev/null)
    fp_avg=$(sqlite3 "$MDB" "SELECT COALESCE(CAST(AVG(LENGTH(text)) AS INT),0) FROM memories WHERE NOT $EXEMPT_SQL;" 2>/dev/null)
    fp_max=$(sqlite3 "$MDB" "SELECT COALESCE(MAX(LENGTH(text)),0) FROM memories WHERE NOT $EXEMPT_SQL;" 2>/dev/null)
    [[ "$row_count" =~ ^[0-9]+$ ]] || row_count=0
    [[ "$fp_min"    =~ ^[0-9]+$ ]] || fp_min=0
    [[ "$fp_avg"    =~ ^[0-9]+$ ]] || fp_avg=0
    [[ "$fp_max"    =~ ^[0-9]+$ ]] || fp_max=0

    # (a) max prong — the dominant footprint signal. Penalty scales with how far the
    #     biggest non-exempt row overshoots the ref, +20 pts per 1× overshoot, capped
    #     at 8 of the 12-pt budget. round() is half-up (awk int(x+0.5)).
    if [ "$fp_max" -gt "$MAX_REF" ]; then
      local max_pen
      max_pen=$(awk -v mx="$fp_max" -v rf="$MAX_REF" 'BEGIN{ p=int(((mx/rf)-1)*20 + 0.5); if(p>8)p=8; print p }')
      corpus_penalty=$((corpus_penalty + max_pen))
    fi
    # (b) avg prong — flat +3 when the mean body exceeds the ref (broad bloat).
    [ "$fp_avg" -gt "$AVG_REF" ] && corpus_penalty=$((corpus_penalty + 3))
    # (c) row_count prong — flat +3 when the corpus has more rows than the ref.
    [ "$row_count" -gt "$ROW_REF" ] && corpus_penalty=$((corpus_penalty + 3))

    # (d) latency prong — DEEP MODE ONLY (one real timed search). Fast mode never
    #     spawns python, so the default stays <100ms. memory_db.py is python-free to
    #     locate but the search itself loads the model; gate it behind deep.
    if [ "$EFFECTIVE_TIER" = "deep" ]; then
      local lat_ms t0 t1 pybin="$CLAUDE/.venv/bin/python" memdb="$SCRIPTS/memory/memory_db.py"
      if [ -x "$pybin" ] && [ -f "$memdb" ]; then
        t0=$(date +%s%N)
        HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 "$pybin" "$memdb" search "memory health corpus" -k 5 >/dev/null 2>&1
        t1=$(date +%s%N)
        lat_ms=$(( (t1 - t0) / 1000000 ))
        lat_str="${lat_ms}ms"
        [ "$lat_ms" -gt "$LAT_REF_MS" ] && corpus_penalty=$((corpus_penalty + 3))
      else
        lat_str="not measured (memory_db.py or venv absent)"
      fi
    fi

    # Single bounded penalty (cap 12) — avoids double-jeopardy with mem-health C1.
    [ "$corpus_penalty" -gt 12 ] && corpus_penalty=12
    corpus_detail="rows=$row_count (ref $ROW_REF), footprint min/avg/max=${fp_min}/${fp_avg}/${fp_max}B (max-ref $MAX_REF, exempt-aware), latency=$lat_str; penalty=-$corpus_penalty"
  fi

  # --- Memory capabilities facet (v3 Tier-0/1 query-time improvements) ---
  # DEEP MODE ONLY (loads python; the fast default stays python-free, like the
  # latency prong). Exercises the NEW query-time capabilities END-TO-END so a
  # regression dents the score instead of passing silently:
  #   CAP1 get-ladder resolves a filename SLUG when the stored name is a human
  #        title (the canonical resolver fix)              -> CLI exit 0
  #   CAP2 an AMBIGUOUS name is REFUSED (not guessed), listing
  #        'did you mean' candidates                       -> CLI exit 3 + stderr
  #   CAP3 a hybrid search over the LIVE store returns a ranked hit (the
  #        fts+vec+model pipeline actually answers)        -> non-empty --json
  # CAP1/CAP2 run against a THROWAWAY temp DB built with sqlite3 (NO model, NO
  # live-DB write): the resolver ladder is pure-SQL, so two same-named rows give a
  # deterministic ambiguity with no embedding needed. CAP3 is a READ-ONLY hybrid
  # search against the live DB (never writes it). Graceful-skip (python/memory_db.py
  # absent, or fixture setup fails) costs NOTHING; only a capability that RUNS and
  # answers WRONG deducts (bounded caps_penalty, cap 6).
  local caps_penalty=0 caps_detail="not exercised (fast mode)"
  if [ "$EFFECTIVE_TIER" = "deep" ]; then
    local capdb pybin3="$CLAUDE/.venv/bin/python" memdb3="$SCRIPTS/memory/memory_db.py"
    if [ ! -x "$pybin3" ] || [ ! -f "$memdb3" ]; then
      caps_detail="not exercised (memory_db.py or venv absent)"
    else
      local caps_fail=0 cap1="?" cap2="?" cap3="?" c2err="" c2rc=0 c3out=""
      capdb=$(mktemp --tmpdir="${TMPDIR:-/tmp}" super-health-caps.XXXXXX)
      # Fixture: init schema via memory_db.py (creates the vec0+fts tables), then
      # INSERT three rows via sqlite3 so the AFTER-INSERT trigger fills FTS with NO
      # model loaded. Two rows share name/slug 'dup_probe' (ambiguous); one is a
      # human-title row whose filename slug is 'zeta_slug_probe'.
      if command -v sqlite3 >/dev/null 2>&1 \
         && HF_HUB_OFFLINE=1 "$pybin3" "$memdb3" --db "$capdb" init >/dev/null 2>&1 \
         && sqlite3 "$capdb" \
              "INSERT INTO memories(path,tier,type,name,description,text) VALUES ('shared/zeta_slug_probe.md','shared','project','Zeta Retrieval Title','t','body one');
               INSERT INTO memories(path,tier,type,name,description,text) VALUES ('instance/x/dup_probe.md','instance','project','dup_probe','t','body two');
               INSERT INTO memories(path,tier,type,name,description,text) VALUES ('instance/y/dup_probe.md','instance','project','dup_probe','t','body three');" >/dev/null 2>&1
      then
        # CAP1: filename slug resolves the human-title row.
        if HF_HUB_OFFLINE=1 "$pybin3" "$memdb3" --db "$capdb" get --name zeta_slug_probe >/dev/null 2>&1; then
          cap1="ok"
        else cap1="FAIL"; caps_fail=$((caps_fail + 1)); fi
        # CAP2: ambiguous name refused (exit 3) with a 'did you mean' list on stderr.
        c2err=$(HF_HUB_OFFLINE=1 "$pybin3" "$memdb3" --db "$capdb" get --name dup_probe 2>&1 >/dev/null); c2rc=$?
        if [ "$c2rc" -eq 3 ] && printf '%s' "$c2err" | grep -qi "did you mean"; then
          cap2="ok"
        else cap2="FAIL(rc=$c2rc)"; caps_fail=$((caps_fail + 1)); fi
      else
        cap1="skip(setup)"; cap2="skip(setup)"
      fi
      rm -f "$capdb"
      # CAP3: live-store hybrid search returns a ranked hit (read-only). --json makes
      # the emptiness test robust (an empty result set prints '[]', with no '"id"').
      c3out=$(HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 "$pybin3" "$memdb3" search "memory health corpus" -k 5 --json 2>/dev/null)
      if printf '%s' "$c3out" | grep -q '"id"'; then
        cap3="ok"
      else cap3="FAIL"; caps_fail=$((caps_fail + 1)); fi
      caps_penalty=$((caps_fail * 2)); [ "$caps_penalty" -gt 6 ] && caps_penalty=6
      caps_detail="get-slug=$cap1, ambiguous-refusal=$cap2, hybrid-topk=$cap3; penalty=-$caps_penalty"
    fi
  fi

  # Apply the cap LAST, THEN subtract the corpus + capabilities penalties, flooring
  # at 0. A broken search store is a hard ceiling (min); corpus bloat and a failed
  # capability are graduated deductions below that ceiling.
  # final_mem = max(0, min(mh_score, mdb_cap) - corpus_penalty - caps_penalty).
  local final_mem=$mh_score
  [ "$final_mem" -gt "$mdb_cap" ] && final_mem=$mdb_cap
  final_mem=$((final_mem - corpus_penalty - caps_penalty))
  [ "$final_mem" -lt 0 ] && final_mem=0

  printf '%s\n' "$mh_out" | grep -v '^SCORE:'
  echo "Memory .memory.db facet: $mdb_detail (mem-health=$mh_score, cap=$mdb_cap)"
  echo "Memory corpus facet: $corpus_detail"
  echo "Memory capabilities facet: $caps_detail"
  echo "SCORE: $final_mem/100"
}

# ── Settings + agents (15%) — ported verbatim from super-health/SKILL.md §1d ──
score_settings() {
  local SCORE=0
  jq . "$CLAUDE/settings.json" > /dev/null 2>&1 && SCORE=$((SCORE + 15))

  local TOTAL=0 VALID=0 a FM
  for a in "$CLAUDE"/agents/*.md; do
    [ -f "$a" ] || continue
    TOTAL=$((TOTAL + 1))
    FM=$(sed -n '2,/^---$/p' "$a" | head -20)
    if head -1 "$a" | grep -q "^---$" && echo "$FM" | grep -q "^model:"; then
      VALID=$((VALID + 1))
    fi
  done
  [ "$TOTAL" -gt 0 ] && SCORE=$((SCORE + VALID * 20 / TOTAL))

  local MODELS_OK=0 MODELS_TOTAL=0 MODEL
  for a in "$CLAUDE"/agents/*.md; do
    [ -f "$a" ] || continue
    MODEL=$(sed -n 's/^model: *//p' "$a" | head -1 | tr -d '"')
    [ -z "$MODEL" ] && continue
    MODELS_TOTAL=$((MODELS_TOTAL + 1))
    case "$MODEL" in opus|sonnet|haiku|"opus[1m]"|"sonnet[1m]"|"haiku[1m]") MODELS_OK=$((MODELS_OK + 1)) ;; esac
  done
  [ "$MODELS_TOTAL" -gt 0 ] && SCORE=$((SCORE + MODELS_OK * 10 / MODELS_TOTAL))

  # Security posture (F0 lesson: sandbox/secrets gains were scorer-invisible).
  # Sandbox must not be disabled in either settings file (absent key = enabled).
  local SANDBOX_OK=1 sf
  for sf in "$CLAUDE/settings.json" "$CLAUDE/settings.local.json"; do
    [ -f "$sf" ] || continue
    [ "$(jq -r '.sandbox.enabled' "$sf" 2>/dev/null)" = "false" ] && SANDBOX_OK=0
  done
  [ "$SANDBOX_OK" -eq 1 ] && SCORE=$((SCORE + 10))

  # Secrets files must not be group/world-accessible (mode & 077 == 0).
  local LAX_SECRETS=0 sfile smode
  for sfile in "$HOME/.claude.json" "$CLAUDE/.credentials.json" "$CLAUDE/scripts/telegram-surface/.env"; do
    [ -f "$sfile" ] || continue
    smode=$(stat -c '%a' "$sfile" 2>/dev/null) || continue
    [ $(( 8#$smode & 8#077 )) -ne 0 ] && LAX_SECRETS=$((LAX_SECRETS + 1))
  done
  [ "$LAX_SECRETS" -eq 0 ] && SCORE=$((SCORE + 10))

  local DENY
  DENY=$(jq '.permissions.deny | length' "$CLAUDE/settings.json" 2>/dev/null || echo 0)
  [ "$DENY" -ge 5 ] 2>/dev/null && SCORE=$((SCORE + 15))

  local ORPHANS=0 NAME
  for a in "$CLAUDE"/agents/*.md; do
    [ -f "$a" ] || continue
    NAME=$(basename "$a" .md)
    case "$NAME" in w-*|orch|meta|w-design-reviewer) continue ;; esac
    [ -d "$CLAUDE/comms/$NAME" ] || ORPHANS=$((ORPHANS + 1))
  done
  [ "$ORPHANS" -eq 0 ] && SCORE=$((SCORE + 10))

  local BROKEN
  BROKEN=$(find "$CLAUDE/agents/" -maxdepth 1 -type l ! -exec test -e {} \; -print 2>/dev/null | wc -l)
  [ "$BROKEN" -eq 0 ] && SCORE=$((SCORE + 10))

  echo "Settings+Agents detail: jsonOK, frontmatter=$VALID/$TOTAL, models=$MODELS_OK/$MODELS_TOTAL, deny=$DENY, orphans=$ORPHANS, brokenSymlinks=$BROKEN, sandboxOK=$SANDBOX_OK, laxSecrets=$LAX_SECRETS"
  echo "SCORE: $SCORE/100"
}

# ── Sessions (5%) — ported verbatim from super-health/SKILL.md §1e ──
score_session() {
  local TIMER_DIR="$CLAUDE/session-timers"
  local SCORE=0
  if [ ! -d "$TIMER_DIR" ]; then
    echo "Sessions: no timer dir (clean)"
    echo "SCORE: 100/100"
    return
  fi
  # A leaked ("zombie") timer set = a PID we cannot confirm alive whose session
  # has ALSO persisted past any plausible lifetime. We corroborate kill -0 with
  # the .start age because kill -0 is unreliable across PID namespaces: when
  # super-health runs inside a sandboxed tool the host PIDs are invisible, so a
  # bare `! kill -0` would mis-flag every live session as a zombie. Age is
  # filesystem-derived and therefore namespace-independent. Bound is agent-aware:
  # time-limited agents hard-block at 53 min (60 min ⇒ genuinely leaked); meta is
  # exempt and an .override bypasses the timer, so those only count past 24 h.
  local ZOMBIES=0 pf PID sid zstart zagent znow zage zbound
  znow=$(date +%s)
  for pf in "$TIMER_DIR"/*.pid; do
    [ -f "$pf" ] || continue
    PID=$(cat "$pf" 2>/dev/null)
    [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null && continue   # verifiably alive
    sid=$(basename "$pf" .pid)
    zstart=$(cat "$TIMER_DIR/${sid}.start" 2>/dev/null)
    if ! [[ "$zstart" =~ ^[0-9]+$ ]]; then ZOMBIES=$((ZOMBIES + 1)); continue; fi
    zagent=$(cat "$TIMER_DIR/${sid}.agent" 2>/dev/null)
    if [ "$zagent" = "meta" ] || [ -f "$TIMER_DIR/${sid}.override" ]; then zbound=86400; else zbound=3600; fi
    zage=$(( znow - zstart ))
    [ "$zage" -gt "$zbound" ] && ZOMBIES=$((ZOMBIES + 1))
  done
  [ "$ZOMBIES" -eq 0 ] && SCORE=$((SCORE + 50))

  local ORPHANS=0 sf af SID
  for sf in "$TIMER_DIR"/*.start; do
    [ -f "$sf" ] || continue
    SID=$(basename "$sf" .start)
    [ ! -f "$TIMER_DIR/${SID}.agent" ] && ORPHANS=$((ORPHANS + 1))
  done
  for af in "$TIMER_DIR"/*.agent; do
    [ -f "$af" ] || continue
    SID=$(basename "$af" .agent)
    [ ! -f "$TIMER_DIR/${SID}.start" ] && ORPHANS=$((ORPHANS + 1))
  done
  [ "$ORPHANS" -eq 0 ] && SCORE=$((SCORE + 30))

  local OVER=0 NOW START AGE_MIN osid
  NOW=$(date +%s)
  for sf in "$TIMER_DIR"/*.start; do
    [ -f "$sf" ] || continue
    osid=$(basename "$sf" .start)
    # meta is exempt from session time limits; an .override also bypasses them.
    [ "$(cat "$TIMER_DIR/${osid}.agent" 2>/dev/null)" = "meta" ] && continue
    [ -f "$TIMER_DIR/${osid}.override" ] && continue
    START=$(cat "$sf" 2>/dev/null)
    [[ "$START" =~ ^[0-9]+$ ]] || continue
    AGE_MIN=$(( (NOW - START) / 60 ))
    [ "$AGE_MIN" -gt 48 ] && OVER=$((OVER + 1))
  done
  [ "$OVER" -eq 0 ] && SCORE=$((SCORE + 20))

  echo "Sessions detail: zombies=$ZOMBIES, orphans=$ORPHANS, over48min=$OVER"
  echo "SCORE: $SCORE/100"
}

# ── Comms (10%) — ported verbatim from super-health/SKILL.md §1g (Phase D-full) ──
score_comms() {
  local DB="$CLAUDE/comms/.broker.db"
  local SCORE=0
  if [ ! -f "$DB" ]; then
    echo "Comms: HCOM broker unavailable (Phase D requires broker; run hcom-init.sh)"
    echo "SCORE: 0/100"
    return
  elif ! command -v sqlite3 >/dev/null 2>&1; then
    echo "Comms: sqlite3 CLI required for Phase D-full"
    echo "SCORE: 0/100"
    return
  fi
  local UNANSWERED_ESC ACTIVE_ORKS BACKFILLED
  UNANSWERED_ESC=$(sqlite3 "$DB" "SELECT COUNT(*) FROM messages WHERE kind='ESC' AND read_at IS NULL AND ts < strftime('%s','now') - 1800;" 2>/dev/null || echo 999)
  [ "$UNANSWERED_ESC" -eq 0 ] 2>/dev/null && SCORE=$((SCORE + 30))

  # Stale-comms declutter (25 pts) — flag active comms dirs that are BOTH stale
  # (>7d since last file activity) AND marked complete (decommissioned / final DONE):
  # those belong in comms/_archive/. Replaces the old "outstanding DIR >4h" check,
  # which counted legacy/archived directives forever — owner tracks live orks directly
  # on his terminals, so liveness isn't the signal; decluttering completed threads is.
  # Deduct 5 per flagged dir, floor 0. (Operates on flat comms dirs now; becomes
  # DB-aware when comms moves to the FTS5+HTML DB — see plan Phase 2 / T7.1b.)
  local STALE_DONE=0 cdir cname newest age_d NOW_S sc_list=""
  NOW_S=$(date +%s)
  for cdir in "$CLAUDE"/comms/*/; do
    [ -d "$cdir" ] || continue
    cname=$(basename "$cdir")
    case "$cname" in _archive|meta) continue ;; esac
    grep -rqiE 'decommissioned|^\*\*Status\*\*:[[:space:]]*(DONE|COMPLETE)' "$cdir" 2>/dev/null || continue
    newest=$(find "$cdir" -type f -printf '%T@\n' 2>/dev/null | sort -nr | head -1 | cut -d. -f1)
    [ -z "$newest" ] && newest=$NOW_S
    age_d=$(( (NOW_S - newest) / 86400 ))
    if [ "$age_d" -gt 7 ]; then
      STALE_DONE=$((STALE_DONE + 1)); sc_list="$sc_list $cname(${age_d}d)"
    fi
  done
  local stale_pts=$(( 25 - 5 * STALE_DONE ))
  [ "$stale_pts" -lt 0 ] && stale_pts=0
  SCORE=$((SCORE + stale_pts))

  # Schema-lint (20 pts) — REAL deterministic batch-lint of the flat comms ledgers,
  # using the exact files + field rules that comms-schema-lint.sh enforces on write.
  # Scans every "## {DIR,RPT,ESC}-NNN" entry across all comms/*/ ledgers; an entry is
  # malformed if it lacks a required field for its kind. Points scale with
  # well-formed/total. Vacuous-full (20) ONLY when zero entries exist to lint
  # (reported honestly) — but a single malformed entry WILL deduct (real failing path).
  local sl_total=0 sl_bad=0 lf kind ln chunk miss sl_pts=20 sl_detail
  for lf in "$CLAUDE"/comms/*/directives.md "$CLAUDE"/comms/*/reports.md "$CLAUDE"/comms/*/escalations.md; do
    [ -f "$lf" ] || continue
    case "$lf" in
      */directives.md)  kind="DIR" ;;
      */reports.md)     kind="RPT" ;;
      */escalations.md) kind="ESC" ;;
      *)                continue ;;
    esac
    while IFS= read -r ln; do
      [ -n "$ln" ] || continue
      sl_total=$((sl_total + 1))
      chunk=$(sed -n "${ln},$((ln + 12))p" "$lf" 2>/dev/null)
      miss=0
      echo "$chunk" | grep -q "^\*\*Time\*\*:" || miss=1
      case "$kind" in
        DIR) echo "$chunk" | grep -q "^\*\*Project\*\*:" || miss=1 ;;
        RPT) echo "$chunk" | grep -q "^\*\*Directive\*\*:" || miss=1
             echo "$chunk" | grep -q "^\*\*Status\*\*:" || miss=1 ;;
        ESC) echo "$chunk" | grep -q "^\*\*Context\*\*:" || miss=1 ;;
      esac
      [ "$miss" -eq 1 ] && sl_bad=$((sl_bad + 1))
    done < <(grep -nE "^## ${kind}-[0-9]+" "$lf" 2>/dev/null | cut -d: -f1)
  done
  if [ "$sl_total" -eq 0 ]; then
    sl_detail="no flat-ledger entries to lint (full)"
  else
    sl_pts=$(( (sl_total - sl_bad) * 20 / sl_total ))
    sl_detail="$((sl_total - sl_bad))/$sl_total well-formed"
  fi
  SCORE=$((SCORE + sl_pts))

  ACTIVE_ORKS=$(sqlite3 "$DB" "SELECT COUNT(DISTINCT CASE WHEN to_agent LIKE '@%' THEN substr(to_agent, 2) ELSE from_agent END) FROM messages WHERE ts > strftime('%s','now') - 604800;" 2>/dev/null || echo 0)
  [ "$ACTIVE_ORKS" -le 5 ] 2>/dev/null && SCORE=$((SCORE + 15))

  BACKFILLED=$(sqlite3 "$DB" "SELECT COUNT(*) FROM backfill_audit;" 2>/dev/null || echo 0)
  [ "$BACKFILLED" -gt 0 ] 2>/dev/null && SCORE=$((SCORE + 10))

  # ── Comms search-store integrity facet (v3 Phase 2, T2.4) ──
  # In ADDITION to the broker-ledger scoring above, validate the FTS5+vec search
  # store at comms/.comms.db (built by scripts/memory/comms_db.py sync). Fast path
  # only: bash + sqlite3 CLI, NO python and NO embedding model. The deep
  # rows==fts==vec assertion (which needs the sqlite-vec extension to COUNT the vec
  # table) lives in infra-test.sh; here we check what plain sqlite3 can see.
  #   ABSENT  → N/A: no penalty (fail-safe for pre-comms-DB systems).
  #   HEALTHY → no penalty (cap stays 100; real DB has rows==fts==173).
  #   BROKEN  → genuine deduction: cap the whole comms component at 50.
  # COMMS_DB_PATH overrides the path (used by the failing-path test against a
  # throwaway COPY — the real .comms.db is never opened for write by this tool).
  local CDB="${COMMS_DB_PATH:-$CLAUDE/comms/.comms.db}"
  local db_detail db_cap=100
  if [ ! -f "$CDB" ]; then
    db_detail="not built (skip)"
  elif ! sqlite3 "$CDB" "SELECT 1;" >/dev/null 2>&1; then
    # File exists but is not openable as SQLite (corrupt / not a DB).
    db_detail="invalid SQLite -> CAP 50"
    db_cap=50
  else
    local cdb_tables cdb_rows cdb_fts t miss_tbl=""
    cdb_tables=$(sqlite3 "$CDB" "SELECT name FROM sqlite_master WHERE type='table';" 2>/dev/null)
    # Check PRESENCE of the three core tables via sqlite_master only — never
    # SELECT COUNT(*) FROM memories_vec from plain sqlite3 (needs sqlite-vec ext).
    for t in memories memories_fts memories_vec; do
      printf '%s\n' "$cdb_tables" | grep -qx "$t" || miss_tbl="$miss_tbl $t"
    done
    cdb_rows=$(sqlite3 "$CDB" "SELECT COUNT(*) FROM memories;" 2>/dev/null)
    cdb_fts=$(sqlite3 "$CDB" "SELECT COUNT(*) FROM memories_fts;" 2>/dev/null)
    [[ "$cdb_rows" =~ ^[0-9]+$ ]] || cdb_rows=0
    [[ "$cdb_fts"  =~ ^[0-9]+$ ]] || cdb_fts=0
    if [ -n "$miss_tbl" ]; then
      db_detail="missing table(s)$miss_tbl -> CAP 50"
      db_cap=50
    elif [ "$cdb_rows" -le 0 ]; then
      db_detail="memories empty (rows=$cdb_rows) -> CAP 50"
      db_cap=50
    elif [ "$cdb_fts" -ne "$cdb_rows" ]; then
      db_detail="fts!=memories (fts=$cdb_fts, rows=$cdb_rows) -> CAP 50"
      db_cap=50
    else
      db_detail="healthy (rows=$cdb_rows, fts=$cdb_fts)"
    fi
  fi
  # Apply the cap LAST so a broken search store is a real ceiling on the component,
  # not a cosmetic note. min(SCORE, db_cap).
  [ "$SCORE" -gt "$db_cap" ] && SCORE=$db_cap

  echo "Comms detail: unansweredESC=$UNANSWERED_ESC, staleDoneComms=$stale_pts/25 (${STALE_DONE} flagged:${sc_list:- none}), schemaLint=$sl_pts/20 ($sl_detail), activeOrks=$ACTIVE_ORKS, backfilled=$BACKFILLED, commsDB=$db_detail"
  echo "SCORE: $SCORE/100"
}

# ── Regression (15%) — run infra-test.sh, score = Pass/Total*100 ──
score_regression() {
  local s="$SCRIPTS/infra-test.sh"
  if [ ! -f "$s" ]; then echo "infra-test.sh missing"; echo "SCORE: 0/100"; return; fi
  # quick -> lighter --quick suite (SKILL.md §1f allows it); standard -> default full
  # suite; deep -> explicit --full. Always colorless for stable parsing.
  local args="--no-color"
  case "$EFFECTIVE_TIER" in
    quick) args="$args --quick" ;;
    deep)  args="$args --full" ;;
    *)     ;;   # standard: default suite
  esac
  local out summary total pass
  out=$(bash "$s" $args 2>&1)
  summary=$(echo "$out" | grep -E 'Tests:.*Pass:.*Fail:' | tail -1)
  total=$(echo "$summary" | grep -oE 'Tests: *[0-9]+' | grep -oE '[0-9]+' | head -1)
  pass=$(echo "$summary" | grep -oE 'Pass: *[0-9]+' | grep -oE '[0-9]+' | head -1)
  [ -z "$total" ] && total=0
  [ -z "$pass" ] && pass=0
  local sc=0
  [ "$total" -gt 0 ] 2>/dev/null && sc=$(( pass * 100 / total ))
  echo "Regression detail: ${summary:-no summary parsed} -> pass-rate $pass/$total"
  echo "SCORE: $sc/100"
}

# ── Subsystems (v3) — one sub-check per v3 feature, summed to /100 ───────────────
# Scales by $EFFECTIVE_TIER: quick = structural (files exist + fail-safe smokes);
# deep/complete = adds py_compile / value-sanity / dead-url probes; the heavy
# script-hygiene de-bake scan runs at the COMPLETE tier only.
#
# Scoring model (denominator-honest): two accumulators, AW (awarded) and PO
# (possible). A sub-check adds its weight to PO ONLY when it is in scope, and adds
# to AW what it actually earned. A feature that is GRACEFUL-ABSENT / N/A (super-mem,
# deferred to Phase 8) touches NEITHER accumulator, so its absence is excluded from
# the denominator — it can neither penalize nor silently inflate the score. EVERY
# in-scope sub-check has a real failing path (a missing artifact / malformed JSON /
# broken smoke earns 0 of its slice), so no sub-check is a flat-100.
# All checks are READ-ONLY: we never run cost-cache-refresh (it writes a live file),
# never edit/delete an inspected artifact, and use empty/garbage/--help stdin for
# every smoke probe.
score_subsystems() {
  local AW=0 PO=0 detail=""
  local deep=0
  [ "$EFFECTIVE_TIER" = "deep" ] && deep=1
  local complete=0
  [ "$TIER" = "complete" ] && complete=1

  # award <weight> <0|1 pass> <short-label>  → accrue into AW/PO + detail trail.
  ss_award() {
    local w="$1" ok="$2" lbl="$3"
    PO=$((PO + w))
    if [ "$ok" -eq 1 ]; then AW=$((AW + w)); detail="$detail ${lbl}:ok"
    else detail="$detail ${lbl}:FAIL"; fi
  }
  # ss_exists <weight> <label> <path...>  → 1 iff every path is a regular file.
  ss_exists() {
    local w="$1" lbl="$2"; shift 2
    local p ok=1
    for p in "$@"; do [ -f "$p" ] || ok=0; done
    ss_award "$w" "$ok" "$lbl"
  }
  # ss_pycompile <weight> <label> <file>  → deep-only py_compile gate.
  ss_pycompile() {
    local w="$1" lbl="$2" f="$3"
    [ "$deep" -eq 1 ] || return 0
    local ok=0
    [ -f "$f" ] && "$PYTHON" -m py_compile "$f" >/dev/null 2>&1 && ok=1
    ss_award "$w" "$ok" "$lbl"
  }
  # ss_json_keys <weight> <label> <file> <key...>  → valid JSON with every key.
  # READ-ONLY: parses, never writes. Missing file / bad JSON / missing key → FAIL.
  ss_json_keys() {
    local w="$1" lbl="$2" f="$3"; shift 3
    local ok=0
    if [ -f "$f" ]; then
      "$PYTHON" - "$f" "$@" <<'PY' >/dev/null 2>&1 && ok=1
import json, sys
f = sys.argv[1]; keys = sys.argv[2:]
d = json.load(open(f))
assert isinstance(d, dict)
for k in keys:
    assert k in d, k
PY
    fi
    ss_award "$w" "$ok" "$lbl"
  }

  local SK="$CLAUDE/skills" SC="$SCRIPTS" HK="$CLAUDE/hooks"

  # 1. render / viewer pipeline (the deleted swarm/subagent monitor is EXCLUDED).
  ss_exists 8 render "$SC/memory/render.py" "$SC/memory/viewer.py" "$SC/memory/comms_viewer.py"
  ss_pycompile 2 render-py "$SC/memory/render.py"
  ss_pycompile 2 viewer-py "$SC/memory/viewer.py"
  ss_pycompile 2 commsviewer-py "$SC/memory/comms_viewer.py"

  # 2. telemetry reader (FULL lane). NO swarm-monitor check.
  ss_exists 7 telemetry "$SC/statusline_telemetry.py" "$SC/statusline-telemetry.sh"
  # quick smoke: printf '{}' must give rc 0 AND non-blank stdout.
  local tout trc
  tout=$(printf '{}' | timeout 30 bash "$SC/statusline-telemetry.sh" 2>/dev/null); trc=$?
  local tsmoke=0
  { [ "$trc" -eq 0 ] && [ -n "$tout" ]; } && tsmoke=1
  ss_award 4 "$tsmoke" telemetry-smoke
  # deep: garbage stdin must STILL be rc 0 (fail-safe).
  if [ "$deep" -eq 1 ]; then
    printf 'not-json @#$%% garbage' | timeout 30 bash "$SC/statusline-telemetry.sh" >/dev/null 2>&1
    local trc2=$?   # capture BEFORE any `local x=0` (a `local` decl resets $?)
    local tg=0; [ "$trc2" -eq 0 ] && tg=1
    ss_award 3 "$tg" telemetry-garbage
  fi

  # 3. cost engine (FULL lane). Inspect schema files only — NEVER run the refresh
  #    script (it writes the live .cost-cache.json).
  ss_exists 6 cost-scripts "$SC/cost_cache_computer.py" "$SC/cost-cache-refresh.sh"
  ss_json_keys 3 cost-cache "$CLAUDE/.cost-cache.json" day_usd total_usd
  # .cost-ledger.json: date->usd map lives under the "days" object (schema 1).
  ss_json_keys 3 cost-ledger "$CLAUDE/.cost-ledger.json" days
  ss_json_keys 3 rate-latest "$CLAUDE/.rate-latest.json" five_hour_pct seven_day_pct
  # deep value-sanity: pcts in 0..100, all ledger day costs >= 0.
  if [ "$deep" -eq 1 ]; then
    local cs_ok=0
    "$PYTHON" - "$CLAUDE/.rate-latest.json" "$CLAUDE/.cost-ledger.json" "$CLAUDE/.cost-cache.json" <<'PY' >/dev/null 2>&1 && cs_ok=1
import json, sys
rate = json.load(open(sys.argv[1]))
for k in ("five_hour_pct", "seven_day_pct"):
    v = float(rate[k]); assert 0 <= v <= 100, (k, v)
ledger = json.load(open(sys.argv[2]))
days = ledger.get("days", {}); assert isinstance(days, dict)
for d, usd in days.items():
    assert float(usd) >= 0, (d, usd)
cache = json.load(open(sys.argv[3]))
for k in ("day_usd", "total_usd"):
    assert float(cache[k]) >= 0, (k, cache[k])
PY
    ss_award 3 "$cs_ok" cost-sanity
  fi

  # 4. nb-monitor.
  ss_exists 6 nb-monitor "$SC/nb-monitor.py" "$SK/nb-monitor/SKILL.md"
  ss_pycompile 2 nb-monitor-py "$SC/nb-monitor.py"

  # 5. latex-warn — existence + fail-safe (no-op invocation rc 0). Hook-health owns
  #    deeper hook scoring; here we only smoke the standalone fail-safe contract.
  ss_exists 4 latex-warn "$HK/latex-warn.sh"
  timeout 15 bash "$HK/latex-warn.sh" </dev/null >/dev/null 2>&1
  local lwrc=$?   # capture BEFORE any `local x=0` (a `local` decl resets $?)
  local lw=0; [ "$lwrc" -eq 0 ] && lw=1
  ss_award 3 "$lw" latex-warn-safe

  # 6. figure-validate.
  ss_exists 6 figure-validate "$SC/figure-validate.py" "$SK/figure-validate/SKILL.md"
  if [ "$deep" -eq 1 ]; then
    local fv=0
    { "$PYTHON" -m py_compile "$SC/figure-validate.py" >/dev/null 2>&1 \
        && timeout 20 "$PYTHON" "$SC/figure-validate.py" --help >/dev/null 2>&1; } && fv=1
    ss_award 3 "$fv" figure-validate-help
  fi

  # 7. experiment-harness.
  ss_exists 6 experiment-harness "$SC/experiment-harness.py" "$SK/experiment-harness/SKILL.md"
  if [ "$deep" -eq 1 ]; then
    local eh=0
    { "$PYTHON" -m py_compile "$SC/experiment-harness.py" >/dev/null 2>&1 \
        && timeout 20 "$PYTHON" "$SC/experiment-harness.py" --help >/dev/null 2>&1; } && eh=1
    ss_award 3 "$eh" experiment-harness-help
  fi

  # 8. better-super. NOTE: better-super-deps.sh lives at scripts/, not skills/.
  ss_exists 6 better-super \
    "$SC/better_super_crawl.py" "$SC/better_super_deps.py" \
    "$CLAUDE/dependencies.yml" "$SK/better-super/SKILL.md"
  if [ "$deep" -eq 1 ]; then
    timeout 30 bash "$SC/better-super-deps.sh" --list >/dev/null 2>&1
    local bdrc=$?   # capture BEFORE any `local x=0` (a `local` decl resets $?)
    local bd=0; [ "$bdrc" -eq 0 ] && bd=1
    ss_award 2 "$bd" better-super-deps
    # crawl against a DEAD url must be fail-safe: NO 'Traceback' in stderr (rc may
    # be non-zero by design). URL is passed via --md (crawl uses flags, not argv).
    local cerr
    cerr=$(timeout 30 "$PYTHON" "$SC/better_super_crawl.py" --md \
      "http://nonexistent.invalid.localhost.dead:9/x" 2>&1 >/dev/null)
    local bc=0; printf '%s' "$cerr" | grep -q "Traceback" || bc=1
    ss_award 2 "$bc" better-super-crawl-safe
  fi

  # 9. super-mem — GRACEFUL-ABSENT (deferred to Phase 8). Until skills/super-mem/
  #    SKILL.md exists, it is N/A: it touches NEITHER AW nor PO (excluded from the
  #    denominator) so it cannot penalize the score NOR silently inflate it. Once
  #    the skill lands, this becomes a real in-scope ss_exists sub-check.
  local sm_detail="N/A(deferred)"
  if [ -f "$SK/super-mem/SKILL.md" ]; then
    ss_exists 4 super-mem "$SK/super-mem/SKILL.md"; sm_detail="present"
  fi

  # 10. script-hygiene (COMPLETE tier ONLY — heavy). Operationalizes the de-bake
  #     rule: scan ~/.claude/**/*.sh for >2 CONSECUTIVE lines of BAKED foreign
  #     language (a heredoc piped into python/node, or a multi-line `python3 -c`/
  #     `node -e` block). Each violating file deducts. READ-ONLY scan (never edits).
  #     Self excluded: this scorer itself uses bounded <<'PY' heredocs INSIDE the
  #     helper fns above, but those are <=2 effective body lines per probe and the
  #     scan threshold is >2 consecutive baked lines; super-health.sh is excluded by
  #     name regardless to avoid the yardstick policing its own measurement probes.
  if [ "$complete" -eq 1 ]; then
    local sh_total=0 sh_bad=0 shf
    while IFS= read -r shf; do
      case "$shf" in */super-health.sh) continue ;; esac
      sh_total=$((sh_total + 1))
      if "$PYTHON" - "$shf" <<'PY' 2>/dev/null
import re, sys
src = open(sys.argv[1], encoding="utf-8", errors="replace").read().splitlines()
# A baked block = a run of >2 consecutive lines that are clearly foreign-language
# body, entered via one of: heredoc piped to python/node, `python3 -c "`/`node -e "`
# opener (then continued lines), or a `| python3`/`| node` consume. We detect the
# OPENER then count consecutive non-blank, non-shell-delimiter continuation lines.
opener = re.compile(
    r'(<<-?\s*[\'"]?\w+[\'"]?\s*$.*(python3?|node))'      # heredoc whose tag feeds py/node (loose)
    r'|(python3?\s+-c\s*[\'"])'                            # python3 -c "  (inline, often multi-line)
    r'|(node\s+-e\s*[\'"])'                                # node -e "
    r'|(\|\s*(python3?|node)\b)')                          # ... | python3
heredoc_open = re.compile(r'<<-?\s*[\'"]?(\w+)[\'"]?')
i, n = 0, len(src)
worst = 0
while i < n:
    line = src[i]
    # Case A: heredoc piped to python/node — count body lines until the closing tag.
    m = heredoc_open.search(line)
    if m and re.search(r'(python3?|node)', line):
        tag = m.group(1); j = i + 1; run = 0
        while j < n and src[j].strip() != tag:
            if src[j].strip():
                run += 1
            j += 1
        worst = max(worst, run)
        i = j + 1; continue
    # Case B: `python3 -c "` / `node -e "` opener not closed on the same line.
    # run counts BAKED BODY lines only — the opener line is the shell invocation,
    # not foreign-language body, so start at 0 (counting it would off-by-one a
    # 2-body-line block into a false >2 violation).
    if re.search(r'(python3?\s+-c|node\s+-e)\s*[\'"]', line) and line.count('"') % 2 == 1:
        j = i + 1; run = 0
        while j < n and src[j].count('"') % 2 == 0 and src[j].strip():
            run += 1; j += 1
        worst = max(worst, run)
        i = j + 1; continue
    i += 1
# >2 consecutive baked body lines = a violation.
sys.exit(1 if worst > 2 else 0)
PY
      then :; else sh_bad=$((sh_bad + 1)); fi
    done < <(find "$CLAUDE" -type f -name '*.sh' 2>/dev/null)
    local hyg_ok=0; [ "$sh_bad" -eq 0 ] && hyg_ok=1
    ss_award 6 "$hyg_ok" script-hygiene
    detail="$detail script-hygiene:${sh_bad}-baked/${sh_total}"
  fi

  # ── Roll up. Guard PO>0 (always true: structural checks are tier-independent). ──
  local sc=0
  [ "$PO" -gt 0 ] && sc=$(( AW * 100 / PO ))
  [ "$sc" -gt 100 ] && sc=100
  echo "Subsystems detail (tier=$EFFECTIVE_TIER, awarded=$AW/$PO): super-mem=$sm_detail;$detail"
  echo "SCORE: $sc/100"
}

# ════════════════════════════════════════════════════════════════════════════
#  Tier banner + budget warnings
# ════════════════════════════════════════════════════════════════════════════
echo "## Superclaude Health Report"
echo ""
echo "**Tier**: $TIER"
case "$TIER" in
  deep)     echo "_Warning: --deep runs infra-test.sh --full + deep hook checks (~20 min of model time if narrated). Mind the session budget._" ;;
  complete) echo "_Warning: --complete = --deep scoring PLUS a model-driven 5-agent post-hoc audit. Mind session budget AND helper usage._" ;;
esac
echo ""

# ── Assert weights sum to 1.00 (defensive; informational if not) ──
WSUM=$(awk -v vals="$(for k in "${COMPONENT_ORDER[@]}"; do printf '%s ' "${WEIGHTS[$k]}"; done)" \
  'BEGIN{s=0;n=split(vals,a," ");for(i=1;i<=n;i++)s+=a[i];printf "%.2f", s}')
if [ "$WSUM" != "1.00" ]; then
  echo "_WARNING: component weights sum to $WSUM, not 1.00 — aggregate will be miscalibrated._"
  echo ""
fi

# ════════════════════════════════════════════════════════════════════════════
#  Run each component, capture its /100, accumulate weighted total.
# ════════════════════════════════════════════════════════════════════════════
declare -A COMP_SCORE
declare -A COMP_DETAIL

for key in "${COMPONENT_ORDER[@]}"; do
  out=$("score_${key}")
  sc=$(echo "$out" | extract_score)
  COMP_SCORE[$key]=$sc
  # Reuse the SAME capture: the rich metric lines are everything EXCEPT the
  # SCORE: contract line (display-only; scores/weights/aggregate are unaffected).
  COMP_DETAIL[$key]=$(printf '%s\n' "$out" | grep -v '^SCORE:')
done

echo "### Component Scores"
echo ""
echo "| Component | Weight | Score | Weighted |"
echo "|-----------|--------|-------|----------|"
for key in "${COMPONENT_ORDER[@]}"; do
  w="${WEIGHTS[$key]}"
  sc="${COMP_SCORE[$key]}"
  weighted=$(awk -v s="$sc" -v w="$w" 'BEGIN{printf "%.2f", s*w}')
  wpct=$(awk -v w="$w" 'BEGIN{printf "%d%%", w*100}')
  printf "| %s | %s | %d/100 | %s |\n" "${COMPONENT_LABEL[$key]}" "$wpct" "$sc" "$weighted"
done

# ── Final weighted aggregate: sum(score*weight), rounded to nearest int ──
FINAL=$(
  for key in "${COMPONENT_ORDER[@]}"; do
    printf '%s %s\n' "${COMP_SCORE[$key]}" "${WEIGHTS[$key]}"
  done | awk '{ t += $1 * $2 } END { printf "%d", (t + 0.5) }'
)
[ -z "$FINAL" ] && FINAL=0
[ "$FINAL" -lt 0 ] && FINAL=0
[ "$FINAL" -gt 100 ] && FINAL=100

# Sum-of-weighted (for the Total row), 1-decimal.
TOTAL_WEIGHTED=$(
  for key in "${COMPONENT_ORDER[@]}"; do
    printf '%s %s\n' "${COMP_SCORE[$key]}" "${WEIGHTS[$key]}"
  done | awk '{ t += $1 * $2 } END { printf "%.1f", t }'
)
printf "| **Total** | **100%%** | | **%s** |\n" "$TOTAL_WEIGHTED"

# ════════════════════════════════════════════════════════════════════════════
#  Component Details — DISPLAY ONLY. Surfaces each component's non-SCORE output
#  lines (the rich metrics the table discards): memory's full 6-criterion
#  mem-health table + the two facet lines (.memory.db + corpus footprint/penalty),
#  Settings+Agents detail, Sessions detail, Comms detail, Regression/Subsystems
#  detail, etc. Rendered by default on every tier (standard/deep/complete AND the
#  default quick run, so the metrics are always visible). Touches no
#  score/weight/aggregate/grade — the final "SCORE: N/100" contract is unchanged.
# ════════════════════════════════════════════════════════════════════════════
echo ""
echo "### Component Details"
for key in "${COMPONENT_ORDER[@]}"; do
  d="${COMP_DETAIL[$key]}"
  echo ""
  echo "**${COMPONENT_LABEL[$key]}**"
  if [ -z "${d//[$' \t\n']/}" ]; then
    echo "_(no detail lines emitted)_"
  else
    printf '%s\n' "$d"
  fi
done

# ── Grade ──
grade() {
  local s="$1"
  if   [ "$s" -ge 90 ]; then echo "A (Production-ready)"
  elif [ "$s" -ge 80 ]; then echo "B (Healthy, minor issues)"
  elif [ "$s" -ge 70 ]; then echo "C (Functional, needs attention)"
  elif [ "$s" -ge 60 ]; then echo "D (Degraded, fix before expanding)"
  else echo "F (Broken, immediate action needed)"
  fi
}

echo ""
echo "**Aggregate: $FINAL/100 — Grade: $(grade "$FINAL")**"

# ── --complete: print the post-hoc audit instruction (script does NOT spawn) ──
if [ "$TIER" = "complete" ]; then
  echo ""
  echo "### Post-Hoc Audit (--complete)"
  echo "NOTE: the model must now run the Step-4 5-agent post-hoc audit per"
  echo "skills/super-health/SKILL.md — dispatch 5 parallel general-purpose audit"
  echo "agents across {rules, agents, skills, hooks, comms+memory}, each capped at"
  echo "600 words, categorizing findings as (a) Replication, (b) DRY/Atomization,"
  echo "(c) >=Medium ROI, then synthesize into a prioritized optimization queue."
  echo "This script intentionally does NOT spawn agents."
fi

echo ""
echo "SCORE: $FINAL/100"
exit 0
