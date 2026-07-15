#!/usr/bin/env bash
# Bite-test for 70-wrong-tool (design/DECISION-DOC.md sec 4, the mechanical R-6 replacement).
# ISOLATED unit test: sources lib-guard.sh + 70-wrong-tool.sh directly and drives the entry
# functions via run_guard in a fresh subshell per invocation (no guard-dispatch.sh /
# guard-post.sh, no repo mutation, no network). All state is hermetic scratch under a
# private $TMPDIR + an explicit WRONG_TOOL_STATE_DIR; session ids come from the stdin JSON.
# Every case is FLAG-level (warn), so rc is 0 throughout.
#
# Cases:
#   (a) governance-skill Skill call (/research, pre)   -> a research marker file is created
#   (b) 4 WebSearch, NO research marker (post)          -> exactly one research-saturation WARN
#   (c) 4 WebSearch WITH a research marker (post)       -> no research-saturation WARN
#   (d) two same-class TOOLING lines (Agent, post)      -> instrument-tripwire WARN on the 2nd
#   (e) SUPERCLAUDE_GUARDS=off                          -> total silence + no marker dropped
#   (f) 3-wide Agent batch, NO swarm marker (pre)       -> a wave/parallel WARN fires
#   (g) 3-wide Agent batch WITH a swarm marker (pre)    -> no wave/parallel WARN

set -uo pipefail

TESTDIR="$(cd "$(dirname "$0")" && pwd)"
GUARDS_DIR="$(cd "$TESTDIR/.." && pwd)"
STAGING="$(cd "$TESTDIR/../../.." && pwd)"   # tests -> guards -> hooks -> staging

TMPD="$(mktemp -d "${TMPDIR:-/tmp}/wrong-tool-bite.XXXXXX")"
trap 'rm -rf "$TMPD"' EXIT
export TMPDIR="$TMPD"
export WRONG_TOOL_STATE_DIR="$TMPD/state"
export WRONG_TOOL_TRIPWIRE_SCRIPT="$STAGING/scripts/instrument-tripwire.py"
mkdir -p "$WRONG_TOOL_STATE_DIR"

fails=0

# ── JSON builders ─────────────────────────────────────────────────────────────
mk_skill() { jq -nc --arg s "$1" --arg sid "$2" \
  '{session_id:$sid, tool_name:"Skill", tool_input:{skill:$s}}'; }
mk_ws()    { jq -nc --arg sid "$1" \
  '{session_id:$sid, tool_name:"WebSearch", tool_input:{query:"x"}}'; }
mk_agent() { jq -nc --arg sid "$1" \
  '{session_id:$sid, tool_name:"Agent", tool_input:{subagent_type:"w-implementer", description:"bite"}}'; }
mk_reviewer() { jq -nc --arg sid "$1" --arg r "$2" \
  '{session_id:$sid, tool_name:"Agent", tool_input:{subagent_type:"w-hostile-reviewer"}, tool_response:$r}'; }

# drive <phase pre|post> <stdin_json> [ENV=VAL ...] -> echoes captured stderr, discards stdout.
drive() {
  local phase="$1" stdin_json="$2"; shift 2
  local fn="guard_wrong_tool"
  [ "$phase" = post ] && fn="guardpost_wrong_tool"
  env "$@" bash -c '
    set -uo pipefail
    [ "$4" = post ] && GUARD_PHASE=post || GUARD_PHASE=pre
    . "$1/lib-guard.sh"
    . "$1/70-wrong-tool.sh"
    guard_init "$2"
    run_guard "$3"
  ' _ "$GUARDS_DIR" "$stdin_json" "$fn" "$phase" 2>&1 1>/dev/null
}

pass() { echo "  PASS: $1"; }
fail() { echo "  FAIL: $1"; echo "    $2"; fails=$((fails + 1)); }

echo "=== test-70-wrong-tool ==="

# ── (a) governance Skill call drops a research marker (mechanical, tool_name==Skill) ──
out=$(drive pre "$(mk_skill research sess-a)")
if [ -f "$WRONG_TOOL_STATE_DIR/sess-a.marker.research" ] && [ -z "$out" ]; then
  pass "(a) /research Skill call -> research marker file created, no warn"
else
  fail "(a) /research Skill call -> research marker file created, no warn" \
       "marker exists? $( [ -f "$WRONG_TOOL_STATE_DIR/sess-a.marker.research" ] && echo yes || echo NO ); stderr='$out'"
fi

# ── (b) 4 WebSearch, no marker -> exactly one research-saturation WARN ──
err_b=""
for _ in 1 2 3 4; do err_b+="$(drive post "$(mk_ws sess-b)")"$'\n'; done
n=$(printf '%s' "$err_b" | grep -c "research-saturation")
if [ "${n:-0}" -eq 1 ]; then
  pass "(b) 4 WebSearch, no research marker -> exactly one research-saturation WARN"
else
  fail "(b) 4 WebSearch, no research marker -> exactly one research-saturation WARN (got ${n:-0})" \
       "stderr was: $err_b"
fi

# ── (c) 4 WebSearch WITH a research marker -> no research-saturation WARN ──
: > "$WRONG_TOOL_STATE_DIR/sess-c.marker.research"
err_c=""
for _ in 1 2 3 4; do err_c+="$(drive post "$(mk_ws sess-c)")"$'\n'; done
if ! printf '%s' "$err_c" | grep -q "research-saturation"; then
  pass "(c) 4 WebSearch WITH research marker -> no research-saturation WARN"
else
  fail "(c) 4 WebSearch WITH research marker -> no research-saturation WARN" "stderr was: $err_c"
fi

# ── (d) two same-class TOOLING lines -> instrument-tripwire WARN on the 2nd ──
if command -v python3 >/dev/null 2>&1; then
  err_d=""
  err_d+="$(drive post "$(mk_reviewer sess-d 'TOOLING: phantom-locator existence check')")"$'\n'
  err_d+="$(drive post "$(mk_reviewer sess-d 'TOOLING: check for phantom locator existence')")"$'\n'
  nd=$(printf '%s' "$err_d" | grep -c "instrument tripwire")
  if [ "${nd:-0}" -eq 1 ]; then
    pass "(d) two same-class TOOLING lines -> instrument-tripwire WARN on the 2nd only"
  else
    fail "(d) two same-class TOOLING lines -> instrument-tripwire WARN on the 2nd only (got ${nd:-0})" \
         "stderr was: $err_d"
  fi
else
  echo "  SKIP: (d) python3 unavailable; instrument-tripwire relay not exercised"
fi

# ── (e) kill-switch off -> total silence AND no marker dropped ──
printf '5' > "$WRONG_TOOL_STATE_DIR/sess-e.websearch.count"   # would warn if not silenced
err_e=$(drive post "$(mk_ws sess-e)" SUPERCLAUDE_GUARDS=off)
out_e2=$(drive pre "$(mk_skill research sess-e2)" SUPERCLAUDE_GUARDS=off)
if [ -z "$err_e" ] && [ -z "$out_e2" ] && [ ! -f "$WRONG_TOOL_STATE_DIR/sess-e2.marker.research" ]; then
  pass "(e) SUPERCLAUDE_GUARDS=off -> silence + no marker dropped"
else
  fail "(e) SUPERCLAUDE_GUARDS=off -> silence + no marker dropped" \
       "err_e='$err_e' out_e2='$out_e2' marker=$( [ -f "$WRONG_TOOL_STATE_DIR/sess-e2.marker.research" ] && echo PRESENT || echo absent )"
fi

# ── (f) 3-wide Agent batch, no swarm marker -> a wave/parallel WARN fires ──
err_f=""
for _ in 1 2 3; do err_f+="$(drive pre "$(mk_agent sess-f)" WRONG_TOOL_BATCH_WINDOW=60)"$'\n'; done
if printf '%s' "$err_f" | grep -q "wave/parallel shape"; then
  pass "(f) 3-wide Agent batch, no swarm marker -> wave/parallel WARN fires"
else
  fail "(f) 3-wide Agent batch, no swarm marker -> wave/parallel WARN fires" "stderr was: $err_f"
fi

# ── (g) 3-wide Agent batch WITH a swarm-dispatch marker -> no wave/parallel WARN ──
: > "$WRONG_TOOL_STATE_DIR/sess-g.marker.swarm-dispatch"
err_g=""
for _ in 1 2 3; do err_g+="$(drive pre "$(mk_agent sess-g)" WRONG_TOOL_BATCH_WINDOW=60)"$'\n'; done
if ! printf '%s' "$err_g" | grep -q "wave/parallel shape"; then
  pass "(g) 3-wide Agent batch WITH swarm marker -> no wave/parallel WARN"
else
  fail "(g) 3-wide Agent batch WITH swarm marker -> no wave/parallel WARN" "stderr was: $err_g"
fi

echo
if [ "$fails" -eq 0 ]; then
  echo "test-70-wrong-tool: ALL PASS"
  exit 0
else
  echo "test-70-wrong-tool: $fails case(s) FAILED"
  exit 1
fi
