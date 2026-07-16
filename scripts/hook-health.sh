#!/bin/bash
# hook-health.sh — deterministic implementation of the /hook-health rubric.
# Canonical runner behind skills/hook-health/SKILL.md (9 weighted criteria).
#
# Usage:
#   bash hook-health.sh [--quick|--standard|--deep] [hook-name|all]
#
# Tiers:
#   --quick     criteria 1-6, 9 (read-only, ~1s)        [default]
#   --standard  + criteria 7 (graceful degradation), 8 (test coverage)
#   --deep      same scoring as --standard (deep agent fuzzing is the model's
#               job per SKILL.md; this script never spawns agents)
#
# Contract: prints a per-criterion breakdown, and as its FINAL stdout line,
# exactly: "SCORE: <int>/100".  Exit code is ALWAYS 0 (reporting tool, not a gate).
#
# Determinism: same hook tree => identical score. No timestamps in the score path.
# Defensive: a missing file/dir/tool yields 0 for THAT check, never a crash.

set -uo pipefail   # NEVER set -e (a single failed sub-check must not abort scoring;
                   #  also: criterion 9 itself penalizes `set -e` in hooks)

CLAUDE="${CLAUDE_DIR:-$HOME/.claude}"
HOOKS_DIR="$CLAUDE/hooks"
MODULES_DIR="$HOOKS_DIR/modules"
TEST_HOOKS="$CLAUDE/scripts/test-hooks.sh"
DISPATCHER="$HOOKS_DIR/session-timer.sh"

# ── Args ──
TIER="quick"
for arg in "$@"; do
  case "$arg" in
    --quick)    TIER="quick" ;;
    --standard) TIER="standard" ;;
    --deep)     TIER="deep" ;;
    all|"")     ;;          # whole-subsystem scope is the only supported scope here
    *)          ;;          # ignore a specific hook-name arg: scoring is subsystem-wide
  esac
done

# ── Helpers ──
# all_hooks: every scored shell file (top-level dispatchers/hooks + numbered modules).
all_hooks() {
  local f
  for f in "$HOOKS_DIR"/*.sh; do [ -f "$f" ] && echo "$f"; done
  for f in "$MODULES_DIR"/*.sh; do [ -f "$f" ] && echo "$f"; done
}
# top_hooks: only the top-level *.sh (entrypoints) — used for the perf check, which
# pipes mock JSON to entrypoints (modules define functions only, they are not entrypoints).
top_hooks() {
  local f
  for f in "$HOOKS_DIR"/*.sh; do [ -f "$f" ] && echo "$f"; done
}

# pct_points <count_ok> <count_total> <max_points>  -> integer points (floor), 0 if total==0
pct_points() {
  local ok="$1" total="$2" max="$3"
  [ "$total" -gt 0 ] 2>/dev/null || { echo 0; return; }
  echo $(( ok * max / total ))
}

SCORE=0
echo "## Hook Health Report (tier: $TIER)"
echo ""
echo "| # | Criterion | Score | Detail |"
echo "|---|-----------|-------|--------|"

# ─────────────────────────────────────────────────────────────
# Criterion 1: Syntax (bash -n) — 20 pts — passing/total*20 — all tiers
# ─────────────────────────────────────────────────────────────
c1_total=0; c1_ok=0
while IFS= read -r f; do
  [ -n "$f" ] || continue
  c1_total=$((c1_total + 1))
  bash -n "$f" 2>/dev/null && c1_ok=$((c1_ok + 1))
done < <(all_hooks)
c1=$(pct_points "$c1_ok" "$c1_total" 20)
SCORE=$((SCORE + c1))
echo "| 1 | Syntax (bash -n) | $c1/20 | $c1_ok/$c1_total pass |"

# ─────────────────────────────────────────────────────────────
# Criterion 2: Permissions (+x) — 10 pts — executable/total*10 — all tiers
# ─────────────────────────────────────────────────────────────
c2_total=0; c2_ok=0
while IFS= read -r f; do
  [ -n "$f" ] || continue
  c2_total=$((c2_total + 1))
  [ -x "$f" ] && c2_ok=$((c2_ok + 1))
done < <(all_hooks)
c2=$(pct_points "$c2_ok" "$c2_total" 10)
SCORE=$((SCORE + c2))
echo "| 2 | Permissions (+x) | $c2/10 | $c2_ok/$c2_total executable |"

# ─────────────────────────────────────────────────────────────
# Criterion 3: Performance <500ms — 15 pts — fast/total*15 — all tiers
#   Pipes mock JSON to each top-level hook (entrypoints), measures wall time.
#   timeout 1s guards against a hook blocking on stdin/long work.
# ─────────────────────────────────────────────────────────────
c3_total=0; c3_ok=0; c3_slow=""
# Redirect pre-compact snapshots to a tempdir so perf tests don't pollute the real dir.
_c3_snap_tmp=$(mktemp -d "${TMPDIR:-/tmp}/hook-health-snap.XXXXXX" 2>/dev/null)
export COMPACT_SNAPSHOT_DIR="$_c3_snap_tmp"
while IFS= read -r f; do
  [ -n "$f" ] || continue
  c3_total=$((c3_total + 1))
  start=$(date +%s%N)
  echo '{"session_id":"hook-health-perf","tool_name":"Read"}' | timeout 1 bash "$f" >/dev/null 2>&1
  end=$(date +%s%N)
  ms=$(( (end - start) / 1000000 ))
  if [ "$ms" -lt 500 ]; then
    c3_ok=$((c3_ok + 1))
  else
    c3_slow="$c3_slow $(basename "$f")(${ms}ms)"
  fi
done < <(top_hooks)
# Clean any timer artifacts the mock perf run may have created (determinism + hygiene).
rm -f "$CLAUDE/session-timers/hook-health-perf".* 2>/dev/null
rm -rf "$_c3_snap_tmp" 2>/dev/null; unset _c3_snap_tmp COMPACT_SNAPSHOT_DIR
c3=$(pct_points "$c3_ok" "$c3_total" 15)
SCORE=$((SCORE + c3))
echo "| 3 | Performance <500ms | $c3/15 | $c3_ok/$c3_total fast${c3_slow:+; slow:$c3_slow} |"

# ─────────────────────────────────────────────────────────────
# Criterion 4: No hardcoded paths — 10 pts — clean/total*10 — all tiers
#   "Hardcoded" = a literal absolute home path baked into the file
#   (e.g. /home/<user>/.claude/...). Using the $HOME *variable* is the CORRECT,
#   non-hardcoded pattern and must NOT be penalized. Per SKILL.md the quick-check
#   is `grep -rn "$HOME"` — double-quoted, so $HOME EXPANDS to the literal home
#   path; a file is "dirty" iff it contains that expanded path. (Prior version
#   grepped the literal text "$HOME" and wrongly flagged correct variable usage.)
# ─────────────────────────────────────────────────────────────
c4_total=0; c4_clean=0; c4_dirty=""
while IFS= read -r f; do
  [ -n "$f" ] || continue
  c4_total=$((c4_total + 1))
  if grep -qF "$HOME/" "$f" 2>/dev/null; then
    c4_dirty="$c4_dirty $(basename "$f")"
  else
    c4_clean=$((c4_clean + 1))
  fi
done < <(all_hooks)
c4=$(pct_points "$c4_clean" "$c4_total" 10)
SCORE=$((SCORE + c4))
echo "| 4 | No hardcoded paths | $c4/10 | $c4_clean/$c4_total clean (\$HOME refs in:${c4_dirty:- none}) |"

# ─────────────────────────────────────────────────────────────
# Criterion 5: Cleanup SOT (10 pts, all tiers)
#   Invariant: per-session timer markers are deleted through ONE glob source of
#   truth (hooks/lib.sh::rm_session_files, which globs "<sid>".*), and every
#   consumer DELEGATES to it instead of enumerating extensions itself.
#   The retired check asserted the OPPOSITE (session-cleanup.sh must NAME each
#   extension). That scored the architecture backwards: it rewarded the very
#   per-consumer hardcoded lists the glob SOT replaced, and would have scored a
#   regression back to them as an improvement. Its own list had drifted too (it
#   demanded "context-warned"; the real marker is ".context-compact-warned",
#   written only by modules/05-context-check.sh), and the partial credit it did
#   award came from READ sites, not delete sites.
#   Sub-checks: 5a SOT is a glob (3); 5b consumers delegate (3);
#               5c no enumerated per-extension deletion has reappeared (4).
#   Consumers are DISCOVERED by grep, never hardcoded, so a new consumer is
#   scored the moment it lands. Full-line comments are stripped before matching,
#   so prose mentioning a helper is never miscounted as a call site.
# ─────────────────────────────────────────────────────────────
LIB="$HOOKS_DIR/lib.sh"
# A per-session marker path: a session-id-ish variable immediately followed by a dot.
_c5_sid='\$\{?[A-Za-z_]*([Ss][Ii][Dd]|SESSION_ID|session_id)\}?"?\.'
# An `rm` of such a path. Correct: the dot is followed by the `*` glob. Enumerated
# (the regression): the dot is followed by a literal ext, a brace list, or a loop var.
_c5_rm="(^|[[:space:];&|(])rm[[:space:]][^;|&]*"
_c5_glob_re="${_c5_rm}${_c5_sid}\\*"
_c5_enum_re="${_c5_rm}${_c5_sid}[^*[:space:]\"]"
# A real call site: the helper at a COMMAND position with an argument. Anchoring to
# command position (not mere presence) excludes its definition, and excludes prose or
# a string literal that happens to name it (this scorer's own detail strings do).
_c5_call_re='(^[[:space:]]*|[;&|(][[:space:]]*)rm_session_files[[:space:]]+["$A-Za-z_]'
# Scope: hooks, modules, and scripts (session-reaper.sh is a consumer and lives there).
c5_files() {
  local f
  for f in "$HOOKS_DIR"/*.sh "$MODULES_DIR"/*.sh "$CLAUDE"/scripts/*.sh; do
    [ -f "$f" ] && echo "$f"
  done
}
# c5_code <file>: file content minus full-line comments.
c5_code() { grep -vE '^[[:space:]]*#' "$1" 2>/dev/null; }

# 5a: the SOT exists, glob-deletes, and does not enumerate.
c5a=0; c5a_d="lib.sh missing"
if [ -f "$LIB" ]; then
  if ! c5_code "$LIB" | grep -qE '^[[:space:]]*rm_session_files\(\)'; then
    c5a_d="SOT rm_session_files not defined"
  elif ! c5_code "$LIB" | grep -qE "$_c5_glob_re"; then
    c5a_d="SOT does not glob-delete <sid>.*"
  elif c5_code "$LIB" | grep -qE "$_c5_enum_re"; then
    c5a_d="SOT enumerates extensions"
  else
    c5a=3; c5a_d="glob SOT ok"
  fi
fi

# 5b: every DISCOVERED consumer delegates to the SOT rather than deleting markers itself.
c5b_total=0; c5b_ok=0; c5b_bad=""
while IFS= read -r f; do
  [ -n "$f" ] || continue
  [ "$f" = "$LIB" ] && continue            # the SOT is the implementation, not a consumer
  _code=$(c5_code "$f")
  _calls=0; _deletes=0
  echo "$_code" | grep -qE "$_c5_call_re" && _calls=1
  echo "$_code" | grep -qE "${_c5_glob_re}|${_c5_enum_re}" && _deletes=1
  [ "$_calls" -eq 0 ] && [ "$_deletes" -eq 0 ] && continue   # not in the cleanup path
  c5b_total=$((c5b_total + 1))
  if [ "$_calls" -eq 1 ] && [ "$_deletes" -eq 0 ]; then
    c5b_ok=$((c5b_ok + 1))
  else
    c5b_bad="$c5b_bad $(basename "$f")"
  fi
done < <(c5_files)
c5b=$(pct_points "$c5b_ok" "$c5b_total" 3)   # 0 consumers => 0 pts (an orphaned SOT is a defect)

# 5c: ANTI-REGRESSION. A hardcoded per-extension deletion list anywhere in the
# cleanup path fails outright. This is the check that would have caught the drift.
c5c=0; c5c_hits=""
while IFS= read -r f; do
  [ -n "$f" ] || continue
  c5_code "$f" | grep -qE "$_c5_enum_re" && c5c_hits="$c5c_hits $(basename "$f")"
done < <(c5_files)
if [ -z "$c5c_hits" ]; then
  c5c=4; c5c_d="no enumerated deletion"
else
  c5c_d="ENUMERATED deletion in:$c5c_hits"
fi

c5=$((c5a + c5b + c5c))
SCORE=$((SCORE + c5))
echo "| 5 | Cleanup SOT | $c5/10 | $c5a_d; $c5b_ok/$c5b_total consumers delegate${c5b_bad:+ (bad:$c5b_bad)}; $c5c_d |"

# ─────────────────────────────────────────────────────────────
# Criterion 6: Module naming NN-*.sh — 5 pts — compliant/total*5 — all tiers
# ─────────────────────────────────────────────────────────────
c6_total=0; c6_ok=0; c6_bad=""
for f in "$MODULES_DIR"/*.sh; do
  [ -f "$f" ] || continue
  c6_total=$((c6_total + 1))
  if basename "$f" | grep -qE '^[0-9]{2}-'; then
    c6_ok=$((c6_ok + 1))
  else
    c6_bad="$c6_bad $(basename "$f")"
  fi
done
c6=$(pct_points "$c6_ok" "$c6_total" 5)
SCORE=$((SCORE + c6))
echo "| 6 | Module naming NN-*.sh | $c6/5 | $c6_ok/$c6_total compliant (bad:${c6_bad:- none}) |"

# ─────────────────────────────────────────────────────────────
# Criterion 7: Graceful degradation — 10 pts — std+ — BINARY
#   Property: dispatcher exits 0 when a module is missing.
#   Deterministic + non-destructive: copy the hooks tree to a tempdir, delete one
#   module from the COPY, run the copied dispatcher with mock JSON, assert exit 0.
#   (Never mutates the live hooks tree; never spawns an agent.)
# ─────────────────────────────────────────────────────────────
if [ "$TIER" = "standard" ] || [ "$TIER" = "deep" ]; then
  c7=0; c7_detail="not run"
  if [ -f "$DISPATCHER" ]; then
    tmp=$(mktemp -d "${TMPDIR:-/tmp}/hook-health-graceful.XXXXXX" 2>/dev/null)
    if [ -n "$tmp" ] && [ -d "$tmp" ]; then
      cp -r "$HOOKS_DIR"/. "$tmp"/ 2>/dev/null
      # Remove the first numbered module from the copy to simulate a missing module.
      victim=$(ls "$tmp"/modules/[0-9]*.sh 2>/dev/null | head -1)
      [ -n "$victim" ] && rm -f "$victim"
      _c7_snap_tmp=$(mktemp -d "${TMPDIR:-/tmp}/hook-health-snap7.XXXXXX" 2>/dev/null)
      echo '{"session_id":"hook-health-graceful","tool_name":"Read"}' \
        | COMPACT_SNAPSHOT_DIR="$_c7_snap_tmp" timeout 5 bash "$tmp/session-timer.sh" >/dev/null 2>&1
      rc=$?
      rm -rf "$tmp" "$_c7_snap_tmp" 2>/dev/null; unset _c7_snap_tmp
      rm -f "$CLAUDE/session-timers/hook-health-graceful".* 2>/dev/null
      if [ "$rc" -eq 0 ]; then
        c7=10; c7_detail="dispatcher exit 0 with missing module ($(basename "${victim:-?}"))"
      else
        c7_detail="dispatcher exit $rc with missing module (NOT graceful)"
      fi
    else
      c7_detail="tempdir creation failed"
    fi
  else
    c7_detail="dispatcher $DISPATCHER not found"
  fi
  SCORE=$((SCORE + c7))
  echo "| 7 | Graceful degradation | $c7/10 | $c7_detail |"
fi

# ─────────────────────────────────────────────────────────────
# Criterion 8: Test coverage — 15 pts — std+ — tested/total*15
#   "tested" = hook/module basename referenced by name in test-hooks.sh.
# ─────────────────────────────────────────────────────────────
if [ "$TIER" = "standard" ] || [ "$TIER" = "deep" ]; then
  c8_total=0; c8_ok=0; c8_untested=""
  if [ -f "$TEST_HOOKS" ]; then
    while IFS= read -r f; do
      [ -n "$f" ] || continue
      c8_total=$((c8_total + 1))
      bn=$(basename "$f")
      if grep -qF "$bn" "$TEST_HOOKS" 2>/dev/null; then
        c8_ok=$((c8_ok + 1))
      else
        c8_untested="$c8_untested $bn"
      fi
    done < <(all_hooks)
    c8=$(pct_points "$c8_ok" "$c8_total" 15)
    c8_detail="$c8_ok/$c8_total referenced in test-hooks.sh (untested:${c8_untested:- none})"
  else
    c8=0
    c8_detail="test-hooks.sh not found"
  fi
  SCORE=$((SCORE + c8))
  echo "| 8 | Test coverage | $c8/15 | $c8_detail |"
fi

# ─────────────────────────────────────────────────────────────
# Criterion 9: No set -e — 5 pts — all tiers — BINARY (any match => 0)
# ─────────────────────────────────────────────────────────────
c9_hits=$(grep -rlE '^[[:space:]]*set -e([[:space:]]|$)' "$HOOKS_DIR"/*.sh "$MODULES_DIR"/*.sh 2>/dev/null)
if [ -z "$c9_hits" ]; then
  c9=5; c9_detail="no 'set -e' found"
else
  c9=0
  c9_detail="found in: $(echo "$c9_hits" | xargs -n1 basename 2>/dev/null | tr '\n' ' ')"
fi
SCORE=$((SCORE + c9))
echo "| 9 | No set -e | $c9/5 | $c9_detail |"

# ── Tier note ──
if [ "$TIER" = "quick" ]; then
  echo ""
  echo "_Note: criteria 7 (graceful degradation, 10pts) and 8 (test coverage, 15pts) are"
  echo " --standard/--deep only; in --quick the max attainable is 75. Reported score below"
  echo " is out of the 75 quick-tier points, normalized to /100 for the contract._"
  # Quick tier max is 75 (criteria 1-6 + 9). Normalize to /100 so the aggregator's
  # weighting math stays consistent regardless of tier.
  SCORE=$(( (SCORE * 100 + 37) / 75 ))   # +37 ≈ round-half-up of /75
  [ "$SCORE" -gt 100 ] && SCORE=100
fi

echo ""
[ "$SCORE" -lt 0 ] && SCORE=0
[ "$SCORE" -gt 100 ] && SCORE=100
echo "SCORE: $SCORE/100"
exit 0
