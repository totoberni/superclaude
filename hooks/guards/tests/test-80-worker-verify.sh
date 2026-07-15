#!/usr/bin/env bash
# Bite-test for 80-worker-verify (Family 6 #23, design/enforcement-gap-ledger.md,
# rules/40-swarm-quality-gates.md R-3). ISOLATED unit test: sources lib-guard.sh +
# the guard directly and drives it via run_guard (no guard-post.sh, no repo
# mutation, no network). Builds a synthetic PostToolUse stdin JSON in a fresh
# subshell per case with GUARD_PHASE=post, then calls
# `run_guard guardpost_worker_verify` and captures stderr. Never blocks (rc=0
# always -- PostToolUse cannot block, and this guard only ever guard_warns).
#
# Payload shape: `tool_name`, `tool_input:{subagent_type, description}`. This
# guard never reads tool_response (it names the obligation, not the diff), so
# the payload omits it, unlike the verdict-shape sibling test.
#
# Cases:
#   (a) Agent return by a w-implementer            -> warn, names "w-implementer"
#   (b) non-Agent tool (Write)                      -> silent (tool filter)
#   (c) SUPERCLAUDE_GUARDS=off + case (a) payload   -> total silence (kill-switch)
#   (d) Agent return, empty subagent_type           -> silent (fail-open)

set -uo pipefail

TESTDIR="$(cd "$(dirname "$0")" && pwd)"
GUARDS_DIR="$(cd "$TESTDIR/.." && pwd)"

fails=0

# run_case <label> <tool_name> <subagent_type> <stderr_must_match|""> <stderr_must_not_match|""> [ENV=VAL ...]
run_case() {
  local label="$1" tool_name="$2" subagent_type="$3" must="$4" mustnot="$5"; shift 5
  local stdin_json err_file rc ok
  stdin_json=$(jq -nc --arg tn "$tool_name" --arg t "$subagent_type" \
    '{tool_name:$tn, tool_input:{subagent_type:$t, description:"bite-test"}}')
  err_file="$(mktemp "${TMPDIR:-/tmp}/worker-verify-bite.XXXXXX")"

  env "$@" GUARD_PHASE=post bash -c '
    set -uo pipefail
    GUARD_PHASE=post
    . "$1/lib-guard.sh"
    . "$1/80-worker-verify.sh"
    guard_init "$2"
    run_guard guardpost_worker_verify
  ' _ "$GUARDS_DIR" "$stdin_json" >/dev/null 2>"$err_file"
  rc=$?

  ok=1
  [ "$rc" -eq 0 ] || { ok=0; echo "    rc=$rc want=0"; }
  if [ -n "$must" ] && ! grep -q "$must" "$err_file"; then
    ok=0; echo "    stderr missing: '$must'"; echo "    stderr was: $(cat "$err_file")"
  fi
  if [ -n "$mustnot" ] && grep -q "$mustnot" "$err_file"; then
    ok=0; echo "    stderr unexpectedly matched: '$mustnot'"; echo "    stderr was: $(cat "$err_file")"
  fi
  rm -f "$err_file"
  if [ "$ok" -eq 1 ]; then
    echo "  PASS: $label"
  else
    echo "  FAIL: $label"
    fails=$((fails + 1))
  fi
}

echo "=== test-80-worker-verify ==="
run_case "(a) Agent return by w-implementer -> R-3 reminder naming worker" "Agent" "w-implementer" "R-3 verification due for 'w-implementer'" ""
run_case "(b) non-Agent tool (Write) -> silent"                            "Write" "w-implementer" ""                                          "GUARD-WARN"
run_case "(c) kill-switch off -> total silence"                            "Agent" "w-implementer" ""                                          "GUARD-WARN" SUPERCLAUDE_GUARDS=off
run_case "(d) empty subagent_type -> silent (fail-open)"                   "Agent" ""              ""                                          "GUARD-WARN"

if [ "$fails" -eq 0 ]; then
  echo "test-80-worker-verify: ALL PASS"
  exit 0
else
  echo "test-80-worker-verify: $fails case(s) FAILED"
  exit 1
fi
