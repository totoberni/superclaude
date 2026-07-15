#!/usr/bin/env bash
# Bite-test for M1 (SEAL-A-verdict.md): a RUNTIME abort inside one guard (a
# set -u unbound-variable reference, a stray exit N) must not skip the guards
# ordered after it -- only an intentional guard_block (exit 2) may propagate.
# Self-contained, /tmp only, never touches ~/.claude or a real repo.
#
# Technique: copy the staging hooks/ tree into a scratch dir, inject a new
# guard function that unconditionally aborts under set -u, splice a call to
# it in FRONT of `dispatch_guard guard_git_policy` in the copied guard-
# dispatch.sh (the dispatcher's own first BLOCK guard), then drive that copy
# end-to-end exactly like a real PreToolUse call. Proves:
#   (o) the injected guard actually aborted (its nounset error reaches stderr)
#   (p) git_policy -- ordered immediately after the abort -- still fires
#       rc=2 on a disabled-git commit (the abort did not skip it)
#   (q) with the abort injected but git-policy ENABLED (benign command), the
#       dispatcher still exits 0 (the abort alone never spuriously blocks)

set -uo pipefail

TESTDIR="$(cd "$(dirname "$0")" && pwd)"
GUARDS_DIR="$(cd "$TESTDIR/.." && pwd)"
HOOKS_DIR="$(cd "$GUARDS_DIR/.." && pwd)"

TMPD="$(mktemp -d "${TMPDIR:-/tmp}/dispatch-abort-bite.XXXXXX")"
trap 'rm -rf "$TMPD"' EXIT
fails=0

# ── Scratch copy of the staging hooks tree, with an aborting guard injected ──
SCRATCH_HOOKS="$TMPD/hooks"
cp -r "$HOOKS_DIR" "$SCRATCH_HOOKS"

cat > "$SCRATCH_HOOKS/guards/05-abort-inject.sh" <<'ABORTEOF'
# Injected ONLY by test-dispatch-abort-isolation.sh: a guard that aborts at
# runtime (set -u unbound-variable reference), never an intentional block.
guard_abort_inject() {
  set -u
  echo "$UNDEFINED_VAR_FOR_ABORT_ISOLATION_TEST"
}
ABORTEOF

# Splice a call to the injected guard in FRONT of guard_git_policy (the
# dispatcher's own first BLOCK guard), mirroring the reviewer's exact probe.
sed -i 's/^dispatch_guard guard_git_policy$/dispatch_guard guard_abort_inject\ndispatch_guard guard_git_policy/' \
  "$SCRATCH_HOOKS/guard-dispatch.sh"
if ! grep -q '^dispatch_guard guard_abort_inject$' "$SCRATCH_HOOKS/guard-dispatch.sh"; then
  echo "test-dispatch-abort-isolation: FATAL setup -- injection splice failed" >&2
  exit 1
fi

DISPATCH="$SCRATCH_HOOKS/guard-dispatch.sh"

# Dedicated non-repo cwd: commit-gate reads the staged diff of `pwd`, so the
# dispatcher must never run inside an actual git repo (never ~/.claude, never
# this test's own tree) for this test to be hermetic and deterministic.
CWD="$TMPD/cwd"
mkdir -p "$CWD"

DISABLED="$TMPD/policy-disabled"
ENABLED="$TMPD/policy-enabled"
printf 'disabled\n' > "$DISABLED"
printf 'enabled\n'  > "$ENABLED"

# ── stdin + run helper ────────────────────────────────────────────────────────
mk_bash_stdin() { jq -nc --arg c "$1" '{tool_name:"Bash", tool_input:{command:$c}}'; }

# run_case <label> <policy_file> <command> <expected_rc> <stderr_must_match|""> <stderr_must_not_match|"">
run_case() {
  local label="$1" policy="$2" content="$3" want_rc="$4" must="$5" mustnot="$6"
  local stdin_file="$TMPD/stdin.json" err_file="$TMPD/stderr.txt"
  mk_bash_stdin "$content" > "$stdin_file"
  ( cd "$CWD" && env "SUPERCLAUDE_GIT_POLICY_FILE=$policy" bash "$DISPATCH" < "$stdin_file" > /dev/null 2> "$err_file" )
  local rc=$?
  local ok=1
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

echo "=== test-dispatch-abort-isolation ==="
run_case "(o) injected guard actually aborts (nounset error reaches stderr)" \
  "$ENABLED" "git commit -m x" 0 "unbound variable" ""
run_case "(p) abort BEFORE git_policy does not skip it: disabled + git commit -> still BLOCK" \
  "$DISABLED" "git commit -m x" 2 "git is disabled" ""
run_case "(q) abort injected, policy enabled, benign command -> still PASS (no spurious block)" \
  "$ENABLED" "git status" 0 "" "GUARD-BLOCK"

if [ "$fails" -eq 0 ]; then
  echo "test-dispatch-abort-isolation: ALL PASS"
  exit 0
else
  echo "test-dispatch-abort-isolation: $fails case(s) FAILED"
  exit 1
fi
