#!/usr/bin/env bash
# Bite-test for 26-git-policy (PHASE2-CONTRACT sec 6). ISOLATED unit test: sources
# lib-guard.sh + the guard directly and drives it via run_guard in a fresh subshell
# per case. No repo mutation, no network. Commands are only INSPECTED by the guard
# (the guard reads the command string; it never executes it), so the git/gh verbs
# in the cases are never run.
#
# The policy state is pointed at a TEST file under $TMPDIR via
# SUPERCLAUDE_GIT_POLICY_FILE so the real ~/.claude/config/git-policy is untouched.
# Any shell history-expansion glyph in test data would be built at runtime with
# printf; none is needed here.
#
# Cases (policy=disabled, each must BLOCK -> exit 2):
#   (a) git commit -m x
#   (b) git -C /tmp/r commit -m x            (-C global option tolerated)
#   (c) cd /tmp/r && git commit -m x         (chained compound command)
#   (d) git push origin main
#   (e) git commit-tree <oid>
#   (f) bash -c "git commit -m x"            (wrapper)
#   (g) GIT_AUTHOR_NAME=x git commit -m y    (env prefix)
#   (h) gh release create v1                 (gh push-like escalation)
# Cases (policy=enabled, must PASS -> exit 0):
#   (i) git commit -m x
#   (j) git status
# Cases (policy=disabled, read-only, must PASS -> exit 0):
#   (k) git status
#   (l) git log
# Kill-switch (policy=disabled + a commit command):
#   (m) SUPERCLAUDE_GUARDS=off -> exit 0, total silence
#
# Flag-write self-unblock cases (independent of policy state; SUPERCLAUDE_GIT_
# POLICY_FILE points at a fresh $TMPDIR target so the real flag is untouched):
#   (n) w-implementer, printf enabled > <flag>  via Bash -> block
#   (o) w-implementer, echo disabled >> <flag>              -> block
#   (p) w-implementer, sed -i s/x/y/ <flag>                 -> block
#   (q) meta,          printf enabled > <flag>              -> pass
#   (r) empty agent,   echo enabled > <flag>                -> block (default-deny)
#   (s) w-implementer, cat <flag>  (read-only)               -> pass

set -uo pipefail

TESTDIR="$(cd "$(dirname "$0")" && pwd)"
GUARDS_DIR="$(cd "$TESTDIR/.." && pwd)"

TMPD="$(mktemp -d "${TMPDIR:-/tmp}/git-policy-bite.XXXXXX")"
trap 'rm -rf "$TMPD"' EXIT

DISABLED="$TMPD/policy-disabled"
ENABLED="$TMPD/policy-enabled"
printf 'disabled\n' >"$DISABLED"
printf 'enabled\n'  >"$ENABLED"

fails=0

# run_case <label> <cmd> <agent> <want_rc> <stderr_must|""> <stderr_mustnot|""> [ENV=VAL ...]
# <agent> feeds GUARD_AGENT directly after guard_init (this test does not source
# hooks/lib.sh, so walk_to_agent never runs and GUARD_AGENT would otherwise stay
# "" for every case -- same isolation rationale as test-20-write-acl.sh).
run_case() {
  local label="$1" cmd="$2" agent="$3" want_rc="$4" must="$5" mustnot="$6"; shift 6
  local stdin_json err_file rc ok
  stdin_json=$(jq -nc --arg c "$cmd" '{tool_name:"Bash", tool_input:{command:$c}}')
  err_file="$TMPD/stderr.txt"

  env "$@" bash -c '
    set -uo pipefail
    . "$1/lib-guard.sh"
    . "$1/26-git-policy.sh"
    guard_init "$2"
    GUARD_AGENT="$3"
    run_guard guard_git_policy
  ' _ "$GUARDS_DIR" "$stdin_json" "$agent" >/dev/null 2>"$err_file"
  rc=$?

  ok=1
  [ "$rc" -eq "$want_rc" ] || { ok=0; echo "    rc=$rc want=$want_rc"; }
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

echo "=== test-26-git-policy ==="

# policy=disabled -> BLOCK (exit 2)
run_case "(a) git commit -m x -> block"                 "git commit -m x"                    "" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case "(b) git -C /tmp/r commit -> block"            "git -C /tmp/r commit -m x"          "" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case "(c) cd /tmp/r && git commit -> block"         "cd /tmp/r && git commit -m x"       "" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case "(d) git push origin main -> block"            "git push origin main"               "" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case "(e) git commit-tree -> block"                 "git commit-tree deadbeef"           "" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case "(f) bash -c wrapper -> block"                 'bash -c "git commit -m x"'          "" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case "(g) env-prefixed commit -> block"             "GIT_AUTHOR_NAME=x git commit -m y"  "" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case "(h) gh release create -> block"               "gh release create v1"               "" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"

# policy=enabled -> PASS (exit 0)
run_case "(i) git commit (enabled) -> pass"             "git commit -m x"                    "" 0 ""            "GUARD-BLOCK" SUPERCLAUDE_GIT_POLICY_FILE="$ENABLED"
run_case "(j) git status (enabled) -> pass"             "git status"                         "" 0 ""            "GUARD-BLOCK" SUPERCLAUDE_GIT_POLICY_FILE="$ENABLED"

# policy=disabled, read-only -> PASS (exit 0)
run_case "(k) git status (disabled) -> pass"            "git status"                         "" 0 ""            "GUARD-BLOCK" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case "(l) git log (disabled) -> pass"               "git log --oneline"                  "" 0 ""            "GUARD-BLOCK" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"

# kill-switch -> total silence
run_case "(m) kill-switch off -> silence"               "git commit -m x"                    "" 0 ""            "GUARD" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED" SUPERCLAUDE_GUARDS=off

# Flag-write self-unblock cases: policy state is irrelevant here (ENABLED file),
# proving the check fires independent of the disabled/enabled gate.
FLAG="$TMPD/flag-target"

run_case "(n) worker printf > flag -> block"            "printf enabled > $FLAG"             "w-implementer" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$FLAG"
run_case "(o) worker echo >> flag -> block"             "echo disabled >> $FLAG"             "w-implementer" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$FLAG"
run_case "(p) worker sed -i flag -> block"              "sed -i s/x/y/ $FLAG"                "w-implementer" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$FLAG"
run_case "(q) meta printf > flag -> pass"               "printf enabled > $FLAG"             "meta"          0 ""            "GUARD-BLOCK" SUPERCLAUDE_GIT_POLICY_FILE="$FLAG"
run_case "(r) empty agent echo > flag -> block"         "echo enabled > $FLAG"               ""              2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$FLAG"
run_case "(s) worker cat flag (read-only) -> pass"      "cat $FLAG"                          "w-implementer" 0 ""            "GUARD-BLOCK" SUPERCLAUDE_GIT_POLICY_FILE="$FLAG"

if [ "$fails" -eq 0 ]; then
  echo "test-26-git-policy: ALL PASS"
  exit 0
else
  echo "test-26-git-policy: $fails case(s) FAILED"
  exit 1
fi
