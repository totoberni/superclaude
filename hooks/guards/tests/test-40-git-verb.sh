#!/usr/bin/env bash
# Bite-test for 40-git-verb (Family 4, design/enforcement-gap-ledger.md #13-#16).
# ISOLATED unit test per the wave-1 dispatch: sources lib-guard.sh + the guard directly
# and drives it via run_guard (no guard-dispatch.sh, no repo mutation, no network).
# Builds a synthetic PreToolUse stdin JSON for a Bash tool call, feeds it through
# guard_init in a fresh subshell per case, then calls `run_guard guard_git_verb` and
# captures stderr. This family only warns (never blocks), so every case expects rc=0.
#
# The shell history-expansion character is never typed as a literal glyph in this file
# (rules/20 Shell Mangling in Content/DB Writes): it is built at runtime via printf and
# interpolated into the case (c) command string.
#
# Cases:
#   (a) `git -C <dir> checkout --ours <dir>/f` -> warn: -C pathspec repeated
#   (b) `git -C <dir> checkout --ours f`       -> pass, no warn
#   (c) memory_db.py write with a runtime bang -> warn: bash mangling
#   (d) `git checkout -b feature`              -> warn: branch create
#   (e) `git status`                           -> pass, no warn
#   (f) SUPERCLAUDE_GUARDS=off + case (a) cmd  -> total silence

set -uo pipefail

TESTDIR="$(cd "$(dirname "$0")" && pwd)"
GUARDS_DIR="$(cd "$TESTDIR/.." && pwd)"

TMPD="$(mktemp -d "${TMPDIR:-/tmp}/git-verb-bite.XXXXXX")"
trap 'rm -rf "$TMPD"' EXIT

BANG="$(printf '\41')"
fails=0

# run_case <label> <cmd> <stderr_must_match|""> <stderr_must_not_match|""> [ENV=VAL ...]
run_case() {
  local label="$1" cmd="$2" must="$3" mustnot="$4"; shift 4
  local stdin_json err_file rc ok
  stdin_json=$(jq -nc --arg c "$cmd" '{tool_name:"Bash", tool_input:{command:$c}}')
  err_file="$TMPD/stderr.txt"

  env "$@" bash -c '
    set -uo pipefail
    . "$1/lib-guard.sh"
    . "$1/40-git-verb.sh"
    guard_init "$2"
    run_guard guard_git_verb
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
  if [ "$ok" -eq 1 ]; then
    echo "  PASS: $label"
  else
    echo "  FAIL: $label"
    fails=$((fails + 1))
  fi
}

echo "=== test-40-git-verb ==="
run_case "(a) -C dir repeated in pathspec -> warn"     "git -C /tmp/repo checkout --ours /tmp/repo/f" "repo-root-relative" ""
run_case "(b) -C dir NOT repeated -> pass"              "git -C /tmp/repo checkout --ours f"           ""                    "GUARD-WARN"
run_case "(c) memory_db.py write + runtime bang -> warn" "python3 memory_db.py update --text 'urgent${BANG}note'" "Write tool instead" ""
run_case "(d) git checkout -b -> warn (branch create)"  "git checkout -b feature"                      "creates a branch"   ""
run_case "(e) git status -> pass"                       "git status"                                   ""                    "GUARD-WARN"
run_case "(f) kill-switch off -> total silence"         "git -C /tmp/repo checkout --ours /tmp/repo/f" ""                    "GUARD-WARN" SUPERCLAUDE_GUARDS=off

if [ "$fails" -eq 0 ]; then
  echo "test-40-git-verb: ALL PASS"
  exit 0
else
  echo "test-40-git-verb: $fails case(s) FAILED"
  exit 1
fi
