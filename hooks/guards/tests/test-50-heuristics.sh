#!/usr/bin/env bash
# Bite-test for 50-heuristics (F7, design/enforcement-gap-ledger.md Family 7 #24/#25).
# ISOLATED unit test: sources lib-guard.sh + 50-heuristics.sh directly and drives the
# guard functions in-process (no guard-dispatch.sh, no repo mutation, /tmp only).
#   - guard_heuristics (pre)  driven via run_guard guard_heuristics
#   - guardpost_heuristics (post) called directly with GUARD_PHASE=post
#
# Cases:
#   (a) Grep + head_limit=10                -> exit 0 + a head-limit WARN
#   (b) Grep, no head_limit                 -> exit 0, no warn
#   (c) non-Grep tool (Bash)                -> exit 0, no warn
#   (d) 21 PostToolUse calls, fixed session -> exactly ONE truncation WARN
#   (e) SUPERCLAUDE_GUARDS=off              -> silence on both guards

set -uo pipefail

TESTDIR="$(cd "$(dirname "$0")" && pwd)"
GUARDS_DIR="$TESTDIR/.."

TMPD="$(mktemp -d "${TMPDIR:-/tmp}/heuristics-bite.XXXXXX")"
trap 'rm -rf "$TMPD"' EXIT
export TMPDIR="$TMPD"

# shellcheck source=/dev/null
. "$GUARDS_DIR/lib-guard.sh"
# shellcheck source=/dev/null
. "$GUARDS_DIR/50-heuristics.sh"

fails=0

# mk_stdin <tool_name> <tool_input_json> <session_id>
mk_stdin() {
  jq -nc --arg t "$1" --argjson ti "$2" --arg sid "$3" \
    '{session_id:$sid, tool_name:$t, tool_input:$ti}'
}

# run_pre <label> <stdin_json> <must|""> <mustnot|""> [ENV=VAL ...]
# Drives guard_heuristics in a subshell (so ENV overrides never leak between cases).
run_pre() {
  local label="$1" stdin_json="$2" must="$3" mustnot="$4"; shift 4
  local err_file rc
  err_file="$(mktemp "$TMPD/err.XXXXXX")"
  (
    for kv in "$@"; do export "${kv?}"; done
    guard_init "$stdin_json"
    run_guard guard_heuristics
  ) 2>"$err_file"
  rc=$?
  local ok=1
  [ "$rc" -eq 0 ] || { ok=0; echo "    rc=$rc want=0"; }
  if [ -n "$must" ] && ! grep -q "$must" "$err_file"; then
    ok=0; echo "    stderr missing: '$must'"; echo "    stderr was: $(cat "$err_file")"
  fi
  if [ -n "$mustnot" ] && grep -q "$mustnot" "$err_file"; then
    ok=0; echo "    stderr unexpectedly matched: '$mustnot'"; echo "    stderr was: $(cat "$err_file")"
  fi
  if [ "$ok" -eq 1 ]; then
    echo "  PASS: $label"
  else
    echo "  FAIL: $label"
    fails=$((fails + 1))
  fi
}

echo "=== test-50-heuristics ==="

JSON_A=$(mk_stdin "Grep" '{"pattern":"foo","head_limit":10}' "pre-a")
run_pre "(a) Grep + head_limit=10 -> exit 0 + head-limit WARN" \
  "$JSON_A" "head-limited grep" ""

JSON_B=$(mk_stdin "Grep" '{"pattern":"foo"}' "pre-b")
run_pre "(b) Grep, no head_limit -> exit 0, no warn" \
  "$JSON_B" "" "GUARD-WARN"

JSON_C=$(mk_stdin "Bash" '{"command":"ls"}' "pre-c")
run_pre "(c) non-Grep tool -> exit 0, no warn" \
  "$JSON_C" "" "GUARD-WARN"

JSON_E1=$(mk_stdin "Grep" '{"pattern":"foo","head_limit":5}' "pre-e")
run_pre "(e) kill-switch off silences head-limit warn" \
  "$JSON_E1" "" "GUARD-WARN" SUPERCLAUDE_GUARDS=off

# ── (d) post-guard: 21 calls, one fixed session -> exactly one crossing WARN ──
SID_D="post-d-fixed-session"
JSON_D=$(mk_stdin "Bash" '{"command":"ls"}' "$SID_D")
ERR_D="$TMPD/err-d-all.txt"
: >"$ERR_D"
(
  export GUARD_PHASE=post
  i=1
  while [ "$i" -le 21 ]; do
    ( guard_init "$JSON_D"; guardpost_heuristics ) 2>>"$ERR_D"
    i=$((i + 1))
  done
)
warn_count=$(grep -c "sonnet-truncation threshold" "$ERR_D")
if [ "${warn_count:-0}" -eq 1 ]; then
  echo "  PASS: (d) 21 post calls, fixed session -> exactly one truncation WARN"
else
  echo "  FAIL: (d) 21 post calls, fixed session -> exactly one truncation WARN (got ${warn_count:-0})"
  echo "    stderr was: $(cat "$ERR_D")"
  fails=$((fails + 1))
fi

# ── (e) post-guard half: kill-switch silences an already-crossed counter ──
SID_E2="post-e2-fixed-session"
JSON_E2=$(mk_stdin "Bash" '{"command":"ls"}' "$SID_E2")
ERR_E2="$TMPD/err-e2.txt"
mkdir -p "$TMPD/superclaude-guard-heuristics"
printf '25' >"$TMPD/superclaude-guard-heuristics/${SID_E2}.calls"
(
  export SUPERCLAUDE_GUARDS=off
  export GUARD_PHASE=post
  guard_init "$JSON_E2"
  guardpost_heuristics
) 2>"$ERR_E2"
if [ ! -s "$ERR_E2" ]; then
  echo "  PASS: (e) kill-switch off silences an already-crossed post counter"
else
  echo "  FAIL: (e) kill-switch off silences an already-crossed post counter"
  echo "    stderr was: $(cat "$ERR_E2")"
  fails=$((fails + 1))
fi

if [ "$fails" -eq 0 ]; then
  echo "test-50-heuristics: ALL PASS"
  exit 0
else
  echo "test-50-heuristics: $fails case(s) FAILED"
  exit 1
fi
