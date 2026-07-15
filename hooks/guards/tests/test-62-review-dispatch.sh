#!/usr/bin/env bash
# Bite-test for 62-review-dispatch (PHASE2-CONTRACT sec 6, enforcement-gap-ledger.md
# Family 5 #17/#20). Self-contained, /tmp only, never touches a real repo or a real
# reviewer dispatch.
#
# 62-review-dispatch.sh is a Wave 2 guard: guard-dispatch.sh's glob already sources
# it (guards/[0-9]*.sh), but the explicit `run_guard guard_review_dispatch` line is
# not yet appended there (task scope forbids editing guard-dispatch.sh; see its own
# "Wave 2 appends" comment). This test therefore generates a small ephemeral harness
# under $TMPD that mirrors guard-dispatch.sh's PreToolUse flow (source lib.sh +
# lib-guard.sh + the guard, guard_init, run_guard) but explicitly calls
# run_guard guard_review_dispatch. The harness is discarded with $TMPD; it is test
# scaffolding, not a guard-subsystem artifact.
#
# Cases:
#   (a) reviewer dispatch, no Ledger: line                       -> block
#   (b) reviewer dispatch, Ledger: line, file exists              -> pass
#   (c) reviewer dispatch, Ledger: line, file does NOT exist      -> block
#   (d) reviewer dispatch, valid ledger, "the producer says" leak -> pass + WARN
#   (e) non-reviewer subagent_type (w-implementer), no ledger     -> pass (not policed)
#   (f) mode=warn, no Ledger: line                                -> pass + WARN
#   (g) SUPERCLAUDE_GUARDS=off, both violations present           -> silence

set -uo pipefail

TESTDIR="$(cd "$(dirname "$0")" && pwd)"
GUARDS_DIR="$(cd "$TESTDIR/.." && pwd)"
HOOKS_DIR="$(cd "$GUARDS_DIR/.." && pwd)"

TMPD="$(mktemp -d "${TMPDIR:-/tmp}/review-dispatch-bite.XXXXXX")"
trap 'rm -rf "$TMPD"' EXIT
fails=0

# ── Ephemeral harness (see header note above) ────────────────────────────────
HARNESS="$TMPD/harness.sh"
cat > "$HARNESS" <<HARNESSEOF
#!/usr/bin/env bash
set -uo pipefail
INPUT=\$(cat)
GUARD_PHASE="pre"
. "$HOOKS_DIR/lib.sh" 2>/dev/null || true
. "$GUARDS_DIR/lib-guard.sh" || { echo "harness: lib-guard.sh missing" >&2; exit 0; }
. "$GUARDS_DIR/62-review-dispatch.sh" || { echo "harness: 62-review-dispatch.sh missing" >&2; exit 0; }
if [ "\${SUPERCLAUDE_GUARDS:-}" = "off" ]; then exit 0; fi
guard_init "\$INPUT"
run_guard guard_review_dispatch
exit 0
HARNESSEOF

# ── Fixtures ───────────────────────────────────────────────────────────────────
LEDGER_OK="$TMPD/rounds.md"
printf '# rounds ledger\n' > "$LEDGER_OK"
LEDGER_MISSING="$TMPD/does-not-exist/rounds.md"

# ── stdin + run helpers ───────────────────────────────────────────────────────
mk_stdin() {
  jq -nc --arg s "$1" --arg p "$2" '{tool_name:"Agent", tool_input:{subagent_type:$s, prompt:$p}}'
}

# run_case <label> <subagent_type> <prompt> <expected_rc> <stderr_must_match|""> <stderr_must_not_match|""> [ENV=VAL ...]
run_case() {
  local label="$1" subtype="$2" prompt="$3" want_rc="$4" must="$5" mustnot="$6"; shift 6
  local stdin_file="$TMPD/stdin.json" err_file="$TMPD/stderr.txt"
  mk_stdin "$subtype" "$prompt" > "$stdin_file"
  ( env "$@" bash "$HARNESS" < "$stdin_file" > /dev/null 2> "$err_file" )
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

# ── Prompt strings ────────────────────────────────────────────────────────────
PROMPT_NO_LEDGER='Objective: review the diff for correctness. Rubric: check tests pass.'

PROMPT_VALID_LEDGER=$(printf 'Objective: review the diff.\nLedger: %s\nRubric: correctness only.' "$LEDGER_OK")

PROMPT_BAD_LEDGER=$(printf 'Objective: review the diff.\nLedger: %s\nRubric: correctness only.' "$LEDGER_MISSING")

PROMPT_ISOLATION_LEAK=$(printf 'Objective: review the diff.\nLedger: %s\nNote: the producer says this was already fixed.\nRubric: correctness only.' "$LEDGER_OK")

PROMPT_BOTH_VIOLATIONS='Objective: review the diff. This continues the self-assessment from the producer.'

echo "=== test-62-review-dispatch ==="
run_case "(a) reviewer, no Ledger: line -> block" \
  "w-reviewer" "$PROMPT_NO_LEDGER" 2 "round ledger" ""
run_case "(b) reviewer, Ledger: line, file exists -> pass" \
  "w-reviewer" "$PROMPT_VALID_LEDGER" 0 "" "GUARD-BLOCK"
run_case "(c) reviewer, Ledger: line, file missing -> block" \
  "w-hostile-reviewer" "$PROMPT_BAD_LEDGER" 2 "round ledger" ""
run_case "(d) reviewer, valid ledger, isolation leak -> pass + WARN" \
  "w-design-reviewer" "$PROMPT_ISOLATION_LEAK" 0 "isolation violation" "GUARD-BLOCK"
run_case "(e) non-reviewer subagent_type, no ledger -> pass (not policed)" \
  "w-implementer" "$PROMPT_NO_LEDGER" 0 "" "GUARD-"
run_case "(f) mode=warn, no Ledger: line -> pass + WARN" \
  "w-reviewer" "$PROMPT_NO_LEDGER" 0 "WARN" "GUARD-BLOCK" SUPERCLAUDE_GUARD_REVIEW_DISPATCH=warn
run_case "(g) SUPERCLAUDE_GUARDS=off, both violations -> silence" \
  "w-reviewer" "$PROMPT_BOTH_VIOLATIONS" 0 "" "GUARD-" SUPERCLAUDE_GUARDS=off

if [ "$fails" -eq 0 ]; then
  echo "test-62-review-dispatch: ALL PASS"
  exit 0
else
  echo "test-62-review-dispatch: $fails case(s) FAILED"
  exit 1
fi
