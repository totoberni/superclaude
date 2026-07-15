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
# lib-guard.sh + 70-wrong-tool.sh (the converge-marker source, see 62's #17 fix) +
# 62-review-dispatch.sh, guard_init, run_guard) but explicitly calls run_guard
# guard_review_dispatch. The harness is discarded with $TMPD; it is test
# scaffolding, not a guard-subsystem artifact.
#
# #17 is scoped to a converge context (SEAL-A M3 fix): the ledger requirement only
# fires when 70-wrong-tool.sh's per-session "converge" marker is present. Cases (a)-
# (g) below drive that marker explicitly via CONV_SID (marker pre-touched) vs
# ADHOC_SID (no marker), hermetically under WRONG_TOOL_STATE_DIR=$TMPD/wt-state.
#
# Cases:
#   (a) converge context, no Ledger: line                          -> block
#   (b) converge context, Ledger: line, file exists                -> pass
#   (c) converge context, Ledger: line, file does NOT exist        -> block
#   (d) converge context, valid ledger, "the producer says" leak   -> pass + WARN
#   (e) converge context, non-reviewer subagent_type, no ledger    -> pass (not policed)
#   (f) converge context, mode=warn, no Ledger: line               -> pass + WARN
#   (g) converge context, SUPERCLAUDE_GUARDS=off, both violations  -> silence
#   (h) NO converge context (ad-hoc/SEAL panel), no Ledger: line   -> pass (SEAL-A M3)
#   (i) NO converge context, isolation leak present                -> pass + WARN
#       (isolation-lint #20 is NOT scoped to converge; it fires regardless)

set -uo pipefail

TESTDIR="$(cd "$(dirname "$0")" && pwd)"
GUARDS_DIR="$(cd "$TESTDIR/.." && pwd)"
HOOKS_DIR="$(cd "$GUARDS_DIR/.." && pwd)"

TMPD="$(mktemp -d "${TMPDIR:-/tmp}/review-dispatch-bite.XXXXXX")"
trap 'rm -rf "$TMPD"' EXIT
fails=0

# Hermetic converge-marker state (see 70-wrong-tool.sh's _wrong_tool_state_dir).
WT_STATE="$TMPD/wt-state"
mkdir -p "$WT_STATE"
CONV_SID="conv-session"
ADHOC_SID="adhoc-session"
: > "$WT_STATE/$CONV_SID.marker.converge"   # simulates /converge having run this session
# ADHOC_SID gets no marker file: simulates an ad-hoc reviewer spawn / SEAL panel.

# ── Ephemeral harness (see header note above) ────────────────────────────────
HARNESS="$TMPD/harness.sh"
cat > "$HARNESS" <<HARNESSEOF
#!/usr/bin/env bash
set -uo pipefail
INPUT=\$(cat)
GUARD_PHASE="pre"
. "$HOOKS_DIR/lib.sh" 2>/dev/null || true
. "$GUARDS_DIR/lib-guard.sh" || { echo "harness: lib-guard.sh missing" >&2; exit 0; }
. "$GUARDS_DIR/70-wrong-tool.sh" || { echo "harness: 70-wrong-tool.sh missing" >&2; exit 0; }
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
mk_stdin() {  # subagent_type prompt session_id
  jq -nc --arg s "$1" --arg p "$2" --arg sid "$3" \
    '{tool_name:"Agent", tool_input:{subagent_type:$s, prompt:$p}, session_id:$sid}'
}

# run_case <label> <subagent_type> <prompt> <session_id> <expected_rc> <stderr_must_match|""> <stderr_must_not_match|""> [ENV=VAL ...]
run_case() {
  local label="$1" subtype="$2" prompt="$3" sid="$4" want_rc="$5" must="$6" mustnot="$7"; shift 7
  local stdin_file="$TMPD/stdin.json" err_file="$TMPD/stderr.txt"
  mk_stdin "$subtype" "$prompt" "$sid" > "$stdin_file"
  ( env WRONG_TOOL_STATE_DIR="$WT_STATE" "$@" bash "$HARNESS" < "$stdin_file" > /dev/null 2> "$err_file" )
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

PROMPT_ADHOC_LEAK='Objective: review the diff. Note: the producer says this was already fixed.'

echo "=== test-62-review-dispatch ==="
run_case "(a) converge context, no Ledger: line -> block" \
  "w-reviewer" "$PROMPT_NO_LEDGER" "$CONV_SID" 2 "round ledger" ""
run_case "(b) converge context, Ledger: line, file exists -> pass" \
  "w-reviewer" "$PROMPT_VALID_LEDGER" "$CONV_SID" 0 "" "GUARD-BLOCK"
run_case "(c) converge context, Ledger: line, file missing -> block" \
  "w-hostile-reviewer" "$PROMPT_BAD_LEDGER" "$CONV_SID" 2 "round ledger" ""
run_case "(d) converge context, valid ledger, isolation leak -> pass + WARN" \
  "w-design-reviewer" "$PROMPT_ISOLATION_LEAK" "$CONV_SID" 0 "isolation violation" "GUARD-BLOCK"
run_case "(e) converge context, non-reviewer subagent_type, no ledger -> pass (not policed)" \
  "w-implementer" "$PROMPT_NO_LEDGER" "$CONV_SID" 0 "" "GUARD-"
run_case "(f) converge context, mode=warn, no Ledger: line -> pass + WARN" \
  "w-reviewer" "$PROMPT_NO_LEDGER" "$CONV_SID" 0 "WARN" "GUARD-BLOCK" SUPERCLAUDE_GUARD_REVIEW_DISPATCH=warn
run_case "(g) converge context, SUPERCLAUDE_GUARDS=off, both violations -> silence" \
  "w-reviewer" "$PROMPT_BOTH_VIOLATIONS" "$CONV_SID" 0 "" "GUARD-" SUPERCLAUDE_GUARDS=off
run_case "(h) NO converge context (ad-hoc/SEAL panel), no Ledger: line -> pass (SEAL-A M3)" \
  "w-hostile-reviewer" "$PROMPT_NO_LEDGER" "$ADHOC_SID" 0 "" "GUARD-BLOCK"
run_case "(i) NO converge context, isolation leak -> pass + WARN (isolation-lint unscoped)" \
  "w-reviewer" "$PROMPT_ADHOC_LEAK" "$ADHOC_SID" 0 "isolation violation" "GUARD-BLOCK"

if [ "$fails" -eq 0 ]; then
  echo "test-62-review-dispatch: ALL PASS"
  exit 0
else
  echo "test-62-review-dispatch: $fails case(s) FAILED"
  exit 1
fi
