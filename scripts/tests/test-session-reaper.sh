#!/usr/bin/env bash
# Bite-test for scripts/session-reaper.sh's self-skip guard. Self-contained: builds a
# fixture under mktemp -d and points the reaper at it via TIMER_DIR (never touches
# ~/.claude/session-timers). Uses --dry-run only, so no kill signal is ever sent even
# to the fixture's fabricated PIDs. Prints PASS/FAIL per case; exits non-zero on failure.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REAPER="$SCRIPT_DIR/../session-reaper.sh"

PASS_COUNT=0
FAIL_COUNT=0

pass() { echo "PASS: $1"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() { echo "FAIL: $1"; FAIL_COUNT=$((FAIL_COUNT + 1)); }

# A PID that is guaranteed dead right now: spawn a trivial subshell and wait for it.
dead_pid() {
  ( exit 0 ) &
  local p=$!
  wait "$p" 2>/dev/null || true
  echo "$p"
}

# -----------------------------------------------------------------------------------------
# (a) live-self session (sid matches CLAUDE_CODE_SESSION_ID, stale/dead recorded PID)
#     must survive the sweep.
# (b) a genuinely-dead non-self session must still be reaped: proves the self-skip
#     did not turn into an over-skip.
# -----------------------------------------------------------------------------------------
run_case_self_skip() {
  local fixture
  fixture="$(mktemp -d)"

  local self_sid="selfsession00000000000000000000"
  local dead_sid="deadsession00000000000000000000"

  dead_pid > "$fixture/${self_sid}.pid"
  echo "fixture-agent" > "$fixture/${self_sid}.agent"
  date +%s > "$fixture/${self_sid}.start"

  dead_pid > "$fixture/${dead_sid}.pid"
  echo "fixture-agent" > "$fixture/${dead_sid}.agent"
  date +%s > "$fixture/${dead_sid}.start"

  local out
  out="$(CLAUDE_CODE_SESSION_ID="$self_sid" TIMER_DIR="$fixture" bash "$REAPER" --dry-run 2>&1)"

  if echo "$out" | grep -q "Would clean timer files for session=${self_sid:0:8}"; then
    fail "(a) live-self session was targeted for cleanup -- output:
$out"
  else
    pass "(a) live-self session survives (never listed for cleanup)"
  fi

  if echo "$out" | grep -q "Would clean timer files for session=${dead_sid:0:8}"; then
    pass "(b) genuinely-dead non-self session is still reaped"
  else
    fail "(b) genuinely-dead non-self session was NOT listed for cleanup -- output:
$out"
  fi

  # Fixture files must still exist: --dry-run never deletes anything, self or not.
  if [ -f "$fixture/${self_sid}.pid" ] && [ -f "$fixture/${dead_sid}.pid" ]; then
    pass "(c) --dry-run left all fixture files on disk untouched"
  else
    fail "(c) --dry-run unexpectedly deleted fixture files"
  fi

  rm -rf "$fixture"
}

run_case_self_skip

echo
echo "test-session-reaper.sh: $PASS_COUNT passed, $FAIL_COUNT failed"
if [ "$FAIL_COUNT" -gt 0 ]; then
  exit 1
fi
exit 0
