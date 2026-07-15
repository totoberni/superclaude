#!/usr/bin/env bash
# Bite-test for instrument-tripwire.py (design/DECISION-DOC.md sec 4 Half B; rules/40 R-6).
# ISOLATED unit test: drives the accumulator CLI directly with a hermetic --state-dir and a
# temp --ledger. Asserts on stdout (the fire message the guard relays) and on the ledger's
# appended INSTRUMENT-TRIPWIRE lines. FLAG-level: the script only ever prints, never blocks.
#
# Cases:
#   (1) one TOOLING line                         -> no output (count 1, below threshold)
#   (2) a 2nd same-CLASS different-text TOOLING  -> fires once + ledger gets a marker
#   (3) a 3rd same-class TOOLING line            -> no NEW output (fires once per class)
#   (4) the identical physical TOOLING line twice-> deduped, never fires
#   (5) two gating commands of the same class    -> fires on the 2nd
#   (6) a non-gating command (ls)                -> never fires

set -uo pipefail

TESTDIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$(cd "$TESTDIR/.." && pwd)/instrument-tripwire.py"

if ! command -v python3 >/dev/null 2>&1; then
  echo "test-instrument-tripwire: SKIP (python3 unavailable)"
  exit 0
fi
if [ ! -f "$SCRIPT" ]; then
  echo "test-instrument-tripwire: FAIL (script not found at $SCRIPT)"
  exit 1
fi

TMPD="$(mktemp -d "${TMPDIR:-/tmp}/tripwire-bite.XXXXXX")"
trap 'rm -rf "$TMPD"' EXIT
STATE="$TMPD/state"; mkdir -p "$STATE"

fails=0
pass() { echo "  PASS: $1"; }
fail() { echo "  FAIL: $1"; echo "    $2"; fails=$((fails + 1)); }

run() { python3 "$SCRIPT" --state-dir "$STATE" "$@"; }

echo "=== test-instrument-tripwire ==="

# ── (1)/(2)/(3) TOOLING accumulation on class 'existence-locator-phantom' ──
LEDGER="$TMPD/rounds-t.md"; : > "$LEDGER"
o1=$(run --session s-t --ledger "$LEDGER" --tooling 'TOOLING: phantom-locator existence check')
o2=$(run --session s-t --ledger "$LEDGER" --tooling 'TOOLING: check for phantom locator existence')
o3=$(run --session s-t --ledger "$LEDGER" --tooling 'TOOLING: existence of the phantom locator')

[ -z "$o1" ] && pass "(1) first TOOLING line -> no fire" \
  || fail "(1) first TOOLING line -> no fire" "got: $o1"

if printf '%s' "$o2" | grep -q "instrument tripwire"; then
  pass "(2) 2nd same-class TOOLING line -> fires"
else
  fail "(2) 2nd same-class TOOLING line -> fires" "got: $o2"
fi

[ -z "$o3" ] && pass "(3) 3rd same-class TOOLING line -> no new fire (once per class)" \
  || fail "(3) 3rd same-class TOOLING line -> no new fire" "got: $o3"

marks=$(grep -c '^INSTRUMENT-TRIPWIRE: ' "$LEDGER")
if [ "${marks:-0}" -eq 1 ]; then
  pass "(2b) ledger received exactly one INSTRUMENT-TRIPWIRE marker"
else
  fail "(2b) ledger received exactly one INSTRUMENT-TRIPWIRE marker (got ${marks:-0})" "$(cat "$LEDGER")"
fi

# ── (4) identical physical TOOLING line fed twice -> deduped, never fires ──
od1=$(run --session s-dedup --tooling 'TOOLING: alpha beta gamma signature')
od2=$(run --session s-dedup --tooling 'TOOLING: alpha beta gamma signature')
if [ -z "$od1" ] && [ -z "$od2" ]; then
  pass "(4) identical TOOLING line twice -> deduped, no fire"
else
  fail "(4) identical TOOLING line twice -> deduped, no fire" "od1='$od1' od2='$od2'"
fi

# ── (5) two gating commands of the same class -> fires on the 2nd ──
oc1=$(run --session s-cmd --command 'pytest tests/test_alpha.py -q')
oc2=$(run --session s-cmd --command 'python3 -m pytest tests/test_beta.py::test_x')
if [ -z "$oc1" ] && printf '%s' "$oc2" | grep -q "pytest"; then
  pass "(5) two pytest gating commands -> fires on the 2nd (class 'pytest')"
else
  fail "(5) two pytest gating commands -> fires on the 2nd" "oc1='$oc1' oc2='$oc2'"
fi

# ── (6) a non-gating command -> never fires, even repeated ──
og1=$(run --session s-nogate --command 'ls -la /tmp')
og2=$(run --session s-nogate --command 'ls -la /tmp')
if [ -z "$og1" ] && [ -z "$og2" ]; then
  pass "(6) non-gating command (ls) -> never fires"
else
  fail "(6) non-gating command (ls) -> never fires" "og1='$og1' og2='$og2'"
fi

echo
if [ "$fails" -eq 0 ]; then
  echo "test-instrument-tripwire: ALL PASS"
  exit 0
else
  echo "test-instrument-tripwire: $fails case(s) FAILED"
  exit 1
fi
