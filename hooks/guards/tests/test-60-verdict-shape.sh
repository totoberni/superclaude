#!/usr/bin/env bash
# Bite-test for 60-verdict-shape (Family 5, design/enforcement-gap-ledger.md #18/#19).
# ISOLATED unit test: sources lib-guard.sh + the guard directly and drives it via
# run_guard (no guard-post.sh, no repo mutation, no network). Builds a synthetic
# PostToolUse stdin JSON for an Agent tool return, feeds it through guard_init in a
# fresh subshell per case with GUARD_PHASE=post, then calls
# `run_guard guardpost_verdict_shape` and captures stderr. Never blocks (rc=0 always).
#
# Payload shape: `tool_name:"Agent"`, `tool_input:{subagent_type, description}`,
# `tool_response:<string>`. This mirrors agent-outcome.sh's own extraction (the
# existing PostToolUse-on-Agent consumer of tool_response) AND the shape that hook
# is actually exercised against in this repo (~/.claude/scripts/test-hooks.sh P71:
# `tool_response` as a plain string), so the guard is proven against real hook input.
#
# Cases:
#   (a) reviewer (w-reviewer) returns "ACCEPT: looks fine"                    -> flag (unrecognized token)
#   (b) reviewer (w-reviewer) returns "VERDICT: CLEAN blocking=0 major=0 minor=0 round=2" -> pass, no warn
#   (c) producer (w-implementer) returns "VERDICT: CLEAN blocking=0 major=0 minor=0 round=1" -> flag (provenance)
#   (d) producer (w-implementer) returns "STATUS: DONE files=2 checkpoint=/tmp/x" -> pass, no warn
#   (e) unknown subagent_type returns "ACCEPT: looks fine"                    -> pass (fail-open)
#   (f) SUPERCLAUDE_GUARDS=off + case (a) payload                             -> total silence

set -uo pipefail

TESTDIR="$(cd "$(dirname "$0")" && pwd)"
GUARDS_DIR="$(cd "$TESTDIR/.." && pwd)"

fails=0

# run_case <label> <subagent_type> <response_text> <stderr_must_match|""> <stderr_must_not_match|""> [ENV=VAL ...]
run_case() {
  local label="$1" subagent_type="$2" resp="$3" must="$4" mustnot="$5"; shift 5
  local stdin_json err_file rc ok
  stdin_json=$(jq -nc --arg t "$subagent_type" --arg r "$resp" \
    '{tool_name:"Agent", tool_input:{subagent_type:$t, description:"bite-test"}, tool_response:$r}')
  err_file="$(mktemp "${TMPDIR:-/tmp}/verdict-shape-bite.XXXXXX")"

  env "$@" GUARD_PHASE=post bash -c '
    set -uo pipefail
    GUARD_PHASE=post
    . "$1/lib-guard.sh"
    . "$1/60-verdict-shape.sh"
    guard_init "$2"
    run_guard guardpost_verdict_shape
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

echo "=== test-60-verdict-shape ==="
run_case "(a) reviewer invents ACCEPT: -> flag"                "w-reviewer"     "ACCEPT: looks fine"                                        "unrecognized verdict token" ""
run_case "(b) reviewer emits canonical VERDICT: -> pass"       "w-reviewer"     "VERDICT: CLEAN blocking=0 major=0 minor=0 round=2"          ""                            "GUARD-WARN"
run_case "(c) producer emits VERDICT: -> flag (provenance)"    "w-implementer"  "VERDICT: CLEAN blocking=0 major=0 minor=0 round=1"          "STATUS-only"                 ""
run_case "(d) producer emits STATUS: -> pass"                  "w-implementer"  "STATUS: DONE files=2 checkpoint=/tmp/x"                     ""                            "GUARD-WARN"
run_case "(e) unknown subagent_type -> pass (fail-open)"       "w-mystery"      "ACCEPT: looks fine"                                          ""                            "GUARD-WARN"
run_case "(f) kill-switch off -> total silence"                "w-reviewer"     "ACCEPT: looks fine"                                          ""                            "GUARD-WARN" SUPERCLAUDE_GUARDS=off

if [ "$fails" -eq 0 ]; then
  echo "test-60-verdict-shape: ALL PASS"
  exit 0
else
  echo "test-60-verdict-shape: $fails case(s) FAILED"
  exit 1
fi
