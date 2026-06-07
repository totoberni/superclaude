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
# Criterion 5: Cleanup patterns — 10 pts — covered/total_temp_types*10 — all tiers
#   Temp file extensions the cleanup hook must reference (per SKILL.md).
# ─────────────────────────────────────────────────────────────
CLEANUP="$HOOKS_DIR/session-cleanup.sh"
c5_total=0; c5_ok=0; c5_miss=""
for ext in start agent pid override context-warned tdd calls; do
  c5_total=$((c5_total + 1))
  if [ -f "$CLEANUP" ] && grep -qE "\.$ext|[{,]${ext}([},])" "$CLEANUP" 2>/dev/null; then
    c5_ok=$((c5_ok + 1))
  else
    c5_miss="$c5_miss $ext"
  fi
done
c5=$(pct_points "$c5_ok" "$c5_total" 10)
SCORE=$((SCORE + c5))
echo "| 5 | Cleanup patterns | $c5/10 | $c5_ok/$c5_total exts (missing:${c5_miss:- none}) |"

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
