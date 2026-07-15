#!/usr/bin/env bash
# Bite-test for 20-write-acl (PHASE2-CONTRACT sec 6; enforcement-gap-ledger.md Family 2,
# rows #4-#7). ISOLATED unit test: sources hooks/lib.sh + guards/lib-guard.sh +
# guards/20-write-acl.sh directly (does NOT go through guard-dispatch.sh), because
# GUARD_AGENT must be injected per-case and the real walk_to_agent proc-tree walk cannot
# be steered from a test harness. guard_init still parses a synthetic PreToolUse-shaped
# stdin JSON for GUARD_TOOL/GUARD_INPUT_JSON; GUARD_AGENT is set directly afterward.
#
# guard_block does `exit 2` in block mode, so each case runs guard_write_acl (via
# run_guard) inside a `()` subshell — the only way to catch that exit as an rc rather than
# killing the test runner. Self-contained, /tmp only, no repo mutation.
#
# Cases:
#   (a) agent=orch          write plans/X/plan.md                 -> block (rule #4)
#   (b) agent=meta          write plans/X/plan.md                 -> pass
#   (c) agent=w-implementer write comms/o-foo/directives.md        -> block (rule #5)
#   (d) agent=o-foo         write comms/o-foo/reports.md           -> pass  (own dir)
#   (e) agent=o-foo         write comms/o-bar/state.md             -> block (rule #5, cross-namespace)
#   (f) agent=orch          write settings.json                    -> block (rule #6)
#   (g) agent=scaf          write <project>/.claude/settings.json  -> block (rule #7, identity-independent)
#   (h) agent=""            write plans/X/plan.md                  -> pass  (fail-open, unknown identity)
#   (i) agent=orch, mode=warn, write plans/X/plan.md               -> exit 0 + WARN (degrade)
#   (x) agent=w-implementer write config/git-policy                -> block (meta-only)
#   (y) agent=orch          write config/git-policy                -> block (meta-only)
#   (z) agent=meta          write config/git-policy                -> pass
#   (w) agent=""            write config/git-policy                -> block (default-deny, NOT fail-open)

set -uo pipefail

TESTDIR="$(cd "$(dirname "$0")" && pwd)"
GUARDS_DIR="$(cd "$TESTDIR/.." && pwd)"
HOOKS_DIR="$(cd "$GUARDS_DIR/.." && pwd)"

TMPD="$(mktemp -d "${TMPDIR:-/tmp}/wacl-bite.XXXXXX")"
trap 'rm -rf "$TMPD"' EXIT

fails=0

# run_case <label> <tool> <file_path> <agent> <want_rc> <stderr_must_match|""> <stderr_must_not_match|""> [ENV=VAL ...]
run_case() {
  local label="$1" tool="$2" file_path="$3" agent="$4" want_rc="$5" must="$6" mustnot="$7"; shift 7
  local stdin_json err_file rc ok=1

  # jq --arg keeps file_path/tool out of the shell so tokens embed verbatim.
  stdin_json=$(jq -nc --arg t "$tool" --arg f "$file_path" \
    '{tool_name:$t, tool_input:{file_path:$f, content:"x"}}')
  err_file="$TMPD/stderr.txt"

  (
    set -uo pipefail
    for kv in "$@"; do export "$kv"; done
    . "$HOOKS_DIR/lib.sh" 2>/dev/null
    . "$GUARDS_DIR/lib-guard.sh"
    . "$GUARDS_DIR/20-write-acl.sh"
    guard_init "$stdin_json"
    GUARD_AGENT="$agent"
    run_guard guard_write_acl
    exit 0
  ) >/dev/null 2>"$err_file"
  rc=$?

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

echo "=== test-20-write-acl ==="
run_case "(a) orch writes plan.md -> block" \
  "Write" "$HOME/.claude/plans/redesign/plan.md" "orch" 2 "" ""

run_case "(b) meta writes plan.md -> pass" \
  "Write" "$HOME/.claude/plans/redesign/plan.md" "meta" 0 "" ""

run_case "(c) worker writes another orch's directives.md -> block" \
  "Write" "$HOME/.claude/comms/o-foo/directives.md" "w-implementer" 2 "" ""

run_case "(d) orch writes its own reports.md -> pass" \
  "Edit" "$HOME/.claude/comms/o-foo/reports.md" "o-foo" 0 "" ""

run_case "(e) orch writes another orch's state.md -> block (cross-namespace)" \
  "Write" "$HOME/.claude/comms/o-bar/state.md" "o-foo" 2 "" ""

run_case "(f) orch writes settings.json -> block (scaf-only)" \
  "Edit" "$HOME/.claude/settings.json" "orch" 2 "" ""

run_case "(g) scaf writes a project-local .claude/settings.json -> block" \
  "Write" "$HOME/projects/cash/sub/.claude/settings.json" "scaf" 2 "" ""

run_case "(h) unknown agent writes plan.md -> pass (fail-open)" \
  "Write" "$HOME/.claude/plans/redesign/plan.md" "" 0 "" ""

run_case "(i) warn mode degrades block to exit 0 + WARN" \
  "Write" "$HOME/.claude/plans/redesign/plan.md" "orch" 0 "WARN" "" \
  SUPERCLAUDE_GUARD_WRITE_ACL=warn

run_case "(x) worker writes config/git-policy -> block (meta-only)" \
  "Write" "$HOME/.claude/config/git-policy" "w-implementer" 2 "" ""

run_case "(y) orch writes config/git-policy -> block (meta-only)" \
  "Write" "$HOME/.claude/config/git-policy" "orch" 2 "" ""

run_case "(z) meta writes config/git-policy -> pass" \
  "Write" "$HOME/.claude/config/git-policy" "meta" 0 "" ""

run_case "(w) unknown agent writes config/git-policy -> block (default-deny, not fail-open)" \
  "Write" "$HOME/.claude/config/git-policy" "" 2 "" ""

if [ "$fails" -eq 0 ]; then
  echo "test-20-write-acl: ALL PASS"
  exit 0
else
  echo "test-20-write-acl: $fails case(s) FAILED"
  exit 1
fi
