#!/usr/bin/env bash
# Bite-test for 10-content-scan (F1). ISOLATED: it sources lib-guard.sh and the
# guard directly (it does NOT go through guard-dispatch.sh, since sibling guards
# may be mid-build and the shared dispatcher is wired by meta at integration).
# Each case builds synthetic tool_input JSON with jq --arg, runs the guard in a
# SUBSHELL (so a guard_block exit 2 is catchable) with GUARD_CURRENT_NAME set by
# run_guard, and asserts rc + stderr. Self-contained, /tmp only, no repo mutation.
#
# TRIGGER-TOKEN FOOTGUN: the 3 auto-fire tokens must never appear as a LIVE literal
# in this source. We store only the DOT-ESCAPED forms and rebuild the live token at
# runtime (${esc/./}); the live token then travels ONLY as JSON data piped into the
# guard (the guard is not the CLI, so this is safe). The em-dash fixture is likewise
# built from its UTF-8 octal bytes so no literal dash is authored here (rules/06).
#
# Cases:
#   (a) content with an em-dash                       -> block (exit 2)
#   (b) benign content                                -> pass  (exit 0)
#   (c) ~/projects file w/ a firewall token           -> block (exit 2)
#   (d) SAME token in a ~/.claude file                -> pass  (exit 0)
#   (e) Agent prompt w/ a runtime-built live trigger  -> block (exit 2)
#   (f) the dot-escaped trigger token                 -> pass  (exit 0)
#   (g) SUPERCLAUDE_GUARD_CONTENT_SCAN=warn + em-dash -> exit 0 + WARN (mode degrade)

set -uo pipefail

TESTDIR="$(cd "$(dirname "$0")" && pwd)"
GUARDDIR="$TESTDIR/.."
# shellcheck source=/dev/null
source "$GUARDDIR/lib-guard.sh"
# shellcheck source=/dev/null
source "$GUARDDIR/10-content-scan.sh"

TMPD="$(mktemp -d "${TMPDIR:-/tmp}/content-scan-bite.XXXXXX")"
trap 'rm -rf "$TMPD"' EXIT
ERR_FILE="$TMPD/stderr.txt"
fails=0

# Runtime-built fixtures (no literal dash / no live token in this source).
EM=$(printf '\342\200\224')                          # U+2014 em-dash byte
LIVE_WF=$(v='.workflow'; printf '%s' "${v/./}")      # live trigger, built at runtime

mk_write() { jq -nc --arg fp "$1" --arg c "$2" \
  '{tool_name:"Write", tool_input:{file_path:$fp, content:$c}}'; }
mk_agent() { jq -nc --arg p "$1" \
  '{tool_name:"Agent", tool_input:{prompt:$p}}'; }

# run_case <label> <json> <want_rc> <must|""> <mustnot|""> [ENV=VAL ...]
run_case() {
  local label="$1" json="$2" want_rc="$3" must="$4" mustnot="$5"; shift 5
  ( for kv in "$@"; do export "$kv"; done
    guard_init "$json"
    run_guard guard_content_scan
  ) >/dev/null 2>"$ERR_FILE"
  local rc=$?
  local ok=1
  [ "$rc" -eq "$want_rc" ] || { ok=0; echo "    rc=$rc want=$want_rc"; }
  if [ -n "$must" ] && ! grep -q "$must" "$ERR_FILE"; then
    ok=0; echo "    stderr missing: '$must'"; echo "    stderr was: $(cat "$ERR_FILE")"
  fi
  if [ -n "$mustnot" ] && grep -q "$mustnot" "$ERR_FILE"; then
    ok=0; echo "    stderr matched forbidden: '$mustnot'"; echo "    stderr was: $(cat "$ERR_FILE")"
  fi
  if [ "$ok" -eq 1 ]; then echo "  PASS: $label"; else echo "  FAIL: $label"; fails=$((fails + 1)); fi
}

echo "=== test-10-content-scan ==="

run_case "(a) em-dash -> block" \
  "$(mk_write "/tmp/scan-a.txt" "benign ${EM} text")" 2 "em-dash" ""

run_case "(b) benign -> pass" \
  "$(mk_write "/tmp/scan-b.txt" "just a normal line of text")" 0 "" "GUARD-BLOCK"

run_case "(c) firewall ref in ~/projects file -> block" \
  "$(mk_write "$HOME/projects/demo/notes.md" "please see memory_db.py for details")" 2 "firewall" ""

run_case "(d) SAME ref in ~/.claude file -> pass" \
  "$(mk_write "$HOME/.claude/demo/notes.md" "please see memory_db.py for details")" 0 "" "GUARD-BLOCK"

run_case "(e) live trigger in Agent prompt -> WARN not block (owner-authorized 2026-07-15)" \
  "$(mk_agent "please run ${LIVE_WF} now")" 0 "trigger" "GUARD-BLOCK"

run_case "(f) dot-escaped trigger -> pass" \
  "$(mk_write "/tmp/scan-f.txt" ".workflow keyword only")" 0 "" "GUARD-BLOCK"

run_case "(g) warn mode + em-dash -> exit 0 + WARN" \
  "$(mk_write "/tmp/scan-g.txt" "benign ${EM} text")" 0 "WARN" "" SUPERCLAUDE_GUARD_CONTENT_SCAN=warn

if [ "$fails" -eq 0 ]; then
  echo "test-10-content-scan: ALL PASS"
  exit 0
else
  echo "test-10-content-scan: $fails case(s) FAILED"
  exit 1
fi
